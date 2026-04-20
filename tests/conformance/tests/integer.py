#!/usr/bin/env python3
import json
import os
import sys

from hypothesis import given, settings, strategies as st


def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])

    @settings(max_examples=test_cases, database=None)
    @given(st.integers(min_value=params["min_value"], max_value=params["max_value"]))
    def run(value):
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"value": value}) + "\n")

    run()


if __name__ == "__main__":
    main()
