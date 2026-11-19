"""Feature-detected catalog registration; no accounts or network at import/load."""
from __future__ import annotations

from contextlib import contextmanager
from importlib import import_module

from .catalog_provider import CatalogProvider
from .install_host import InstallationHost


def register_catalog(ctx, runtime):
    """Only the new registrar plus explicit grant exposes Team in Skills."""
    registrar = getattr(ctx, "register_skill_catalog", None)
    granted = getattr(ctx, "has_capability", None)
    if not callable(registrar) or not callable(granted) or not granted("skills.catalog"):
        return None
    base = import_module("hermes_cli.skill_catalog").SkillCatalogProvider

    class Provider(CatalogProvider, base):
        """Identity remains literal class data for host descriptor validation."""

    @contextmanager
    def services():
        service = runtime._service()  # pylint: disable=protected-access
        try:
            yield service
        finally:
            runtime._close_service(service)  # pylint: disable=protected-access

    provider = Provider(services, runtime.plugin_id, InstallationHost(runtime.profile[1]),
                        mutations=getattr(ctx, "skill_catalog_mutations", None))
    handle = registrar(provider)
    if handle is not None:
        runtime.registrations.append(handle)
    return provider
