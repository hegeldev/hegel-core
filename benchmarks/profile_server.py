#!/usr/bin/env python3
"""
Profile the server-side hot path for the composite benchmarks.

Runs everything in-process (server trio loop in main thread, client in a
background thread) so cProfile captures the event loop.  Also patches the
worker-thread function to collect a separate per-thread profile.

Usage:
    uv run python benchmarks/profile_server.py
    uv run python benchmarks/profile_server.py --benchmark shrink_matrices
"""

import argparse
import cProfile
import io
import os
import pstats
import socket as socket_module
import sys
import threading

import trio
import trio.socket

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hegel.protocol import Connection
from hegel.server import run_server_on_connection
from tests.client import Client, ClientConnection
from tests.client.client import (
    collection,
    generate_from_schema,
    start_span,
    stop_span,
)

INT_SCHEMA = {"type": "integer"}
INT_1_10 = {"type": "integer", "min_value": 1, "max_value": 10}
INT_0_10000 = {"type": "integer", "min_value": 0, "max_value": 10000}


def make_inprocess_client():
    server_socket, client_socket = socket_module.socketpair()
    server_fd = os.dup(server_socket.fileno())
    server_socket.close()

    async def _run_server():
        trio_sock = trio.socket.fromfd(
            server_fd, socket_module.AF_UNIX, socket_module.SOCK_STREAM
        )
        os.close(server_fd)
        stream = trio.SocketStream(trio_sock)
        async with trio.open_nursery() as nursery:
            conn = Connection(stream, nursery=nursery, name="Server")
            await run_server_on_connection(conn)

    # Run the server in a background thread so the client can block in the
    # foreground — we profile the server thread separately via threading.setprofile.
    def server_thread_fn():
        trio.run(_run_server)

    t = threading.Thread(target=server_thread_fn, daemon=True)
    t.start()

    client_connection = ClientConnection(client_socket)
    client = Client(client_connection)
    return client, client_connection, t


def bench_composite_of_lists(client):
    def test():
        c = collection()
        result = []
        while c.more():
            start_span()
            a = generate_from_schema(INT_SCHEMA)
            b = generate_from_schema(INT_SCHEMA)
            stop_span()
            result.append(a + b)
        assert len(result) < 10

    try:
        client.run_test(
            test,
            test_cases=200,
            seed=42,
            database=None,
            suppress_health_check=["too_slow"],
        )
    except AssertionError:
        pass


def bench_shrink_matrices(client):
    def test():
        rows = generate_from_schema(INT_1_10)
        cols = generate_from_schema(INT_1_10)
        matrix = [
            [generate_from_schema(INT_0_10000) for _ in range(cols)]
            for _ in range(rows)
        ]
        n = len(matrix)
        if len(matrix[0]) != n:
            return
        for i in range(n):
            for j in range(i + 1, n):
                if matrix[i][j] != matrix[j][i]:
                    raise AssertionError("non-symmetric matrix")

    try:
        client.run_test(
            test,
            test_cases=200,
            seed=42,
            database=None,
            suppress_health_check=["too_slow"],
        )
    except AssertionError:
        pass


BENCHMARKS = {
    "composite_of_lists": bench_composite_of_lists,
    "shrink_matrices": bench_shrink_matrices,
}


def _print_profile(pr, title, n=30):
    buf = io.StringIO()
    ps = pstats.Stats(pr, stream=buf).sort_stats("cumulative")
    ps.print_stats(n)
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    print(buf.getvalue())


def run_with_profile(bench_name, bench_fn):
    # Collect per-thread profiles via threading.setprofile.
    thread_profiles: dict[int, cProfile.Profile] = {}
    lock = threading.Lock()

    def thread_profiler(frame, event, arg):
        tid = threading.get_ident()
        with lock:
            if tid not in thread_profiles:
                pr = cProfile.Profile()
                thread_profiles[tid] = pr
                pr.enable()

    threading.setprofile(thread_profiler)

    # Also profile the calling (client) thread.
    client_pr = cProfile.Profile()
    client_pr.enable()

    client, conn, server_thread = make_inprocess_client()
    with conn:
        bench_fn(client)
    server_thread.join(timeout=5)

    client_pr.disable()
    threading.setprofile(None)
    for pr in thread_profiles.values():
        pr.disable()

    # Print the client-thread profile.
    _print_profile(client_pr, f"CLIENT THREAD — {bench_name}")

    # Print combined worker/server thread profiles.
    main_tid = threading.main_thread().ident
    for tid, pr in thread_profiles.items():
        label = "SERVER (trio event loop)" if tid != main_tid else "main"
        _print_profile(pr, f"{label} — {bench_name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--benchmark",
        choices=list(BENCHMARKS),
        default="shrink_matrices",
        help="which benchmark to profile (default: shrink_matrices)",
    )
    args = parser.parse_args()
    run_with_profile(args.benchmark, BENCHMARKS[args.benchmark])


if __name__ == "__main__":
    main()
