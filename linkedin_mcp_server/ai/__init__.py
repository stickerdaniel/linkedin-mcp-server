"""
AI module for generating personalized connection messages.

Provides template-based message generation with personalization
based on profile information.
"""

from .message_generator import (
    ConnectionMessageGenerator,
    get_message_generator,
)
from .prompts import (
    MESSAGE_GENERATION_TEMPLATE,
    MINIMAL_MESSAGE_TEMPLATE,
    REASON_TEMPLATES,
    SYSTEM_PROMPT,
    get_reason_template,
)

__all__ = [
    # Message generator
    "ConnectionMessageGenerator",
    "get_message_generator",
    # Prompts
    "SYSTEM_PROMPT",
    "MESSAGE_GENERATION_TEMPLATE",
    "MINIMAL_MESSAGE_TEMPLATE",
    "REASON_TEMPLATES",
    "get_reason_template",
]
