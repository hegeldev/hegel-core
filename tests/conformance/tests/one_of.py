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

    if mode == "non_basic":
        # Mirror the span protocol: draw index, then draw from selected alternative.
        # This produces 2 generate calls (index + value).
        index_strategy = st.sampled_from(range(len(alternatives)))

        @st.composite
        def non_basic_one_of(draw):
            i = draw(index_strategy)
            return draw(alternatives[i])

        strategy = non_basic_one_of()
        generate_count = 2
    else:
        strategy = st.one_of(*alternatives)
        generate_count = 1

    @settings(max_examples=test_cases, database=None)
    @given(strategy)
    def run(value):
        with open(metrics_file, "a") as f:
            f.write(
                json.dumps({"value": value, "generate_count": generate_count}) + "\n"
            )

    run()


if __name__ == "__main__":
    main()
