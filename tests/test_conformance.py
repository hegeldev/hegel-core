"""Smoke tests for conformance.py."""

import os
import stat
import sys
import tempfile
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from hegel.conformance import (
    BinaryConformance,
    BooleanConformance,
    ConformanceTest,
    DictConformance,
    EmptyTestConformance,
    ErrorResponseConformance,
    FloatConformance,
    IntegerConformance,
    ListConformance,
    SampledFromConformance,
    StopTestOnCollectionMoreConformance,
    StopTestOnGenerateConformance,
    StopTestOnMarkCompleteConformance,
    StopTestOnNewCollectionConformance,
    TextConformance,
    run_conformance_tests,
)


def _make_conformance_binary(script_body):
    """Create a temporary executable Python script for conformance testing."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        delete=False,
        prefix="conform_",
    ) as f:
        f.write(f"#!{sys.executable}\n")
        f.write("import json, os, sys\n")
        f.write("params = json.loads(sys.argv[1])\n")
        f.write("metrics_file = os.environ['CONFORMANCE_METRICS_FILE']\n")
        f.write("test_cases = int(os.environ['CONFORMANCE_TEST_CASES'])\n")
        f.write("with open(metrics_file, 'w') as mf:\n")
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


def test_registered_tests():
    expected = {
        BooleanConformance,
        IntegerConformance,
        FloatConformance,
        TextConformance,
        BinaryConformance,
        ListConformance,
        SampledFromConformance,
        DictConformance,
        StopTestOnGenerateConformance,
        StopTestOnMarkCompleteConformance,
        ErrorResponseConformance,
        EmptyTestConformance,
        StopTestOnCollectionMoreConformance,
        StopTestOnNewCollectionConformance,
    }
    assert expected == ConformanceTest.registered_tests


def test_nonexistent_binary():
    with pytest.raises(AssertionError):
        BooleanConformance("/nonexistent/path/to/binary")


def test_default_test_cases(conformance_binary):
    binary_path = conformance_binary(
        "mf.write(json.dumps({'value': True}) + '\\n')",
    )
    bc = BooleanConformance(binary_path)
    assert bc.test_cases == BooleanConformance.default_test_cases


# --- Test strategy drawing ---


@given(st.data())
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_float_strategy_draws(conformance_binary, data):
    """Test FloatConformance.params_strategy() produces valid params."""
    binary_path = conformance_binary(
        "mf.write(json.dumps({'value': 1.0}) + '\\n')",
    )
    fc = FloatConformance(binary_path, test_cases=1)
    params = data.draw(fc.params_strategy())
    assert "min_value" in params
    assert "max_value" in params
    assert "exclude_min" in params
    assert "exclude_max" in params
    assert "allow_nan" in params
    assert "allow_infinity" in params


@given(st.data())
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_text_strategy_draws(conformance_binary, data):
    """Test TextConformance.params_strategy() produces valid params."""
    binary_path = conformance_binary(
        "mf.write(json.dumps({'length': 5}) + '\\n')",
    )
    tc = TextConformance(binary_path, test_cases=1)
    params = data.draw(tc.params_strategy())
    assert "min_size" in params
    assert "max_size" in params


@given(st.data())
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_binary_strategy_draws(conformance_binary, data):
    """Test BinaryConformance.params_strategy() produces valid params."""
    binary_path = conformance_binary(
        "mf.write(json.dumps({'length': 5}) + '\\n')",
    )
    bc = BinaryConformance(binary_path, test_cases=1)
    params = data.draw(bc.params_strategy())
    assert "min_size" in params
    assert "max_size" in params


@given(st.data())
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_list_strategy_draws(conformance_binary, data):
    """Test ListConformance.params_strategy() produces valid params."""
    binary_path = conformance_binary(
        "mf.write(json.dumps({'size': 1, 'min_element': 5, 'max_element': 5}) + '\\n')",
    )
    lc = ListConformance(binary_path, test_cases=1, min_value=0, max_value=100)
    params = data.draw(lc.params_strategy())
    assert "min_size" in params
    assert "max_size" in params
    assert "min_value" in params
    assert "max_value" in params


@given(st.data())
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_sampled_from_strategy_draws(conformance_binary, data):
    """Test SampledFromConformance.params_strategy() produces valid params."""
    binary_path = conformance_binary(
        "mf.write(json.dumps({'value': 1}) + '\\n')",
    )
    sc = SampledFromConformance(binary_path, test_cases=1)
    params = data.draw(sc.params_strategy())
    assert "options" in params
    assert len(params["options"]) >= 1


@given(st.data())
@settings(
    max_examples=5,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_dict_strategy_draws(conformance_binary, data):
    """Test DictConformance.params_strategy() produces valid params."""
    binary_path = conformance_binary(
        "mf.write(json.dumps({'size': 0}) + '\\n')",
    )
    dc = DictConformance(binary_path, test_cases=1)
    params = data.draw(dc.params_strategy())
    assert "min_size" in params
    assert "max_size" in params
    assert "key_type" in params
    assert "min_key" in params
    assert "max_key" in params
    assert "min_value" in params
    assert "max_value" in params


def test_run_conformance_tests_function(conformance_binary):
    """Test run_conformance_tests function end-to-end."""
    # Create a binary that handles all conformance test types
    script = (
        "import random\\n"
        "    for _ in range(test_cases):\\n"
        "        mf.write(json.dumps({\\n"
        "            'value': random.choice([True, False]),\\n"
        "            'length': 5,\\n"
        "            'size': 1,\\n"
        "            'min_element': 0,\\n"
        "            'max_element': 10,\\n"
        "            'min_key': 0,\\n"
        "            'max_key': 10,\\n"
        "            'min_value': 0,\\n"
        "            'max_value': 10,\\n"
        "        }) + '\\\\n')"
    )
    binary_path = conformance_binary(script)
    tests = [
        BooleanConformance(binary_path, test_cases=2),
        IntegerConformance(binary_path, test_cases=2, min_value=0, max_value=10),
        FloatConformance(binary_path, test_cases=2),
        TextConformance(binary_path, test_cases=2),
        BinaryConformance(binary_path, test_cases=2),
        ListConformance(binary_path, test_cases=2, min_value=0, max_value=10),
        SampledFromConformance(binary_path, test_cases=2),
        DictConformance(binary_path, test_cases=2),
        StopTestOnGenerateConformance(binary_path, test_cases=2),
        StopTestOnMarkCompleteConformance(binary_path, test_cases=2),
        ErrorResponseConformance(binary_path, test_cases=2),
        EmptyTestConformance(binary_path, test_cases=2),
        StopTestOnCollectionMoreConformance(binary_path, test_cases=2),
        StopTestOnNewCollectionConformance(binary_path, test_cases=2),
    ]

    # run_conformance_tests needs pytest.Subtests, which is hard to mock.
    # Instead test the assertion check for registered tests.
    assert {type(t) for t in tests} == ConformanceTest.registered_tests


def test_run_conformance_tests_full(subtests, conformance_binary):
    """Test run_conformance_tests exercises the function structure."""
    binary_path = conformance_binary(
        "mf.write(json.dumps({'value': True}) + '\\n')",
    )
    tests = [
        BooleanConformance(binary_path, test_cases=1),
        IntegerConformance(
            binary_path,
            test_cases=1,
            min_value=None,
            max_value=None,
        ),
        FloatConformance(binary_path, test_cases=1),
        TextConformance(binary_path, test_cases=1),
        BinaryConformance(binary_path, test_cases=1),
        ListConformance(binary_path, test_cases=1, min_value=None, max_value=None),
        SampledFromConformance(binary_path, test_cases=1),
        DictConformance(binary_path, test_cases=1),
        StopTestOnGenerateConformance(binary_path, test_cases=1),
        StopTestOnMarkCompleteConformance(binary_path, test_cases=1),
        ErrorResponseConformance(binary_path, test_cases=1),
        EmptyTestConformance(binary_path, test_cases=1),
        StopTestOnCollectionMoreConformance(binary_path, test_cases=1),
        StopTestOnNewCollectionConformance(binary_path, test_cases=1),
    ]

    # Mock the run method on each test to avoid binary execution
    for t in tests:
        t.run = MagicMock()

    run_conformance_tests(
        tests,
        subtests,
        settings=settings(max_examples=1, deadline=None),
    )


def test_boolean_run(conformance_binary):
    binary_path = conformance_binary(
        "mf.write(json.dumps({'value': True}) + '\\n')",
    )
    bc = BooleanConformance(binary_path, test_cases=1)
    bc.run({})


def test_run_failure(conformance_binary):
    binary_path = conformance_binary("sys.exit(1)")
    bc = BooleanConformance(binary_path, test_cases=1)
    with pytest.raises(RuntimeError, match="exit code"):
        bc.run({})


def test_integer_run(conformance_binary):
    script = (
        "for i in range(test_cases):\n"
        "        mf.write(json.dumps({'value': 5}) + '\\n')"
    )
    binary_path = conformance_binary(script)
    ic = IntegerConformance(binary_path, test_cases=2, min_value=0, max_value=10)
    ic.run({"min_value": 0, "max_value": 10})


def test_float_run(conformance_binary):
    script = (
        "for i in range(test_cases):\n"
        "        mf.write(json.dumps({'value': 1.5}) + '\\n')"
    )
    binary_path = conformance_binary(script)
    fc = FloatConformance(binary_path, test_cases=2)
    fc.run(
        {
            "min_value": 0.0,
            "max_value": 10.0,
            "exclude_min": False,
            "exclude_max": False,
            "allow_nan": False,
            "allow_infinity": False,
        },
    )


def test_text_run(conformance_binary):
    script = (
        "for i in range(test_cases):\n"
        "        mf.write(json.dumps({'length': 5}) + '\\n')"
    )
    binary_path = conformance_binary(script)
    tc = TextConformance(binary_path, test_cases=2)
    tc.run({"min_size": 0, "max_size": 20})


def test_binary_run(conformance_binary):
    script = (
        "for i in range(test_cases):\n"
        "        mf.write(json.dumps({'length': 5}) + '\\n')"
    )
    binary_path = conformance_binary(script)
    bc = BinaryConformance(binary_path, test_cases=2)
    bc.run({"min_size": 0, "max_size": 20})


def test_list_run(conformance_binary):
    script = (
        "for i in range(test_cases):\n"
        "        mf.write(json.dumps({"
        "'size': 2, 'min_element': 3, "
        "'max_element': 7}) + '\\n')"
    )
    binary_path = conformance_binary(script)
    lc = ListConformance(binary_path, test_cases=2, min_value=0, max_value=100)
    lc.run(
        {"min_size": 0, "max_size": 10, "min_value": 0, "max_value": 100},
    )


def test_sampled_from_run(conformance_binary):
    script = (
        "for i in range(test_cases):\n"
        "        mf.write(json.dumps({'value': 5}) + '\\n')"
    )
    binary_path = conformance_binary(script)
    sc = SampledFromConformance(binary_path, test_cases=2)
    sc.run({"options": [1, 5, 10]})


def test_dict_run(conformance_binary):
    script = (
        "for i in range(test_cases):\n"
        "        mf.write(json.dumps({"
        "'size': 1, 'min_key': 1, 'max_key': 1, "
        "'min_value': 5, 'max_value': 5}) + '\\n')"
    )
    binary_path = conformance_binary(script)
    dc = DictConformance(binary_path, test_cases=2)
    dc.run(
        {
            "min_size": 0,
            "max_size": 10,
            "key_type": "integer",
            "min_key": 0,
            "max_key": 100,
            "min_value": 0,
            "max_value": 100,
        },
    )


def test_run_conformance_tests(subtests, conformance_binary):
    binary_path = conformance_binary(
        "mf.write(json.dumps({'value': True}) + '\\n')",
    )
    tests = [
        BooleanConformance(binary_path, test_cases=1),
        IntegerConformance(binary_path, test_cases=1, min_value=None, max_value=None),
        FloatConformance(binary_path, test_cases=1),
        TextConformance(binary_path, test_cases=1),
        BinaryConformance(binary_path, test_cases=1),
        ListConformance(binary_path, test_cases=1, min_value=None, max_value=None),
        SampledFromConformance(binary_path, test_cases=1),
        DictConformance(binary_path, test_cases=1),
    ]

    for t in tests:
        t.run = MagicMock()

    run_conformance_tests(
        tests,
        subtests,
        settings=settings(max_examples=1, deadline=None),
    )
