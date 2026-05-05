#!/usr/bin/env python3
"""
Start the local no-auth test server, wait for it to be ready, run the MCP
test suite, and stop the server on exit.

Usage:
    test/run-local-tests.py [--port N] [--include-integration]
"""

from __future__ import annotations

import argparse
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_MODULE = REPO_ROOT / "src" / "cnpg_mcp_test_server.py"
TEST_RUNNER = REPO_ROOT / "test" / "test-mcp.py"


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError as e:
            last_err = e
            time.sleep(0.1)
    raise TimeoutError(
        f"test server did not bind 127.0.0.1:{port} within {timeout:.1f}s "
        f"(last error: {last_err})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=None,
                        help="Local port for the test server (default: free OS port)")
    parser.add_argument("--include-integration", action="store_true",
                        help="Run mutating CloudNativePG Kubernetes integration tests")
    args = parser.parse_args()

    port = args.port or _pick_free_port()
    server_cmd = [
        sys.executable,
        str(SERVER_MODULE),
        "--no-auth",
        "--identity", "test-user",
        "--host", "127.0.0.1",
        "--port", str(port),
    ]

    print(f"Starting local no-auth test server on 127.0.0.1:{port}", flush=True)
    server = subprocess.Popen(server_cmd, cwd=REPO_ROOT)

    try:
        try:
            _wait_for_port(port)
        except TimeoutError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3

        print(f"Server ready; running {TEST_RUNNER.name} against /test", flush=True)
        runner_cmd = [
            sys.executable,
            str(TEST_RUNNER),
            "--url", f"http://127.0.0.1:{port}/test",
            "--no-auth",
        ]
        if args.include_integration:
            runner_cmd.append("--include-integration")
        return subprocess.call(runner_cmd, cwd=REPO_ROOT)
    finally:
        if server.poll() is None:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("warning: server did not exit on SIGTERM; killing",
                      file=sys.stderr)
                server.kill()
                server.wait()


if __name__ == "__main__":
    sys.exit(main())
