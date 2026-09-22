"""Authenticated Desktop REST routes for Collective Wisdom.

The host mounts ``router`` under ``/api/plugins/collective-wisdom`` after
its normal dashboard authentication gate.  Every request constructs the
existing owner-bound InvocationService from the request process' current
profile scope; no identity supplied by the renderer is accepted.
"""

from __future__ import annotations

from importlib import import_module, util
from pathlib import Path
import sys

from fastapi import HTTPException
from fastapi.routing import APIRoute
from hermes_cli.config import load_config
from hermes_cli.plugins import PluginContext
from hermes_cli.plugins_manifest import PluginManifest
from hermes_cli.web_server_profiles import _config_profile_scope


def _load_backend():
    """Load adjacent Git-package code without requiring pip or changing sys.path."""
    package = Path(__file__).resolve().parents[1] / "hermes_wisdom_plugin"
    name = f"{__name__}._backend"
    spec = util.spec_from_file_location(
        name, package / "__init__.py", submodule_search_locations=[str(package)]
    )
    if spec is None or spec.loader is None:
        raise ImportError("Collective Wisdom backend package is unavailable")
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return import_module(f"{name}.desktop_api"), import_module(f"{name}.runtime")


_api, _runtime = _load_backend()
InvocationService = _runtime.InvocationService
current_profile = _runtime.current_profile
build_router = _api.build_router

_PLUGIN_ID = "collective-wisdom"
_context = PluginContext(
    PluginManifest(name=_PLUGIN_ID, kind="standalone", key=_PLUGIN_ID),
    manager=None,  # get/set_config and state do not consult the manager
)


def _notification_config() -> dict:
    return {
        "notifications": {
            key: _context.get_config(f"notifications.{key}", default)
            for key, default in {
                "delivery_mode": "agent",
                "new_skills": "immediate",
                "updates": "immediate",
                "team_published": "immediate",
            }.items()
        }
    }


def _service() -> InvocationService:
    """Resolve profile home and owner afresh for each authenticated request."""
    return InvocationService(
        _context.state.data_dir,
        current_profile(),
        config=_notification_config(),
        get_config=_context.get_config,
        set_config=_context.set_config,
        state=_context.state,
    )


def _enabled() -> bool:
    """Read the selected profile's current Agent-plugin gate, including after I/O."""
    plugins = load_config().get("plugins") or {}
    return (
        _PLUGIN_ID in (plugins.get("enabled") or [])
        and _PLUGIN_ID not in (plugins.get("disabled") or [])
    )


class _ProfileScopedRoute(APIRoute):
    """Scope legacy hosts with the same validated profile context as core REST.

    Desktop's authenticated REST bridge supplies ``?profile=``. The host
    resolver validates that profile and installs a request-local home override;
    owner/account identity still comes only from that scoped host state. On a
    host that already ships PR #108678 this is an idempotent nested scope.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def scoped(request):
            with _config_profile_scope(request.query_params.get("profile")):
                if not _enabled():
                    raise HTTPException(status_code=404, detail="Plugin not found")
                return await original(request)

        return scoped


router = build_router(
    _service, workflow=_api.DesktopWorkflowApi(enabled=_enabled), route_class=_ProfileScopedRoute
)
