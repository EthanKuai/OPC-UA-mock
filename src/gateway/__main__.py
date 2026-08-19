from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from contract import load_contract
from security import Certificates

from .server import serve

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "tags.yaml"
CERTS = ROOT / "certs"
PLC_HOST = "127.0.0.1"
PLC_PORT = 5020
# Bind every interface; asyncua rewrites the advertised host to the one the
# client actually reached, so this does not become an unresolvable "0.0.0.0".
OPCUA_PORT = 4840
PATH = "/plant/server/"


async def main(opcua_port: int, plc_port: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # asyncua logs every standard-nodeset quirk and every subscription publish
    # at INFO. At one poll per 100 ms that buries the gateway's own log.
    logging.getLogger("asyncua").setLevel(logging.WARNING)

    certs = Certificates(CERTS)
    if not certs.gateway_cert.is_file():
        # Refusing to start beats quietly serving an unencrypted endpoint.
        raise SystemExit(f"no certificate at {certs.gateway_cert}; run `just certs`")

    await serve(
        load_contract(CONFIG),
        (PLC_HOST, plc_port),
        f"opc.tcp://0.0.0.0:{opcua_port}{PATH}",
        certs.server(),
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(
        main(
            int(args[0]) if len(args) > 0 else OPCUA_PORT,
            int(args[1]) if len(args) > 1 else PLC_PORT,
        )
    )
