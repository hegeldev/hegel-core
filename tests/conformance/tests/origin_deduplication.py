#!/usr/bin/env python3
"""Reference conformance binary for origin deduplication tests.

Uses Hypothesis's ConjectureRunner directly (the same mechanism the hegel
server uses) to verify that correct origin formatting causes failures to
deduplicate to a single interesting example.

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
import sys
from random import Random

from hypothesis import settings, strategies as st
from hypothesis.internal.conjecture.engine import ConjectureRunner


def _extract_origin(exc, tb):
    """Extract origin: exc_type + innermost file:line.

    This is the correct implementation: it does NOT include the error
    message (which may contain generated values) or the full stack trace
    (which varies by call site).
    """
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

    strategy = st.integers(min_value=0, max_value=100)

    def test_function(data):
        value = data.draw(strategy)
        try:
            if mode == "value_in_error_message":
                assert value <= 10, f"Generated value {value} exceeded threshold 10"
            elif mode == "multiple_call_sites":
                if value % 2 == 0:
                    _call_path_a(value)
                else:
                    _call_path_b(value)
        except AssertionError as e:
            origin = _extract_origin(e, e.__traceback__)
            data.mark_interesting(origin)

    runner = ConjectureRunner(
        test_function,
        settings=settings(max_examples=test_cases, database=None, deadline=None),
        random=Random(42),
    )
    runner.run()

    interesting_count = len(runner.interesting_examples)
    with open(metrics_file, "a") as f:
        f.write(json.dumps({"interesting_test_cases": interesting_count}) + "\n")


if __name__ == "__main__":
    main()
