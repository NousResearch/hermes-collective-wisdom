"""Strict parser for the existing hash-covered setup guidance document.

Format and behavior are selectively extracted from Agent 0924f7946b's
``agent_led/setup_document.py`` and setup guidance models. Publisher prose is
data only; parsing it never grants execution authority.
"""

from __future__ import annotations

import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contract import canonical_json_bytes
from .credential_scan import scan_credentials


SETUP_PATH = "refs/wisdom-setup.md"
PREFIX = (
    "# Setup and portability\n\n"
    "Installing files does not authorize executing these instructions.\n\n```json\n"
)
SUFFIX = "\n```\n"


def _safe_visible_line(value: str) -> str:
    if any(
        char in "\r\n" or ord(char) < 32 or ord(char) == 127
        or unicodedata.category(char) == "Cf"
        for char in value
    ):
        raise ValueError("setup guidance must contain single visible lines")
    if scan_credentials([{"path": SETUP_PATH, "content": value}]):
        raise ValueError("setup guidance contains a credential-shaped string")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True,
                              hide_input_in_errors=True)


class SetupRequirement(_Strict):
    """One publisher-declared prerequisite, never its secret value."""

    kind: Literal[
        "command", "account", "service", "permission", "env_var", "skill", "script"
    ]
    name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=300)
    handoff: str | None = Field(default=None, max_length=300)

    @field_validator("name", "purpose", "handoff")
    @classmethod
    def _visible(cls, value: str | None) -> str | None:
        return _safe_visible_line(value) if value is not None else None


class SetupDocument(_Strict):
    """Version-one setup guidance stored in ``refs/wisdom-setup.md``."""

    schema_version: Literal[1] = 1
    execution_requires_user_approval: Literal[True] = True
    requirements: list[SetupRequirement] = Field(default_factory=list, max_length=40)
    setup_instructions: list[str] = Field(default_factory=list, max_length=20)
    credential_handoff: list[str] = Field(default_factory=list, max_length=20)
    compatibility_limits: list[str] = Field(default_factory=list, max_length=20)
    verification_step: str = Field(min_length=1, max_length=400)
    removed_or_generalized: list[str] = Field(default_factory=list, max_length=40)
    related_skills: list[str] = Field(default_factory=list, max_length=20)

    @field_validator(
        "setup_instructions", "credential_handoff", "compatibility_limits",
        "removed_or_generalized", "related_skills",
    )
    @classmethod
    def _visible_lines(cls, values: list[str]) -> list[str]:
        return [_safe_visible_line(value) for value in values]

    @field_validator("verification_step")
    @classmethod
    def _visible_verification(cls, value: str) -> str:
        return _safe_visible_line(value)


def parse_setup_document(body: bytes) -> SetupDocument:
    """Parse only complete canonical v1 guidance; never repair or truncate it."""

    if not isinstance(body, bytes):
        raise TypeError("setup document must be bytes")
    text = body.decode("utf-8")
    if not text.startswith(PREFIX) or not text.endswith(SUFFIX):
        raise ValueError("setup document has an unsupported format")
    raw = text[len(PREFIX) : -len(SUFFIX)]
    document = SetupDocument.model_validate_json(raw, strict=True)
    canonical = canonical_json_bytes(
        document.model_dump(mode="json", exclude_none=True)
    ).decode("utf-8")
    if canonical != raw:
        raise ValueError("setup document is not the complete canonical guidance")
    return document
