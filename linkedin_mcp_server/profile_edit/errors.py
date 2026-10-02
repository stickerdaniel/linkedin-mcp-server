"""Structured outcomes for profile editing.

A business outcome (stale state, ambiguous target, validation) is returned to
the caller as data with a stable code, not raised as a tool error: an agent
needs the code to decide what to do next, and the user needs the explanation.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ProfileEditErrorCode(StrEnum):
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    EXPERIENCE_NOT_FOUND = "EXPERIENCE_NOT_FOUND"
    AMBIGUOUS_EXPERIENCE = "AMBIGUOUS_EXPERIENCE"
    SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
    SELECTOR_NOT_FOUND = "SELECTOR_NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    STALE_CHANGE_SET = "STALE_CHANGE_SET"
    CHANGE_SET_NOT_FOUND = "CHANGE_SET_NOT_FOUND"
    CHANGE_SET_NOT_PENDING = "CHANGE_SET_NOT_PENDING"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    WRITES_DISABLED = "WRITES_DISABLED"
    LINKEDIN_SAVE_FAILED = "LINKEDIN_SAVE_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    UNSUPPORTED_FIELD = "UNSUPPORTED_FIELD"


_EXPLANATIONS: dict[ProfileEditErrorCode, str] = {
    ProfileEditErrorCode.AUTHENTICATION_REQUIRED: "LinkedIn needs you to sign in or clear a security check in the browser. Nothing was bypassed or retried.",
    ProfileEditErrorCode.PROFILE_NOT_FOUND: "The signed-in member's profile could not be opened.",
    ProfileEditErrorCode.EXPERIENCE_NOT_FOUND: "No experience on the profile matches that reference.",
    ProfileEditErrorCode.AMBIGUOUS_EXPERIENCE: "More than one experience matches; pass the exact experienceId of one candidate.",
    ProfileEditErrorCode.SKILL_NOT_FOUND: "That skill is not on the profile, or LinkedIn offered no exact match for it.",
    ProfileEditErrorCode.SELECTOR_NOT_FOUND: "An expected LinkedIn control was not found, so nothing else was clicked. LinkedIn's page may have changed.",
    ProfileEditErrorCode.VALIDATION_ERROR: "The proposed change is not valid as given.",
    ProfileEditErrorCode.STALE_CHANGE_SET: "The profile changed after this change set was proposed. Create a new proposal; nothing was overwritten.",
    ProfileEditErrorCode.CHANGE_SET_NOT_FOUND: "No change set with that id exists.",
    ProfileEditErrorCode.CHANGE_SET_NOT_PENDING: "This change set is no longer awaiting approval and cannot be applied.",
    ProfileEditErrorCode.CONFIRMATION_REQUIRED: "apply_profile_changes modifies LinkedIn and requires confirm=true after the user has approved the preview.",
    ProfileEditErrorCode.WRITES_DISABLED: "LinkedIn writes are disabled. Set MCP_LINKEDIN_WRITE_ENABLED=true for the server to allow them.",
    ProfileEditErrorCode.LINKEDIN_SAVE_FAILED: "LinkedIn did not accept the save.",
    ProfileEditErrorCode.VERIFICATION_FAILED: "The save was submitted but the re-read value on LinkedIn does not match what was approved.",
    ProfileEditErrorCode.PARTIAL_FAILURE: "Some changes were applied and verified; the rest were not. See results for each field.",
    ProfileEditErrorCode.UNSUPPORTED_FIELD: "That field cannot be edited by this server.",
}


class ProfileEditError(Exception):
    """An expected, explainable reason a profile-edit operation stopped."""

    def __init__(
        self,
        code: ProfileEditErrorCode,
        message: str | None = None,
        **details: Any,
    ) -> None:
        self.code = code
        self.message = message or _EXPLANATIONS[code]
        self.details = details
        super().__init__(f"{code}: {self.message}")

    def to_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "ERROR",
            "error": str(self.code),
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result
