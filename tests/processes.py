"""Spawning real plant processes for tests that need a real process boundary."""

from __future__ import annotations

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
    port: int

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn(module: str, port: int, *args: int) -> Process:
    """Run `python -m <module> <port> ...` and return once it is listening."""
    proc = subprocess.Popen(
        [sys.executable, "-m", module, str(port), *(str(a) for a in args)],
        cwd=REPO_ROOT,
        env=os.environ | {"PYTHONPATH": str(REPO_ROOT / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
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
