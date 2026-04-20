#!/usr/bin/env python3
import json
import os
import sys

from hypothesis import given, settings, strategies as st


def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])

    kwargs = {"min_size": params["min_size"]}
    if params["max_size"] is not None:
        kwargs["max_size"] = params["max_size"]

    @settings(max_examples=test_cases, database=None)
    @given(st.binary(**kwargs))
    def run(value):
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"length": len(value)}) + "\n")

    run()


if __name__ == "__main__":
    main()
