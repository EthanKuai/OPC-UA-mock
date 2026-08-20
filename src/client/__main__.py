from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from contract import CmdCode, load_contract
from security import Certificates

from .controller import DeviceController, Update

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "tags.yaml"
CERTS = ROOT / "certs"
ENDPOINT = "opc.tcp://127.0.0.1:4840/plant/server/"
DEMO_SPEED = 1.0

log = logging.getLogger("client")


async def show(update: Update) -> None:
    if update.ok:
        log.info("%s = %s", update.signal.name, update.value)
    else:
        log.warning("%s is %s", update.signal.name, update.status.name)


async def main(endpoint: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # asyncua logs every publish request at INFO, which buries the values.
    logging.getLogger("asyncua").setLevel(logging.WARNING)

    certs = Certificates(CERTS)
    if not certs.client_cert.is_file():
        raise SystemExit(f"no certificate at {certs.client_cert}; run `just certs`")

    controller = DeviceController(
        load_contract(CONFIG), endpoint, security=certs.client()
    )
    await controller.connect()
    await controller.watch(show)

    await controller.write_setpoint("Conveyor1.SpeedSetpoint", DEMO_SPEED)
    await controller.command(CmdCode.START)
    try:
        # Not a poll: everything above is already subscribed, and this only
        # keeps the process alive so the notifications have somewhere to land.
        await asyncio.Event().wait()
    finally:
        await controller.command(CmdCode.STOP)
        await controller.disconnect()


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main(args[0] if args else ENDPOINT))
