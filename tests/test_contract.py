"""Pinned contract models, canonical hashes, and runtime artifact checks."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import hermes_wisdom_plugin.contract as contract_module
from hermes_wisdom_plugin.contract import (
    CONTRACT_PIN,
    ContentFile,
    PackageManifest,
    SystemSpecification,
    author_description_hash,
    canonical_content_manifest,
    canonical_json_bytes,
    derive_content_hash,
    parse_manifest_bytes,
    sanitize_author_description,
    sha256_address,
)


CONTRACTS = Path(contract_module.__file__).with_name("contracts")
VECTORS = CONTRACTS / "canonical-hash-vectors.v1.json"
MANIFEST_SCHEMA = CONTRACTS / "skill-manifest.schema.v1.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _vectors() -> dict[str, object]:
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def _content_files(vector: dict[str, object]) -> list[ContentFile]:
    return [
        ContentFile(path=item["path"], mode=item["mode"], hash=item["hash"])
        for item in vector["files"]
    ]


def test_runtime_contract_artifacts_match_the_pinned_digests() -> None:
    """Runtime schema and vectors stay byte-identical to the contract pin."""

    assert _digest(MANIFEST_SCHEMA) == CONTRACT_PIN.manifest_schema_sha256
    assert _digest(VECTORS) == CONTRACT_PIN.canonical_vectors_sha256


def test_gateway_canonical_vectors_match_exactly() -> None:
    """Gateway vectors reproduce exact UTF-8 content and addresses."""

    vectors = _vectors()
    files = []
    for item in vectors["files"]:
        body = base64.b64decode(item["content_base64"], validate=True)
        assert body.decode("utf-8") == item["content_utf8"]
        assert sha256_address(body) == item["hash"]
        files.append(
            ContentFile(path=item["path"], mode=item["mode"], hash=item["hash"])
        )

    assert canonical_content_manifest(files).decode("utf-8") == (
        vectors["canonical_content_manifest_utf8"]
    )
    assert derive_content_hash(files) == vectors["content_hash"]
    manifest = next(item for item in vectors["files"] if item["path"] == "skill.manifest.json")
    assert manifest["hash"] == vectors["package_manifest_hash"]

    canonical = sanitize_author_description(vectors["author_description_input"])
    assert canonical == vectors["canonical_author_description"]
    assert author_description_hash(canonical) == vectors["author_description_hash"]


def test_content_hash_commits_to_mode_and_path() -> None:
    """A content hash binds file location and executable mode."""

    blob = sha256_address(b"same")
    plain = derive_content_hash([ContentFile(path="SKILL.md", mode="file", hash=blob)])
    executable = derive_content_hash(
        [ContentFile(path="SKILL.md", mode="exec", hash=blob)]
    )
    moved = derive_content_hash(
        [ContentFile(path="refs/SKILL.md", mode="file", hash=blob)]
    )
    assert len({plain, executable, moved}) == 3


@pytest.mark.parametrize("vector", _vectors()["content_hash_cases"])
def test_content_hash_matches_cross_language_unicode_vectors(
    vector: dict[str, object],
) -> None:
    """Python sorting matches the Gateway's Unicode ordering vectors."""

    for item in vector["files"]:
        body = base64.b64decode(item["content_base64"], validate=True)
        assert body.decode("utf-8") == item["content_utf8"]
        assert sha256_address(body) == item["hash"]

    files = _content_files(vector)
    expected_manifest = vector["canonical_content_manifest_utf8"].encode("utf-8")
    assert canonical_content_manifest(files) == expected_manifest
    assert canonical_content_manifest(list(reversed(files))) == expected_manifest
    assert derive_content_hash(files) == vector["content_hash"]
    assert derive_content_hash(list(reversed(files))) == vector["content_hash"]


def test_canonical_json_is_sorted_compact_utf8() -> None:
    """Canonical JSON is sorted, compact, and emits Unicode as UTF-8."""

    assert canonical_json_bytes({"z": "caf\u00e9", "a": 1}) == (
        b'{"a":1,"z":"caf\xc3\xa9"}'
    )


def _valid_manifest_bytes() -> bytes:
    manifest = PackageManifest(
        name="strict-manifest",
        requirements=SystemSpecification.model_validate(
            {"hermes": {"minimum_version": "0.1.0"}}
        ),
    )
    return canonical_json_bytes(manifest.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda value: value.replace(
                b'{"name":', b'{"schema_version":1,"name":', 1
            ),
            "duplicate JSON key",
        ),
        (
            lambda value: value.replace(
                b'"minimum_context_window":null',
                b'"minimum_context_window":NaN',
            ),
            "non-finite JSON number",
        ),
        (lambda value: value[:-1], "Expecting"),
        (lambda value: value.replace(b"strict-manifest", b"strict-\xff"), "utf-8"),
    ],
)
def test_manifest_parser_rejects_malformed_bytes(mutate, error: str) -> None:
    """Malformed, ambiguous, and non-UTF-8 manifests fail closed."""

    with pytest.raises((UnicodeDecodeError, ValueError), match=error):
        parse_manifest_bytes(mutate(_valid_manifest_bytes()))


def test_manifest_parser_rejects_unknown_fields_and_invalid_hashes() -> None:
    """Unknown wire fields and malformed content addresses are rejected."""

    value = json.loads(_valid_manifest_bytes())
    value["unexpected"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        parse_manifest_bytes(canonical_json_bytes(value))

    with pytest.raises(ValidationError, match="invalid sha256 address"):
        ContentFile(path="SKILL.md", mode="file", hash="sha256:not-a-digest")


def test_manifest_parser_bounds_integer_values_to_json_safe_range() -> None:
    """Wire integers cannot exceed the cross-language safe range."""

    value = json.loads(_valid_manifest_bytes())
    value["requirements"]["model"]["minimum_context_window"] = (
        9_007_199_254_740_992
    )
    with pytest.raises(ValidationError):
        parse_manifest_bytes(canonical_json_bytes(value))
