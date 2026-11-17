import pytest

from vmgateway.config import CredentialsConfig
from vmgateway.creds import make_credentials, validate_custom
from vmgateway.errors import GatewayError
from vmgateway.models import LeaseState

from .test_leases import env, user  # noqa: F401  (fixture reuse)


@pytest.mark.parametrize(
    "os_, username, password",
    [
        ("linux", "dev", "abcdefgh"),
        ("windows", "tester", "Abcdef12"),
        ("macos", "me.two", "x" * 8),
    ],
)
def test_valid(os_, username, password):
    validate_custom(os_, username, password)


@pytest.mark.parametrize(
    "os_, username, password, match",
    [
        ("linux", "9bad", "abcdefgh", "username"),
        ("linux", "a" * 21, "abcdefgh", "username"),
        ("linux", "ok", "short", "8"),
        ("linux", "ok", "has\nnewline", "printable"),
        ("windows", "ok", "alllowercase", "complexity"),
        ("windows", "tester", "Tester123!", "contain"),
    ],
)
def test_invalid(os_, username, password, match):
    with pytest.raises(GatewayError, match=match):
        validate_custom(os_, username, password)


def test_generated_modes_take_custom_values():
    c = make_credentials(
        CredentialsConfig(mode="cloudinit", username="agent"), username="me", password="Secret-123"
    )
    assert (c.username, c.password) == ("me", "Secret-123") and c.ssh_public_key


def test_static_mode_ignores_custom_until_applied(monkeypatch):
    monkeypatch.setenv("PW", "tpl")
    c = make_credentials(
        CredentialsConfig(mode="static", username="tplusr", password_env="PW"), "me", "Secret-123"
    )
    assert (c.username, c.password) == ("tplusr", "tpl")


async def test_lease_with_custom_creds_cloudinit(env):  # noqa: F811
    mgr, backends, _, _ = env
    alice = user()
    lz = await mgr.create(alice, "ubuntu", username="dev", password="Passw0rd-x")
    lz = await mgr.wait(alice, lz.id, 5)
    assert lz.state == LeaseState.READY
    assert (lz.credentials.username, lz.credentials.password) == ("dev", "Passw0rd-x")
    assert backends["pve"].vms[lz.ref.vm_id]["creds"].username == "dev"


async def test_lease_with_custom_creds_static_applied(env, monkeypatch):  # noqa: F811
    mgr, backends, _, _ = env
    monkeypatch.setenv("STATIC_PW", "tpl-pw")
    t = mgr.config.template("macos")
    t.credentials = CredentialsConfig(mode="static", username="tplusr", password_env="STATIC_PW")
    alice = user()
    lz = await mgr.wait(alice, (await mgr.create(alice, "macos", password="NewPass-99")).id, 5)
    assert lz.state == LeaseState.READY
    assert (lz.credentials.username, lz.credentials.password) == ("tplusr", "NewPass-99")
    assert backends["mac"].applied == [("tplusr", "tpl-pw", "tplusr", "NewPass-99")]


async def test_invalid_custom_rejected_before_provisioning(env):  # noqa: F811
    mgr, backends, _, _ = env
    with pytest.raises(GatewayError):
        await mgr.create(user(), "ubuntu", password="short")
    assert backends["pve"].vms == {}
