"""What the OPC UA side refuses.

Every test here runs the gateway with certificates configured, which is the
state the shipped `just gateway` runs in. The point of the suite is the
contrast with tests/test_contention.py, where a second master walks into the
same plant over Modbus and is asked for nothing at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from asyncua import Client, ua

from client import DeviceController
from contract import CmdCode, Contract, load_contract
from plant import serving
from processes import free_port, spawn
from security import (
    CLIENT_URI,
    Certificates,
    ClientSecurity,
    ensure_certificates,
    issue,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "tags.yaml"

INTRUDER_URI = "urn:mock.local:plant:intruder"


@pytest.fixture
def contract() -> Contract:
    return load_contract(CONFIG)


@pytest.fixture
def plc():
    process = spawn("plc", free_port())
    yield process
    process.kill()


@pytest.fixture(scope="module")
def cert_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("certs")


@pytest.fixture
async def certs(cert_root) -> Certificates:
    # Idempotent, so this is a fresh generation once and a no-op afterwards.
    return await ensure_certificates(cert_root)


@pytest.fixture
async def intruder(cert_root) -> ClientSecurity:
    """A perfectly valid certificate that nobody put in the trusted folder."""
    key = cert_root / "intruder-key.pem"
    cert = cert_root / "intruder.der"
    await issue(key, cert, INTRUDER_URI, server=False)
    return ClientSecurity(
        cert,
        key,
        Certificates(cert_root).gateway_cert,
        INTRUDER_URI,
        "operator",
        "operator",
    )


async def test_the_gateway_offers_nothing_but_an_encrypted_endpoint(
    contract, plc, certs
):
    """Discovery is unauthenticated by design - a client has to be able to ask
    what is on offer. What is on offer is the point."""
    async with serving(contract, plc.port, certs.server()) as endpoint:
        endpoints = await Client(endpoint).connect_and_get_server_endpoints()

    assert endpoints
    for described in endpoints:
        assert described.SecurityMode == ua.MessageSecurityMode.SignAndEncrypt
        assert described.SecurityPolicyUri.endswith("Basic256Sha256")
        tokens = {token.TokenType for token in described.UserIdentityTokens}
        assert tokens == {ua.UserTokenType.UserName}


async def test_an_anonymous_client_is_refused(contract, plc, certs):
    """No certificate, no account, no encryption: there is no endpoint for
    this client to land on."""
    async with serving(contract, plc.port, certs.server()) as endpoint:
        client = Client(endpoint)
        with pytest.raises((ua.UaError, OSError, asyncio.TimeoutError)):
            await client.connect()
        await _quiet_disconnect(client)


async def test_a_certificate_nobody_trusted_is_refused_and_kept(
    contract, plc, certs, intruder
):
    """The intruder's certificate is as valid as the client's. The only thing
    it lacks is a copy in the trusted folder - and the refusal has to leave
    the evidence somewhere an operator can find it."""
    assert not list(certs.rejected.iterdir())

    async with serving(contract, plc.port, certs.server()) as endpoint:
        controller = DeviceController(contract, endpoint, security=intruder)
        with pytest.raises((ua.UaError, OSError, asyncio.TimeoutError)):
            await controller.connect()
        await controller.disconnect()

    kept = list(certs.rejected.iterdir())
    assert [path.read_bytes() for path in kept] == [intruder.certificate.read_bytes()]


async def test_a_trusted_certificate_with_the_wrong_password_is_refused(
    contract, plc, certs
):
    """The certificate says which application this is. The account says who is
    driving it, and the gateway wants both."""
    wrong = replace(certs.client(), password="not-the-password")

    async with serving(contract, plc.port, certs.server()) as endpoint:
        controller = DeviceController(contract, endpoint, security=wrong)
        with pytest.raises((ua.UaError, OSError, asyncio.TimeoutError)):
            await controller.connect()
        await controller.disconnect()


async def test_the_provisioned_client_drives_the_plant_through_all_of_it(
    contract, plc, certs
):
    """And none of the above stops the client that was actually provisioned:
    it resolves the namespace, subscribes, and commands, over an encrypted
    channel with a user account attached."""
    async with serving(contract, plc.port, certs.server()) as endpoint:
        controller = DeviceController(contract, endpoint, security=certs.client())
        await controller.connect()
        try:
            assert controller.idx is not None
            seen: list[str] = []

            async def sink(update):
                seen.append(update.signal.name)

            await controller.watch(sink)
            await controller.write_setpoint("Conveyor1.SpeedSetpoint", 1.0)
            await controller.command(CmdCode.START)
            await _eventually(lambda: len(seen) >= len(contract.signals))
            await controller.command(CmdCode.STOP)
        finally:
            await controller.disconnect()


async def _eventually(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition never held within {timeout}s")


async def _quiet_disconnect(client: Client) -> None:
    try:
        await client.disconnect()
    except Exception:
        pass
