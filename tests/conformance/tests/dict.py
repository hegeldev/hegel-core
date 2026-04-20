#!/usr/bin/env python3
import json
import os
import sys

from hypothesis import given, settings, strategies as st


def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])

    value_strategy = st.integers(
        min_value=params["min_value"], max_value=params["max_value"]
    )

    if params["key_type"] == "integer":
        key_strategy = st.integers(
            min_value=params["min_key"], max_value=params["max_key"]
        )
    else:
        key_strategy = st.text()

    @settings(max_examples=test_cases, database=None)
    @given(
        st.dictionaries(
            key_strategy,
            value_strategy,
            min_size=params["min_size"],
            max_size=params["max_size"],
        )
    )
    def run(d):
        if params["key_type"] == "integer":
            min_key = min(d.keys()) if d else None
            max_key = max(d.keys()) if d else None
        else:
            min_key = None
            max_key = None

        metrics = {
            "size": len(d),
            "min_key": min_key,
            "max_key": max_key,
            "min_value": min(d.values()) if d else None,
            "max_value": max(d.values()) if d else None,
        }
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(metrics) + "\n")

    run()


if __name__ == "__main__":
    main()
