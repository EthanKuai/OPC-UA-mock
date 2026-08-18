from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from contract import CmdCode, load_contract

from .controller import DeviceController, Update

CONFIG = Path(__file__).resolve().parents[2] / "config" / "tags.yaml"
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

    controller = DeviceController(load_contract(CONFIG), endpoint)
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
