"""Strict consumed-field checks shared by generic catalog endpoints."""
# JSON data must not coerce bools, integers or strings.
# pylint: disable=unidiomatic-typecheck,too-many-boolean-expressions
import re

from .errors import WisdomValidationError

_OPAQUE = re.compile(r"[A-Za-z0-9_-]{16,256}", re.ASCII)
_FINDING = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,191}", re.ASCII)
_ADMISSION = {"registration_id", "generation", "review_id", "reservation_id",
              "expected_path_revision", "expected_installation_revision"}


def validate_effect(request, fields):
    """Reject malformed authority before opening any scoped service."""
    if "action" in fields and request.get("action") not in {"install", "update", "uninstall"}:
        raise WisdomValidationError("invalid action")
    version = request.get("version")
    if "version" in fields and version is not None and (
        type(version) is not str or not 1 <= len(version) <= 16 or not version.isascii()
        or not version.isdecimal() or version.startswith("0")
    ):
        raise WisdomValidationError("invalid version")
    if "acknowledgements" in fields:
        values = request.get("acknowledgements")
        if (type(values) is not list or len(values) > 1000
                or any(type(value) is not str or not _FINDING.fullmatch(value) for value in values)
                or len(set(values)) != len(values)):
            raise WisdomValidationError("invalid acknowledgements")
    if "admission" not in fields:
        return
    validate_admission(request)

def validate_admission(request):
    """Keep catalog-specific admission separate from the native sequence."""
    if "admission" not in request:
        raise WisdomValidationError("admission required")
    admission = request["admission"]
    if admission is None:  # only completed replay can consume this shape
        return
    if type(admission) is not dict or set(admission) != _ADMISSION:
        raise WisdomValidationError("invalid admission")
    for key in ("registration_id", "generation", "review_id", "reservation_id"):
        if type(admission[key]) is not str or not _OPAQUE.fullmatch(admission[key]):
            raise WisdomValidationError("invalid admission identity")
    revision = admission["expected_path_revision"]
    if type(revision) is not int or not 0 <= revision <= 9007199254740991:
        raise WisdomValidationError("invalid path revision")
    revision = admission["expected_installation_revision"]
    if revision is not None and (type(revision) is not str or not 1 <= len(revision) <= 256
                                 or any(ord(char) < 32 for char in revision)):
        raise WisdomValidationError("invalid installation revision")
