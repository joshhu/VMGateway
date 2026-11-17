"""Minimal RFB (VNC) client that grabs one full framebuffer as PNG.

Supports security types None and VNC Authentication, raw encoding only. Transport-agnostic: give it any
object with `async read(n) -> bytes` and `async write(data)`, e.g. an SSH-forwarded TCP channel (Tart)
or a websocket (Proxmox VE vncwebsocket).
"""

from __future__ import annotations

import asyncio
import io
import struct
from typing import Protocol

from ..errors import BackendError


class Stream(Protocol):
    async def read(self, n: int) -> bytes: ...
    async def write(self, data: bytes) -> None: ...


class StreamReaderWriter:
    """Adapts an asyncio (or asyncssh) reader/writer pair to Stream."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def read(self, n: int) -> bytes:
        data = await self.reader.readexactly(n)
        return bytes(data)

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        drain = getattr(self.writer, "drain", None)
        if drain:
            await drain()


class WebSocketStream:
    """Adapts a `websockets` connection carrying binary RFB frames to Stream."""

    def __init__(self, ws):
        self.ws = ws
        self.buf = bytearray()

    async def read(self, n: int) -> bytes:
        while len(self.buf) < n:
            msg = await self.ws.recv()
            self.buf += msg if isinstance(msg, bytes) else msg.encode()
        out, self.buf = bytes(self.buf[:n]), self.buf[n:]
        return out

    async def write(self, data: bytes) -> None:
        await self.ws.send(data)


def _vnc_auth_response(password: str, challenge: bytes) -> bytes:
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    key = password.encode()[:8].ljust(8, b"\0")
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in key)  # VNC reverses the bit order of each key byte
    enc = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()  # 3DES with K|K|K == single DES
    return enc.update(challenge) + enc.finalize()


async def _handshake(s: Stream, password: str | None) -> tuple[int, int]:
    version = await s.read(12)
    if not version.startswith(b"RFB "):
        raise BackendError(f"not a VNC server: {version!r}")
    minor = int(version[8:11])
    await s.write(b"RFB 003.008\n" if minor >= 8 else b"RFB 003.003\n")
    if minor >= 7:
        (n,) = struct.unpack("!B", await s.read(1))
        if n == 0:
            (ln,) = struct.unpack("!I", await s.read(4))
            raise BackendError(f"VNC refused: {(await s.read(ln)).decode(errors='replace')}")
        types = list(await s.read(n))
        chosen = 2 if (2 in types and password) else (1 if 1 in types else None)
        if chosen is None:
            raise BackendError(f"unsupported VNC security types {types}")
        await s.write(struct.pack("!B", chosen))
    else:
        (chosen,) = struct.unpack("!I", await s.read(4))
    if chosen == 2:
        await s.write(_vnc_auth_response(password or "", await s.read(16)))
    if chosen == 2 or minor >= 8:
        (result,) = struct.unpack("!I", await s.read(4))
        if result != 0:
            reason = ""
            if minor >= 8:
                (ln,) = struct.unpack("!I", await s.read(4))
                reason = (await s.read(ln)).decode(errors="replace")
            raise BackendError(f"VNC authentication failed {reason}".strip())
    await s.write(b"\x01")  # ClientInit: shared
    w, h = struct.unpack("!HH", await s.read(4))
    await s.read(16)  # server pixel format (we override it)
    (name_len,) = struct.unpack("!I", await s.read(4))
    await s.read(name_len)
    return w, h


async def capture(s: Stream, password: str | None, timeout: float = 30) -> bytes:
    """Return the current screen as PNG bytes."""
    from PIL import Image

    async def run() -> bytes:
        w, h = await _handshake(s, password)
        # 32 bpp little-endian true colour: bytes arrive as B, G, R, X.
        pf = struct.pack("!BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        await s.write(struct.pack("!B3x", 0) + pf)
        # Raw, plus the DesktopSize / ExtendedDesktopSize pseudo-encodings. Apple's
        # Virtualization.framework server (Tart --vnc-experimental) crashes the VM without them.
        encodings = [0, -223, -308]
        await s.write(struct.pack(f"!BxH{len(encodings)}i", 2, len(encodings), *encodings))
        await s.write(struct.pack("!BBHHHH", 3, 0, 0, 0, w, h))  # full, non-incremental update
        fb = bytearray(w * h * 4)
        covered = 0
        while covered < w * h:
            (msg,) = struct.unpack("!B", await s.read(1))
            if msg == 0:  # FramebufferUpdate
                _, n = struct.unpack("!BH", await s.read(3))
                for _ in range(n):
                    x, y, rw, rh, enc = struct.unpack("!HHHHi", await s.read(12))
                    if enc == -223:  # DesktopSize: new framebuffer size, no payload
                        w, h, fb, covered = rw, rh, bytearray(rw * rh * 4), 0
                        await s.write(struct.pack("!BBHHHH", 3, 0, 0, 0, w, h))
                        continue
                    if enc == -308:  # ExtendedDesktopSize: screen layout payload
                        (screens,) = struct.unpack("!B3x", await s.read(4))
                        await s.read(16 * screens)
                        if (rw, rh) != (w, h):
                            w, h, fb, covered = rw, rh, bytearray(rw * rh * 4), 0
                            await s.write(struct.pack("!BBHHHH", 3, 0, 0, 0, w, h))
                        continue
                    if enc != 0:
                        raise BackendError(f"VNC server sent unsupported encoding {enc}")
                    data = await s.read(rw * rh * 4)
                    for row in range(rh):
                        off = ((y + row) * w + x) * 4
                        fb[off : off + rw * 4] = data[row * rw * 4 : (row + 1) * rw * 4]
                    covered += rw * rh
            elif msg == 1:  # SetColourMapEntries
                _, _, n = struct.unpack("!BHH", await s.read(5))
                await s.read(n * 6)
            elif msg == 2:  # Bell
                continue
            elif msg == 3:  # ServerCutText
                await s.read(3)
                (ln,) = struct.unpack("!I", await s.read(4))
                await s.read(ln)
            else:
                raise BackendError(f"unexpected VNC message {msg}")
        img = Image.frombuffer("RGB", (w, h), bytes(fb), "raw", "BGRX", 0, 1)
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        return out.getvalue()

    try:
        return await asyncio.wait_for(run(), timeout)
    except TimeoutError as e:
        raise BackendError("VNC screenshot timed out") from e
    except (asyncio.IncompleteReadError, ConnectionError) as e:
        raise BackendError(f"VNC connection closed: {e}") from e


def downscale(png: bytes, max_width: int | None) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    if not max_width or img.width <= max_width:
        return png
    h = round(img.height * max_width / img.width)
    out = io.BytesIO()
    img.convert("RGB").resize((max_width, h), Image.Resampling.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()
