import io
import re
from fractions import Fraction

import cbor2
import pytest
from cbor2 import _decoder as cbor2_python
from hypothesis import assume, given, settings as Settings, strategies as st
from hypothesis._settings import local_settings
from hypothesis.control import _current_build_context
from hypothesis.strategies._internal.regex import IncompatibleWithAlphabet

from hegel.conformance import _character_params, text_params_strategy
from hegel.schema import HEGEL_STRING_TAG, _encode_value, from_schema


def assert_all_examples(strategy, predicate, settings=None):
    """Asserts that all examples of the given strategy match the predicate."""
    if context := _current_build_context.value:
        with local_settings(Settings(parent=settings)):
            for _ in range(20):
                s = context.data.draw(strategy)
                msg = f"Found {s!r} using strategy {strategy} which does not match"
                assert predicate(s), msg
    else:

        @given(strategy)
        @Settings(parent=settings, database=None)
        def assert_examples(s):
            msg = f"Found {s!r} using strategy {strategy} which does not match"
            assert predicate(s), msg

        assert_examples()


@st.composite
def string_schemas(draw):
    params = draw(text_params_strategy())
    return {"type": "string", **params}


@st.composite
def regex_schemas(draw):
    schema = {"type": "regex", "pattern": r"[a-z]+", "fullmatch": draw(st.booleans())}
    if draw(st.booleans()):
        schema["alphabet"] = draw(_character_params())

    try:
        from_schema(schema).validate()
    except IncompatibleWithAlphabet:
        assume(False)

    return schema


def primitive_hashable_schemas():
    return (
        st.just({"type": "null"})
        | st.just({"type": "boolean"})
        | st.builds(
            lambda min_val, max_val: {
                "type": "integer",
                "min_value": min_val,
                "max_value": max_val,
            },
            # TODO this is a bad strategy, make it better
            min_val=st.integers(-1000, 0),
            max_val=st.integers(0, 1000),
        )
        | string_schemas()
        | st.just({"type": "email"})
        | st.just({"type": "ipv4"})
        | st.just({"type": "ipv6"})
        | st.just({"type": "date"})
        | st.just({"type": "time"})
        | st.just({"type": "datetime"})
    )


def hashable_schemas():
    return st.recursive(
        primitive_hashable_schemas(),
        lambda inner: st.builds(
            lambda elements: {"type": "tuple", "elements": elements},
            elements=st.lists(inner, min_size=0, max_size=3),
        ),
    )


# Strategy that generates arbitrary valid schemas for from_schema
def schemas():
    return st.recursive(
        # Base cases: simple schemas with no nested schemas
        hashable_schemas()
        | regex_schemas()
        | st.builds(
            lambda min_val, max_val: {
                "type": "float",
                "min_value": min_val,
                "max_value": max_val,
                "allow_nan": False,
                "allow_infinity": False,
                "exclude_min": False,
                "exclude_max": False,
                "width": 64,
            },
            # TODO this is a bad strategy, make it better
            min_val=st.floats(-1000, 0, allow_nan=False),
            max_val=st.floats(0, 1000, allow_nan=False),
        )
        # const with JSON-serializable values
        | st.builds(
            lambda v: {"type": "constant", "value": v},
            v=st.none() | st.booleans() | st.integers() | st.text(max_size=5),
        )
        # sampled_from with JSON-serializable values
        | st.builds(
            lambda vs: {"type": "sampled_from", "values": vs},
            vs=st.lists(
                st.none() | st.booleans() | st.integers(),
                min_size=1,
                max_size=5,
                unique=True,
            ),
        ),
        # Recursive cases: schemas that contain other schemas
        lambda inner: (
            # list
            st.builds(
                lambda elements, max_size: {
                    "type": "list",
                    "elements": elements,
                    "min_size": 0,
                    "max_size": max_size,
                },
                elements=inner,
                max_size=st.integers(min_value=0, max_value=3),
            )
            # unique list - only hashable elements
            | st.builds(
                lambda elements, max_size: {
                    "type": "list",
                    "unique": True,
                    "elements": elements,
                    "min_size": 0,
                    "max_size": max_size,
                },
                elements=hashable_schemas(),
                max_size=st.integers(min_value=0, max_value=3),
            )
            # dict (keys must be strings for JSON)
            | st.builds(
                lambda values, max_size: {
                    "type": "dict",
                    "keys": {"type": "string", "min_size": 1, "max_size": 5},
                    "values": values,
                    "min_size": 0,
                    "max_size": max_size,
                },
                values=inner,
                max_size=st.integers(min_value=0, max_value=3),
            )
            # tuple
            | st.builds(
                lambda elements: {"type": "tuple", "elements": elements},
                elements=st.lists(inner, min_size=0, max_size=3),
            )
            # one_of
            | st.builds(
                lambda options: {"type": "one_of", "generators": options},
                options=st.lists(inner, min_size=1, max_size=3),
            )
        ),
        max_leaves=10,
    )


