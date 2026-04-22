#!/usr/bin/env python3
"""
Benchmark suite for composite/nested-draw patterns.

Replicates the two hegel-rust hypothesis tests that showed significant
performance regression under trio vs threading:
  - test_composite_of_lists
  - test_can_shrink_matrices_with_length_param

Spawns the real hegel server with --stdio and talks to it over
stdin/stdout pipes, matching the transport used by real library clients.

Usage:
    uv run python benchmarks/bench_composite.py
    uv run python benchmarks/bench_composite.py --runs 3
    uv run python benchmarks/bench_composite.py --server path/to/hegel

Environment:
    HEGEL_SERVER_COMMAND  command to launch the hegel server (default: uv run hegel)
"""

import argparse
import os
import shlex
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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

_DEFAULT_SERVER_CMD = shlex.split(
    os.environ.get("HEGEL_SERVER_COMMAND", "uv run hegel")
)


class _StdioSocket:
    """Wraps a subprocess's stdin/stdout as a recv/sendall socket interface."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc

    def recv(self, n: int) -> bytes:
        return self._proc.stdout.read(n)

    def sendall(self, data: bytes) -> None:
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def settimeout(self, timeout) -> None:
        pass

    def shutdown(self, how: int) -> None:
        pass

    def close(self) -> None:
        try:
            self._proc.stdin.close()
        except OSError:
            pass


def make_client(server_cmd=None) -> tuple[Client, ClientConnection, subprocess.Popen]:
    """Spawn a hegel --stdio server and return a connected client."""
    if server_cmd is None:
        server_cmd = _DEFAULT_SERVER_CMD

    proc = subprocess.Popen(
        server_cmd + ["--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    client_connection = ClientConnection(_StdioSocket(proc))
    client = Client(client_connection)
    return client, client_connection, proc


def bench_composite_of_lists(client: Client) -> float:
    """
    Mirrors hegel-rust test_composite_of_lists.

    Finds a list whose elements are composites (each composite draws two
    integers and adds them), with length >= 10.  Each list element uses
    start_span + 2 generates + stop_span, making this round-trip-heavy.
    """

    def test() -> None:
        c = collection()
        result = []
        while c.more():
            start_span()
            a = generate_from_schema(INT_SCHEMA)
            b = generate_from_schema(INT_SCHEMA)
            stop_span()
            result.append(a + b)
        assert len(result) < 10

    t0 = time.perf_counter()
    try:
        client.run_test(
            test,
            test_cases=200,
            seed=42,
            database=None,
            suppress_health_check=["too_slow"],
        )
    except AssertionError:
        pass  # expected: test finds a list of length >= 10
    return time.perf_counter() - t0


def bench_shrink_matrices(client: Client) -> float:
    """
    Mirrors hegel-rust test_can_shrink_matrices_with_length_param.

    Draws a rows×columns matrix and looks for a non-symmetric square matrix.
    Round-trip count per test case is 2 + rows*cols, stressing shrinking.
    """

    def test() -> None:
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

    t0 = time.perf_counter()
    try:
        client.run_test(
            test,
            test_cases=200,
            seed=42,
            database=None,
            suppress_health_check=["too_slow"],
        )
    except AssertionError:
        pass  # expected: test finds a non-symmetric matrix
    return time.perf_counter() - t0


BENCHMARKS = [
    ("composite_of_lists", bench_composite_of_lists),
    ("shrink_matrices", bench_shrink_matrices),
]


def run_benchmark(name, fn, runs, server_cmd):
    times = []
    for i in range(runs):
        client, conn, proc = make_client(server_cmd)
        try:
            with conn:
                elapsed = fn(client)
        finally:
            proc.wait(timeout=10)
        times.append(elapsed)
        print(f"  run {i + 1}/{runs}: {elapsed:.3f}s")
    return times


def main() -> None:
    parser = argparse.ArgumentParser(description="Composite benchmark suite")
    parser.add_argument("--runs", type=int, default=5, help="runs per benchmark")
    parser.add_argument("--server", default=None, help="hegel server command")
    parser.add_argument(
        "benchmarks",
        nargs="*",
        help="benchmark names to run (default: all)",
    )
    args = parser.parse_args()

    server_cmd = shlex.split(args.server) if args.server else None
    selected = args.benchmarks or [name for name, _ in BENCHMARKS]

    for name, fn in BENCHMARKS:
        if name not in selected:
            continue
        print(f"\n=== {name} ({args.runs} runs) ===")
        times = run_benchmark(name, fn, args.runs, server_cmd)
        if len(times) == 1:
            print(f"  result: {times[0]:.3f}s")
        else:
            mean = statistics.mean(times)
            stdev = statistics.stdev(times)
            print(f"  mean={mean:.3f}s  stdev={stdev:.3f}s  min={min(times):.3f}s  max={max(times):.3f}s")


if __name__ == "__main__":
    main()
