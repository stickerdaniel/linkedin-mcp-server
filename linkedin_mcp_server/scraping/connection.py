"""LLM-driven connection state analysis for LinkedIn profiles."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ConnectionState = Literal[
    "already_connected",
    "pending",
    "incoming_request",
    "connectable",
    "follow_only",
    "unavailable",
]


class ProfileAnalysis(BaseModel):
    """LLM-produced analysis of a LinkedIn profile's connection state."""

    state: ConnectionState = Field(
        description="The relationship state between the viewer and this profile.",
    )
    action_button_text: str | None = Field(
        default=None,
        description=(
            "Exact visible text of the button to click for the connection action. "
            "For example 'Connect', 'Accept', 'Vernetzen', 'Annehmen'. "
            "None when no actionable button exists (already_connected, pending, unavailable)."
        ),
    )
    reasoning: str = Field(
        default="",
        description="Brief reasoning for the classification.",
    )


ANALYSIS_SYSTEM_PROMPT = """\
You are analyzing a LinkedIn profile page to determine the connection state \
between the viewer and the profile owner. You will receive the visible text \
of the profile page.

Classify the state as one of:

- already_connected: The viewer and profile owner are 1st-degree connections. \
  Indicators: "· 1st" near the name, "Message" as the primary action button, \
  or "Remove connection" in a menu. No Connect button is present.
- pending: A connection request has already been sent and is waiting. \
  Indicators: a "Pending" button is visible.
- incoming_request: The profile owner sent the viewer a connection request. \
  Indicators: "Accept" and "Ignore" buttons are visible.
- connectable: A "Connect" button is visible, either directly or in a \
  More/overflow menu. The viewer can send a new connection request.
- follow_only: Only a "Follow" button is the primary action, with no \
  Connect option visible anywhere.
- unavailable: None of the above states are detectable.

For action_button_text, provide the EXACT text shown on the actionable button \
(e.g. "Connect", "Accept"). This must match what is visually displayed on the \
page. Set to null for non-actionable states.\
"""


def build_analysis_message(page_text: str) -> str:
    """Build the user message for profile connection state analysis."""
    # Cap text to avoid excessive token usage; action area is near the top
    trimmed = page_text[:3000]
    return (
        "Here is the visible text from the LinkedIn profile page:\n\n"
        f"---\n{trimmed}\n---"
    )
