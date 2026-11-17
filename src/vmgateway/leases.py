"""Lease lifecycle: create → provisioning → ready → releasing → released (or failed / expired)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .backends.base import MANAGED_PREFIX, Backend
from .config import GatewayConfig, Resources, TemplateConfig, TemplateSource
from .creds import make_credentials, validate_custom
from .errors import GatewayError
from .models import ACTIVE_STATES, Lease, LeaseState, User, VMRef
from .scheduler import Reservation, Scheduler

log = logging.getLogger(__name__)

TERMINAL = frozenset({LeaseState.RELEASED, LeaseState.FAILED, LeaseState.EXPIRED})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LeaseManager:
    def __init__(
        self,
        config: GatewayConfig,
        store,
        backends: dict[str, Backend],
        now: Callable[[], datetime] = _utcnow,
    ):
        self.config = config
        self.policy = config.policy
        self.store = store
        self.backends = backends
        self.scheduler = Scheduler(backends)
        self.now = now
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}  # lease id -> provisioning task
        self._events: dict[str, asyncio.Event] = {}
        self._loop_task: asyncio.Task | None = None
        self._orphan_candidates: set[tuple[str, str]] = set()  # (host, vm name) seen once without a lease

    # ---- lifecycle of the manager itself -------------------------------------

    async def start(self) -> None:
        await self.reconcile(startup=True)
        self._loop_task = asyncio.create_task(self._background_loop(), name="vmgw-reaper")

    async def stop(self) -> None:
        tasks = [t for t in [self._loop_task, *self._tasks.values()] if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._loop_task = None

    async def _background_loop(self) -> None:
        last_reconcile = self.now()
        while True:
            await asyncio.sleep(self.policy.reaper_interval_s)
            try:
                await self.reap()
                if (self.now() - last_reconcile).total_seconds() >= self.policy.reconcile_interval_s:
                    await self.reconcile()
                    last_reconcile = self.now()
            except Exception:  # keep the loop alive no matter what
                log.exception("reaper iteration failed")

    # ---- helpers --------------------------------------------------------------

    def _ttl(self, minutes: int | None) -> timedelta:
        m = self.policy.ttl_default_minutes if minutes is None else minutes
        if not self.policy.ttl_min_minutes <= m <= self.policy.ttl_max_minutes:
            raise GatewayError(
                "invalid_ttl",
                f"ttl_minutes must be between {self.policy.ttl_min_minutes} and {self.policy.ttl_max_minutes}",
                400,
            )
        return timedelta(minutes=m)

    def _template(self, template_id: str) -> TemplateConfig:
        t = self.config.find_template(template_id)
        if t is None:
            known = ", ".join(x.id for x in self.config.templates)
            raise GatewayError("unknown_template", f"{template_id!r}; available: {known}", 404)
        return t

    def _visible(self, user: User, lease: Lease | None) -> Lease:
        if lease is None or (lease.owner != user.name and not user.is_admin):
            raise GatewayError("not_found", "lease not found", 404)
        return lease

    async def _save(self, lease: Lease) -> None:
        await self.store.update_lease(lease)
        if lease.state in TERMINAL or lease.state == LeaseState.READY:
            ev = self._events.get(lease.id)
            if ev:
                ev.set()

    async def _reservations(self) -> dict[tuple[str, str], Reservation]:
        out: dict[tuple[str, str], Reservation] = {}
        for lz in await self.store.list_leases(states=[LeaseState.PROVISIONING]):
            if not lz.host or not lz.node:
                continue
            r = out.setdefault((lz.host, lz.node), Reservation())
            r.vms += 1
            r.memory_gb += lz.resources.memory_gb
            if (t := self.config.find_template(lz.template_id)) and t.os == "macos":
                r.macos += 1
        return out

    # ---- public API -------------------------------------------------------------

    async def create(
        self,
        user: User,
        template_id: str,
        cpu: int | None = None,
        memory_gb: int | None = None,
        disk_gb: int | None = None,
        ttl_minutes: int | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> Lease:
        template = self._template(template_id)
        ttl = self._ttl(ttl_minutes)
        validate_custom(template.os, username, password)
        custom = (username, password) if (username or password) else None
        d = template.defaults
        resources = Resources(
            cpu=cpu or d.cpu,
            memory_gb=memory_gb or d.memory_gb,
            disk_gb=max(disk_gb or d.disk_gb, d.disk_gb),  # disks only grow
        )
        async with self._lock:
            limit = user.max_leases if user.max_leases is not None else self.policy.default_max_leases
            mine = await self.store.list_leases(owner=user.name, states=ACTIVE_STATES)
            if len(mine) >= limit:
                raise GatewayError(
                    "quota_exceeded", f"{user.name} already holds {len(mine)}/{limit} leases", 429
                )
            backend, source = await self.scheduler.pick(template, resources, await self._reservations())
            if custom and template.credentials.mode == "static" and not hasattr(backend, "set_credentials"):
                raise GatewayError(
                    "unsupported", f"{backend.type} cannot change credentials of template {template.id}", 400
                )
            now = self.now()
            lease = Lease(
                id=secrets.token_hex(4),
                owner=user.name,
                template_id=template.id,
                state=LeaseState.PROVISIONING,
                resources=resources,
                created_at=now,
                expires_at=now + ttl,
                host=backend.name,
                node=source.node,
            )
            await self.store.insert_lease(lease)
        self._events[lease.id] = asyncio.Event()
        self._tasks[lease.id] = asyncio.create_task(
            self._provision(lease, template, source, backend, custom), name=f"provision-{lease.id}"
        )
        log.info("lease %s: %s for %s on %s/%s", lease.id, template.id, user.name, backend.name, source.node)
        return lease

    async def _provision(
        self,
        lease: Lease,
        template: TemplateConfig,
        source: TemplateSource,
        backend: Backend,
        custom: tuple[str | None, str | None] | None = None,
    ):
        try:
            username, password = custom or (None, None)
            creds = make_credentials(template.credentials, username, password)
            ref = await backend.provision(lease.vm_name, template, source, lease.resources, creds)
            lease.ref = ref
            await self.store.update_lease(lease)
            access = await backend.wait_ready(ref, template, template.ready_timeout_s, creds)
            if custom and template.credentials.mode == "static":
                desired = creds.model_copy(
                    update={"username": username or creds.username, "password": password or creds.password}
                )
                creds = await backend.set_credentials(ref, template, access, creds, desired)
            lease.access = access
            lease.credentials = creds
            lease.state = LeaseState.READY
            await self._save(lease)
            log.info("lease %s ready at %s", lease.id, access.ip)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("lease %s provisioning failed: %s", lease.id, e)
            lease.error = f"{type(e).__name__}: {e}"
            await self._teardown(lease, LeaseState.FAILED)
        finally:
            self._tasks.pop(lease.id, None)

    async def _find_ref(self, lease: Lease) -> VMRef | None:
        if lease.ref:
            return lease.ref
        backend = self.backends.get(lease.host or "")
        if backend is None:
            return None
        for ref in await backend.list_managed():
            if ref.name == lease.vm_name:
                return ref
        return None

    async def _teardown(self, lease: Lease, final: LeaseState) -> Lease:
        """Destroy the VM (if any). On failure the lease stays RELEASING for the reaper to retry."""
        try:
            ref = await self._find_ref(lease)
            if ref is not None:
                await self.backends[ref.host].destroy(ref)
        except Exception as e:
            log.warning("lease %s destroy failed: %s", lease.id, e)
            lease.state = LeaseState.RELEASING
            lease.error = f"destroy failed: {type(e).__name__}: {e}"
            if final != LeaseState.RELEASED:
                lease.error += f" (then {final.value})"
            await self._save(lease)
            return lease
        lease.state = final
        if final == LeaseState.RELEASED and lease.error and lease.error.startswith("destroy failed"):
            lease.error = None
        await self._save(lease)
        return lease

    async def get(self, user: User, lease_id: str) -> Lease:
        return self._visible(user, await self.store.get_lease(lease_id))

    async def list(self, user: User, all_users: bool = False, include_inactive: bool = False) -> list[Lease]:
        states = None if include_inactive else ACTIVE_STATES
        owner = None if (all_users and user.is_admin) else user.name
        return await self.store.list_leases(owner=owner, states=states)

    async def wait(self, user: User, lease_id: str, timeout: float) -> Lease:
        lease = await self.get(user, lease_id)
        if lease.state != LeaseState.PROVISIONING:
            return lease
        ev = self._events.setdefault(lease_id, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(ev.wait(), timeout)
        return await self.get(user, lease_id)

    async def renew(self, user: User, lease_id: str, ttl_minutes: int) -> Lease:
        lease = await self.get(user, lease_id)
        if lease.state not in (LeaseState.PROVISIONING, LeaseState.READY):
            raise GatewayError("not_active", f"lease is {lease.state.value}", 409)
        lease.expires_at = self.now() + self._ttl(ttl_minutes)
        await self.store.update_lease(lease)
        return lease

    async def destroy(self, user: User, lease_id: str) -> Lease:
        lease = await self.get(user, lease_id)
        return await self._release(lease, LeaseState.RELEASED)

    async def _release(self, lease: Lease, final: LeaseState) -> Lease:
        if lease.state in TERMINAL:
            return lease
        task = self._tasks.get(lease.id)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            lease = await self.store.get_lease(lease.id)  # pick up ref written before cancel
            if lease.state in TERMINAL:
                return lease
        lease.state = LeaseState.RELEASING
        await self.store.update_lease(lease)
        return await self._teardown(lease, final)

    async def reap(self) -> None:
        now = self.now()
        for lease in await self.store.list_leases(states=ACTIVE_STATES):
            if lease.state == LeaseState.RELEASING and lease.id not in self._tasks:
                final = (
                    LeaseState.EXPIRED
                    if lease.error and "(then expired)" in lease.error
                    else LeaseState.RELEASED
                )
                await self._teardown(lease, final)
            elif lease.expires_at <= now:
                log.info("lease %s expired", lease.id)
                await self._release(lease, LeaseState.EXPIRED)

    async def reconcile(self, startup: bool = False) -> None:
        """Repair state after crashes and delete orphaned vmgw-* VMs."""
        if startup:
            for lease in await self.store.list_leases(states=[LeaseState.PROVISIONING]):
                if lease.id in self._tasks:
                    continue
                lease.error = "gateway restarted during provisioning"
                await self._teardown(lease, LeaseState.FAILED)
        # An orphan must be seen on two consecutive passes before it is destroyed, so a VM that
        # someone is creating right now (another process, a live test) is never caught mid-flight.
        active = {lz.vm_name for lz in await self.store.list_leases(states=ACTIVE_STATES)}
        seen: set[tuple[str, str]] = set()
        for name, backend in self.backends.items():
            try:
                managed = await backend.list_managed()
            except Exception as e:
                log.warning("reconcile: cannot list %s: %s", name, e)
                continue
            for ref in managed:
                if not ref.name.startswith(MANAGED_PREFIX) or ref.name in active:
                    continue
                key = (name, ref.name)
                seen.add(key)
                if key not in self._orphan_candidates:
                    log.info(
                        "reconcile: %s on %s has no lease; will delete if still orphaned next pass",
                        ref.name,
                        name,
                    )
                    continue
                log.warning("reconcile: destroying orphan %s on %s", ref.name, name)
                try:
                    await backend.destroy(ref)
                    seen.discard(key)
                except Exception as e:
                    log.warning("reconcile: orphan %s destroy failed: %s", ref.name, e)
        self._orphan_candidates = seen
