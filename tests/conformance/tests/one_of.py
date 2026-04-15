#!/usr/bin/env python3
import json
import os
import sys

from hypothesis import given, settings, strategies as st


def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])
    mode = params.get("mode", "basic")

    ranges = params["ranges"]
    alternatives = []
    for r in ranges:
        gen = st.integers(min_value=r["min_value"], max_value=r["max_value"])
        if mode == "transformed":
            gen = gen.map(lambda x: -x)
        elif mode == "non_basic":
            gen = gen.filter(lambda x: x % 2 == 0)
        alternatives.append(gen)

    strategy = st.one_of(*alternatives)

    @settings(max_examples=test_cases, database=None)
    @given(strategy)
    def run(value):
        with open(metrics_file, "a") as f:
            f.write(json.dumps({"value": value}) + "\n")

    run()


if __name__ == "__main__":
    main()
