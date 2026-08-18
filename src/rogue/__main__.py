from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from contract import load_contract

from .master import RogueMaster

CONFIG = Path(__file__).resolve().parents[2] / "config" / "tags.yaml"
PLC_HOST = "127.0.0.1"
PLC_PORT = 5020

# A signal tags.yaml says the gateway owns, held at a speed nobody asked for.
SIGNAL = "Conveyor1.SpeedSetpoint"
SPEED = 0.2
PERIOD = 0.5

log = logging.getLogger("rogue")


async def main(port: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    contract = load_contract(CONFIG)
    rogue = RogueMaster(contract, PLC_HOST, port)
    await rogue.connect()
    log.warning(
        "writing %s = %s every %d ms. Nothing in the protocol says I may not.",
        SIGNAL,
        SPEED,
        PERIOD * 1000,
    )
    try:
        while True:
            await rogue.write_signal(SIGNAL, SPEED)
            log.info("%s <- %s", SIGNAL, SPEED)
            await asyncio.sleep(PERIOD)
    finally:
        rogue.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main(int(args[0]) if args else PLC_PORT))
