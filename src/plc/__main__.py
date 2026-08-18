from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from contract import load_contract
from machine import Conveyor

from .modbus_server import PlcModbusServer
from .scan import Plc

CONFIG = Path(__file__).resolve().parents[2] / "config" / "tags.yaml"
# 502 is privileged; 5020 is the conventional unprivileged Modbus TCP port.
LISTEN = ("0.0.0.0", 5020)

log = logging.getLogger("plc")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    contract = load_contract(CONFIG)
    conveyor = Conveyor(ramp_time=0.5, max_speed=2.0)
    plc = Plc(contract, conveyor)

    server = PlcModbusServer(plc.memory, LISTEN)
    scan = asyncio.create_task(_run_scan(plc))
    log.info(
        "scan %d ms, listening on %s:%d",
        contract.meta.scan_period_ms,
        *LISTEN,
    )
    try:
        await server.serve_forever()
    finally:
        scan.cancel()


async def _run_scan(plc: Plc) -> None:
    await plc.run()
    # run() only returns on its own if the watchdog tripped.
    log.error("scan watchdog tripped after %d ticks", plc.stats.ticks)


if __name__ == "__main__":
    asyncio.run(main())
