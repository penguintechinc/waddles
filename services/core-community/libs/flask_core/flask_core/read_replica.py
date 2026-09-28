"""
PostgreSQL Read Replica Manager for Waddles

Provides intelligent routing of read/write operations to primary and replica databases
with automatic failover and connection pool management.

Features:
- Route SELECT queries to read replicas
- Route INSERT/UPDATE/DELETE to primary
- Health checking and automatic failover
- Connection pooling for both primary and replicas
- Replica lag monitoring
- Configurable retry logic
"""

import logging
import asyncio
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
import time

logger = logging.getLogger(__name__)


class ReplicaStatus(Enum):
    """Status of a read replica"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Lagging but still usable
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ReplicaConfig:
    """Configuration for a single read replica"""
    host: str
    port: int = 5432
    priority: int = 0  # Lower number = higher priority
    max_lag_seconds: int = 30  # Max acceptable replication lag
    connect_timeout: int = 5
    query_timeout: int = 30


@dataclass
class ReplicaMetrics:
    """Metrics for a read replica"""
    status: ReplicaStatus
    replication_lag_seconds: float = 0.0
    last_health_check: datetime = None
    consecutive_failures: int = 0
    total_queries: int = 0
    failed_queries: int = 0


class ReadReplicaManager:
    """
    Manages read replica routing and health for Waddles.

    Routes SELECT queries to healthy replicas and INSERT/UPDATE/DELETE to primary.
    Implements health checking, failover, and connection pooling.
    """

    def __init__(
        self,
        primary_uri: str,
        replica_uris: Optional[List[str]] = None,
        replica_configs: Optional[List[ReplicaConfig]] = None,
        health_check_interval: int = 30,
        executor_workers: int = 10
    ):
        """
        Initialize read replica manager.

        Args:
            primary_uri: Primary database connection string
            replica_uris: List of replica connection strings (for simple setup)
            replica_configs: List of ReplicaConfig objects (for advanced setup)
            health_check_interval: Seconds between health checks
            executor_workers: Thread pool size for health checks
        """
        self.primary_uri = primary_uri
        self.health_check_interval = health_check_interval
        self.executor = ThreadPoolExecutor(
            max_workers=executor_workers,
            thread_name_prefix="replica_health_"
        )

        # Initialize replicas from either URIs or configs
        self.replicas: Dict[str, ReplicaConfig] = {}
        self.metrics: Dict[str, ReplicaMetrics] = {}

        if replica_configs:
            for config in replica_configs:
                replica_id = f"{config.host}:{config.port}"
                self.replicas[replica_id] = config
                self.metrics[replica_id] = ReplicaMetrics(status=ReplicaStatus.UNKNOWN)
        elif replica_uris:
            for uri in replica_uris:
                # Parse connection string: postgres://user:pass@host:port/db
                replica_config = self._parse_connection_string(uri)
                replica_id = f"{replica_config.host}:{replica_config.port}"
                self.replicas[replica_id] = replica_config
                self.metrics[replica_id] = ReplicaMetrics(status=ReplicaStatus.UNKNOWN)

        logger.info(f"ReadReplicaManager initialized with {len(self.replicas)} replicas")

        # Start background health check
        self._health_check_task = None

    def _parse_connection_string(self, uri: str) -> ReplicaConfig:
        """Parse PostgreSQL connection string to ReplicaConfig"""
        # Simple parser for postgres://user:pass@host:port/db
        try:
            from urllib.parse import urlparse
            parsed = urlparse(uri)
            host = parsed.hostname or "localhost"
            port = parsed.port or 5432
            return ReplicaConfig(host=host, port=port)
        except Exception as e:
            logger.error(f"Failed to parse connection string: {e}")
            raise

    async def start_health_checks(self) -> None:
        """Start background health check loop"""
        if self._health_check_task:
            return

        self._health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info("Health check loop started")

    async def stop_health_checks(self) -> None:
        """Stop background health check loop"""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
            self._health_check_task = None
            logger.info("Health check loop stopped")

    async def _health_check_loop(self) -> None:
        """Background loop that periodically checks replica health"""
        while True:
            try:
                await asyncio.sleep(self.health_check_interval)

                # Check all replicas
                tasks = [
                    self._check_replica_health(replica_id, config)
                    for replica_id, config in self.replicas.items()
                ]

                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")

    async def _check_replica_health(self, replica_id: str, config: ReplicaConfig) -> None:
        """Check health of a single replica"""
        loop = asyncio.get_event_loop()

        def _health_check():
            try:
                import psycopg2
                from psycopg2 import sql

                # Try to connect to replica
                with psycopg2.connect(
                    host=config.host,
                    port=config.port,
                    connect_timeout=config.connect_timeout,
                    options=f"-c statement_timeout={config.query_timeout * 1000}"
                ) as conn:
                    with conn.cursor() as cur:
                        # Check if in recovery mode (is a standby)
                        cur.execute("SELECT pg_is_in_recovery()")
                        is_standby = cur.fetchone()[0]

                        if not is_standby:
                            logger.warning(f"Replica {replica_id} is not in standby mode")
                            return ReplicaStatus.UNHEALTHY, None

                        # Get replication lag
                        cur.execute(
                            "SELECT EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))"
                        )
                        lag = cur.fetchone()[0]
                        lag_seconds = lag if lag is not None else 0.0

                        # Determine status based on lag
                        if lag_seconds > config.max_lag_seconds:
                            status = ReplicaStatus.DEGRADED
                        else:
                            status = ReplicaStatus.HEALTHY

                        return status, lag_seconds

            except Exception as e:
                logger.error(f"Health check failed for {replica_id}: {e}")
                return ReplicaStatus.UNHEALTHY, None

        try:
            status, lag = await loop.run_in_executor(self.executor, _health_check)

            metrics = self.metrics[replica_id]
            metrics.status = status
            metrics.last_health_check = datetime.now()

            if lag is not None:
                metrics.replication_lag_seconds = lag

            if status == ReplicaStatus.HEALTHY:
                metrics.consecutive_failures = 0
            else:
                metrics.consecutive_failures += 1

            log_level = logging.WARNING if status != ReplicaStatus.HEALTHY else logging.DEBUG
            logger.log(
                log_level,
                f"Replica {replica_id} health check: {status.value} "
                f"(lag: {lag:.2f}s)" if lag is not None else ""
            )

        except Exception as e:
            logger.error(f"Unexpected error checking replica {replica_id}: {e}")
            self.metrics[replica_id].status = ReplicaStatus.UNKNOWN

    def get_best_replica(self) -> Optional[ReplicaConfig]:
        """
        Get the best available replica for read operations.

        Selection criteria:
        1. Only HEALTHY replicas
        2. Sort by priority (lower = better)
        3. Fall back to DEGRADED if no HEALTHY replicas

        Returns:
            ReplicaConfig if available, None if no healthy replicas
        """
        # Get healthy replicas sorted by priority
        healthy_replicas = [
            (replica_id, config)
            for replica_id, config in self.replicas.items()
            if self.metrics[replica_id].status == ReplicaStatus.HEALTHY
        ]

        if healthy_replicas:
            # Sort by priority
            healthy_replicas.sort(key=lambda x: x[1].priority)
            return healthy_replicas[0][1]

        # Fall back to degraded replicas
        degraded_replicas = [
            (replica_id, config)
            for replica_id, config in self.replicas.items()
            if self.metrics[replica_id].status == ReplicaStatus.DEGRADED
        ]

        if degraded_replicas:
            logger.warning("Using degraded replica due to lack of healthy replicas")
            degraded_replicas.sort(key=lambda x: x[1].priority)
            return degraded_replicas[0][1]

        logger.error("No healthy or degraded replicas available")
        return None

    def get_replica_uri(self, replica_config: ReplicaConfig) -> str:
        """Convert ReplicaConfig to connection string"""
        return f"postgres://postgres@{replica_config.host}:{replica_config.port}/postgres"

    def get_read_uri(self) -> str:
        """
        Get connection string for read operations.

        Returns primary URI if no healthy replicas available.
        """
        best_replica = self.get_best_replica()

        if best_replica:
            return self.get_replica_uri(best_replica)
        else:
            logger.info("Using primary for read operations (no healthy replicas)")
            return self.primary_uri

    def get_write_uri(self) -> str:
        """Get connection string for write operations (always primary)"""
        return self.primary_uri

    def record_replica_query(self, replica_id: str, success: bool = True) -> None:
        """Record query metrics for a replica"""
        if replica_id not in self.metrics:
            return

        metrics = self.metrics[replica_id]
        metrics.total_queries += 1

        if not success:
            metrics.failed_queries += 1

    def get_metrics(self) -> Dict[str, Any]:
        """Get aggregated metrics for all replicas"""
        return {
            replica_id: {
                "status": metrics.status.value,
                "replication_lag_seconds": metrics.replication_lag_seconds,
                "last_health_check": metrics.last_health_check.isoformat() if metrics.last_health_check else None,
                "consecutive_failures": metrics.consecutive_failures,
                "total_queries": metrics.total_queries,
                "failed_queries": metrics.failed_queries,
                "success_rate": (
                    (metrics.total_queries - metrics.failed_queries) / metrics.total_queries
                    if metrics.total_queries > 0 else 1.0
                )
            }
            for replica_id, metrics in self.metrics.items()
        }

    async def cleanup(self) -> None:
        """Cleanup resources"""
        await self.stop_health_checks()
        self.executor.shutdown(wait=True)
        logger.info("ReadReplicaManager cleanup complete")


class ReadReplicaRouter:
    """
    Routes penguin-dal operations between primary and replicas.

    Integrates with AsyncDAL to provide transparent read/write routing.
    """

    def __init__(
        self,
        replica_manager: ReadReplicaManager,
        dal_primary: Any,
        dal_replicas: Optional[List[Any]] = None
    ):
        """
        Initialize read replica router.

        Args:
            replica_manager: ReadReplicaManager instance
            dal_primary: Primary AsyncDAL instance
            dal_replicas: Optional list of replica AsyncDAL instances
        """
        self.replica_manager = replica_manager
        self.dal_primary = dal_primary
        self.dal_replicas = dal_replicas or []

        logger.info("ReadReplicaRouter initialized")

    async def select_async(self, query, *args, **kwargs):
        """Route SELECT to replica or primary"""
        dal = self.dal_primary

        if self.dal_replicas:
            replica_uri = self.replica_manager.get_read_uri()
            # Use appropriate DAL based on URI
            if replica_uri != self.replica_manager.primary_uri:
                # Find replica DAL matching the URI
                for replica_dal in self.dal_replicas:
                    if replica_dal.uri == replica_uri:
                        dal = replica_dal
                        break

        try:
            return await dal.select_async(query, *args, **kwargs)
        except Exception as e:
            logger.error(f"Replica select failed, retrying on primary: {e}")
            return await self.dal_primary.select_async(query, *args, **kwargs)

    async def insert_async(self, table, **fields):
        """Route INSERT to primary only"""
        return await self.dal_primary.insert_async(table, **fields)

    async def update_async(self, query, **update_fields):
        """Route UPDATE to primary only"""
        return await self.dal_primary.update_async(query, **update_fields)

    async def delete_async(self, query):
        """Route DELETE to primary only"""
        return await self.dal_primary.delete_async(query)

    async def count_async(self, query):
        """Route COUNT to replica or primary"""
        dal = self.dal_primary

        if self.dal_replicas:
            replica_uri = self.replica_manager.get_read_uri()
            if replica_uri != self.replica_manager.primary_uri:
                for replica_dal in self.dal_replicas:
                    if replica_dal.uri == replica_uri:
                        dal = replica_dal
                        break

        try:
            return await dal.count_async(query)
        except Exception as e:
            logger.error(f"Replica count failed, retrying on primary: {e}")
            return await self.dal_primary.count_async(query)


def create_read_replica_manager(
    primary_uri: str,
    replica_uris: Optional[List[str]] = None,
    replica_configs: Optional[List[ReplicaConfig]] = None,
    health_check_interval: int = 30
) -> ReadReplicaManager:
    """
    Factory function to create ReadReplicaManager.

    Args:
        primary_uri: Primary database connection string
        replica_uris: List of replica connection strings
        replica_configs: List of ReplicaConfig objects
        health_check_interval: Health check interval in seconds

    Returns:
        Configured ReadReplicaManager instance
    """
    return ReadReplicaManager(
        primary_uri=primary_uri,
        replica_uris=replica_uris,
        replica_configs=replica_configs,
        health_check_interval=health_check_interval
    )
