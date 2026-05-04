#!/usr/bin/env python3
"""
Run the local test server under `coverage run` and exercise it with the
plugin-based test runner. On exit, combine parallel-mode coverage files and
print a report (and optionally an HTML report).

Usage:
    test/run-coverage.py [--port N] [--html]

Designed for `make dev-coverage`. For in-cluster coverage, see the
test sidecar Dockerfile and chart deployment.
"""

from __future__ import annotations

import argparse
import os
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
COVERAGERC = REPO_ROOT / ".coveragerc"


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


def _purge_coverage_data() -> None:
    """
    Delete coverage *data* files only — `.coverage` and the parallel-mode
    siblings `.coverage.<host>.<pid>.<rand>`. Must NOT match `.coveragerc`.
    """
    candidates = [REPO_ROOT / ".coverage", *REPO_ROOT.glob(".coverage.*")]
    for f in candidates:
        if f.is_file():
            f.unlink()


def _coverage_cli(*args: str) -> int:
    return subprocess.call(
        [sys.executable, "-m", "coverage", *args],
        cwd=REPO_ROOT,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=None,
                        help="Local port for the test server (default: free OS port)")
    parser.add_argument("--html", action="store_true",
                        help="Also generate HTML report under coverage-html/")
    parser.add_argument("--keep-data", action="store_true",
                        help="Don't delete .coverage* before the run")
    args = parser.parse_args()

    try:
        import coverage  # noqa: F401
    except ImportError:
        print("error: `coverage` is not installed. Run:", file=sys.stderr)
        print("  pip install -r test/requirements.txt", file=sys.stderr)
        return 2

    port = args.port or _pick_free_port()

    if not args.keep_data:
        _purge_coverage_data()

    env = os.environ.copy()
    env["COVERAGE_RCFILE"] = str(COVERAGERC)

    server_cmd = [
        sys.executable, "-m", "coverage", "run",
        "--rcfile", str(COVERAGERC),
        str(SERVER_MODULE),
        "--no-auth",
        "--identity", "test-user",
        "--host", "127.0.0.1",
        "--port", str(port),
    ]

    print(f"⏩ Starting test server under coverage on 127.0.0.1:{port}", flush=True)
    server = subprocess.Popen(server_cmd, env=env, cwd=REPO_ROOT)

    test_exit = 1
    try:
        try:
            _wait_for_port(port)
        except TimeoutError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3

        print(f"✅ Server ready; running {TEST_RUNNER.name} against /test", flush=True)
        test_exit = subprocess.call(
            [sys.executable, str(TEST_RUNNER),
             "--url", f"http://127.0.0.1:{port}/test",
             "--no-auth"],
            cwd=REPO_ROOT,
        )
    finally:
        # SIGTERM lets coverage's atexit hook flush the parallel data file —
        # the test server installs handlers that turn SIGTERM into a clean
        # sys.exit(0) so atexit actually runs (see _install_clean_shutdown_handlers
        # in src/cnpg_mcp_test_server.py).
        if server.poll() is None:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("warning: server did not exit on SIGTERM; killing",
                      file=sys.stderr)
                server.kill()
                server.wait()

    print(flush=True)
    print("⏩ Combining parallel coverage data...", flush=True)
    if _coverage_cli("combine") != 0:
        print("error: `coverage combine` failed", file=sys.stderr)
        return 4

    print(flush=True)
    print("=" * 70, flush=True)
    print("Coverage report (src/)", flush=True)
    print("=" * 70, flush=True)
    rc_report = _coverage_cli("report")
    if args.html:
        print(flush=True)
        print("⏩ Writing HTML report...", flush=True)
        _coverage_cli("html")
        print("   coverage-html/index.html")

    return test_exit or rc_report


if __name__ == "__main__":
    sys.exit(main())