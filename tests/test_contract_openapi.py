"""Bounded verifier for the immutable Gateway OpenAPI contract.

The OpenAPI snapshot is intentionally not checked into or packaged by this
repository. Run this module directly with a pinned Gateway checkout, or use an
authenticated GitHub API request in CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from hermes_wisdom_plugin.contract import CONTRACT_PIN


GATEWAY_REPOSITORY = "NousResearch/gateway-gateway"
OPENAPI_PATH = "openapi.json"
MAX_OPENAPI_BYTES = 2 * 1024 * 1024


def _read_bounded(response) -> bytes:
    payload = response.read(MAX_OPENAPI_BYTES + 1)
    if len(payload) > MAX_OPENAPI_BYTES:
        raise ValueError(f"OpenAPI exceeds {MAX_OPENAPI_BYTES} bytes")
    return payload


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


def _openapi_from_github(token: str) -> bytes:
    revision = urllib.parse.quote(CONTRACT_PIN.gateway_commit, safe="")
    url = (
        f"https://api.github.com/repos/{GATEWAY_REPOSITORY}/contents/"
        f"{OPENAPI_PATH}?ref={revision}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github.raw+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "hermes-collective-wisdom-contract-verifier",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return _read_bounded(response)


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
    """Run the immutable OpenAPI check from a development or CI checkout."""

    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--gateway-dir", type=Path)
    source.add_argument(
        "--github-token-env",
        metavar="NAME",
        help="environment variable holding a token with private repository read access",
    )
    args = parser.parse_args()

    if args.gateway_dir is not None:
        payload = _openapi_from_checkout(args.gateway_dir)
    else:
        token = os.environ.get(args.github_token_env, "")
        if not token:
            parser.error(f"{args.github_token_env} is unset or empty")
        payload = _openapi_from_github(token)

    print(json.dumps(verify_openapi(payload), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