def test_null():
    assert_all_examples(from_schema({"type": "null"}), lambda x: x is None)


def test_boolean():
    assert_all_examples(from_schema({"type": "boolean"}), lambda x: x in [True, False])


def test_integer():
    assert_all_examples(
        from_schema({"type": "integer", "min_value": 0, "max_value": 10}),
        lambda x: isinstance(x, int) and 0 <= x <= 10,
    )


def test_rationals():
    assert_all_examples(
        from_schema({"type": "rational", "min_value": 0, "max_value": 1}),
        lambda x: isinstance(x, Fraction) and 0 <= x <= 1,
    )


def test_complex():
    assert_all_examples(
        from_schema({"type": "complex", "min_magnitude": 0, "max_magnitude": 1}),
        lambda x: isinstance(x, complex) and abs(x) <= 1,
    )


def test_float():
    assert_all_examples(
        from_schema(
            {
                "type": "float",
                "min_value": 0.0,
                "max_value": 1.0,
                "allow_nan": False,
                "allow_infinity": False,
                "exclude_min": False,
                "exclude_max": False,
                "width": 64,
            }
        ),
        lambda x: isinstance(x, float) and 0.0 <= x <= 1.0,
    )


def test_float_exclusive():
    assert_all_examples(
        from_schema(
            {
                "type": "float",
                "min_value": 0.0,
                "max_value": 1.0,
                "exclude_min": True,
                "exclude_max": True,
                "allow_nan": False,
                "allow_infinity": False,
                "width": 64,
            }
        ),
        lambda x: 0.0 < x < 1.0,
    )


def test_string():
    assert_all_examples(
        from_schema({"type": "string", "min_size": 1, "max_size": 5}),
        lambda x: isinstance(x, str) and 1 <= len(x) <= 5,
    )


def test_string_pattern():
    assert_all_examples(
        from_schema({"type": "regex", "pattern": r"^[a-z]+$", "fullmatch": True}),
        lambda x: x.isalpha() and x.islower(),
    )


def test_email():
    assert_all_examples(from_schema({"type": "email"}), lambda x: "@" in x)


def test_ipv4():
    def check(x):
        parts = x.split(".")
        return len(parts) == 4 and all(0 <= int(p) <= 255 for p in parts)

    assert_all_examples(from_schema({"type": "ipv4"}), check)


def test_ipv6():
    assert_all_examples(from_schema({"type": "ipv6"}), lambda x: ":" in x)


def test_date():
    assert_all_examples(
        from_schema({"type": "date"}),
        lambda x: re.match(r"^\d{4}-\d{2}-\d{2}$", x),
    )


def test_time():
    assert_all_examples(
        from_schema({"type": "time"}),
        lambda x: re.match(r"^\d{2}:\d{2}:\d{2}", x),
    )


def test_datetime():
    assert_all_examples(from_schema({"type": "datetime"}), lambda x: "T" in x)


