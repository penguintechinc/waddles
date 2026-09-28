"""
Comprehensive AAA Logging Configuration
========================================

Authentication, Authorization, and Audit (AAA) logging for all Waddles modules.

Provides:
- Console logging for container orchestration
- File logging with rotation
- Optional syslog support
- Structured logging format
- All log categories: AUTH, AUTHZ, AUDIT, ERROR, SYSTEM
"""

import logging
import sys
from logging.handlers import RotatingFileHandler, SysLogHandler
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
import json  # noqa: F401 - used in StructuredFormatter


class StructuredFormatter(logging.Formatter):
    """
    Structured log formatter with consistent format:
    [timestamp] LEVEL module:version EVENT_TYPE community=X user=Y action=Z result=STATUS [additional_fields]
    """

    def __init__(self, module_name: str, version: str):
        super().__init__()
        self.module_name = module_name
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with structured data"""
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        level = record.levelname

        # Extract structured data from record
        event_type = getattr(record, 'event_type', 'GENERAL')
        community = getattr(record, 'community', '')
        user = getattr(record, 'user', '')
        action = getattr(record, 'action', '')
        result = getattr(record, 'result', '')
        execution_time = getattr(record, 'execution_time', None)

        # Build base message
        parts = [
            f"[{timestamp}]",
            level,
            f"{self.module_name}:{self.version}",
            event_type
        ]

        # Add context fields
        if community:
            parts.append(f"community={community}")
        if user:
            parts.append(f"user={user}")
        if action:
            parts.append(f"action={action}")
        if result:
            parts.append(f"result={result}")
        if execution_time is not None:
            parts.append(f"time={execution_time}ms")

        # Add message
        parts.append(record.getMessage())

        # Add additional fields if present
        if hasattr(record, 'additional'):
            additional = record.additional
            if isinstance(additional, dict):
                for key, value in additional.items():
                    parts.append(f"{key}={value}")

        return " ".join(parts)


class AAALogger:
    """
    Comprehensive AAA logger for Waddles modules.
    """

    def __init__(
        self,
        module_name: str,
        version: str,
        log_level: str = "INFO",
        log_dir: Optional[str] = None,
        enable_syslog: bool = False,
        syslog_host: str = "localhost",
        syslog_port: int = 514,
        syslog_facility: str = "LOCAL0"
    ):
        """
        Initialize AAA logger.

        Args:
            module_name: Name of the module
            version: Module version
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
            log_dir: Directory for log files (default: /var/log/waddlebotlog)
            enable_syslog: Enable syslog output
            syslog_host: Syslog server host
            syslog_port: Syslog server port
            syslog_facility: Syslog facility
        """
        self.module_name = module_name
        self.version = version
        self.log_level = getattr(logging, log_level.upper())
        self.log_dir = log_dir or "/var/log/waddlebotlog"
        self.enable_syslog = enable_syslog
        self.syslog_host = syslog_host
        self.syslog_port = syslog_port
        self.syslog_facility = getattr(SysLogHandler, f"LOG_{syslog_facility.upper()}")

        # Create logger
        self.logger = logging.getLogger(f"waddlebot.{module_name}")
        self.logger.setLevel(self.log_level)
        self.logger.propagate = False

        # Clear existing handlers
        self.logger.handlers.clear()

        # Setup handlers
        self._setup_console_handler()
        self._setup_file_handler()
        if self.enable_syslog:
            self._setup_syslog_handler()

    def _setup_console_handler(self):
        """Setup console handler for stdout/stderr"""
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self.log_level)
        console_handler.setFormatter(StructuredFormatter(self.module_name, self.version))
        self.logger.addHandler(console_handler)

    def _setup_file_handler(self):
        """Setup rotating file handler"""
        try:
            # Create log directory if it doesn't exist
            log_path = Path(self.log_dir)
            log_path.mkdir(parents=True, exist_ok=True)

            # Create rotating file handler (10MB, 5 backups)
            log_file = log_path / f"{self.module_name}.log"
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10MB
                backupCount=5
            )
            file_handler.setLevel(self.log_level)
            file_handler.setFormatter(StructuredFormatter(self.module_name, self.version))
            self.logger.addHandler(file_handler)

        except Exception as e:
            self.logger.error(f"Failed to setup file handler: {e}")

    def _setup_syslog_handler(self):
        """Setup syslog handler"""
        try:
            syslog_handler = SysLogHandler(
                address=(self.syslog_host, self.syslog_port),
                facility=self.syslog_facility
            )
            syslog_handler.setLevel(self.log_level)
            syslog_handler.setFormatter(StructuredFormatter(self.module_name, self.version))
            self.logger.addHandler(syslog_handler)

        except Exception as e:
            self.logger.error(f"Failed to setup syslog handler: {e}")

    def auth(self, action: str, user: str, result: str, **kwargs):
        """Log authentication event"""
        extra = self._build_extra("AUTH", user=user, action=action, result=result, **kwargs)
        self.logger.info(f"Authentication: {action} - {result}", extra=extra)

    def authz(self, action: str, user: str, community: str, result: str, **kwargs):
        """Log authorization event"""
        extra = self._build_extra("AUTHZ", user=user, community=community, action=action, result=result, **kwargs)
        self.logger.info(f"Authorization: {action} - {result}", extra=extra)

    def audit(self, action: str, user: str, community: str, result: str, **kwargs):
        """Log audit event"""
        extra = self._build_extra("AUDIT", user=user, community=community, action=action, result=result, **kwargs)
        self.logger.info(f"Audit: {action} - {result}", extra=extra)

    def debug(self, message: str, **kwargs):
        """Log debug message"""
        extra = self._build_extra("DEBUG", **kwargs)
        self.logger.debug(message, extra=extra)

    def info(self, message: str, **kwargs):
        """Log info message"""
        extra = self._build_extra("INFO", **kwargs)
        self.logger.info(message, extra=extra)

    def warning(self, message: str, **kwargs):
        """Log warning message"""
        extra = self._build_extra("WARNING", **kwargs)
        self.logger.warning(message, extra=extra)

    def error(self, message: str, **kwargs):
        """Log error event"""
        extra = self._build_extra("ERROR", **kwargs)
        self.logger.error(message, extra=extra)

    def critical(self, message: str, **kwargs):
        """Log critical message"""
        extra = self._build_extra("CRITICAL", **kwargs)
        self.logger.critical(message, extra=extra)

    def system(self, message: str, **kwargs):
        """Log system event"""
        extra = self._build_extra("SYSTEM", **kwargs)
        self.logger.info(message, extra=extra)

    def performance(self, action: str, execution_time: int, **kwargs):
        """Log performance metric"""
        extra = self._build_extra("PERFORMANCE", action=action, execution_time=execution_time, **kwargs)
        self.logger.info(f"Performance: {action} completed in {execution_time}ms", extra=extra)

    def _build_extra(self, event_type: str, **kwargs) -> Dict[str, Any]:
        """Build extra data for logging"""
        extra = {
            'event_type': event_type,
            'community': kwargs.get('community', ''),
            'user': kwargs.get('user', ''),
            'action': kwargs.get('action', ''),
            'result': kwargs.get('result', ''),
            'execution_time': kwargs.get('execution_time')
        }

        # Add additional fields
        additional = {k: v for k, v in kwargs.items() if k not in extra}
        if additional:
            extra['additional'] = additional

        return extra


def setup_aaa_logging(
    module_name: str,
    version: str,
    log_level: Optional[str] = None,
    log_dir: Optional[str] = None,
    enable_syslog: Optional[bool] = None,
    syslog_host: Optional[str] = None,
    syslog_port: Optional[int] = None,
    syslog_facility: Optional[str] = None
) -> AAALogger:
    """
    Setup AAA logging for a module.

    Args:
        module_name: Module name
        version: Module version
        log_level: Logging level (defaults to INFO)
        log_dir: Log directory (defaults to /var/log/waddlebotlog)
        enable_syslog: Enable syslog (defaults to False)
        syslog_host: Syslog host (defaults to localhost)
        syslog_port: Syslog port (defaults to 514)
        syslog_facility: Syslog facility (defaults to LOCAL0)

    Returns:
        Configured AAALogger instance
    """
    import os

    return AAALogger(
        module_name=module_name,
        version=version,
        log_level=log_level or os.getenv('LOG_LEVEL', 'INFO'),
        log_dir=log_dir or os.getenv('LOG_DIR', '/var/log/waddlebotlog'),
        enable_syslog=(enable_syslog if enable_syslog is not None
                       else os.getenv('ENABLE_SYSLOG', 'false').lower() == 'true'),
        syslog_host=syslog_host or os.getenv('SYSLOG_HOST', 'localhost'),
        syslog_port=syslog_port or int(os.getenv('SYSLOG_PORT', '514')),
        syslog_facility=syslog_facility or os.getenv('SYSLOG_FACILITY', 'LOCAL0')
    )


def get_logger(module_name: str) -> logging.Logger:
    """
    Get logger for a module.

    Args:
        module_name: Module name

    Returns:
        Logger instance
    """
    return logging.getLogger(f"waddlebot.{module_name}")
