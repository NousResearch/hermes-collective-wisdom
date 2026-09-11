"""Tests for the extracted pure Wisdom wire object builders."""

from __future__ import annotations

import hashlib
import json

import pytest

from hermes_wisdom_plugin import wire


def test_full_64_hex_address() -> None:
    """Wire addresses retain the full SHA-256 digest."""

    address = wire.wire_address(b"")
    assert address == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert len(address.split(":", 1)[1]) == 64


def test_address_differs_from_local_truncated_namespace() -> None:
    """Wire and truncated local hashes cannot be confused."""

    data = b"hello world"
    full = wire.wire_address(data)
    truncated = "sha256:" + hashlib.sha256(data).hexdigest()[:16]
    assert full != truncated
    assert len(full.split(":")[1]) == 64
    assert len(truncated.split(":")[1]) == 16


def test_canonical_json_sorted_no_whitespace() -> None:
    """Canonical JSON has sorted keys and no incidental bytes."""

    output = wire.canonical_json_bytes({"b": 1, "a": 2})
    assert output == b'{"a":2,"b":1}'
    assert b" " not in output
    assert not output.endswith(b"\n")


def test_canonical_json_stable() -> None:
    """Equivalent mappings produce identical canonical bytes."""

    value = {"type": "tree", "entries": [{"name": "x", "hash": "sha256:aa"}]}
    assert wire.canonical_json_bytes(value) == wire.canonical_json_bytes(dict(value))


def test_build_tree_blob_and_exec(tmp_path) -> None:
    """Tree entries preserve blob kind and executable mode."""

    directory = tmp_path / "skill"
    directory.mkdir()
    (directory / "SKILL.md").write_text("hello", encoding="utf-8")
    script = directory / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    script.chmod(0o755)

    objects = wire.ObjectSet()
    tree_hash = wire.build_tree(
        directory, objects, max_object_bytes=wire.DEFAULT_MAX_OBJECT_BYTES
    )
    kind, data = objects.objects[tree_hash]
    assert kind == wire.KIND_TREE
    tree = json.loads(data)
    entries = {entry["name"]: entry for entry in tree["entries"]}
    assert entries["SKILL.md"]["mode"] == wire.MODE_FILE
    assert entries["run.sh"]["mode"] == wire.MODE_EXEC
    assert [entry["name"] for entry in tree["entries"]] == ["SKILL.md", "run.sh"]


def test_unicode_tree_matches_cross_writer_utf8_hash(tmp_path) -> None:
    """Mixed BMP and astral names sort by UTF-8 bytes across writers."""

    root = tmp_path / "unicode-tree"
    root.mkdir()
    for name in ("\U00010000.txt", "\ue000.txt"):
        (root / name).write_bytes(b"fixture\n")
    objects = wire.ObjectSet()
    address = wire.build_tree(root, objects, max_object_bytes=10000)
    assert address == "sha256:ac7acfcc7360485c38b318fccc37dc930073ccde42c523cffe51314d5940e912"
    tree = json.loads(objects.objects[address][1])
    assert [entry["name"] for entry in tree["entries"]] == [
        "\ue000.txt",
        "\U00010000.txt",
    ]


def test_build_tree_dedups_identical_blobs(tmp_path) -> None:
    """Identical file bytes occupy one object address."""

    directory = tmp_path / "skill"
    (directory / "a").mkdir(parents=True)
    (directory / "b").mkdir(parents=True)
    (directory / "a" / "f.txt").write_text("same", encoding="utf-8")
    (directory / "b" / "f.txt").write_text("same", encoding="utf-8")
    objects = wire.ObjectSet()
    wire.build_tree(directory, objects, max_object_bytes=wire.DEFAULT_MAX_OBJECT_BYTES)
    blob_hashes = [
        address
        for address, (kind, _) in objects.objects.items()
        if kind == wire.KIND_BLOB
    ]
    assert len(set(blob_hashes)) == 1


def test_build_tree_skips_symlink(tmp_path) -> None:
    """Symlinks never enter a wire tree."""

    directory = tmp_path / "skill"
    directory.mkdir()
    (directory / "real.txt").write_text("x", encoding="utf-8")
    try:
        (directory / "link.txt").symlink_to(directory / "real.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported here")
    objects = wire.ObjectSet()
    tree_hash = wire.build_tree(
        directory, objects, max_object_bytes=wire.DEFAULT_MAX_OBJECT_BYTES
    )
    tree = json.loads(objects.objects[tree_hash][1])
    names = [entry["name"] for entry in tree["entries"]]
    assert "link.txt" not in names
    assert "real.txt" in names


def test_build_tree_rejects_oversize_blob(tmp_path) -> None:
    """A file exceeding the negotiated object bound fails closed."""

    directory = tmp_path / "skill"
    directory.mkdir()
    (directory / "big").write_bytes(b"x" * 100)
    objects = wire.ObjectSet()
    with pytest.raises(ValueError, match="100 bytes > max_object_bytes 10"):
        wire.build_tree(directory, objects, max_object_bytes=10)


def test_build_commit_shape_and_address() -> None:
    """Commit bytes have the deployed shape and matching address."""

    objects = wire.ObjectSet()
    commit_hash = wire.build_commit(
        "sha256:tree",
        ["sha256:p"],
        owner="o",
        device="dev",
        message="m",
        objects=objects,
        ts="2026-07-18T00:00:00Z",
    )
    kind, data = objects.objects[commit_hash]
    commit = json.loads(data)
    assert kind == wire.KIND_COMMIT
    assert wire.wire_address(data) == commit_hash
    assert commit["type"] == "commit"
    assert commit["tree"] == "sha256:tree"
    assert commit["parents"] == ["sha256:p"]
    assert commit["author"] == {"owner": "o", "device": "dev"}
    assert commit["artifact_type"] == "skill"
