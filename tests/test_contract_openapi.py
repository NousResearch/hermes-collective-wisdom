"""Bounded verifier for the immutable Gateway OpenAPI contract.

The OpenAPI snapshot is intentionally not checked into or packaged by this
repository. The check against the real artifact is opt-in: it runs only with a
checkout of the Collective Wisdom service, passed as WISDOM_GATEWAY_DIR to
pytest or as --gateway-dir when running this module directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_wisdom_plugin.contract import CONTRACT_PIN


OPENAPI_PATH = "openapi.json"
MAX_OPENAPI_BYTES = 2 * 1024 * 1024


def _openapi_from_checkout(gateway_dir: Path) -> bytes:
    return subprocess.run(
        [
            "git",
            "-C",
            str(gateway_dir),
            "show",
            f"{CONTRACT_PIN.gateway_commit}:{OPENAPI_PATH}",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    ).stdout


def verify_openapi(payload: bytes) -> dict[str, object]:
    """Verify the pinned digest before checking required OpenAPI shapes."""

    if len(payload) > MAX_OPENAPI_BYTES:
        raise ValueError(f"OpenAPI exceeds {MAX_OPENAPI_BYTES} bytes")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != CONTRACT_PIN.openapi_sha256:
        raise ValueError(
            "Gateway OpenAPI digest mismatch: "
            f"expected {CONTRACT_PIN.openapi_sha256}, got {digest}"
        )

    contract = json.loads(payload.decode("utf-8"))
    draft = contract["components"]["schemas"]["WisdomDraftRecord"]
    assert "changes_requested" in draft["properties"]["state"]["enum"]
    assert "/v1/sync/org/proposals/{n}/return" in contract["paths"]
    return {
        "gateway_commit": CONTRACT_PIN.gateway_commit,
        "openapi_sha256": digest,
        "openapi_bytes": len(payload),
    }


def test_openapi_verifier_rejects_unpinned_bytes() -> None:
    """An artifact from any unpinned revision fails before parsing."""

    with pytest.raises(ValueError, match="digest mismatch"):
        verify_openapi(b'{"openapi":"3.1.0"}')


def test_pinned_openapi_when_gateway_checkout_is_configured() -> None:
    """Optionally verify the real immutable artifact during pytest."""

    checkout = os.environ.get("WISDOM_GATEWAY_DIR")
    if checkout is None:
        pytest.skip("set WISDOM_GATEWAY_DIR for the immutable OpenAPI source check")
    result = verify_openapi(_openapi_from_checkout(Path(checkout)))
    assert result["gateway_commit"] == CONTRACT_PIN.gateway_commit


def main() -> int:
    """Run the immutable OpenAPI check against a local service checkout."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-dir", type=Path, required=True)
    args = parser.parse_args()
    payload = _openapi_from_checkout(args.gateway_dir)
    print(json.dumps(verify_openapi(payload), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
