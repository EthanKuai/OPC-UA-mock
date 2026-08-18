"""The client against the real stack: a PLC in its own process, a gateway
speaking OPC UA over a real socket, and the controller driving it.

The gateway runs in this process rather than its own. Its OPC UA traffic still
crosses a socket, and keeping it here is what lets a test push a decoy
namespace in front of the plant's and take the server away mid-subscription.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from client import DEADBAND, DeviceController, Update
from contract import CmdCode, Contract, load_contract
from gateway import Gateway
from processes import free_port, spawn, wait_for_port

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "tags.yaml"

DECOY_URI = "http://decoy.local/not-the-plant/"
# Where the plant's namespace lands when nothing is registered ahead of it:
# 0 is the OPC UA namespace, 1 is the server's own application URI.
UNSHIFTED_INDEX = 2

SETPOINT = "Conveyor1.SpeedSetpoint"
POSITION = "Conveyor1.Position"

# Several gateway poll periods (100 ms each): long enough that a client
# notifying per poll rather than per change would be caught.
QUIET = 0.5
IDLE = 1.5


@pytest.fixture
def contract() -> Contract:
    return load_contract(CONFIG)


@pytest.fixture
def plc():
    process = spawn("plc", free_port())
    yield process
    process.kill()


class GatewayHarness:
    """A gateway that can be started, stopped and started again on the same
    endpoint, optionally with another namespace registered before the plant."""

    def __init__(self, contract: Contract, plc_port: int, decoy: str | None) -> None:
        self.contract = contract
        self.plc_port = plc_port
        self.decoy = decoy
        self.port = free_port()
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}/plant/server/"
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        gateway = Gateway(self.contract, "127.0.0.1", self.plc_port, self.endpoint)
        if self.decoy is not None:
            # Slip in between the server coming up and the gateway registering
            # the contract's URI, so the plant does not land on index 2.
            inner = gateway.server.init

            async def init_then_decoy(*args, **kwargs):
                await inner(*args, **kwargs)
                await gateway.server.register_namespace(self.decoy)

            gateway.server.init = init_then_decoy

        await gateway.init()
        self.task = asyncio.create_task(gateway.run())
        await wait_for_port(self.port)

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.task = None


class Collector:
    """Every Update the controller hands us, in arrival order."""

    def __init__(self) -> None:
        self.updates: list[Update] = []
        # settle() empties `updates`; this keeps what each signal last said.
        self.latest: dict[str, Update] = {}

    async def __call__(self, update: Update) -> None:
        self.updates.append(update)
        self.latest[update.signal.name] = update

    def of(self, name: str) -> list[Update]:
        return [u for u in self.updates if u.signal.name == name]

    async def settle(self, timeout: float = 10.0) -> None:
        """Wait for the flow of updates to stop, then start counting afresh."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            seen = len(self.updates)
            await asyncio.sleep(QUIET)
            if len(self.updates) == seen:
                self.updates.clear()
                return
        raise AssertionError("updates never stopped arriving")

    async def wait_for(self, name: str, timeout: float = 5.0) -> Update:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            seen = self.of(name)
            if seen:
                return seen[-1]
            await asyncio.sleep(0.05)
        raise AssertionError(f"no update for {name} within {timeout}s")


@dataclass
class Stack:
    gateway: GatewayHarness
    controller: DeviceController
    updates: Collector


@pytest.fixture
async def plant(contract, plc):
    """Builds a running gateway, a connected controller and a subscription."""
    built: list[Stack] = []

    async def build(decoy: str | None = None) -> Stack:
        harness = GatewayHarness(contract, plc.port, decoy)
        await harness.start()
        controller = DeviceController(contract, harness.endpoint)
        await controller.connect()
        updates = Collector()
        await controller.watch(updates)
        stack = Stack(harness, controller, updates)
        built.append(stack)
        # An open port is not a working gateway: it subscribes to its own
        # writable nodes only after its first poll, and a setpoint written
        # before that is dropped. Waiting for quiet waits that window out.
        await updates.settle()
        return stack

    yield build

    for stack in built:
        await stack.controller.disconnect()
        await stack.gateway.stop()


async def test_the_namespace_index_is_resolved_not_assumed(plant, contract):
    """A decoy namespace registered before the plant's moves every NodeId in
    the address space. A client that resolves the URI does not notice."""
    stack = await plant(decoy=DECOY_URI)

    assert stack.controller.idx != UNSHIFTED_INDEX

    assert set(stack.updates.latest) == {s.name for s in contract.signals}
    for update in stack.updates.latest.values():
        assert update.ok, update.status
        assert update.value is not None
    # Methods are resolved by the same path, so they move with it.
    await stack.controller.command(CmdCode.STOP)


async def test_notifications_follow_changes_not_the_poll_clock(plant):
    """The gateway rewrites every node every 100 ms whether or not the value
    moved. Only the moves are allowed to reach the client."""
    stack = await plant()

    await asyncio.sleep(IDLE)
    assert stack.updates.updates == [], "notified without anything changing"

    await stack.controller.write_setpoint(SETPOINT, 1.0)
    update = await stack.updates.wait_for(SETPOINT)
    assert update.value == pytest.approx(1.0)


async def test_jitter_inside_the_deadband_is_not_reported(plant):
    """0.1% wobble around a setpoint is noise, not news.

    The deadband is absolute and applied in the client. A ua.DataChangeFilter
    would push the same job onto the server, where asyncua ANDs it with the
    change test - which is what the bad-status test below would then catch.
    """
    stack = await plant()
    await stack.controller.write_setpoint(SETPOINT, 1.0)
    await stack.updates.settle()

    for value in (1.001, 0.999, 1.001, 0.999, 1.0):
        assert abs(value - 1.0) < DEADBAND
        await stack.controller.write_setpoint(SETPOINT, value)
        await asyncio.sleep(0.1)

    await asyncio.sleep(QUIET)
    assert stack.updates.of(SETPOINT) == []

    # The same subscription must still report a real move, or the test above
    # would pass just as well with a broken subscription.
    await stack.controller.write_setpoint(SETPOINT, 1.5)
    update = await stack.updates.wait_for(SETPOINT)
    assert update.value == pytest.approx(1.5)


async def test_the_subscription_is_restored_after_the_gateway_goes_away(plant, contract):
    """The gateway is stopped mid-run and started again. The client has to
    come back on its own, with its monitored items intact."""
    stack = await plant()

    await stack.gateway.stop()
    stack.updates.updates.clear()
    await stack.gateway.start()

    for signal in contract.signals:
        update = await stack.updates.wait_for(signal.name, timeout=30.0)
        assert update.ok, update.status
    # The resolved nodes survived too, or this would raise BadNodeIdUnknown.
    await stack.controller.command(CmdCode.STOP)


async def test_a_plc_owned_signal_is_refused_before_it_reaches_the_wire(plant):
    """Position is the machine's own measurement. A controller that can write
    it is a controller that will one day be asked to."""
    stack = await plant()

    with pytest.raises(ValueError):
        await stack.controller.write_setpoint(POSITION, 5.0)


async def test_a_dead_plc_reaches_the_client_as_a_bad_status(plant, plc, contract):
    """Killing the PLC has to arrive as a status change, not as silence and a
    stale value that still looks Good.

    This is what rules out a server-side deadband: the values do not move when
    the PLC dies, so a filter that weighs status changes against the deadband
    drops the one notification that matters.
    """
    stack = await plant()

    plc.kill()

    for signal in contract.signals:
        update = await stack.updates.wait_for(signal.name, timeout=10.0)
        assert not update.ok
        assert update.value is None
