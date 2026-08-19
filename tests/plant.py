"""Running the gateway inside the test process.

Its OPC UA traffic still crosses a socket, so the protocol stays real. Keeping
the object here is what lets a test push a decoy namespace in front of the
plant's, hand it a certificate, or take the server away mid-subscription.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from contract import Contract
from gateway import Gateway
from processes import free_port, wait_for_port
from security import ServerSecurity

# asyncua's shutdown waits for every client connection to end, and a connection
# the server itself refused does not reliably end - so a suite that tests
# refusals hangs here about half the time. Bounded, because the event loop this
# runs on is thrown away immediately afterwards either way.
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
    ) -> None:
        self.contract = contract
        self.plc_port = plc_port
        self.decoy = decoy
        self.security = security
        self.port = free_port()
        self.endpoint = f"opc.tcp://127.0.0.1:{self.port}/plant/server/"
        self.task: asyncio.Task | None = None

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
        self.task = asyncio.create_task(gateway.run())
        await wait_for_port(self.port)

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        await asyncio.wait({self.task}, timeout=STOP_TIMEOUT)
        self.task = None


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
