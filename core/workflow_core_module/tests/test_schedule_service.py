"""`services/schedule_service.py` -- APScheduler-backed cron/interval/one_time schedules.

This file had 0% coverage. While writing this suite,
`_handle_schedule_execution` was found to parse `context_data` from
`row[0][2]` (actually the `max_executions` column -- the query selects
`context_data, execution_count, max_executions` in that order) instead of
`row[0][0]`, so `json.loads()` raised `TypeError` on every APScheduler-
triggered execution of a schedule with `max_executions` set, silently
swallowed by the method's own except block (the workflow just never ran).
Fixed at the source.

The real `AsyncIOScheduler` is only exercised for construction (cheap,
no I/O); every test that would otherwise call `.start()`/`.add_job()`/etc
replaces `service.scheduler` with a `MagicMock()` so no background jobs or
event-loop timers actually get scheduled.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.schedule_service import (
    InvalidScheduleException,
    ScheduleNotFoundException,
    ScheduleService,
    ScheduleServiceException,
)


def _service(dal: MagicMock | None = None, workflow_engine: MagicMock | None = None) -> ScheduleService:
    svc = ScheduleService(
        dal=dal or MagicMock(),
        workflow_engine=workflow_engine or MagicMock(),
    )
    svc.scheduler = MagicMock()
    return svc


class TestExceptions:
    def test_schedule_not_found_exception(self) -> None:
        exc = ScheduleNotFoundException("sched-1")
        assert exc.status_code == 404
        assert "sched-1" in exc.message

    def test_invalid_schedule_exception(self) -> None:
        exc = InvalidScheduleException("bad config")
        assert exc.status_code == 400
        assert "bad config" in exc.message


class TestInit:
    def test_init_sets_defaults(self) -> None:
        svc = _service()
        assert svc.grace_period_minutes == 15
        assert svc._is_running is False
        assert svc._active_schedules == {}


class TestStartStopScheduler:
    @pytest.mark.asyncio
    async def test_start_scheduler_success(self) -> None:
        svc = _service()
        svc._load_active_schedules = AsyncMock()
        with patch("asyncio.create_task") as mock_create_task:
            result = await svc.start_scheduler()
        assert result is True
        assert svc._is_running is True
        svc.scheduler.start.assert_called_once()
        mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_scheduler_already_running_returns_false(self) -> None:
        svc = _service()
        svc._is_running = True
        assert await svc.start_scheduler() is False

    @pytest.mark.asyncio
    async def test_start_scheduler_failure_raises(self) -> None:
        svc = _service()
        svc._load_active_schedules = AsyncMock(side_effect=Exception("db down"))
        with pytest.raises(ScheduleServiceException):
            await svc.start_scheduler()

    @pytest.mark.asyncio
    async def test_stop_scheduler_not_running_returns_false(self) -> None:
        svc = _service()
        assert await svc.stop_scheduler() is False

    @pytest.mark.asyncio
    async def test_stop_scheduler_success(self) -> None:
        svc = _service()
        svc._is_running = True
        result = await svc.stop_scheduler()
        assert result is True
        assert svc._is_running is False
        svc.scheduler.shutdown.assert_called_once_with(wait=True)

    @pytest.mark.asyncio
    async def test_stop_scheduler_failure_returns_false(self) -> None:
        svc = _service()
        svc._is_running = True
        svc.scheduler.shutdown.side_effect = Exception("shutdown failed")
        assert await svc.stop_scheduler() is False


class TestAddSchedule:
    @pytest.mark.asyncio
    async def test_invalid_schedule_type(self) -> None:
        svc = _service()
        with pytest.raises(InvalidScheduleException):
            await svc.add_schedule("wf-1", {"schedule_type": "bogus"}, 1, 1)

    @pytest.mark.asyncio
    async def test_cron_missing_expression(self) -> None:
        svc = _service()
        with pytest.raises(InvalidScheduleException):
            await svc.add_schedule("wf-1", {"schedule_type": "cron"}, 1, 1)

    @pytest.mark.asyncio
    async def test_cron_invalid_expression(self) -> None:
        svc = _service()
        with pytest.raises(InvalidScheduleException):
            await svc.add_schedule(
                "wf-1", {"schedule_type": "cron", "cron_expression": "not a cron"}, 1, 1
            )

    @pytest.mark.asyncio
    async def test_cron_success(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        result = await svc.add_schedule(
            "wf-1", {"schedule_type": "cron", "cron_expression": "0 12 * * *"}, 1, 1
        )
        assert result["schedule_type"] == "cron"
        assert result["schedule_id"] in svc._active_schedules
        dal.executesql.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interval_missing_seconds(self) -> None:
        svc = _service()
        with pytest.raises(InvalidScheduleException):
            await svc.add_schedule("wf-1", {"schedule_type": "interval"}, 1, 1)

    @pytest.mark.asyncio
    async def test_interval_zero_seconds_invalid(self) -> None:
        svc = _service()
        with pytest.raises(InvalidScheduleException):
            await svc.add_schedule(
                "wf-1", {"schedule_type": "interval", "interval_seconds": 0}, 1, 1
            )

    @pytest.mark.asyncio
    async def test_interval_success(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        result = await svc.add_schedule(
            "wf-1", {"schedule_type": "interval", "interval_seconds": 60}, 1, 1
        )
        assert result["schedule_type"] == "interval"

    @pytest.mark.asyncio
    async def test_one_time_missing_scheduled_time(self) -> None:
        svc = _service()
        with pytest.raises(InvalidScheduleException):
            await svc.add_schedule("wf-1", {"schedule_type": "one_time"}, 1, 1)

    @pytest.mark.asyncio
    async def test_one_time_invalid_format(self) -> None:
        svc = _service()
        with pytest.raises(InvalidScheduleException):
            await svc.add_schedule(
                "wf-1",
                {"schedule_type": "one_time", "scheduled_time": "not-a-date"},
                1, 1,
            )

    @pytest.mark.asyncio
    async def test_one_time_in_past_invalid(self) -> None:
        svc = _service()
        past = (datetime.utcnow() - timedelta(days=1)).isoformat()
        with pytest.raises(InvalidScheduleException):
            await svc.add_schedule(
                "wf-1", {"schedule_type": "one_time", "scheduled_time": past}, 1, 1
            )

    @pytest.mark.asyncio
    async def test_one_time_success(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        result = await svc.add_schedule(
            "wf-1", {"schedule_type": "one_time", "scheduled_time": future}, 1, 1
        )
        assert result["schedule_type"] == "one_time"

    @pytest.mark.asyncio
    async def test_registers_with_scheduler_when_running(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        svc._is_running = True
        svc._register_schedule_with_scheduler = AsyncMock()
        await svc.add_schedule(
            "wf-1", {"schedule_type": "interval", "interval_seconds": 30}, 1, 1
        )
        svc._register_schedule_with_scheduler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_db_error_wraps_in_schedule_service_exception(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(side_effect=Exception("db exploded"))
        svc = _service(dal=dal)
        with pytest.raises(ScheduleServiceException):
            await svc.add_schedule(
                "wf-1", {"schedule_type": "interval", "interval_seconds": 30}, 1, 1
            )


class TestRemoveSchedule:
    @pytest.mark.asyncio
    async def test_remove_from_memory_success(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        svc._active_schedules["sched-1"] = {"workflow_id": "wf-1"}
        result = await svc.remove_schedule("sched-1", user_id=1)
        assert result is True
        assert "sched-1" not in svc._active_schedules
        svc.scheduler.remove_job.assert_called_once_with("sched-1")

    @pytest.mark.asyncio
    async def test_remove_not_in_memory_loads_from_db(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=[("row",)])
        svc = _service(dal=dal)
        result = await svc.remove_schedule("sched-2", user_id=1)
        assert result is True

    @pytest.mark.asyncio
    async def test_remove_not_found_raises(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=[])
        svc = _service(dal=dal)
        with pytest.raises(ScheduleNotFoundException):
            await svc.remove_schedule("missing", user_id=1)

    @pytest.mark.asyncio
    async def test_remove_job_scheduler_error_is_swallowed(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        svc._active_schedules["sched-1"] = {"workflow_id": "wf-1"}
        svc.scheduler.remove_job.side_effect = Exception("not scheduled")
        result = await svc.remove_schedule("sched-1", user_id=1)
        assert result is True

    @pytest.mark.asyncio
    async def test_remove_db_error_wraps_exception(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(side_effect=Exception("db down"))
        svc = _service(dal=dal)
        svc._active_schedules["sched-1"] = {"workflow_id": "wf-1"}
        with pytest.raises(ScheduleServiceException):
            await svc.remove_schedule("sched-1", user_id=1)


class TestUpdateSchedule:
    @pytest.mark.asyncio
    async def test_update_not_found_raises(self) -> None:
        svc = _service()
        with pytest.raises(ScheduleNotFoundException):
            await svc.update_schedule("missing", {"interval_seconds": 30}, user_id=1)

    @pytest.mark.asyncio
    async def test_update_success_not_running(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        svc._active_schedules["sched-1"] = {
            "schedule_type": "interval", "workflow_id": "wf-1",
        }
        result = await svc.update_schedule(
            "sched-1", {"schedule_type": "interval", "interval_seconds": 120}, user_id=1
        )
        assert result["max_executions"] is None

    @pytest.mark.asyncio
    async def test_update_success_while_running_reregisters(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        svc._is_running = True
        svc._active_schedules["sched-1"] = {
            "schedule_type": "interval", "workflow_id": "wf-1",
        }
        svc._register_schedule_with_scheduler = AsyncMock()
        await svc.update_schedule(
            "sched-1", {"schedule_type": "interval", "interval_seconds": 120}, user_id=1
        )
        svc.scheduler.remove_job.assert_called_once_with("sched-1")
        svc._register_schedule_with_scheduler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_remove_job_error_is_swallowed(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        svc = _service(dal=dal)
        svc._is_running = True
        svc._active_schedules["sched-1"] = {
            "schedule_type": "interval", "workflow_id": "wf-1",
        }
        svc.scheduler.remove_job.side_effect = Exception("no job")
        svc._register_schedule_with_scheduler = AsyncMock()
        await svc.update_schedule(
            "sched-1", {"schedule_type": "interval", "interval_seconds": 120}, user_id=1
        )

    @pytest.mark.asyncio
    async def test_update_invalid_new_config_propagates(self) -> None:
        svc = _service()
        svc._active_schedules["sched-1"] = {
            "schedule_type": "interval", "workflow_id": "wf-1",
        }
        with pytest.raises(InvalidScheduleException):
            await svc.update_schedule(
                "sched-1", {"schedule_type": "interval", "interval_seconds": -1}, user_id=1
            )

    @pytest.mark.asyncio
    async def test_update_db_error_wraps_exception(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(side_effect=Exception("db down"))
        svc = _service(dal=dal)
        svc._active_schedules["sched-1"] = {
            "schedule_type": "interval", "workflow_id": "wf-1",
        }
        with pytest.raises(ScheduleServiceException):
            await svc.update_schedule(
                "sched-1", {"schedule_type": "interval", "interval_seconds": 30}, user_id=1
            )


class TestCheckDueSchedules:
    @pytest.mark.asyncio
    async def test_no_due_schedules_returns_empty(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=[])
        svc = _service(dal=dal)
        assert await svc.check_due_schedules() == []

    @pytest.mark.asyncio
    async def test_max_executions_reached_marks_inactive_and_skips(self) -> None:
        dal = MagicMock()
        row = ("s1", "wf-1", "interval", None, 60, datetime.utcnow(), 5, 5, "{}")
        dal.executesql = AsyncMock(side_effect=[[row], None])
        svc = _service(dal=dal)
        triggered = await svc.check_due_schedules()
        assert triggered == []
        assert dal.executesql.await_count == 2

    @pytest.mark.asyncio
    async def test_missed_grace_period_is_skipped(self) -> None:
        dal = MagicMock()
        old_time = datetime.utcnow() - timedelta(hours=1)
        row = ("s1", "wf-1", "interval", None, 60, old_time, 0, None, "{}")
        dal.executesql = AsyncMock(return_value=[row])
        svc = _service(dal=dal)
        triggered = await svc.check_due_schedules()
        assert triggered == []

    @pytest.mark.asyncio
    async def test_due_schedule_is_triggered(self) -> None:
        dal = MagicMock()
        row = ("s1", "wf-1", "interval", None, 60, datetime.utcnow(), 0, None, json.dumps({"x": 1}))
        dal.executesql = AsyncMock(return_value=[row])
        svc = _service(dal=dal)
        with patch("asyncio.create_task") as mock_create_task:
            triggered = await svc.check_due_schedules()
        assert len(triggered) == 1
        assert triggered[0]["schedule_id"] == "s1"
        mock_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_row_processing_error_is_logged_and_skipped(self) -> None:
        dal = MagicMock()
        bad_row = ("s1", "wf-1", "interval", None, 60, datetime.utcnow(), 0, None, "not-json")
        dal.executesql = AsyncMock(return_value=[bad_row])
        svc = _service(dal=dal)
        triggered = await svc.check_due_schedules()
        assert triggered == []

    @pytest.mark.asyncio
    async def test_outer_query_failure_wraps_exception(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(side_effect=Exception("query failed"))
        svc = _service(dal=dal)
        with pytest.raises(ScheduleServiceException):
            await svc.check_due_schedules()


class TestCalculateNextExecution:
    def test_cron_success(self) -> None:
        next_exec = ScheduleService.calculate_next_execution(
            schedule_type="cron", cron_expression="0 0 * * *"
        )
        assert isinstance(next_exec, datetime)

    def test_cron_missing_expression_raises(self) -> None:
        with pytest.raises(InvalidScheduleException):
            ScheduleService.calculate_next_execution(schedule_type="cron")

    def test_cron_invalid_expression_raises(self) -> None:
        with pytest.raises(InvalidScheduleException):
            ScheduleService.calculate_next_execution(
                schedule_type="cron", cron_expression="garbage"
            )

    def test_interval_success(self) -> None:
        next_exec = ScheduleService.calculate_next_execution(
            schedule_type="interval", interval_seconds=30
        )
        assert next_exec > datetime.utcnow()

    def test_interval_missing_raises(self) -> None:
        with pytest.raises(InvalidScheduleException):
            ScheduleService.calculate_next_execution(schedule_type="interval")

    def test_one_time_success(self) -> None:
        future = (datetime.utcnow() + timedelta(days=1)).isoformat()
        next_exec = ScheduleService.calculate_next_execution(
            schedule_type="one_time", scheduled_time=future
        )
        assert isinstance(next_exec, datetime)

    def test_one_time_missing_raises(self) -> None:
        with pytest.raises(InvalidScheduleException):
            ScheduleService.calculate_next_execution(schedule_type="one_time")

    def test_one_time_invalid_format_raises(self) -> None:
        with pytest.raises(InvalidScheduleException):
            ScheduleService.calculate_next_execution(
                schedule_type="one_time", scheduled_time="not-a-date"
            )

    def test_one_time_in_past_raises(self) -> None:
        past = (datetime.utcnow() - timedelta(days=1)).isoformat()
        with pytest.raises(InvalidScheduleException):
            ScheduleService.calculate_next_execution(
                schedule_type="one_time", scheduled_time=past
            )

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(InvalidScheduleException):
            ScheduleService.calculate_next_execution(schedule_type="unknown")


class TestLoadActiveSchedules:
    @pytest.mark.asyncio
    async def test_loads_rows_into_memory(self) -> None:
        dal = MagicMock()
        row = ("s1", "wf-1", "interval", datetime.utcnow(), 0, None)
        dal.executesql = AsyncMock(return_value=[row])
        svc = _service(dal=dal)
        await svc._load_active_schedules()
        assert "s1" in svc._active_schedules

    @pytest.mark.asyncio
    async def test_swallows_db_error(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(side_effect=Exception("db down"))
        svc = _service(dal=dal)
        await svc._load_active_schedules()  # must not raise
        assert svc._active_schedules == {}


class TestRegisterScheduleWithScheduler:
    @pytest.mark.asyncio
    async def test_cron_registers_job(self) -> None:
        svc = _service()
        await svc._register_schedule_with_scheduler(
            "s1", "wf-1", {"schedule_type": "cron", "cron_expression": "0 0 * * *"},
            datetime.utcnow(),
        )
        svc.scheduler.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_interval_registers_job(self) -> None:
        svc = _service()
        await svc._register_schedule_with_scheduler(
            "s1", "wf-1", {"schedule_type": "interval", "interval_seconds": 30},
            datetime.utcnow(),
        )
        svc.scheduler.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_one_time_does_not_register(self) -> None:
        svc = _service()
        await svc._register_schedule_with_scheduler(
            "s1", "wf-1", {"schedule_type": "one_time"}, datetime.utcnow(),
        )
        svc.scheduler.add_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_job_error_is_swallowed(self) -> None:
        svc = _service()
        svc.scheduler.add_job.side_effect = Exception("boom")
        await svc._register_schedule_with_scheduler(
            "s1", "wf-1", {"schedule_type": "interval", "interval_seconds": 30},
            datetime.utcnow(),
        )  # must not raise


class TestHandleScheduleExecution:
    @pytest.mark.asyncio
    async def test_no_row_returns_early(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=[])
        engine = MagicMock()
        svc = _service(dal=dal, workflow_engine=engine)
        await svc._handle_schedule_execution("s1", "wf-1")
        engine.execute_workflow.assert_not_called()

    @pytest.mark.asyncio
    async def test_max_executions_reached_returns_early(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=[(json.dumps({}), 5, 5)])
        engine = MagicMock()
        svc = _service(dal=dal, workflow_engine=engine)
        await svc._handle_schedule_execution("s1", "wf-1")
        engine.execute_workflow.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_executes_and_updates_count(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(
            side_effect=[[(json.dumps({"key": "value"}), 0, None)], None]
        )
        engine = MagicMock()
        engine.execute_workflow = AsyncMock()
        svc = _service(dal=dal, workflow_engine=engine)
        await svc._handle_schedule_execution("s1", "wf-1")
        engine.execute_workflow.assert_awaited_once()
        assert dal.executesql.await_count == 2

    @pytest.mark.asyncio
    async def test_execution_error_is_swallowed(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=[(json.dumps({}), 0, None)])
        engine = MagicMock()
        engine.execute_workflow = AsyncMock(side_effect=Exception("engine boom"))
        svc = _service(dal=dal, workflow_engine=engine)
        await svc._handle_schedule_execution("s1", "wf-1")  # must not raise


class TestExecuteScheduledWorkflow:
    @pytest.mark.asyncio
    async def test_success_updates_memory_cache(self) -> None:
        dal = MagicMock()
        dal.executesql = AsyncMock(return_value=None)
        engine = MagicMock()
        engine.execute_workflow = AsyncMock()
        svc = _service(dal=dal, workflow_engine=engine)
        svc._active_schedules["s1"] = {"execution_count": 0, "next_execution_at": None}
        await svc._execute_scheduled_workflow(
            "s1", "wf-1", {"execution_id": "e1"}, "interval", None, 60
        )
        engine.execute_workflow.assert_awaited_once()
        assert svc._active_schedules["s1"]["execution_count"] == 1

    @pytest.mark.asyncio
    async def test_engine_failure_is_logged_and_swallowed(self) -> None:
        dal = MagicMock()
        engine = MagicMock()
        engine.execute_workflow = AsyncMock(side_effect=Exception("engine down"))
        svc = _service(dal=dal, workflow_engine=engine)
        await svc._execute_scheduled_workflow(
            "s1", "wf-1", {"execution_id": "e1"}, "interval", None, 60
        )  # must not raise


class TestCheckDueSchedulesLoop:
    @pytest.mark.asyncio
    async def test_loop_runs_until_stopped(self) -> None:
        svc = _service()
        svc._is_running = True
        svc.check_due_schedules = AsyncMock(side_effect=lambda: setattr(svc, "_is_running", False))
        with patch("asyncio.sleep", new=AsyncMock()):
            await svc._check_due_schedules_loop()
        svc.check_due_schedules.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_loop_logs_and_continues_on_error(self) -> None:
        svc = _service()
        svc._is_running = True
        calls = {"n": 0}

        async def _check() -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                svc._is_running = False
                return
            raise Exception("check failed")

        svc.check_due_schedules = _check
        with patch("asyncio.sleep", new=AsyncMock()):
            await svc._check_due_schedules_loop()
        assert calls["n"] == 2


class TestSchedulerEventListener:
    def test_job_error_event_logs_error(self) -> None:
        svc = _service()
        event = MagicMock(job_id="s1", exception=Exception("boom"))
        svc._scheduler_event_listener(event)  # must not raise

    def test_job_success_event_logs_info(self) -> None:
        svc = _service()
        event = MagicMock(job_id="s1", exception=None)
        svc._scheduler_event_listener(event)

    def test_no_job_id_is_noop(self) -> None:
        svc = _service()
        event = MagicMock(job_id=None)
        svc._scheduler_event_listener(event)

    def test_listener_exception_is_swallowed(self) -> None:
        svc = _service()
        event = MagicMock()
        type(event).job_id = property(lambda self: (_ for _ in ()).throw(Exception("bad event")))
        svc._scheduler_event_listener(event)  # must not raise
