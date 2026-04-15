#!/usr/bin/env python3
"""Reference conformance binary for origin deduplication tests.

Tests that the origin field in mark_complete is coarse enough to correctly
deduplicate failures. Uses Hypothesis directly as the reference implementation.

Two modes:
- value_in_error_message: fails with the generated value in the error message.
  Hypothesis deduplicates correctly because InterestingOrigin uses exc_type +
  file + line, not the error message.
- multiple_call_sites: the same buggy function is reached via different call
  paths. Hypothesis deduplicates correctly because InterestingOrigin uses the
  innermost frame (where the assertion is), not the full stack trace.
"""

import json
import os
import sys

from hypothesis import Phase, given, settings, strategies as st

try:
    ExceptionGroup
except NameError:
    from exceptiongroup import ExceptionGroup


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
    mode = params["mode"]

    if mode == "value_in_error_message":

        @settings(max_examples=200, database=None, phases={Phase.generate})
        @given(st.integers(0, 100))
        def test(x):
            assert x <= 10, f"Generated value {x} exceeded threshold 10"

    elif mode == "multiple_call_sites":

        @settings(max_examples=200, database=None, phases={Phase.generate})
        @given(st.integers(0, 100))
        def test(x):
            if x % 2 == 0:
                _call_path_a(x)
            else:
                _call_path_b(x)

    else:
        raise ValueError(f"Unknown mode: {mode}")

    interesting_count = 0
    try:
        test()
    except ExceptionGroup as e:
        interesting_count = len(e.exceptions)
    except AssertionError:
        interesting_count = 1

    with open(metrics_file, "a") as f:
        f.write(json.dumps({"interesting_test_cases": interesting_count}) + "\n")


if __name__ == "__main__":
    main()
