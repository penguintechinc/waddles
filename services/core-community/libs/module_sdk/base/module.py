"""
Base module classes and data structures for Waddles Module SDK.

This module provides the foundational abstract classes and data structures
for creating Waddles modules.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExecuteRequest:
    """
    Request data structure for module execution.

    Attributes:
        command: The command to execute
        args: Arguments passed to the command
        user_id: ID of the user making the request
        entity_id: ID of the entity (user/bot) in the platform context
        community_id: ID of the community/server/channel
        session_id: Unique session identifier for tracking
        platform: Platform identifier (e.g., 'discord', 'twitch', 'matrix')
        metadata: Additional metadata for the request
        scopes: List of permission scopes for this request
    """
    command: str
    args: List[str]
    user_id: str
    entity_id: str
    community_id: str
    session_id: str
    platform: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    scopes: List[str] = field(default_factory=list)


@dataclass
class ExecuteResponse:
    """
    Response data structure for module execution.

    Attributes:
        success: Whether the execution was successful
        message: Optional message to return to the user
        data: Optional dictionary of data to return
        error: Optional error message if execution failed
        targets: List of target dictionaries for response routing
                 Each target should contain platform-specific delivery info
    """
    success: bool
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    targets: List[Dict[str, Any]] = field(default_factory=list)


class BaseModule(ABC):
    """
    Abstract base class for Waddles modules.

    All modules must inherit from this class and implement the execute method.
    Modules should define their name, version, and required scopes as class attributes.

    Class Attributes:
        MODULE_NAME: Unique identifier for the module
        MODULE_VERSION: Semantic version string
        REQUIRED_SCOPES: List of permission scopes required by this module
    """

    MODULE_NAME: str = "base_module"
    MODULE_VERSION: str = "0.1.0"
    REQUIRED_SCOPES: List[str] = []

    @abstractmethod
    def execute(self, request: ExecuteRequest) -> ExecuteResponse:
        """
        Execute the module's primary function.

        Args:
            request: ExecuteRequest containing command, args, and context

        Returns:
            ExecuteResponse with results of the execution

        Raises:
            NotImplementedError: Must be implemented by subclasses
        """
        raise NotImplementedError("Subclasses must implement execute()")

    def validate_scopes(self, granted: List[str]) -> bool:
        """
        Validate that all required scopes are present in the granted scopes.

        Args:
            granted: List of scopes granted to the user/session

        Returns:
            True if all required scopes are granted, False otherwise
        """
        if not self.REQUIRED_SCOPES:
            return True

        granted_set = set(granted)
        required_set = set(self.REQUIRED_SCOPES)

        return required_set.issubset(granted_set)

    def get_module_info(self) -> Dict[str, Any]:
        """
        Get information about this module.

        Returns:
            Dictionary containing module metadata
        """
        return {
            "name": self.MODULE_NAME,
            "version": self.MODULE_VERSION,
            "required_scopes": self.REQUIRED_SCOPES,
        }
