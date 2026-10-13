"""Collective Wisdom Gateway base URL, owned by the plugin.

Hermes core resolved this in ``tools.skills_sync_client.resolve_sync_base_url``
until commit 64ad33e32d removed Skill Sync from core (baseline 9cf7960f). The
plugin keeps the same precedence, values and default so every existing setup
reaches the same Gateway: ``HERMES_SYNC_BASE_URL``, then ``sync.base_url`` in
the profile's config.yaml, then the production Gateway. The result never has a
trailing slash; the client appends ``/v1/sync/``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any, cast

logger = logging.getLogger(__name__)

ENV_BASE_URL = "HERMES_SYNC_BASE_URL"
CONFIG_SECTION = "sync"
CONFIG_KEY = "base_url"
DEFAULT_GATEWAY_BASE_URL = "https://gateway-gateway.nousresearch.com"

ConfigLoader = Callable[[], Mapping[str, Any] | None]


def host_config_loader(config_module: Any) -> ConfigLoader | None:
    """Return the host's side-effect-free config reader, if it has one."""

    for name in ("load_config_readonly", "load_config"):
        loader = getattr(config_module, name, None)
        if callable(loader):
            return cast(ConfigLoader, loader)
    return None


def _configured_base_url(load_config: ConfigLoader | None) -> str | None:
    if load_config is None:
        return None
    try:
        config = load_config() or {}
        section = config.get(CONFIG_SECTION) if isinstance(config, Mapping) else None
        value = section.get(CONFIG_KEY) if isinstance(section, Mapping) else None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.debug(
            "collective-wisdom: config %s.%s read failed: %s", CONFIG_SECTION, CONFIG_KEY, exc
        )
        return None
    return value if isinstance(value, str) else None


def resolve_gateway_base_url(
    *, load_config: ConfigLoader | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """Return the Gateway base URL by env, then config, then the production default."""

    env = (os.environ if environ is None else environ).get(ENV_BASE_URL)
    if env and env.strip():
        return env.strip().rstrip("/")
    configured = _configured_base_url(load_config)
    if configured and configured.strip():
        return configured.strip().rstrip("/")
    return DEFAULT_GATEWAY_BASE_URL