@given(st.integers() | st.text())
def test_const(v):
    assert_all_examples(from_schema({"type": "constant", "value": v}), lambda x: x == v)


def test_sampled_from():
    assert_all_examples(
        from_schema({"type": "sampled_from", "values": [1, 2, 3]}),
        lambda x: x in [1, 2, 3],
    )


def test_one_of():
    assert_all_examples(
        from_schema(
            {"type": "one_of", "generators": [{"type": "boolean"}, {"type": "null"}]}
        ),
        lambda x: x is None or isinstance(x, bool),
    )


def test_list():
    assert_all_examples(
        from_schema({"type": "list", "elements": {"type": "integer"}, "min_size": 0}),
        lambda x: isinstance(x, list) and all(isinstance(i, int) for i in x),
    )


def test_list_size():
    assert_all_examples(
        from_schema(
            {
                "type": "list",
                "elements": {"type": "integer"},
                "min_size": 2,
                "max_size": 5,
            }
        ),
        lambda x: 2 <= len(x) <= 5,
    )


def test_unique_list():
    assert_all_examples(
        from_schema(
            {
                "type": "list",
                "unique": True,
                "elements": {"type": "integer", "min_value": 0, "max_value": 100},
                "min_size": 5,
                "max_size": 10,
            }
        ),
        lambda x: isinstance(x, list) and len(x) == len(set(x)) and 5 <= len(x) <= 10,
    )


def test_dict():
    def check(x):
        return (
            isinstance(x, list)
            and all(isinstance(kv, tuple) and len(kv) == 2 for kv in x)
            and all(isinstance(k, str) and len(k) >= 1 for k, _ in x)
            and all(isinstance(val, int) for _, val in x)
        )

    assert_all_examples(
        from_schema(
            {
                "type": "dict",
                "keys": {"type": "string", "min_size": 1},
                "values": {"type": "integer"},
                "min_size": 0,
            }
        ),
        check,
    )


def test_dict_size():
    assert_all_examples(
        from_schema(
            {
                "type": "dict",
                "keys": {"type": "string", "min_size": 0},
                "values": {"type": "integer"},
                "min_size": 1,
                "max_size": 3,
            }
        ),
        lambda x: 1 <= len(x) <= 3,
    )


def test_dict_default_keys():
    def check(x):
        return isinstance(x, list) and all(isinstance(k, str) for k, _ in x)

    assert_all_examples(
        from_schema(
            {
                "type": "dict",
                "keys": {"type": "string", "min_size": 0},
                "values": {"type": "integer"},
                "min_size": 0,
            }
        ),
        check,
    )


def test_tuple():
    def check(x):
        return (
            isinstance(x, tuple)
            and len(x) == 3
            and isinstance(x[0], int)
            and isinstance(x[1], str)
            and isinstance(x[2], bool)
        )

    assert_all_examples(
        from_schema(
            {
                "type": "tuple",
                "elements": [
                    {"type": "integer"},
                    {"type": "string", "min_size": 0},
                    {"type": "boolean"},
                ],
            }
        ),
        check,
    )


def test_tuple_empty():
    assert_all_examples(
        from_schema({"type": "tuple", "elements": []}), lambda x: x == ()
    )


def test_unique_list_of_tuples():
    def check(x):
        return isinstance(x, list) and all(isinstance(elem, tuple) for elem in x)

    assert_all_examples(
        from_schema(
            {
                "type": "list",
                "unique": True,
                "elements": {
                    "type": "tuple",
                    "elements": [{"type": "integer"}, {"type": "integer"}],
                },
                "min_size": 0,
            }
        ),
        check,
    )


