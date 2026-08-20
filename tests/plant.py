"""Running the gateway inside the test process.

Its OPC UA traffic still crosses a socket, so the protocol stays real. Keeping
the object here is what lets a test push a decoy namespace in front of the
plant's, hand it a certificate, or take the server away mid-subscription.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

from contract import Contract
from gateway import Gateway
from processes import free_port, wait_for_port
from security import ServerSecurity

# A gateway whose first poll has not landed yet has nothing confirmed to
# write against - a fixture that connects and writes right away needs this,
# even though the gateway itself no longer needs it to stay safe.
PRIMED_TIMEOUT = 20.0

# asyncua's shutdown waits for every client connection to end, and a connection
# that ended badly - a peer that went away mid-request, or one the server
# refused - does not reliably finish. Bounded, because the event loop this runs
# on is thrown away immediately afterwards either way.
STOP_TIMEOUT = 5.0


class GatewayHarness:
    """A gateway that can be started, stopped and started again on the same
    endpoint."""

    def __init__(
        self,
        contract: Contract,
        plc_port: int,
        decoy: str | None = None,
        security: ServerSecurity | None = None,
        wait_for_primed: bool = True,
    ) -> None:
        self.contract = contract
        self.plc_port = plc_port
        self.decoy = decoy
        self.security = security
        # A test deliberately pointed at a PLC that will never answer wants
        # the gateway in its unprimed window for the whole test, not raced
        # past it - see test_a_write_before_the_first_poll_is_refused_not_dropped.
        self.wait_for_primed = wait_for_primed
        self.port = free_port()
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}/plant/server/"
        self.task: asyncio.Task | None = None
        self.gateway: Gateway | None = None

    async def start(self) -> None:
        gateway = Gateway(
            self.contract, "127.0.0.1", self.plc_port, self.endpoint, self.security
        )
        if self.decoy is not None:
            # Slip in between the server coming up and the gateway registering
            # the contract's URI, so the plant does not land on index 2.
            inner = gateway.server.init

            async def init_then_decoy(*args, **kwargs):
                await inner(*args, **kwargs)
                await gateway.server.register_namespace(self.decoy)

            gateway.server.init = init_then_decoy

        await gateway.init()
        self.gateway = gateway
        self.task = asyncio.create_task(gateway.run())
        await wait_for_port(self.port)
        if self.wait_for_primed:
            await asyncio.wait_for(gateway.primed.wait(), PRIMED_TIMEOUT)

    async def stop(self) -> None:
        if self.task is None:
            return
        if self.gateway is not None:
            await self.gateway.stop()
        done, _ = await asyncio.wait({self.task}, timeout=STOP_TIMEOUT)
        if not done:
            self.task.cancel()
        # Best effort: a connection that ended badly can hold the socket past
        # this point (see STOP_TIMEOUT above), and this harness hands the same
        # port straight back to the next gateway. A start() on a still-taken
        # port fails loudly on its own, so there is nothing to assert here.
        await self._wait_for_port_free()
        self.task = None

    async def _wait_for_port_free(self, timeout: float = STOP_TIMEOUT) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", self.port))
                return
            except OSError:
                await asyncio.sleep(0.05)


@asynccontextmanager
async def serving(
    contract: Contract, plc_port: int, security: ServerSecurity | None = None
):
    """A gateway for the length of a `with` block. Yields its endpoint."""
    harness = GatewayHarness(contract, plc_port, security=security)
    await harness.start()
    try:
        yield harness.endpoint
    finally:
        await harness.stop()
