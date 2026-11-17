import asyncio
import io
import struct

import pytest
from PIL import Image

from vmgateway.backends.vnc import _vnc_auth_response, capture, downscale
from vmgateway.errors import GatewayError
from vmgateway.models import LeaseState

from .test_leases import env, user  # noqa: F401


def png(w, h, color=(10, 20, 30)):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


def test_downscale():
    big = png(3000, 1500)
    small = Image.open(io.BytesIO(downscale(big, 1500)))
    assert small.size == (1500, 750)
    assert downscale(big, None) == big
    assert downscale(png(800, 600), 1600) == png(800, 600)


class FakeVNCServer:
    """Speaks just enough RFB 3.8 (VNC auth, raw) to exercise the client, incl. ExtendedDesktopSize."""

    def __init__(self, w=4, h=2, password="secret"):
        self.w, self.h, self.password = w, h, password
        self.to_client = asyncio.Queue()
        self.from_client = bytearray()
        self.challenge = bytes(range(16))

    # Stream interface used by capture()
    buf = b""

    async def read(self, n):
        while len(self.buf) < n:
            self.buf += await self.to_client.get()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    async def write(self, data):
        self.from_client += data
        await self._react()

    def send(self, data):
        self.to_client.put_nowait(data)

    def start(self):
        self.send(b"RFB 003.008\n")
        self.stage = "version"

    async def _react(self):
        d = self.from_client
        if self.stage == "version" and len(d) >= 12:
            del d[:12]
            self.send(bytes([1, 2]))
            self.stage = "sec"
        if self.stage == "sec" and len(d) >= 1:
            del d[:1]
            self.send(self.challenge)
            self.stage = "auth"
        if self.stage == "auth" and len(d) >= 16:
            ok = bytes(d[:16]) == _vnc_auth_response(self.password, self.challenge)
            del d[:16]
            self.send(struct.pack("!I", 0 if ok else 1) + (b"" if ok else struct.pack("!I", 3) + b"bad"))
            self.stage = "init" if ok else "dead"
        if self.stage == "init" and len(d) >= 1:
            del d[:1]
            pf = bytes(16)
            self.send(struct.pack("!HH", self.w, self.h) + pf + struct.pack("!I", 4) + b"test")
            self.stage = "msgs"
        if self.stage == "msgs":
            while d:
                t = d[0]
                size = {0: 20, 3: 10}.get(t)
                if t == 2:
                    if len(d) < 4:
                        return
                    (n,) = struct.unpack("!H", bytes(d[2:4]))
                    size = 4 + 4 * n
                if size is None or len(d) < size:
                    return
                del d[:size]
                if t == 3:
                    # One ExtendedDesktopSize pseudo-rect, then the raw pixels in two rects.
                    hdr = struct.pack("!BxH", 0, 3)
                    eds = (
                        struct.pack("!HHHHi", 0, 0, self.w, self.h, -308) + struct.pack("!B3x", 1) + bytes(16)
                    )
                    row = bytes([0, 0, 255, 0]) * self.w  # BGRX red
                    r1 = struct.pack("!HHHHi", 0, 0, self.w, 1, 0) + row
                    r2 = struct.pack("!HHHHi", 0, 1, self.w, 1, 0) + row
                    self.send(hdr + eds + r1 + r2)


async def test_capture_against_fake_server():
    srv = FakeVNCServer()
    srv.start()
    img = Image.open(io.BytesIO(await capture(srv, "secret", timeout=5)))
    assert img.size == (4, 2)
    assert img.getpixel((0, 0)) == (255, 0, 0) and img.getpixel((3, 1)) == (255, 0, 0)


async def test_capture_bad_password():
    srv = FakeVNCServer()
    srv.start()
    with pytest.raises(Exception, match="authentication failed"):
        await capture(srv, "wrong", timeout=5)


async def test_manager_screenshot(env):  # noqa: F811
    mgr, *_ = env
    alice = user()
    lz = await mgr.wait(alice, (await mgr.create(alice, "ubuntu")).id, 5)
    data = await mgr.screenshot(alice, lz.id, max_width=None)
    assert Image.open(io.BytesIO(data)).format == "PNG"


async def test_screenshot_requires_ready_and_owner(env):  # noqa: F811
    mgr, backends, *_ = env
    backends["pve"].delay = 1
    alice = user()
    lz = await mgr.create(alice, "ubuntu")
    with pytest.raises(GatewayError) as e:
        await mgr.screenshot(alice, lz.id)
    assert e.value.status == 409
    with pytest.raises(GatewayError) as e:
        await mgr.screenshot(user("bob"), lz.id)
    assert e.value.status == 404
    assert (await mgr.get(alice, lz.id)).state == LeaseState.PROVISIONING
