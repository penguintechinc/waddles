"""
Waddles Module SDK

A software development kit for creating Waddles modules with
standardized interfaces, configuration management, and utilities.

The SDK provides:
- Base classes for creating modules (BaseModule)
- Request/Response data structures (ExecuteRequest, ExecuteResponse)
- Configuration management (BaseConfig)
- Common utilities for module development

Example:
    ```python
    from module_sdk import BaseModule, ExecuteRequest, ExecuteResponse

    class MyModule(BaseModule):
        MODULE_NAME = "my_module"
        MODULE_VERSION = "1.0.0"
        REQUIRED_SCOPES = ["read", "write"]

        def execute(self, request: ExecuteRequest) -> ExecuteResponse:
            # Module logic here
            return ExecuteResponse(
                success=True,
                message="Command executed successfully"
            )
    ```
"""

from .base import (
    BaseModule,
    BaseConfig,
    ExecuteRequest,
    ExecuteResponse,
)


__version__ = "0.1.0"

__all__ = [
    'BaseModule',
    'BaseConfig',
    'ExecuteRequest',
    'ExecuteResponse',
]
