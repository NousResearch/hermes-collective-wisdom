"""Feature-detected catalog registration; no accounts or network at import/load."""
from __future__ import annotations

from contextlib import contextmanager
from importlib import import_module

from .catalog_provider import CatalogProvider
from .install_host import InstallationHost


def _cached_label():
    """Snapshot only an already-known, identity-checked Team name; never fetch it."""
    fallback = CatalogProvider.label
    try:
        account = import_module("hermes_cli.nous_account").cached_nous_portal_account_info()
        if account is None or account.logged_in is not True:
            return fallback
        name = account.org_name
        if (type(name) is not str or not name.strip()  # pylint: disable=unidiomatic-typecheck
                or name.strip() == account.org_id
                or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in name)):
            return fallback
        name.encode("utf-8")
        return f"{fallback} · {name.strip()}"[:80]
    except Exception:  # pylint: disable=broad-exception-caught
        # Older hosts and unavailable account chrome retain the generic label.
        return fallback


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
    provider.label = _cached_label()
    handle = registrar(provider)
    if handle is not None:
        runtime.registrations.append(handle)
    return provider
