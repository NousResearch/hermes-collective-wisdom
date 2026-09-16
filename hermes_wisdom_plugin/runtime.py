"""Owned terminal command lifetime on the existing Hermes plugin APIs."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from importlib import import_module
from pathlib import Path

from .account import HermesAccountAdapter
from .client import WisdomClient
from .errors import WisdomAuthError, WisdomError, WisdomValidationError

SLASH_BUDGET_SECONDS = 25.0
_UNAVAILABLE = "Collective Wisdom is unavailable. Complete setup in your terminal."
_BOOTSTRAP = """
import importlib.util, json, sys
from pathlib import Path
request = json.loads(sys.stdin.read())
sys.path[:] = request['sys_path']
package = Path(request['package'])
spec = importlib.util.spec_from_file_location(
    '_wisdom_command_worker', package / '__init__.py',
    submodule_search_locations=[str(package)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
from _wisdom_command_worker.runtime import worker_main
print(json.dumps(worker_main(request)))
"""


def current_profile() -> tuple[str, Path]:
    """Read the active profile using existing host APIs, without reading credentials."""
    name = import_module("hermes_cli.profiles").get_active_profile_name()
    home = import_module("hermes_constants").get_hermes_home()
    return name, Path(home).resolve()


class InvocationService:
    """Lazily compose the workflow for one captured profile and account."""

    def __init__(self, root: Path, profile: tuple[str, Path]):
        self.root = root
        self.profile = profile
        self.account = None
        self.client = None
        self._onboarding = None
        self._closed = False

    def check_owner(self):
        """Fence profile/account changes before and after every operation."""
        if self._closed:
            raise WisdomError("Collective Wisdom plugin is unloaded")
        if current_profile() != self.profile:
            raise WisdomAuthError("Hermes profile changed; reload the plugin")
        if self.account is None:
            self.account = HermesAccountAdapter.current()
        return self.account.credentials()

    def binding(self) -> dict[str, str]:
        """Return a non-secret binding for the short-lived slash worker."""
        self.check_owner()
        return {
            "profile_name": self.account.profile_name,
            "profile_home": self.account.profile_home,
            "account_id": self.account.account_id,
            "organization_id": self.account.organization_id,
            "base_url": self.account.base_url,
        }

    def _open(self):
        self.check_owner()
        if self.client is None:
            self.client = WisdomClient(
                self.check_owner, self.account.organization_id, timeout=10,
                install_credential_provider=self._install_credentials,
            )
        if self._onboarding is None:
            from .onboarding import Onboarding  # pylint: disable=import-outside-toplevel
            from .store import WisdomStore  # pylint: disable=import-outside-toplevel

            self._onboarding = Onboarding(self.account, WisdomStore(self.root), self.client)
        return self._onboarding

    def _install_credentials(self):
        self.check_owner()
        return self.account.credentials(scope="wisdom:install")

    def setup(self, *, accept_disclosure: bool):
        """Create state only after explicit terminal acceptance and scope checks."""
        if not accept_disclosure:
            raise WisdomValidationError("Collective Wisdom requires disclosure acceptance")
        self._install_credentials()
        result = self._open().setup(accept_disclosure=True)
        self.check_owner()
        return result

    def status(self, *, include_operations=False):
        """Inspect local setup without enrolling or contacting the Gateway."""
        self.check_owner()
        if not (self.root / "wisdom.db").is_file():
            return {"configured": False, "state": "not_configured", "reason": "not_configured"}
        result = self._open().status()
        if include_operations and result.get("configured"):
            owner = {**self.binding(), "installation_id": result.get("installation_id")}
            result["pending_operations"] = [
                {"operation_id": operation["id"], "kind": operation["kind"],
                 "phase": operation["phase"], "skill_id": operation["entity_id"]}
                for operation in self._onboarding.store.pending_operations()
                if json.loads(operation["payload_json"]).get("owner") == owner
            ]
        self.check_owner()
        return result

    def _installation_owner(self):
        self.require_setup()
        self._install_credentials()
        return {**self.binding(), "installation_id":
                self._onboarding.store.existing_installation_identity()}

    def _installation_services(self):
        from .install_execution import InstallExecutor  # pylint: disable=import-outside-toplevel
        from .install_host import InstallationHost  # pylint: disable=import-outside-toplevel
        from .install_receipts import InstallReceipts  # pylint: disable=import-outside-toplevel
        from .install_review import InstallReviewer  # pylint: disable=import-outside-toplevel

        self._installation_owner()
        store = self._onboarding.store
        host = InstallationHost(self.profile[1])
        receipts = InstallReceipts(store, self._installation_owner)
        reviewer = InstallReviewer(self.client, store, receipts, host, self._installation_owner)
        executor = InstallExecutor(self.client, store, receipts, reviewer,
                                   host, self._installation_owner)
        return reviewer, executor

    def install_plan(self, reference, *, update_mode=None):
        """Prepare an owner-bound terminal review without installing any files."""
        return self._installation_services()[0].plan(reference, update_mode=update_mode)

    def install_apply(self, receipt, *, accept_partial=False):
        """Apply or retry only the explicitly selected terminal receipt."""
        return self._installation_services()[1].apply(receipt, accept_partial=accept_partial)

    def list_installations(self):
        """Inspect private installation state only through the terminal entry."""
        return self._installation_services()[1].installed()

    def uninstall(self, skill_id):
        """Perform the removal already confirmed by the terminal controller."""
        return self._installation_services()[1].uninstall(skill_id)

    def require_setup(self):
        """Deny discovery before creating state or issuing a request."""
        self.check_owner()
        if not (self.root / "wisdom.db").is_file():
            raise WisdomValidationError("Run hermes wisdom setup in your terminal first")
        self._open().require_setup()

    def _discovery(self):
        from .discovery import Discovery  # pylint: disable=import-outside-toplevel

        self.require_setup()
        return Discovery(
            self.client, self.check_owner,
            portal_base_url=self.account.portal_base_url,
            organization_id=self.account.organization_id,
        )

    def list_skills(self):
        """Read one raw discovery page."""
        return self._discovery().list_skills()

    def search_skills(self, query=None):
        """Read published skills matching the supplied query."""
        return self._discovery().search_skills(query)

    def show(self, skill_id):
        """Inspect published metadata only."""
        return self._discovery().show(skill_id)

    def resolve_skill(self, reference):
        """Resolve an ID or unique slug for readable slash navigation."""
        return self._discovery().resolve_skill(reference)

    def versions(self, skill_id):
        """Read published version history."""
        return self._discovery().versions(skill_id)

    def close(self):
        """Close this invocation's client."""
        self._closed = True
        if self.client is not None:
            self.client.close()


class CommandRuntime:
    """Own registrations and cancellable, read-only slash requests."""

    def __init__(self, root: Path, profile: tuple[str, Path]):
        self.root = Path(root).resolve()
        self.profile = profile
        self._lock = threading.RLock()
        self._closed = False
        self._children = set()
        self._services = set()
        self.registrations = []

    def _service(self):
        with self._lock:
            if self._closed:
                raise WisdomError("Collective Wisdom plugin is unloaded")
            service = InvocationService(self.root, self.profile)
            self._services.add(service)
            return service

    def cli(self, args) -> int:
        """Synchronous native CLI entry, including interactive terminal setup."""
        from .commands import run_cli  # pylint: disable=import-outside-toplevel

        service = self._service()
        try:
            return run_cli(args, service)
        finally:
            service.close()
            with self._lock:
                self._services.discard(service)

    def slash(self, raw_args: str):
        """Capture the calling profile before the host moves an awaitable to a thread."""
        context = contextvars.copy_context()

        async def resolve():
            task = context.run(asyncio.create_task, self._slash(raw_args))
            return await task

        return resolve()

    async def _slash(self, raw_args: str) -> str:
        """Run network work off-loop; cancellation stops the owned worker too."""
        from .commands import run_slash  # pylint: disable=import-outside-toplevel

        service = self._service()
        cancel = threading.Event()
        task = None
        try:
            try:
                words = shlex.split(raw_args)
            except ValueError:
                return "Invalid Wisdom command. Use /wisdom help."
            command = words[0].casefold() if words else "help"
            if command in {"help", "--help", "-h", "setup", "install", "installed", "uninstall"}:
                return run_slash("help" if command in {"--help", "-h"} else raw_args, service)
            binding = service.binding()
            task = asyncio.create_task(asyncio.to_thread(
                self._run_worker, raw_args, binding, cancel
            ))
            result = await asyncio.shield(task)
            service.check_owner()
            return result
        except Exception:  # pylint: disable=broad-exception-caught
            return _UNAVAILABLE
        finally:
            cancel.set()
            if task is not None and not task.done():
                await asyncio.shield(task)
            service.close()
            with self._lock:
                self._services.discard(service)

    def _run_worker(self, raw_args, binding, cancel):
        # A process boundary provides a real deadline even for stalled DNS or a
        # slow-drip HTTP response. No bearer is serialized or passed on argv.
        deadline = time.monotonic() + SLASH_BUDGET_SECONDS
        request = {
            "package": str(Path(__file__).resolve().parent), "sys_path": sys.path,
            "root": str(self.root), "profile": [self.profile[0], str(self.profile[1])],
            "binding": binding, "args": raw_args,
        }
        payload = json.dumps(request)
        environment = os.environ.copy()
        # Host multi-profile callbacks may bind a ContextVar rather than an env
        # var. Project that captured scope into this child only, never the host.
        environment["HERMES_HOME"] = str(self.profile[1])
        with self._lock:
            if self._closed or cancel.is_set():
                return _UNAVAILABLE
            child = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable, "-c", _BOOTSTRAP],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, env=environment,
            )
            self._children.add(child)
        try:
            while not cancel.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "Collective Wisdom request timed out. Please retry."
                try:
                    output, _ = child.communicate(payload, timeout=min(0.1, remaining))
                    result = json.loads(output) if child.returncode == 0 else None
                    return result if isinstance(result, str) else _UNAVAILABLE
                except subprocess.TimeoutExpired:
                    payload = None
            return _UNAVAILABLE
        except (OSError, ValueError):
            return _UNAVAILABLE
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()
            with self._lock:
                self._children.discard(child)

    def close(self):
        """Unload only owned registrations, clients and command workers."""
        with self._lock:
            self._closed = True
            for child in self._children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=1)
            for service in self._services:
                service.close()
            for registration in reversed(self.registrations):
                registration.dispose()
            self.registrations.clear()


def worker_main(request) -> str:
    """Evaluate one read-only slash command against the captured owner."""
    from .commands import run_slash  # pylint: disable=import-outside-toplevel

    service = InvocationService(
        Path(request["root"]), (request["profile"][0], Path(request["profile"][1]))
    )
    try:
        if service.binding() != request["binding"]:
            return _UNAVAILABLE
        result = run_slash(request["args"], service)
        service.check_owner()
        return result
    except WisdomError:
        return _UNAVAILABLE
    finally:
        service.close()
