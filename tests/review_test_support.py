"""Fresh owner and exact-package fixtures shared by install/update review tests."""
# pylint: disable=missing-function-docstring

import json
from types import SimpleNamespace

from hermes_wisdom_plugin.contract import (
    ContentFile, HermesRequirement, SystemSpecification, derive_content_hash, sha256_address,
)


def _owner(**changes):
    value = {
        "profile_name": "default",
        "profile_home": "/profiles/default",
        "account_id": "account-1",
        "organization_id": "org-1",
        "base_url": "https://gateway.example",
        "installation_id": "install-1",
    }
    value.update(changes)
    return value


def _spec(**changes):
    values: dict = {"hermes": HermesRequirement(minimum_version="0.1.0")}
    values.update(changes)
    return SystemSpecification(**values)


def _files(spec=None):
    specification = spec or _spec()
    manifest = json.dumps(
        {
            "schema_version": 1,
            "name": "managed-skill",
            "requirements": specification.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return [
        ("SKILL.md", "file", b"# Managed\n"),
        ("skill.manifest.json", "file", manifest),
    ]


def _content(files):
    records = [
        ContentFile(path=path, mode=mode, hash=sha256_address(body))
        for path, mode, body in files
    ]
    return SimpleNamespace(
        commit="sha256:" + "c" * 64,
        content_hash=derive_content_hash(records),
    )
