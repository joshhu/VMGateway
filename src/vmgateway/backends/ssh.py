"""Shared SSH command runner for the Tart (macOS) and Hyper-V (Windows) backends."""

from __future__ import annotations

import asyncio
import base64
import logging

import asyncssh

from ..config import SSHConfig
from ..errors import BackendError

log = logging.getLogger(__name__)


class SSHRunner:
    def __init__(self, cfg: SSHConfig):
        self.cfg = cfg
        self._conn: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()

    async def _connection(self) -> asyncssh.SSHClientConnection:
        async with self._lock:
            if self._conn is None or self._conn.is_closed():
                self._conn = await asyncssh.connect(
                    self.cfg.host,
                    self.cfg.port,
                    username=self.cfg.user,
                    client_keys=[self.cfg.key_path],
                    known_hosts=self.cfg.known_hosts,
                    keepalive_interval=30,
                    connect_timeout=15,
                )
            return self._conn

    async def _reset(self) -> None:
        async with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    async def run(self, command: str, timeout: float = 120, check: bool = True) -> str:
        for attempt in (1, 2):
            try:
                conn = await self._connection()
                result = await asyncio.wait_for(conn.run(command, check=False), timeout)
                break
            except TimeoutError as e:
                raise BackendError(f"{self.cfg.host}: timed out after {timeout:.0f}s: {command[:120]}") from e
            except (OSError, asyncssh.Error) as e:
                await self._reset()
                if attempt == 2:
                    raise BackendError(f"{self.cfg.host}: ssh failed: {e}") from e
                log.info("ssh %s: reconnecting after %s", self.cfg.host, e)
        stdout = str(result.stdout or "")
        if check and result.exit_status != 0:
            err = str(result.stderr or "").strip() or stdout.strip()
            raise BackendError(f"{self.cfg.host}: exit {result.exit_status}: {err[-800:]}")
        return stdout

    async def run_ps(self, script: str, timeout: float = 300, check: bool = True) -> str:
        """Run a PowerShell 7 script on a Windows host. Errors abort the script with a non-zero exit."""
        full = (
            "$ErrorActionPreference = 'Stop'\n"
            "$ProgressPreference = 'SilentlyContinue'\n"
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n" + script
        )
        encoded = base64.b64encode(full.encode("utf-16-le")).decode()
        return await self.run(f"pwsh -NoProfile -NonInteractive -EncodedCommand {encoded}", timeout, check)

    async def close(self) -> None:
        await self._reset()


async def guest_run(
    host: str, port: int, username: str, password: str, command: str, timeout: float = 120
) -> str:
    """Run a command inside a guest over SSH with password auth (static-credential templates)."""
    try:
        async with asyncssh.connect(
            host,
            port,
            username=username,
            password=password,
            client_keys=None,
            known_hosts=None,
            connect_timeout=20,
        ) as conn:
            r = await asyncio.wait_for(conn.run(command, check=False), timeout)
    except (OSError, asyncssh.Error, TimeoutError) as e:
        raise BackendError(f"guest {host}: ssh failed: {e}") from e
    if r.exit_status != 0:
        raise BackendError(f"guest {host}: exit {r.exit_status}: {str(r.stderr or r.stdout).strip()[-500:]}")
    return str(r.stdout or "")
