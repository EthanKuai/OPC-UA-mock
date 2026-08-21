from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from contract import load_contract
from machine import Cnc, CncState, Conveyor

from .modbus_server import PlcModbusServer
from .scan import Plc

# Long enough that the gateway's 100 ms poll reliably catches each state at
# least once; short enough that a cycle finishes in about a second.
CNC_DURATIONS = {
    CncState.HOMING: 0.3,
    CncState.LOADING: 0.3,
    CncState.RUNNING: 0.3,
    CncState.UNLOADING: 0.3,
}

CONFIG = Path(__file__).resolve().parents[2] / "config" / "tags.yaml"
# Bind every interface, but 502 is privileged; 5020 is the conventional
# unprivileged Modbus TCP port. Overridable so a test can own its own PLC.
BIND = "0.0.0.0"
DEFAULT_PORT = 5020

log = logging.getLogger("plc")


async def main(port: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    contract = load_contract(CONFIG)
    conveyor = Conveyor(ramp_time=0.5, max_speed=2.0)
    cnc = Cnc(durations=CNC_DURATIONS)
    plc = Plc(contract, conveyor, cnc)

    server = PlcModbusServer(plc.memory, (BIND, port))
    scan = asyncio.create_task(_run_scan(plc))
    log.info(
        "scan %d ms, listening on %s:%d",
        contract.meta.scan_period_ms,
        BIND,
        port,
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
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT))
