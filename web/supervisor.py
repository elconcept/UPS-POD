"""Supervise the UPS worker and Gunicorn in a single Docker container.

Both services must stay alive. If either exits unexpectedly, the container
stops and Docker restarts the whole application. SIGTERM stops both cleanly.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

CHILDREN: list[subprocess.Popen] = []
SHUTDOWN = False


def request_shutdown(_signum: int, _frame: object) -> None:
    global SHUTDOWN
    SHUTDOWN = True


def main() -> int:
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    commands = [
        [sys.executable, "/app/worker.py"],
        ["gunicorn", "--workers", "1", "--threads", "4",
         "--bind", "0.0.0.0:8080", "--timeout", "120",
         "--access-logfile", "-", "app:app"],
    ]
    try:
        for cmd in commands:
            CHILDREN.append(subprocess.Popen(cmd, env=os.environ.copy()))
        while not SHUTDOWN:
            for child in CHILDREN:
                result = child.poll()
                if result is not None:
                    print(f"Supervised process exited ({child.args[0]}): {result}",
                          file=sys.stderr, flush=True)
                    return 1
            time.sleep(1)
        return 0
    finally:
        for child in CHILDREN:
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + 12
        for child in CHILDREN:
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
