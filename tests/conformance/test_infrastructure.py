import os
import stat
import sys
import tempfile
import unicodedata

import pytest
from hypothesis import given, settings, strategies as st

from hegel.conformance import BooleanConformance, ConformanceTest, text_params_strategy
from tests.utils import find_any


def _make_conformance_binary(script_body):
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        delete=False,
        prefix="conform_",
        encoding="utf-8",
    ) as f:
        f.write(f"#!{sys.executable}\n")
        f.write("import json, os, sys\n")
        f.write("params = json.loads(sys.argv[1])\n")
        f.write("metrics_file = os.environ['CONFORMANCE_METRICS_FILE']\n")
        f.write("test_cases = int(os.environ['CONFORMANCE_TEST_CASES'])\n")
        f.write("with open(metrics_file, 'w', encoding='utf-8') as mf:\n")
        f.write(f"    {script_body}\n")
    f.close()
    os.chmod(f.name, os.stat(f.name).st_mode | stat.S_IEXEC)
    return f.name


@pytest.fixture
def conformance_binary():
    paths = []

    def make(script_body):
        path = _make_conformance_binary(script_body)
        paths.append(path)
        return path

    yield make
    for p in paths:
        os.unlink(p)


def test_nonexistent_binary():
    with pytest.raises(AssertionError):
        BooleanConformance("/nonexistent/path/to/binary")


def test_run_failure(conformance_binary):
    binary_path = conformance_binary("sys.exit(1)")
    test = BooleanConformance(binary_path, skip_server_metrics=True)
    with pytest.raises(RuntimeError, match="exit code"):
        test.run({})


def test_missing_server_metrics_raises(conformance_binary):
    binary_path = conformance_binary("mf.write(json.dumps({'value': True}) + '\\n')")
    test = BooleanConformance(binary_path)
    with pytest.raises(RuntimeError, match="Server metrics file is empty"):
        test.run({})


class _SingleEntryConformance(ConformanceTest):
    register_class = False

    def params_strategy(self):
        return st.just({})

    def validate(self, metrics_list, params) -> None:
        assert len(metrics_list) == 1


@pytest.mark.parametrize(
    "char",
    [
        "\x85",  # next line (NEL)
        "\u2028",  # line separator
        "\u2029",  # paragraph separator
    ],
)
def test_jsonl_parsing_does_not_split_on_unicode_line_boundaries(
    char, conformance_binary
):
    # Construct a raw JSON line with the literal character embedded inside a
    # string value. Python's json.dumps escapes control characters, so we
    # build the JSON by hand to simulate what a non-Python JSON library that
    # doesn't escape these characters might produce.
    s = '{"text": "hello' + char + 'world"}'
    assert len(s.splitlines()) > 1

    binary_path = conformance_binary(f"mf.write({s!r} + '\\n')")
    conformance = _SingleEntryConformance(binary_path, skip_server_metrics=True)
    conformance.run({})


@st.composite
def _text_from_params(draw, *, no_surrogates):
    params = draw(text_params_strategy(no_surrogates=no_surrogates))
    text_keys = {"min_size", "max_size"}
    text_params = {k: v for k, v in params.items() if k in text_keys}
    char_params = {k: v for k, v in params.items() if k not in text_keys}
    return draw(st.text(st.characters(**char_params), **text_params))


@settings(suppress_health_check=["too_slow"], max_examples=10)
@given(_text_from_params(no_surrogates=True))
def test_text_conformance_test_no_surrogates(s):
    assert all(unicodedata.category(c) != "Cs" for c in s)


def test_text_conformance_test_surrogates():
    find_any(
        text_params_strategy(no_surrogates=False),
        lambda p: (
            "Cs" not in p.get("exclude_categories", [])
            and "Cs" in p.get("categories", ["Cs"])
            and "codec" not in p
            and p.get("min_codepoint", 0) <= 0xD800
            and p.get("max_codepoint", 0x10FFFF) >= 0xDFFF
        ),
    )
    find_any(
        st.text(st.characters(categories=["Cs"]), min_size=1),
        lambda s: any(unicodedata.category(c) == "Cs" for c in s),
    )
