#!/usr/bin/env python3
"""Reference conformance binary for origin deduplication tests.

Runs the test through the actual hegel server (via socketpair) so that the
server reports interesting_test_cases in CONFORMANCE_SERVER_RUN_METRICS_FILE.

Two modes:
- value_in_error_message: the test fails with the generated value in the
  error message. A correct origin (exc_type + innermost file:line) will
  deduplicate all failures to 1.
- multiple_call_sites: the same buggy function is called from multiple
  code paths. A correct origin (using the innermost frame) will
  deduplicate to 1.
"""

import json
import os
import socket
import sys
from pathlib import Path
from threading import Thread

# Add project root to path so we can import the test client.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from hegel.protocol import Connection
from hegel.server import run_server_on_connection
from tests.client import Client, ClientConnection, generate_from_schema

try:
    ExceptionGroup
except NameError:  # pragma: no cover
    from exceptiongroup import ExceptionGroup


def _extract_origin(exc, tb):
    """Extract origin: exc_type + innermost file:line."""
    filename = ""
    lineno = 0
    if tb is not None:
        while tb.tb_next is not None:
            tb = tb.tb_next
        filename = tb.tb_frame.f_code.co_filename
        lineno = tb.tb_lineno
    return f"{type(exc).__name__} at {filename}:{lineno}"


def _buggy_function(x):
    """A function with a single bug, always at the same location."""
    assert x <= 10


def _call_path_a(x):
    _buggy_function(x)


def _call_path_b(x):
    _buggy_function(x)


def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])
    mode = params["mode"]

    server_socket, client_socket = socket.socketpair()
    thread = Thread(
        target=run_server_on_connection,
        args=(Connection(server_socket),),
        daemon=True,
    )
    thread.start()

    with ClientConnection(client_socket) as conn:
        client = Client(conn)

        def test():
            x = generate_from_schema(
                {"type": "integer", "min_value": 0, "max_value": 100}
            )
            with open(metrics_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({}) + "\n")
            if mode == "value_in_error_message":
                assert x <= 10, f"Generated value {x} exceeded threshold 10"
            elif mode == "multiple_call_sites":
                if x % 2 == 0:
                    _call_path_a(x)
                else:
                    _call_path_b(x)

        try:
            client.run_test(test, test_cases=test_cases, seed=42)
        except (AssertionError, ExceptionGroup):
            pass  # Expected - the test finds failures

    thread.join(timeout=10)


if __name__ == "__main__":
    main()
