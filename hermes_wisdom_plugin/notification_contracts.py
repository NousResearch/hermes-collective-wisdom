"""Shared bounded contracts for candidate notification presentation and commands."""

from __future__ import annotations

import re

CANDIDATE_SLASH_TARGET_MAX_LENGTH = 256
MAX_QUALIFICATION_COUNT = 365

_CANDIDATE_SLASH_TARGET = re.compile(
    rf"[A-Za-z0-9_-]{{1,{CANDIDATE_SLASH_TARGET_MAX_LENGTH}}}\Z"
)


def is_candidate_slash_target(value: object) -> bool:
    """Return whether a value is safe and accepted as a candidate slash target."""

    return isinstance(value, str) and _CANDIDATE_SLASH_TARGET.fullmatch(value) is not None
