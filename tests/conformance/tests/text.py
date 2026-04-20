#!/usr/bin/env python3
import json
import os
import sys

from hypothesis import given, settings, strategies as st


def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])

    char_kwargs = {}
    text_kwargs = {}

    text_kwargs["min_size"] = params["min_size"]
    if params.get("max_size") is not None:
        text_kwargs["max_size"] = params["max_size"]

    if "codec" in params:
        char_kwargs["codec"] = params["codec"]
    if "min_codepoint" in params:
        char_kwargs["min_codepoint"] = params["min_codepoint"]
    if "max_codepoint" in params:
        char_kwargs["max_codepoint"] = params["max_codepoint"]
    if "categories" in params:
        char_kwargs["categories"] = params["categories"]
    if "exclude_categories" in params:
        char_kwargs["exclude_categories"] = params["exclude_categories"]
    if "include_characters" in params:
        char_kwargs["include_characters"] = params["include_characters"]
    if "exclude_characters" in params:
        char_kwargs["exclude_characters"] = params["exclude_characters"]

    @settings(max_examples=test_cases, database=None)
    @given(st.text(st.characters(**char_kwargs), **text_kwargs))
    def run(value):
        codepoints = [ord(c) for c in value]
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"codepoints": codepoints}) + "\n")

    run()


if __name__ == "__main__":
    main()
