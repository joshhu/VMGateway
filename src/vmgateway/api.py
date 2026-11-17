"""REST API under /v1."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .auth import authenticate, create_user, install_snippets, require_admin, revoke_user, user_view
from .errors import GatewayError
from .leases import LeaseManager
from .models import User
from .views import hosts_view, lease_view, template_view


class CreateLease(BaseModel):
    template: str
    cpu: int | None = Field(default=None, ge=1, le=64)
    memory_gb: int | None = Field(default=None, ge=1, le=512)
    disk_gb: int | None = Field(default=None, ge=1, le=4096)
    ttl_minutes: int | None = None
    username: str | None = Field(default=None, description="Guest account to create/use (optional)")
    password: str | None = Field(default=None, description="Its password (optional; random if omitted)")
    wait: bool = False
    wait_timeout_s: float = Field(default=600, ge=0, le=1800)


class Renew(BaseModel):
    ttl_minutes: int


class NewUser(BaseModel):
    name: str
    admin: bool = False
    max_leases: int | None = None


def get_manager(request: Request) -> LeaseManager:
    return request.app.state.manager


async def current_user(request: Request, authorization: str | None = Header(default=None)) -> User:
    return await authenticate(request.app.state.manager.store, authorization)


async def collect_capacity(mgr: LeaseManager) -> dict:
    names = list(mgr.backends)
    results = await asyncio.gather(*(mgr.backends[n].capacity() for n in names), return_exceptions=True)
    return {n: (str(r) if isinstance(r, BaseException) else r) for n, r in zip(names, results, strict=True)}


router = APIRouter(prefix="/v1")


@router.get("/templates")
async def list_templates(user: User = Depends(current_user), mgr: LeaseManager = Depends(get_manager)):
    return [template_view(t) for t in mgr.config.templates]


@router.get("/hosts")
async def list_hosts(user: User = Depends(current_user), mgr: LeaseManager = Depends(get_manager)):
    return hosts_view(mgr.config, await collect_capacity(mgr), detailed=user.is_admin)


@router.post("/leases", status_code=201)
async def create_lease(
    body: CreateLease, user: User = Depends(current_user), mgr: LeaseManager = Depends(get_manager)
):
    lease = await mgr.create(
        user,
        body.template,
        body.cpu,
        body.memory_gb,
        body.disk_gb,
        body.ttl_minutes,
        body.username,
        body.password,
    )
    if body.wait:
        lease = await mgr.wait(user, lease.id, body.wait_timeout_s)
    return lease_view(lease)


@router.get("/leases")
async def list_leases(
    all: bool = False,
    include_inactive: bool = False,
    user: User = Depends(current_user),
    mgr: LeaseManager = Depends(get_manager),
):
    return [lease_view(x, include_secrets=False) for x in await mgr.list(user, all, include_inactive)]


@router.get("/leases/{lease_id}")
async def get_lease(
    lease_id: str,
    include_key: bool = False,
    wait_s: float = 0,
    user: User = Depends(current_user),
    mgr: LeaseManager = Depends(get_manager),
):
    lease = await (mgr.wait(user, lease_id, min(wait_s, 1800)) if wait_s > 0 else mgr.get(user, lease_id))
    return lease_view(
        lease, include_secrets=lease.owner == user.name or user.is_admin, include_key=include_key
    )


@router.get("/leases/{lease_id}/screenshot", response_class=Response)
async def screenshot_lease(
    lease_id: str,
    max_width: int = 1600,
    user: User = Depends(current_user),
    mgr: LeaseManager = Depends(get_manager),
):
    png = await mgr.screenshot(user, lease_id, max_width or None)
    return Response(png, media_type="image/png")


@router.post("/leases/{lease_id}/renew")
async def renew_lease(
    lease_id: str, body: Renew, user: User = Depends(current_user), mgr: LeaseManager = Depends(get_manager)
):
    return lease_view(await mgr.renew(user, lease_id, body.ttl_minutes), include_secrets=False)


@router.delete("/leases/{lease_id}")
async def delete_lease(
    lease_id: str, user: User = Depends(current_user), mgr: LeaseManager = Depends(get_manager)
):
    return lease_view(await mgr.destroy(user, lease_id), include_secrets=False)


@router.post("/users", status_code=201)
async def add_user(
    body: NewUser, user: User = Depends(current_user), mgr: LeaseManager = Depends(get_manager)
):
    require_admin(user)
    key = await create_user(mgr.store, body.name, body.admin, body.max_leases)
    return {"name": body.name, "api_key": key, "install": install_snippets(mgr.config.server.public_url, key)}


@router.get("/users")
async def get_users(user: User = Depends(current_user), mgr: LeaseManager = Depends(get_manager)):
    require_admin(user)
    return [user_view(u) for u in await mgr.store.list_users()]


@router.delete("/users/{name}")
async def delete_user(
    name: str, user: User = Depends(current_user), mgr: LeaseManager = Depends(get_manager)
):
    require_admin(user)
    await revoke_user(mgr.store, name)
    return {"name": name, "revoked": True}


def install(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError):
        return JSONResponse(exc.to_dict(), status_code=exc.status)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    app.include_router(router)
