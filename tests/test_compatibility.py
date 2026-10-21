"""Deterministic compatibility kernel tests."""

from hermes_wisdom_plugin.compatibility import (
    LocalCapabilities,
    SkillDeclarations,
    evaluate,
    skill_declarations,
)
from hermes_wisdom_plugin.contract import (
    FilesystemRequirement,
    HermesRequirement,
    ModelRequirement,
    NetworkRequirement,
    PluginRequirement,
    RuntimeRequirement,
    SystemSpecification,
    ToolRequirement,
)


def spec(**updates):
    """Build a minimal package requirement specification."""

    values = {"hermes": HermesRequirement(minimum_version="0.1.0")}
    values.update(updates)
    return SystemSpecification(**values)


def local(**updates):
    """Build a minimal host capability snapshot."""

    values = {
        "hermes_version": "1.0.0",
        "os": "darwin",
        "architecture": "arm64",
        "runtime": {"shell": True, "browser": False, "code": True, "sandbox": True},
    }
    values.update(updates)
    return LocalCapabilities(**values)


def test_all_four_compatibility_outcomes_are_deterministic():
    """The evaluator deterministically reaches every outcome category."""

    assert evaluate(spec(), local()).outcome == "compatible"
    assert (
        evaluate(
            spec(
                tools=[
                    ToolRequirement(name="git", minimum_version="2", auto_install=False)
                ]
            ),
            local(),
        ).outcome
        == "compatible_after_setup"
    )
    assert (
        evaluate(spec(known_limitations=["manual replay only"]), local()).outcome
        == "partial"
    )
    assert (
        evaluate(spec(runtime=RuntimeRequirement(browser=True)), local()).outcome
        == "blocked_pending_action"
    )


def test_skill_evaluator_is_not_a_compatibility_input():
    """Advisory skill evaluation is not an authority input."""

    result = evaluate(spec(), local())
    assert not hasattr(result, "skill_evaluator")


def test_host_supplied_capabilities_are_scoped_and_do_not_retain_secrets():
    """Evaluation consumes named host facts without retaining secret values."""

    requirement = spec(
        model=ModelRequirement(
            capabilities=["tools", "vision"], minimum_context_window=32000
        ),
        tools=[ToolRequirement(name="terminal", auto_install=False)],
        plugins=[PluginRequirement(id="example/plugin")],
        credentials=["EXAMPLE_TOKEN"],
        connections=["team-mcp", "telegram"],
        filesystem=FilesystemRequirement(
            read=["/allowed/readable"], write=["/allowed/writable"]
        ),
        network=NetworkRequirement(destinations=["api.example.test"]),
        runtime=RuntimeRequirement(shell=True, browser=True, code=True),
    )
    detected = local(
        model_capabilities=frozenset({"tools", "vision"}),
        context_window=128000,
        enabled_tools={"terminal": None},
        plugins={"example/plugin": None},
        credentials=frozenset({"EXAMPLE_TOKEN"}),
        connections=frozenset({"team-mcp", "telegram"}),
        filesystem_readable=frozenset({"/allowed/readable"}),
        filesystem_writable=frozenset({"/allowed/writable"}),
        network_access=False,
        runtime={"shell": True, "browser": True, "code": True, "sandbox": True},
    )

    assert "super-secret-value" not in repr(detected)
    result = evaluate(requirement, detected)
    assert result.outcome == "compatible_after_setup"
    assert result.setup_actions == ("allow network access to api.example.test",)


def test_present_tool_and_plugin_with_unknown_version_satisfy_unversioned_requirement():
    """Presence is sufficient only when no minimum version was declared."""

    result = evaluate(
        spec(
            tools=[ToolRequirement(name="git", auto_install=False)],
            plugins=[PluginRequirement(id="example/plugin")],
        ),
        local(enabled_tools={"git": None}, plugins={"example/plugin": None}),
    )
    assert result.outcome == "compatible"


def test_platform_and_architecture_aliases_match():
    """Equivalent platform and architecture names normalize consistently."""

    result = evaluate(
        spec(platforms=["macOS"], architectures=["aarch64"]),
        local(os="darwin", architecture="arm64"),
    )
    assert result.outcome == "compatible"
    assert not result.notes


def test_recorded_architecture_mismatch_is_a_note_never_a_block():
    """Hermes has no architecture gating; a recorded CPU list is informational."""

    result = evaluate(spec(architectures=["arm64"]), local(os="linux", architecture="x86_64"))
    assert result.outcome == "compatible"
    assert not result.blocked
    assert result.notes == ("Shared with architectures: arm64; this machine is x86_64",)


def test_undeclared_recorded_platform_and_hermes_floor_are_notes():
    """Legacy pins the skill did not declare inform; they do not block install."""

    result = evaluate(
        spec(hermes=HermesRequirement(minimum_version="9.0.0"), platforms=["macOS"]),
        local(os="linux", architecture="x86_64"),
    )
    assert result.outcome == "compatible"
    assert result.notes == (
        "Shared with Hermes >= 9.0.0; this machine runs Hermes 1.0.0",
        "Shared with platforms: macOS; this machine is Linux",
    )


def test_platform_declared_in_skill_frontmatter_blocks_with_exact_reason():
    """Hermes semantics: a SKILL.md platforms list is enforced on this OS."""

    declared = skill_declarations({"name": "x", "platforms": ["macos"]})
    result = evaluate(spec(platforms=["macOS"]), local(os="linux"), declared)
    assert result.outcome == "blocked_pending_action"
    assert result.blocked == ("requires macOS; this machine is Linux",)
    assert evaluate(spec(), local(os="darwin"), declared).outcome == "compatible"


def test_hermes_floor_declared_in_skill_frontmatter_blocks():
    """Only a SKILL.md-declared Hermes floor blocks an older host."""

    declared = skill_declarations({"metadata": {"hermes": {"minimum_version": "2.0"}}})
    result = evaluate(spec(), local(hermes_version="1.0.0"), declared)
    assert result.blocked == ("requires Hermes 2.0 or newer; this machine runs Hermes 1.0.0",)
    assert evaluate(spec(), local(hermes_version="2.1.0"), declared).outcome == "compatible"


def test_skill_declarations_ignore_malformed_frontmatter():
    """Non-dict or malformed values declare nothing (Hermes: absent = all platforms)."""

    assert skill_declarations(None) == SkillDeclarations()
    assert skill_declarations({"platforms": [1, ""], "metadata": "x"}) == SkillDeclarations()
    assert skill_declarations({"platforms": "linux"}).platforms == ("linux",)
