"""Setup commands are bounded by total bytes, not by a count."""

# pylint: disable=missing-function-docstring

from pathlib import Path

import pytest
from review_test_support import _content
from test_install_review import Client, _reviewer

import hermes_wisdom_plugin.package as package_module
from hermes_wisdom_plugin.contract import canonical_json_bytes
from hermes_wisdom_plugin.package import PackagePolicyError, PackageSecurityDecision
from hermes_wisdom_plugin.setup_document import (
    MAX_SETUP_COMMANDS_TOTAL_BYTES,
    PREFIX,
    SETUP_PATH,
    SUFFIX,
    parse_installed_setup_document,
    parse_setup_document,
)

FOUR = ["printf one", "printf two", "printf three", "printf four"]
# 21 commands of 256 characters: each within the line bound, 5376 bytes together.
OVER = ["printf " + "x" * 249] * 21


def _guidance(commands: list[str]) -> bytes:
    value = {
        "schema_version": 1, "execution_requires_user_approval": True,
        "requirements": [], "setup_instructions": ["Configure the client."],
        "setup_commands": commands, "credential_handoff": [], "compatibility_limits": [],
        "verification_step": "Check installed skill.",
        "removed_or_generalized": [], "related_skills": [],
    }
    return PREFIX.encode() + canonical_json_bytes(value) + SUFFIX.encode()


class _Host:
    def parse_skill_frontmatter(self, _source: Path) -> dict[str, object]:
        return {}

    def check_package_security(self, _overlay: Path) -> PackageSecurityDecision:
        return PackageSecurityDecision(allowed=True)


def _share(tmp_path: Path, body: bytes):
    skill = tmp_path / "my-skill"
    (skill / "refs").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Test.\n---\n\n# Test\n", encoding="utf-8"
    )
    (skill / SETUP_PATH).write_bytes(body)
    return package_module.prepare_package(
        skill, host=_Host(), overlay_root=tmp_path / "overlays",
        author_description="A valid description.", owner="owner",
        installation_id="installation-123456",
    )


def test_four_short_commands_parse_as_four_separate_commands():
    assert parse_setup_document(_guidance(FOUR)).setup_commands == FOUR


def test_commands_at_the_byte_budget_parse_and_one_byte_more_does_not():
    at_budget = ["x" * 256] * (MAX_SETUP_COMMANDS_TOTAL_BYTES // 256)
    assert parse_setup_document(_guidance(at_budget)).setup_commands == at_budget
    with pytest.raises(ValueError, match=r"setup_commands: 5121 bytes of commands"):
        parse_setup_document(_guidance([*at_budget, "y"]))


def test_share_refuses_over_budget_commands_naming_field_and_measured_size(tmp_path: Path):
    with pytest.raises(PackagePolicyError) as refused:
        _share(tmp_path, _guidance(OVER))
    assert str(refused.value) == (
        f"{SETUP_PATH} is invalid: setup_commands: 5376 bytes of commands; "
        f"the limit is {MAX_SETUP_COMMANDS_TOTAL_BYTES} bytes in total"
    )
    assert not (tmp_path / "overlays").exists()


def test_share_admits_four_commands_byte_for_byte(tmp_path: Path):
    body = _guidance(FOUR)
    package = _share(tmp_path, body)
    assert (package.overlay / SETUP_PATH).read_bytes() == body


def test_installed_parse_withholds_only_over_budget_commands():
    document, degraded = parse_installed_setup_document(_guidance(OVER))
    assert document.setup_commands is None
    assert document.setup_instructions == ["Configure the client."]
    assert degraded == (
        f"{SETUP_PATH} field setup_commands: 5376 bytes of commands; the limit is "
        f"{MAX_SETUP_COMMANDS_TOTAL_BYTES} bytes in total. These commands are not shown "
        "and will not run."
    )
    # Any other defect still rejects the whole document.
    with pytest.raises(ValueError):
        parse_installed_setup_document(
            _guidance(OVER).replace(b'"verification_step":"Check installed skill."',
                                    b'"verification_step":""')
        )


def _install_plan(tmp_path: Path, body: bytes) -> dict:
    client = Client()
    client.files.append((SETUP_PATH, "file", body))
    content = _content(client.files)
    client.version_value["content_hash"] = content.content_hash
    client.version_value["commit"] = content.commit
    return _reviewer(tmp_path, client=client).plan("skill-1@v1")


def test_install_review_shows_four_commands(tmp_path: Path):
    assert _install_plan(tmp_path, _guidance(FOUR))["setup_commands"] == FOUR


def test_install_review_withholds_over_budget_commands_with_the_exact_reason(tmp_path: Path):
    plan = _install_plan(tmp_path, _guidance(OVER))
    assert plan["setup_commands"] == []
    assert any(
        "field setup_commands: 5376 bytes of commands" in item
        for item in plan["compatibility"]["limitations"]
    )
