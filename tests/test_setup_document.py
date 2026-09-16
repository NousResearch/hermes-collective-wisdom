"""Strict parsing tests for installed, hash-covered setup guidance."""

# pylint: disable=import-outside-toplevel,missing-function-docstring

import pytest

from hermes_wisdom_plugin.contract import canonical_json_bytes


def _document(value: dict) -> bytes:
    from hermes_wisdom_plugin.setup_document import PREFIX, SUFFIX

    return PREFIX.encode() + canonical_json_bytes(value) + SUFFIX.encode()


def _value() -> dict:
    return {
        "schema_version": 1,
        "execution_requires_user_approval": True,
        "requirements": [
            {
                "kind": "env_var",
                "name": "EXAMPLE_TOKEN",
                "purpose": "Authenticate the example service",
                "handoff": "Obtain it from the service administrator.",
            }
        ],
        "setup_instructions": ["Configure the local example client."],
        "credential_handoff": ["Keep the value in the private profile environment."],
        "compatibility_limits": [],
        "verification_step": "Check that the example client can authenticate.",
        "removed_or_generalized": [],
        "related_skills": [],
    }


def test_parse_setup_document_accepts_only_complete_canonical_wire_bytes():
    from hermes_wisdom_plugin.setup_document import parse_setup_document

    raw = _document(_value())
    parsed = parse_setup_document(raw)

    assert parsed.requirements[0].name == "EXAMPLE_TOKEN"
    assert parsed.verification_step.startswith("Check")
    for changed in (
        raw + b"extra",
        raw.replace(b'"schema_version":1', b'"schema_version":2'),
        raw.replace(
            b'"execution_requires_user_approval":true',
            b'"execution_requires_user_approval":false',
        ),
        raw.replace(b'"requirements":', b'"unknown":[],"requirements":'),
        raw.replace(b"{", b"{ ", 1),
    ):
        with pytest.raises(ValueError):
            parse_setup_document(changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("verification_step", "printf sk-" + "a" * 32),
        ("setup_instructions", ["first\nsecond"]),
        ("verification_step", "first\u202esecond"),
    ],
)
def test_setup_document_rejects_secret_shapes_and_command_controls(field, value):
    from hermes_wisdom_plugin.setup_document import parse_setup_document

    document = _value()
    document[field] = value
    with pytest.raises(ValueError):
        parse_setup_document(_document(document))
