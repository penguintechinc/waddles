"""
Waddles Module SDK - Base Classes

This package provides the foundational classes for building Waddles modules.
"""

from .module import (
    ExecuteRequest,
    ExecuteResponse,
    BaseModule,
)
from .config import BaseConfig


__all__ = [
    'ExecuteRequest',
    'ExecuteResponse',
    'BaseModule',
    'BaseConfig',
]
