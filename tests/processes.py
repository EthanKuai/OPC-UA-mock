"""Spawning real plant processes for tests that need a real process boundary."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

READY_TIMEOUT = 5.0
READY_POLL = 0.05


@dataclass
class Process:
    proc: subprocess.Popen
    port: int | None = None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


def _popen(module: str, args: tuple[int, ...]) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", module, *(str(a) for a in args)],
        cwd=REPO_ROOT,
        env=os.environ | {"PYTHONPATH": str(REPO_ROOT / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch(module: str, *args: int) -> Process:
    """Run `python -m <module> ...` and return straight away. A process that
    only makes outbound connections has no port to wait on; whether it has
    started is visible in what it does to the PLC."""
    return Process(_popen(module, args))


def spawn(module: str, port: int, *args: int) -> Process:
    """Run `python -m <module> <port> ...` and return once it is listening."""
    proc = _popen(module, (port, *args))
    process = Process(proc, port)
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{module} exited early:\n{proc.stdout.read()}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return process
        except OSError:
            time.sleep(READY_POLL)
    process.kill()
    raise RuntimeError(f"{module} never listened on {port}:\n{proc.stdout.read()}")


async def wait_for_port(port: int, timeout: float = 10.0) -> None:
    """Wait until something accepts TCP on `port`, without blocking the loop.
    For a server started inside this process rather than spawned."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(READY_POLL)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise AssertionError(f"nothing listening on {port} after {timeout}s")
