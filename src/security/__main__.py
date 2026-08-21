from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .certs import USER, ensure_certificates

CERTS = Path(__file__).resolve().parents[2] / "certs"


async def main(root: Path) -> None:
    certs = await ensure_certificates(root)
    print(f"gateway  {certs.gateway_cert}")
    print(f"client   {certs.client_cert}")
    print(f"inspect  {certs.inspect_cert}   (for `just inspect`, asyncua's own CLI)")
    print(
        f"trusted  {certs.trusted}   ({len(list(certs.trusted.iterdir()))} certificate(s))"
    )
    print(f"rejected {certs.rejected}")
    print(f"user     {USER}")


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main(Path(args[0]) if args else CERTS))