def test_nested_dict_of_lists():
    def check(x):
        if not isinstance(x, list) or not 1 <= len(x) <= 2:
            return False
        for key, val in x:
            if not isinstance(key, str) or not isinstance(val, list) or len(val) > 5:
                return False
        return True

    assert_all_examples(
        from_schema(
            {
                "type": "dict",
                "keys": {"type": "string", "min_size": 1},
                "values": {
                    "type": "list",
                    "elements": {"type": "integer"},
                    "min_size": 0,
                    "max_size": 5,
                },
                "min_size": 1,
                "max_size": 2,
            }
        ),
        check,
    )


def _assert_no_strings(value: object) -> None:
    assert not isinstance(value, str), value
    if isinstance(value, list):
        for v in value:
            _assert_no_strings(v)
    if isinstance(value, tuple):
        for v in value:
            _assert_no_strings(v)
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_no_strings(k)
            _assert_no_strings(v)


@Settings(deadline=None)
@given(st.data())
def test_from_schema(data):
    schema = data.draw(schemas())
    value = data.draw(from_schema(schema))
    value = _encode_value(value)
    # all strings should have been encoded to our custom HEGEL_STRING_TAG.
    _assert_no_strings(value)

    def tag_hook(decoder, tag):
        # Unsigned bignum, negative bignum, and custom encoding for utf8 + surrogates
        # respectively
        # https://www.iana.org/assignments/cbor-tags/cbor-tags.xhtml
        if tag.tag in {2, 3, HEGEL_STRING_TAG}:
            return
        raise AssertionError(f"Saw CBOR tag {tag}")

    encoded = cbor2.dumps(value)
    # use the pure-python implementation instead of the default c one so we can
    # monkeypatch its automatic conversion of tags to types to disable it and forward
    # everything to tag_hook.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cbor2_python, "semantic_decoders", {})
        cbor2_python.CBORDecoder(io.BytesIO(encoded), tag_hook=tag_hook).decode()


def test_invalid_schema():
    with pytest.raises(KeyError):
        from_schema({})
    with pytest.raises(ValueError, match="Unsupported schema"):
        from_schema({"type": "unknown"})


@given(from_schema({"type": "binary", "min_size": 1, "max_size": 10}))
def test_binary_schema(example):
    assert isinstance(example, bytes)
    assert 1 <= len(example) <= 10


@given(from_schema({"type": "url"}))
def test_url_schema(example):
    assert "://" in example


@given(from_schema({"type": "domain", "max_length": 255}))
def test_domain_schema(example):
    assert "." in example or len(example) > 0


@given(from_schema({"type": "domain", "max_length": 50}))
def test_domain_with_max_length(example):
    assert len(example) <= 50


def test_float_defaults():
    assert_all_examples(
        from_schema({"type": "float"}),
        lambda x: isinstance(x, float),
    )


def test_string_min_size_default():
    assert_all_examples(
        from_schema({"type": "string"}),
        lambda x: isinstance(x, str),
    )


def test_binary_min_size_default():
    assert_all_examples(
        from_schema({"type": "binary"}),
        lambda x: isinstance(x, bytes),
    )


def test_regex_fullmatch_default():
    assert_all_examples(
        from_schema({"type": "regex", "pattern": r"[a-z]+"}),
        lambda x: isinstance(x, str),
    )


def test_list_min_size_default():
    assert_all_examples(
        from_schema({"type": "list", "elements": {"type": "integer"}}),
        lambda x: isinstance(x, list),
    )


def test_unique_list_min_size_default():
    assert_all_examples(
        from_schema(
            {
                "type": "list",
                "unique": True,
                "elements": {"type": "integer", "min_value": 0, "max_value": 100},
            }
        ),
        lambda x: isinstance(x, list),
    )


def test_dict_min_size_default():
    assert_all_examples(
        from_schema(
            {
                "type": "dict",
                "keys": {"type": "string"},
                "values": {"type": "integer"},
            }
        ),
        lambda x: isinstance(x, list),
    )


def test_domain_max_length_default():
    assert_all_examples(
        from_schema({"type": "domain"}),
        lambda x: isinstance(x, str),
    )
