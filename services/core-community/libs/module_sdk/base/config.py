"""
Base configuration classes for Waddles modules.

This module provides base configuration classes using Pydantic for
validation and settings management.
"""

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, validator


class BaseConfig(BaseModel):
    """
    Base configuration class for Waddles modules.

    This class uses Pydantic for validation and provides a foundation
    for module-specific configuration classes.

    Attributes:
        module_name: Name of the module this config belongs to
        enabled: Whether the module is enabled
        debug: Whether debug mode is enabled
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        settings: Additional module-specific settings
    """

    module_name: str = Field(..., description="Name of the module")
    enabled: bool = Field(default=True, description="Whether the module is enabled")
    debug: bool = Field(default=False, description="Enable debug mode")
    log_level: str = Field(default="INFO", description="Logging level")
    settings: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional module-specific settings"
    )

    @validator('log_level')
    def validate_log_level(cls, v):
        """
        Validate that log_level is a valid logging level.

        Args:
            v: The log level string to validate

        Returns:
            The validated log level string (uppercase)

        Raises:
            ValueError: If the log level is invalid
        """
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        v_upper = v.upper()

        if v_upper not in valid_levels:
            raise ValueError(
                f"log_level must be one of {valid_levels}, got {v}"
            )

        return v_upper

    @validator('module_name')
    def validate_module_name(cls, v):
        """
        Validate that module_name is not empty and contains valid characters.

        Args:
            v: The module name to validate

        Returns:
            The validated module name

        Raises:
            ValueError: If the module name is invalid
        """
        if not v or not v.strip():
            raise ValueError("module_name cannot be empty")

        # Module names should be alphanumeric with underscores/hyphens
        if not all(c.isalnum() or c in ('_', '-') for c in v):
            raise ValueError(
                "module_name must contain only alphanumeric characters, "
                "underscores, or hyphens"
            )

        return v

    class Config:
        """Pydantic configuration."""
        extra = 'allow'  # Allow extra fields for module-specific configs
        validate_assignment = True  # Validate on attribute assignment

    def get_setting(self, key: str, default: Any = None) -> Any:
        """
        Get a setting value from the settings dictionary.

        Args:
            key: The setting key to retrieve
            default: Default value if key is not found

        Returns:
            The setting value or default
        """
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        """
        Set a setting value in the settings dictionary.

        Args:
            key: The setting key to set
            value: The value to set
        """
        self.settings[key] = value

    def update_settings(self, new_settings: Dict[str, Any]) -> None:
        """
        Update multiple settings at once.

        Args:
            new_settings: Dictionary of settings to update
        """
        self.settings.update(new_settings)
