"""
Schedule Service - Cron and Scheduled Workflow Execution
=========================================================

Comprehensive schedule management service for workflow automation with:
- APScheduler integration for background scheduling
- croniter support for cron expression parsing
- Three schedule types: cron, interval, one_time
- Next execution time calculation
- Missed execution handling with grace period
- Execution count tracking and limits
- WorkflowEngine integration for automatic execution
- Comprehensive AAA logging and audit trails

Features:
- start_scheduler() - Start background APScheduler with health checks
- stop_scheduler() - Graceful shutdown with pending job completion
- add_schedule() - Add cron/interval/one-time schedules
- remove_schedule() - Remove active schedule
- update_schedule() - Update schedule configuration
- check_due_schedules() - Check and trigger due workflows
- calculate_next_execution() - Calculate next run times
- Grace period handling for missed executions
- Execution count limits and enforcement
- Status tracking and metrics

Integration Points:
- APScheduler: Background job scheduling
- croniter: Cron expression parsing
- WorkflowEngine: Workflow execution triggering
- AsyncDAL: Schedule persistence and management
- PostgreSQL: workflow_schedules table storage
"""

import asyncio
import logging
import json
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from uuid import uuid4
from enum import Enum

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from croniter import croniter
from flask_core import describe_db_error

logger = logging.getLogger(__name__)


class ScheduleType(str, Enum):
    """Schedule types supported"""
    CRON = "cron"
    INTERVAL = "interval"
    ONE_TIME = "one_time"


class ScheduleStatus(str, Enum):
    """Schedule status"""
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ScheduleServiceException(Exception):
    """Base exception for schedule service errors"""
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ScheduleNotFoundException(ScheduleServiceException):
    """Raised when schedule not found"""
    def __init__(self, schedule_id: str):
        super().__init__(f"Schedule not found: {schedule_id}", status_code=404)


class InvalidScheduleException(ScheduleServiceException):
    """Raised when schedule configuration is invalid"""
    def __init__(self, message: str):
        super().__init__(f"Invalid schedule configuration: {message}", status_code=400)


def _failure_detail(exc: BaseException) -> str:
    """Describe `exc` for an exception message that is surfaced to API clients.

    This module's own exceptions carry authored text and pass through unchanged;
    anything else (a DB driver / Redis / HTTP error) is reduced to its type and
    SQLSTATE so bound values (message content, usernames) never reach a client
    or a downstream log line that re-logs the message.
    """
    if isinstance(exc, ScheduleServiceException):
        return exc.message
    return describe_db_error(exc)


