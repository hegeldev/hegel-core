#!/usr/bin/env python3
import json
import math
import os
import sys

from hypothesis import given, settings, strategies as st


def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])

    kwargs = {}
    if params["min_value"] is not None:
        kwargs["min_value"] = params["min_value"]
    if params["max_value"] is not None:
        kwargs["max_value"] = params["max_value"]

    has_min = params["min_value"] is not None
    has_max = params["max_value"] is not None
    allow_nan = params["allow_nan"]
    allow_infinity = params["allow_infinity"]

    # match Hypothesis defaults
    if allow_nan is None:
        allow_nan = not has_min and not has_max
    if allow_infinity is None:
        allow_infinity = not has_min or not has_max

    kwargs["allow_nan"] = allow_nan
    kwargs["allow_infinity"] = allow_infinity

    @settings(max_examples=test_cases, database=None)
    @given(st.floats(**kwargs))
    def run(value):
        # apply exclude_min/exclude_max as filters, matching how hegel libraries work
        if (
            params["exclude_min"]
            and params["min_value"] is not None
            and value == params["min_value"]
        ):
            return
        if (
            params["exclude_max"]
            and params["max_value"] is not None
            and value == params["max_value"]
        ):
            return

        metrics = {
            "value": value,
            "is_nan": math.isnan(value),
            "is_infinite": math.isinf(value),
        }
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(metrics) + "\n")

    run()


if __name__ == "__main__":
    main()
