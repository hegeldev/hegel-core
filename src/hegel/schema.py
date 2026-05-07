import hashlib
import json
from fractions import Fraction
from typing import Any

from cbor2 import CBORTag
from hypothesis import strategies as st
from hypothesis.internal.cache import LRUCache
from hypothesis.internal.conjecture.data import ConjectureData
from hypothesis.provisional import domains, urls
from hypothesis.strategies import SearchStrategy

FROM_SCHEMA_CACHE: LRUCache = LRUCache(1024)


class BooleansStrategy(SearchStrategy[bool]):
    def __init__(self, p: float):
        super().__init__()
        self.p = p

    def do_draw(self, data: ConjectureData) -> bool:
        return data.draw_boolean(p=self.p)


# used to allow encoding of surrogate code points. See https://github.com/hegeldev/hegel-core/pull/72
HEGEL_STRING_TAG = 91


def _encode_value(value: object) -> object:
    # prepare a final generated value for transport in the protocol.
    if isinstance(value, str):
        # this is sometimes called "WTF-8" encoding: https://wtf-8.codeberg.page/. It is
        # identical to UTF-8 except it drops the well-formed requirement that surrogates
        # not appear.
        return CBORTag(HEGEL_STRING_TAG, value.encode("utf-8", "surrogatepass"))
    if isinstance(value, list):
        return [_encode_value(v) for v in value]
    if isinstance(value, tuple):
        return [_encode_value(v) for v in value]
    # nocover because we don't actually generate dicts yet
    if isinstance(value, dict):  # pragma: no cover
        return {_encode_value(k): _encode_value(v) for k, v in value.items()}
    if isinstance(value, Fraction):
        return (value.numerator, value.denominator)
    if isinstance(value, complex):
        return (value.real, value.imag)
    return value


def _from_schema(schema: dict[str, Any]) -> SearchStrategy[Any]:
    schema_type = schema["type"]

    if schema_type == "constant":
        return st.just(schema["value"])
    if schema_type == "one_of":
        return st.one_of(
            [
                st.tuples(st.just(i), _from_schema(s))
                for i, s in enumerate(schema["generators"])
            ]
        )
    if schema_type == "boolean":
        return BooleansStrategy(schema.get("p", 0.5))
    if schema_type == "integer":
        return st.integers(
            min_value=schema.get("min_value"),
            max_value=schema.get("max_value"),
        )
    if schema_type == "float":
        return st.floats(
            schema.get("min_value"),
            schema.get("max_value"),
            allow_nan=schema.get("allow_nan"),
            allow_infinity=schema.get("allow_infinity"),
            width=schema.get("width", 64),
            exclude_min=schema.get("exclude_min", False),
            exclude_max=schema.get("exclude_max", False),
        )
    if schema_type == "fraction":
        return st.fractions(
            min_value=schema.get("min_value"),
            max_value=schema.get("max_value"),
            max_denominator=schema.get("max_denominator"),
        )
    if schema_type == "complex":
        return st.complex_numbers(
            min_magnitude=schema.get("min_magnitude", 0),
            max_magnitude=schema.get("max_magnitude"),
            allow_infinity=schema.get("allow_infinity"),
            allow_nan=schema.get("allow_nan"),
            allow_subnormal=schema.get("allow_subnormal", True),
            width=schema.get("width", 128),
        )
    if schema_type == "string":
        characters_schema = {
            k: v for k, v in schema.items() if k not in {"type", "min_size", "max_size"}
        }
        return st.text(
            alphabet=st.characters(**characters_schema),
            min_size=schema.get("min_size", 0),
            max_size=schema.get("max_size"),
        )
    if schema_type == "binary":
        return st.binary(
            min_size=schema.get("min_size", 0),
            max_size=schema.get("max_size"),
        )
    if schema_type == "regex":
        alphabet_schema = schema.get("alphabet")
        return st.from_regex(
            schema["pattern"],
            fullmatch=schema.get("fullmatch", False),
            alphabet=(
                None if alphabet_schema is None else st.characters(**alphabet_schema)
            ),
        )
    if schema_type == "list":
        return st.lists(
            _from_schema(schema["elements"]),
            min_size=schema.get("min_size", 0),
            max_size=schema.get("max_size"),
            unique=schema.get("unique", False),
        )
    if schema_type == "dict":
        return st.dictionaries(
            keys=_from_schema(schema["keys"]),
            values=_from_schema(schema["values"]),
            min_size=schema.get("min_size", 0),
            max_size=schema.get("max_size"),
        ).map(lambda d: list(d.items()))
    if schema_type == "tuple":
        elements = [_from_schema(s) for s in schema["elements"]]
        return st.tuples(*elements)
    if schema_type == "email":
        return st.emails()
    if schema_type == "url":
        return urls()
    if schema_type == "domain":
        return domains(max_length=schema.get("max_length", 255))
    if schema_type == "ip_address":
        return st.ip_addresses(v=schema["version"]).map(str)
    if schema_type == "date":
        return st.dates().map(lambda d: d.isoformat())
    if schema_type == "time":
        return st.times().map(lambda t: t.isoformat())
    if schema_type == "datetime":
        return st.datetimes().map(lambda dt: dt.isoformat())
    if schema_type == "uuid":
        return st.uuids(version=schema.get("version")).map(str)

    raise ValueError(f"Unsupported schema: {schema}")


def from_schema(schema: dict[str, Any]) -> SearchStrategy[Any]:
    key = json.dumps(schema, sort_keys=True).encode("utf-8")
    key = hashlib.sha1(key).digest()[:32]
    try:
        return FROM_SCHEMA_CACHE[key]
    except KeyError:
        result = _from_schema(schema)
        FROM_SCHEMA_CACHE[key] = result
        return result