class ScheduleService:
    """
    Comprehensive schedule management service for workflow automation.

    Manages cron schedules, interval schedules, and one-time executions
    with proper error handling, execution tracking, and state persistence.

    Attributes:
        dal: AsyncDAL database instance
        workflow_engine: WorkflowEngine for execution
        scheduler: APScheduler AsyncIOScheduler instance
        grace_period_minutes: Grace period for missed executions
        logger_instance: Logger for AAA logging
    """

    def __init__(
        self,
        dal,
        workflow_engine,
        grace_period_minutes: int = 15,
        logger_instance: Optional[logging.Logger] = None
    ):
        """
        Initialize schedule service.

        Args:
            dal: AsyncDAL database instance
            workflow_engine: WorkflowEngine for triggering workflows
            grace_period_minutes: Grace period for missed executions (default 15 min)
            logger_instance: Optional logger instance for AAA logging
        """
        self.dal = dal
        self.workflow_engine = workflow_engine
        self.grace_period_minutes = grace_period_minutes
        self.logger = logger_instance or logger

        # Initialize APScheduler
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_listener(
            self._scheduler_event_listener,
            EVENT_JOB_EXECUTED | EVENT_JOB_ERROR
        )

        # Track active schedules in memory (for quick reference)
        self._active_schedules: Dict[str, Dict[str, Any]] = {}
        self._is_running = False

        self.logger.info(
            "ScheduleService initialized",
            extra={
                "event_type": "SYSTEM",
                "action": "schedule_service_init",
                "grace_period_minutes": grace_period_minutes,
            }
        )

    async def start_scheduler(self) -> bool:
        """
        Start background APScheduler.

        Loads all active schedules from database and starts the scheduler
        with background job execution. Includes health check and error handling.

        Returns:
            True if successfully started, False otherwise

        Raises:
            ScheduleServiceException: On critical startup failure
        """
        if self._is_running:
            self.logger.warning("Scheduler already running")
            return False

        try:
            # Load all active schedules from database
            await self._load_active_schedules()

            # Start the scheduler
            self.scheduler.start()
            self._is_running = True

            self.logger.info(
                "APScheduler started successfully",
                extra={
                    "event_type": "SYSTEM",
                    "action": "scheduler_start",
                    "active_schedules": len(self._active_schedules),
                }
            )

            # Start the background check_due_schedules task
            asyncio.create_task(self._check_due_schedules_loop())

            return True

        except Exception as e:
            self.logger.error(
                f"Failed to start scheduler: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "scheduler_start",
                    "result": "FAILURE",
                }
            )
            raise ScheduleServiceException(f"Failed to start scheduler: {_failure_detail(e)}")

    async def stop_scheduler(self) -> bool:
        """
        Gracefully shutdown scheduler.

        Waits for pending jobs to complete before shutting down.
        Updates schedule states in database.

        Returns:
            True if successfully stopped
        """
        if not self._is_running:
            self.logger.warning("Scheduler not running")
            return False

        try:
            # Allow pending jobs to complete (5 second grace period)
            self.scheduler.shutdown(wait=True)
            self._is_running = False

            self.logger.info(
                "APScheduler stopped successfully",
                extra={
                    "event_type": "SYSTEM",
                    "action": "scheduler_stop",
                }
            )

            return True

        except Exception as e:
            self.logger.error(
                f"Error stopping scheduler: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "scheduler_stop",
                }
            )
            return False

    async def add_schedule(
        self,
        workflow_id: str,
        schedule_config: Dict[str, Any],
        user_id: int,
        community_id: int,
        context_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Add a new schedule for workflow execution.

        Supports three schedule types:
        - cron: Standard cron expression (e.g., "0 12 * * *")
        - interval: Interval in seconds
        - one_time: Single execution at specific datetime

        Args:
            workflow_id: UUID of workflow to schedule
            schedule_config: Schedule configuration with:
                - schedule_type: "cron" | "interval" | "one_time"
                - cron_expression: (cron) Cron expression string
                - interval_seconds: (interval) Interval in seconds
                - scheduled_time: (one_time) ISO 8601 datetime string
                - timezone: (optional) Timezone (default: UTC)
                - max_executions: (optional) Max executions limit
            user_id: User creating the schedule
            community_id: Community ID for context
            context_data: Optional context data passed to workflow

        Returns:
            Created schedule data with schedule_id and next_execution_at

        Raises:
            InvalidScheduleException: If schedule config invalid
            ScheduleServiceException: On database errors
        """
        try:
            # Step 1: Validate schedule configuration
            schedule_type = schedule_config.get("schedule_type")
            if schedule_type not in [s.value for s in ScheduleType]:
                raise InvalidScheduleException(
                    f"Invalid schedule_type: {schedule_type}. "
                    f"Must be one of: cron, interval, one_time"
                )

            # Step 2: Type-specific validation
            timezone = schedule_config.get("timezone", "UTC")
            next_execution_at = None

            match schedule_type:
                case "cron":
                    cron_expr = schedule_config.get("cron_expression")
                    if not cron_expr:
                        raise InvalidScheduleException("cron_expression required for cron type")

                    # Validate cron expression
                    try:
                        croniter(cron_expr, datetime.now())
                    except Exception as e:
                        raise InvalidScheduleException(f"Invalid cron expression: {str(e)}")

                    # Calculate next execution
                    next_execution_at = self.calculate_next_execution(
                        schedule_type="cron",
                        cron_expression=cron_expr,
                        timezone=timezone
                    )

                case "interval":
                    interval_seconds = schedule_config.get("interval_seconds")
                    if not interval_seconds or interval_seconds <= 0:
                        raise InvalidScheduleException(
                            "interval_seconds required and must be > 0 for interval type"
                        )

                    next_execution_at = datetime.utcnow() + timedelta(seconds=interval_seconds)

                case "one_time":
                    scheduled_time = schedule_config.get("scheduled_time")
                    if not scheduled_time:
                        raise InvalidScheduleException(
                            "scheduled_time required for one_time type (ISO 8601)"
                        )

                    try:
                        next_execution_at = datetime.fromisoformat(scheduled_time)
                    except ValueError as e:
                        raise InvalidScheduleException(
                            f"Invalid scheduled_time format: {str(e)}"
                        )

                    # one_time schedules must be in future
                    if next_execution_at <= datetime.utcnow():
                        raise InvalidScheduleException(
                            "scheduled_time must be in the future"
                        )

            # Step 3: Generate schedule ID
            schedule_id = str(uuid4())

            # Step 4: Prepare database record
            insert_query = """
            INSERT INTO workflow_schedules (
                schedule_id, workflow_id, schedule_type,
                cron_expression, interval_seconds, scheduled_time,
                timezone, is_active, next_execution_at,
                context_data, execution_count, max_executions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

            params = [
                schedule_id,
                workflow_id,
                schedule_type,
                schedule_config.get("cron_expression"),
                schedule_config.get("interval_seconds"),
                schedule_config.get("scheduled_time"),
                timezone,
                True,  # is_active
                next_execution_at,
                json.dumps(context_data or {}),
                0,  # execution_count starts at 0
                schedule_config.get("max_executions"),
            ]

            await self.dal.executesql(insert_query, params)

            # Step 5: Load schedule into memory and register with APScheduler
            schedule_data = {
                "schedule_id": schedule_id,
                "workflow_id": workflow_id,
                "schedule_type": schedule_type,
                "next_execution_at": next_execution_at,
                "is_active": True,
                "execution_count": 0,
                "max_executions": schedule_config.get("max_executions"),
            }

            self._active_schedules[schedule_id] = schedule_data

            # Step 6: Register with APScheduler if scheduler is running
            if self._is_running:
                await self._register_schedule_with_scheduler(
                    schedule_id,
                    workflow_id,
                    schedule_config,
                    next_execution_at
                )

            # Step 7: Audit logging
            self.logger.info(
                f"Schedule created: {schedule_id} for workflow {workflow_id}",
                extra={
                    "event_type": "AUDIT",
                    "action": "schedule_create",
                    "schedule_id": schedule_id,
                    "workflow_id": workflow_id,
                    "user": str(user_id),
                    "community": str(community_id),
                    "schedule_type": schedule_type,
                }
            )

            return schedule_data

        except InvalidScheduleException:
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to create schedule: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "schedule_create",
                    "workflow_id": workflow_id,
                    "user": str(user_id),
                    "result": "FAILURE",
                }
            )
            raise ScheduleServiceException(f"Failed to create schedule: {_failure_detail(e)}")

    async def remove_schedule(self, schedule_id: str, user_id: int) -> bool:
        """
        Remove a schedule.

        Deactivates the schedule in database and removes from APScheduler.

        Args:
            schedule_id: UUID of schedule to remove
            user_id: User removing the schedule

        Returns:
            True if successfully removed

        Raises:
            ScheduleNotFoundException: If schedule not found
        """
        try:
            # Check if schedule exists
            if schedule_id not in self._active_schedules:
                # Try to load from database
                query = "SELECT * FROM workflow_schedules WHERE schedule_id = ?"
                row = await self.dal.executesql(query, [schedule_id])
                if not row:
                    raise ScheduleNotFoundException(schedule_id)

            # Remove from APScheduler
            try:
                self.scheduler.remove_job(schedule_id)
            except Exception:
                # Job may not exist in scheduler yet
                pass

            # Update database to mark as inactive
            update_query = """
            UPDATE workflow_schedules
            SET is_active = FALSE
            WHERE schedule_id = ?
            """
            await self.dal.executesql(update_query, [schedule_id])

            # Remove from memory cache
            self._active_schedules.pop(schedule_id, None)

            # Audit logging
            self.logger.info(
                f"Schedule removed: {schedule_id}",
                extra={
                    "event_type": "AUDIT",
                    "action": "schedule_remove",
                    "schedule_id": schedule_id,
                    "user": str(user_id),
                }
            )

            return True

        except ScheduleNotFoundException:
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to remove schedule {schedule_id}: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "schedule_remove",
                    "schedule_id": schedule_id,
                    "user": str(user_id),
                    "result": "FAILURE",
                }
            )
            raise ScheduleServiceException(f"Failed to remove schedule: {_failure_detail(e)}")

    async def update_schedule(
        self,
        schedule_id: str,
        schedule_config: Dict[str, Any],
        user_id: int
    ) -> Dict[str, Any]:
        """
        Update an existing schedule.

        Updates schedule configuration and recalculates next execution time.

        Args:
            schedule_id: UUID of schedule to update
            schedule_config: Updated schedule configuration
            user_id: User updating the schedule

        Returns:
            Updated schedule data

        Raises:
            ScheduleNotFoundException: If schedule not found
            InvalidScheduleException: If new config invalid
        """
        try:
            # Load current schedule
            if schedule_id not in self._active_schedules:
                raise ScheduleNotFoundException(schedule_id)

            current = self._active_schedules[schedule_id]

            # Validate new configuration
            schedule_type = schedule_config.get("schedule_type", current["schedule_type"])
            timezone = schedule_config.get("timezone", "UTC")

            # Calculate new next execution
            next_execution_at = self.calculate_next_execution(
                schedule_type=schedule_type,
                cron_expression=schedule_config.get("cron_expression"),
                interval_seconds=schedule_config.get("interval_seconds"),
                scheduled_time=schedule_config.get("scheduled_time"),
                timezone=timezone
            )

            # Update database
            update_query = """
            UPDATE workflow_schedules
            SET
                schedule_type = ?,
                cron_expression = ?,
                interval_seconds = ?,
                scheduled_time = ?,
                timezone = ?,
                next_execution_at = ?,
                max_executions = ?
            WHERE schedule_id = ?
            """

            params = [
                schedule_type,
                schedule_config.get("cron_expression"),
                schedule_config.get("interval_seconds"),
                schedule_config.get("scheduled_time"),
                timezone,
                next_execution_at,
                schedule_config.get("max_executions"),
                schedule_id,
            ]

            await self.dal.executesql(update_query, params)

            # Update memory cache
            self._active_schedules[schedule_id].update({
                "schedule_type": schedule_type,
                "next_execution_at": next_execution_at,
                "max_executions": schedule_config.get("max_executions"),
            })

            # Re-register with APScheduler if running
            if self._is_running:
                try:
                    self.scheduler.remove_job(schedule_id)
                except Exception:
                    pass

                workflow_id = current["workflow_id"]
                await self._register_schedule_with_scheduler(
                    schedule_id,
                    workflow_id,
                    schedule_config,
                    next_execution_at
                )

            # Audit logging
            self.logger.info(
                f"Schedule updated: {schedule_id}",
                extra={
                    "event_type": "AUDIT",
                    "action": "schedule_update",
                    "schedule_id": schedule_id,
                    "user": str(user_id),
                    "changes": schedule_config,
                }
            )

            return self._active_schedules[schedule_id]

        except (ScheduleNotFoundException, InvalidScheduleException):
            raise
        except Exception as e:
            self.logger.error(
                f"Failed to update schedule {schedule_id}: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "schedule_update",
                    "schedule_id": schedule_id,
                    "user": str(user_id),
                    "result": "FAILURE",
                }
            )
            raise ScheduleServiceException(f"Failed to update schedule: {_failure_detail(e)}")

    async def check_due_schedules(self) -> List[Dict[str, Any]]:
        """
        Check and trigger due schedules.

        Runs every minute (typically via background task) to check for
        schedules that are due for execution. Handles:
        - Missed executions with grace period
        - Execution count limits
        - Proper trigger data and context

        Returns:
            List of triggered schedule execution data

        Raises:
            ScheduleServiceException: On execution errors
        """
        triggered = []

        try:
            now = datetime.utcnow()

            # Query for due schedules
            query = """
            SELECT
                schedule_id, workflow_id, schedule_type,
                cron_expression, interval_seconds, next_execution_at,
                execution_count, max_executions, context_data
            FROM workflow_schedules
            WHERE is_active = TRUE
                AND next_execution_at <= ?
            ORDER BY next_execution_at ASC
            """

            rows = await self.dal.executesql(query, [now])

            for row in rows:
                try:
                    schedule_id = row[0]
                    workflow_id = row[1]
                    schedule_type = row[2]
                    cron_expr = row[3]
                    interval_seconds = row[4]
                    next_exec = row[5]
                    exec_count = row[6]
                    max_execs = row[7]
                    context_str = row[8]

                    # Check execution count limit
                    if max_execs and exec_count >= max_execs:
                        self.logger.info(
                            f"Schedule {schedule_id} reached max executions",
                            extra={
                                "event_type": "AUDIT",
                                "action": "schedule_limit_reached",
                                "schedule_id": schedule_id,
                                "execution_count": exec_count,
                                "max_executions": max_execs,
                            }
                        )
                        # Mark as inactive
                        await self.dal.executesql(
                            "UPDATE workflow_schedules SET is_active = FALSE WHERE schedule_id = ?",
                            [schedule_id]
                        )
                        continue

                    # Check grace period for missed executions
                    grace_threshold = now - timedelta(minutes=self.grace_period_minutes)
                    if next_exec < grace_threshold:
                        self.logger.warning(
                            f"Schedule {schedule_id} missed execution window",
                            extra={
                                "event_type": "AUDIT",
                                "action": "schedule_missed",
                                "schedule_id": schedule_id,
                                "next_execution": str(next_exec),
                                "grace_threshold": str(grace_threshold),
                            }
                        )
                        continue

                    # Prepare context data for execution
                    context_data = json.loads(context_str) if context_str else {}

                    # Trigger workflow execution
                    execution_id = str(uuid4())
                    trigger_data = {
                        "schedule_id": schedule_id,
                        "execution_id": execution_id,
                        "triggered_at": now.isoformat(),
                        **context_data
                    }

                    # Execute workflow asynchronously
                    asyncio.create_task(
                        self._execute_scheduled_workflow(
                            schedule_id,
                            workflow_id,
                            trigger_data,
                            schedule_type,
                            cron_expr,
                            interval_seconds
                        )
                    )

                    triggered.append({
                        "schedule_id": schedule_id,
                        "workflow_id": workflow_id,
                        "execution_id": execution_id,
                        "triggered_at": now.isoformat(),
                    })

                    self.logger.info(
                        f"Schedule triggered: {schedule_id}",
                        extra={
                            "event_type": "AUDIT",
                            "action": "schedule_triggered",
                            "schedule_id": schedule_id,
                            "workflow_id": workflow_id,
                            "execution_id": execution_id,
                        }
                    )

                except Exception as e:
                    self.logger.error(
                        f"Error triggering schedule {row[0]}: {describe_db_error(e)}",
                        extra={
                            "event_type": "ERROR",
                            "action": "schedule_trigger_error",
                            "schedule_id": row[0],
                            "result": "FAILURE",
                        }
                    )

            return triggered

        except Exception as e:
            self.logger.error(
                f"Error checking due schedules: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "check_due_schedules",
                    "result": "FAILURE",
                }
            )
            raise ScheduleServiceException(f"Error checking schedules: {_failure_detail(e)}")

    @staticmethod
    def calculate_next_execution(
        schedule_type: str,
        cron_expression: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        scheduled_time: Optional[str] = None,
        timezone: str = "UTC"
    ) -> datetime:
        """
        Calculate next execution time for a schedule.

        Args:
            schedule_type: Type of schedule (cron, interval, one_time)
            cron_expression: (cron) Cron expression string
            interval_seconds: (interval) Interval in seconds
            scheduled_time: (one_time) ISO 8601 datetime
            timezone: Timezone string (default UTC)

        Returns:
            datetime of next execution

        Raises:
            InvalidScheduleException: If parameters invalid for type
        """
        match schedule_type:
            case "cron":
                if not cron_expression:
                    raise InvalidScheduleException(
                        "cron_expression required for cron schedule"
                    )
                try:
                    cron = croniter(cron_expression, datetime.utcnow())
                    return cron.get_next(datetime)
                except Exception as e:
                    raise InvalidScheduleException(
                        f"Failed to calculate next execution: {str(e)}"
                    )

            case "interval":
                if not interval_seconds or interval_seconds <= 0:
                    raise InvalidScheduleException(
                        "Valid interval_seconds required for interval schedule"
                    )
                return datetime.utcnow() + timedelta(seconds=interval_seconds)

            case "one_time":
                if not scheduled_time:
                    raise InvalidScheduleException(
                        "scheduled_time required for one_time schedule"
                    )
                try:
                    dt = datetime.fromisoformat(scheduled_time)
                    if dt <= datetime.utcnow():
                        raise InvalidScheduleException(
                            "scheduled_time must be in future"
                        )
                    return dt
                except ValueError as e:
                    raise InvalidScheduleException(
                        f"Invalid scheduled_time format: {str(e)}"
                    )

            case _:
                raise InvalidScheduleException(f"Unknown schedule type: {schedule_type}")

    # ========================================================================
    # Private Helper Methods
    # ========================================================================

    async def _load_active_schedules(self) -> None:
        """Load all active schedules from database into memory."""
        try:
            query = """
            SELECT schedule_id, workflow_id, schedule_type, next_execution_at,
                   execution_count, max_executions
            FROM workflow_schedules
            WHERE is_active = TRUE
            """
            rows = await self.dal.executesql(query, [])

            for row in rows:
                schedule_id = row[0]
                self._active_schedules[schedule_id] = {
                    "schedule_id": schedule_id,
                    "workflow_id": row[1],
                    "schedule_type": row[2],
                    "next_execution_at": row[3],
                    "execution_count": row[4],
                    "max_executions": row[5],
                    "is_active": True,
                }

            self.logger.info(
                f"Loaded {len(self._active_schedules)} active schedules",
                extra={
                    "event_type": "SYSTEM",
                    "action": "load_schedules",
                    "count": len(self._active_schedules),
                }
            )

        except Exception as e:
            self.logger.error(
                f"Error loading schedules: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "load_schedules",
                }
            )

    async def _register_schedule_with_scheduler(
        self,
        schedule_id: str,
        workflow_id: str,
        schedule_config: Dict[str, Any],
        next_execution_at: datetime
    ) -> None:
        """Register a schedule with APScheduler."""
        try:
            schedule_type = schedule_config.get("schedule_type")

            match schedule_type:
                case "cron":
                    trigger = CronTrigger.from_crontab(
                        schedule_config.get("cron_expression")
                    )

                case "interval":
                    trigger = IntervalTrigger(
                        seconds=schedule_config.get("interval_seconds")
                    )

                case "one_time":
                    # For one-time, we don't use APScheduler
                    # They are handled by check_due_schedules
                    return

            self.scheduler.add_job(
                func=self._handle_schedule_execution,
                trigger=trigger,
                id=schedule_id,
                args=[schedule_id, workflow_id],
                replace_existing=True,
                next_run_time=next_execution_at,
            )

        except Exception as e:
            self.logger.error(
                f"Error registering schedule with APScheduler: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "register_schedule",
                    "schedule_id": schedule_id,
                }
            )

    async def _handle_schedule_execution(
        self,
        schedule_id: str,
        workflow_id: str
    ) -> None:
        """Handle execution of a scheduled workflow."""
        try:
            # Load schedule from database
            query = "SELECT context_data, execution_count, max_executions FROM workflow_schedules WHERE schedule_id = ?"
            row = await self.dal.executesql(query, [schedule_id])

            if not row:
                return

            # Query selects (context_data, execution_count, max_executions) in
            # that order -- this read `row[0][2]` (max_executions, an int)
            # for context_data instead of `row[0][0]`, so `json.loads()`
            # raised TypeError ("must be str, bytes or bytearray, not int")
            # on every APScheduler-triggered execution of any cron/interval
            # schedule with `max_executions` set, silently swallowed by the
            # except block below -- the workflow simply never ran. Undetected
            # because this file had 0% test coverage.
            context_data = json.loads(row[0][0]) if row[0][0] else {}
            exec_count = row[0][1]
            max_execs = row[0][2]

            # Check limit
            if max_execs and exec_count >= max_execs:
                return

            # Execute workflow
            execution_id = str(uuid4())
            trigger_data = {
                "schedule_id": schedule_id,
                "execution_id": execution_id,
                "triggered_at": datetime.utcnow().isoformat(),
                **context_data
            }

            await self.workflow_engine.execute_workflow(
                workflow_id=workflow_id,
                trigger_data=trigger_data
            )

            # Update execution count
            update_query = """
            UPDATE workflow_schedules
            SET execution_count = execution_count + 1,
                last_execution_at = CURRENT_TIMESTAMP
            WHERE schedule_id = ?
            """
            await self.dal.executesql(update_query, [schedule_id])

        except Exception as e:
            self.logger.error(
                f"Error executing scheduled workflow: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "schedule_execution",
                    "schedule_id": schedule_id,
                    "workflow_id": workflow_id,
                }
            )

    async def _execute_scheduled_workflow(
        self,
        schedule_id: str,
        workflow_id: str,
        trigger_data: Dict[str, Any],
        schedule_type: str,
        cron_expr: Optional[str],
        interval_seconds: Optional[int]
    ) -> None:
        """Execute a scheduled workflow and update its execution info."""
        try:
            # Execute the workflow
            await self.workflow_engine.execute_workflow(
                workflow_id=workflow_id,
                trigger_data=trigger_data
            )

            # Calculate next execution
            next_exec = self.calculate_next_execution(
                schedule_type=schedule_type,
                cron_expression=cron_expr,
                interval_seconds=interval_seconds
            )

            # Update database
            update_query = """
            UPDATE workflow_schedules
            SET
                execution_count = execution_count + 1,
                last_execution_at = CURRENT_TIMESTAMP,
                last_execution_id = ?,
                next_execution_at = ?
            WHERE schedule_id = ?
            """

            params = [
                trigger_data.get("execution_id"),
                next_exec,
                schedule_id,
            ]

            await self.dal.executesql(update_query, params)

            # Update memory cache
            if schedule_id in self._active_schedules:
                self._active_schedules[schedule_id]["execution_count"] += 1
                self._active_schedules[schedule_id]["next_execution_at"] = next_exec

            self.logger.info(
                f"Schedule executed successfully: {schedule_id}",
                extra={
                    "event_type": "AUDIT",
                    "action": "schedule_executed",
                    "schedule_id": schedule_id,
                    "workflow_id": workflow_id,
                    "execution_id": trigger_data.get("execution_id"),
                }
            )

        except Exception as e:
            self.logger.error(
                f"Error executing scheduled workflow {schedule_id}: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "schedule_execution_error",
                    "schedule_id": schedule_id,
                    "workflow_id": workflow_id,
                    "result": "FAILURE",
                }
            )

    async def _check_due_schedules_loop(self) -> None:
        """Background task that checks for due schedules every minute."""
        while self._is_running:
            try:
                await asyncio.sleep(60)  # Check every minute
                await self.check_due_schedules()

            except Exception as e:
                self.logger.error(
                    f"Error in check_due_schedules loop: {describe_db_error(e)}",
                    extra={
                        "event_type": "ERROR",
                        "action": "check_due_schedules_loop",
                    }
                )

    def _scheduler_event_listener(self, event) -> None:
        """Listen to APScheduler events."""
        try:
            if event.job_id:
                if hasattr(event, "exception") and event.exception:
                    self.logger.error(
                        f"APScheduler job failed: {event.job_id}",
                        extra={
                            "event_type": "ERROR",
                            "action": "scheduler_job_error",
                            "job_id": event.job_id,
                            "exception": str(event.exception),
                        }
                    )
                else:
                    self.logger.info(
                        f"APScheduler job executed: {event.job_id}",
                        extra={
                            "event_type": "SYSTEM",
                            "action": "scheduler_job_executed",
                            "job_id": event.job_id,
                        }
                    )
        except Exception as e:
            self.logger.error(
                f"Error in scheduler event listener: {describe_db_error(e)}",
                extra={
                    "event_type": "ERROR",
                    "action": "scheduler_listener_error",
                }
            )
