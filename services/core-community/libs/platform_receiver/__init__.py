"""platform_receiver — shared library for WaddleBot platform receiver bots.

Import the base class and event schema helpers from here:

    from platform_receiver import PlatformReceiverBase
    from platform_receiver.schema import EventSchema
    from platform_receiver.response import format_text_response
"""
from .base import PlatformReceiverBase

__all__ = ["PlatformReceiverBase"]
