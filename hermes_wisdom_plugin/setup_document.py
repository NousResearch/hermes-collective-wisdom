"""Strict parser for the existing hash-covered setup guidance document.

Format and behavior are selectively extracted from Agent 0924f7946b's
``agent_led/setup_document.py`` and setup guidance models. Publisher prose is
data only; parsing it never grants execution authority.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .contract import canonical_json_bytes
from .credential_scan import scan_credentials


SETUP_PATH = "refs/wisdom-setup.md"
PREFIX = (
    "# Setup and portability\n\n"
    "Installing files does not authorize executing these instructions.\n\n```json\n"
)
SUFFIX = "\n```\n"
# Combined UTF-8 bytes of every setup command: the budget of 20 lines (the
# cap on setup_instructions) at the 256-character command line bound. There
# is no command count: splitting a command never costs more than joining it.
MAX_SETUP_COMMAND_CHARS = 256
MAX_SETUP_COMMANDS_TOTAL_BYTES = 20 * MAX_SETUP_COMMAND_CHARS


class SetupGuidanceError(ValueError):
    """The setup document is invalid; the message names the exact field."""


def setup_commands_size(values: list[str]) -> int:
    """Measured size of the setup commands: their combined UTF-8 bytes."""
    return sum(len(value.encode("utf-8")) for value in values)


def _commands_over_budget(values: object) -> str | None:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        return None
    size = setup_commands_size(values)
    if size <= MAX_SETUP_COMMANDS_TOTAL_BYTES:
        return None
    return (
        f"{size} bytes of commands; the limit is "
        f"{MAX_SETUP_COMMANDS_TOTAL_BYTES} bytes in total"
    )


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
    setup_commands: list[str] | None = None
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

    @field_validator("setup_commands")
    @classmethod
    def _visible_commands(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if any(not value.strip() or len(value) > MAX_SETUP_COMMAND_CHARS for value in values):
            raise ValueError("setup commands must be non-empty bounded lines")
        problem = _commands_over_budget(values)
        if problem is not None:
            raise ValueError(problem)
        return [_safe_visible_line(value) for value in values]

    @field_validator("verification_step")
    @classmethod
    def _visible_verification(cls, value: str) -> str:
        return _safe_visible_line(value)


def _validation_message(exc: ValidationError) -> str:
    """Field-level reasons without the publisher's values (inputs are hidden)."""
    reasons = [
        f"{'.'.join(str(part) for part in error['loc']) or 'document'}: "
        f"{str(error['msg']).removeprefix('Value error, ')}"
        for error in exc.errors()
    ]
    return f"{SETUP_PATH} is invalid: " + "; ".join(reasons)


def _parse(body: bytes, *, drop_oversized_commands: bool) -> tuple[SetupDocument, str | None]:
    if not isinstance(body, bytes):
        raise TypeError("setup document must be bytes")
    text = body.decode("utf-8")
    if not text.startswith(PREFIX) or not text.endswith(SUFFIX):
        raise SetupGuidanceError(f"{SETUP_PATH} has an unsupported format")
    raw = text[len(PREFIX) : -len(SUFFIX)]
    expected, dropped = raw, None
    if drop_oversized_commands:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise SetupGuidanceError(f"{SETUP_PATH} is not valid JSON") from exc
        if isinstance(data, dict):
            dropped = _commands_over_budget(data.get("setup_commands"))
        if dropped is not None:
            dropped = (
                f"{SETUP_PATH} field setup_commands: {dropped}. These commands are "
                "not shown and will not run."
            )
            if canonical_json_bytes(data).decode("utf-8") != raw:
                raise SetupGuidanceError(
                    f"{SETUP_PATH} is not the complete canonical guidance"
                )
            # Every other field is still validated exactly; only the
            # commands are set aside.
            expected = canonical_json_bytes(
                {key: value for key, value in data.items() if key != "setup_commands"}
            ).decode("utf-8")
    try:
        document = SetupDocument.model_validate_json(expected, strict=True)
    except ValidationError as exc:
        raise SetupGuidanceError(_validation_message(exc)) from exc
    canonical = canonical_json_bytes(
        document.model_dump(mode="json", exclude_none=True)
    ).decode("utf-8")
    if canonical != expected:
        raise SetupGuidanceError(f"{SETUP_PATH} is not the complete canonical guidance")
    return document, dropped


def parse_setup_document(body: bytes) -> SetupDocument:
    """Parse only complete canonical v1 guidance; never repair or truncate it."""

    return _parse(body, drop_oversized_commands=False)[0]


def parse_installed_setup_document(body: bytes) -> tuple[SetupDocument, str | None]:
    """Parse installed guidance; over-budget commands degrade only that field.

    Returns the document without ``setup_commands`` and the exact reason when
    the commands exceed ``MAX_SETUP_COMMANDS_TOTAL_BYTES``; such commands are
    never shown or run. Every other defect still rejects the whole document.
    """

    return _parse(body, drop_oversized_commands=True)


def check_shared_setup_document(body: bytes) -> None:
    """Refuse a share whose setup document is invalid, naming the exact field."""

    try:
        parse_setup_document(body)
    except UnicodeDecodeError as exc:
        raise SetupGuidanceError(f"{SETUP_PATH} must be UTF-8 text") from exc
