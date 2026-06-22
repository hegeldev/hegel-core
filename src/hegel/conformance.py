import json
import os
import subprocess
import sys
import tempfile
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Collection, Sequence
from encodings.aliases import aliases
from pathlib import Path
from typing import Any, ClassVar

import pytest
from hypothesis import (
    Phase,
    assume,
    currently_in_test_context,
    given,
    note,
    settings as Settings,
    strategies as st,
)
from hypothesis.errors import InvalidArgument
from hypothesis.internal import charmap
from hypothesis.internal.conjecture.data import Status


def _can_encode(codec: str) -> bool:
    try:
        "".encode(codec)
        return True
    except Exception:
        return False


ALL_CATEGORIES = list(charmap.categories())
ALL_CODECS = sorted({c for c in set(aliases).union(aliases.values()) if _can_encode(c)})


# Recommended integer bounds for conformance testing, by language capability.
# Languages with fixed-width integers should use the appropriate constant.
# Languages with arbitrary-precision integers MUST use BIGINT bounds to exercise
# CBOR bignum tag decoding (tags 2 and 3, triggered at values ≥ 2^64).
# Using narrower bounds hides a class of CBOR decoding bugs where the library
# silently produces wrong types for large values.
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
BIGINT_MIN = -(2**128)
BIGINT_MAX = 2**128


@st.composite
def _character_params(
    draw: st.DrawFn, *, no_surrogates: bool = False
) -> dict[str, Any]:
    params: dict[str, Any] = {}

    use_codec = draw(st.booleans())
    use_min_codepoint = draw(st.booleans())
    use_max_codepoint = draw(st.booleans())
    use_categories = draw(st.booleans())
    use_exclude_categories = draw(st.booleans())
    use_exclude_chars = draw(st.booleans())
    use_include_chars = draw(st.booleans())

    # categories and exclude_categories are mutually exclusive
    assume(not (use_categories and use_exclude_categories))

    if use_codec:
        params["codec"] = draw(st.sampled_from(ALL_CODECS))

    if use_min_codepoint:
        params["min_codepoint"] = draw(st.integers(0, 0x10FFFF))

    if use_max_codepoint:
        lo = params.get("min_codepoint", 0)
        params["max_codepoint"] = draw(st.integers(lo, 0x10FFFF))

    if use_categories:
        params["categories"] = draw(st.lists(st.sampled_from(ALL_CATEGORIES)))

    if use_exclude_categories:
        params["exclude_categories"] = draw(st.lists(st.sampled_from(ALL_CATEGORIES)))

    if no_surrogates:
        if use_categories:
            params["categories"] = [c for c in params["categories"] if c != "Cs"]
        else:
            exclude_categories = set(params.get("exclude_categories", [])) | {"Cs"}
            # make json serializable
            params["exclude_categories"] = list(exclude_categories)

    if use_exclude_chars:
        params["exclude_characters"] = draw(st.text())

    if use_include_chars:
        params["include_characters"] = draw(st.text())

    # reject invalid combinations
    try:
        st.characters(**params).validate()
    except (InvalidArgument, ValueError):
        assume(False)

    return params


@st.composite
def text_params_strategy(
    draw: st.DrawFn, *, no_surrogates: bool = False
) -> dict[str, Any]:
    char_params = draw(_character_params(no_surrogates=no_surrogates))
    min_size = draw(st.integers(0, 20))
    max_size = draw(st.none() | st.integers(min_size, 20))
    params: dict[str, Any] = {"min_size": min_size, **char_params}
    if max_size is not None:
        params["max_size"] = max_size
    return params


@st.composite
def _integer_params_strategy(
    draw: st.DrawFn,
    min_value: int | None,
    max_value: int | None,
) -> dict[str, Any]:
    drawn_min = min_value
    drawn_max = max_value

    use_min = draw(st.booleans())
    use_max = draw(st.booleans())

    if min_value is not None and use_min:
        drawn_min = draw(st.integers(min_value=min_value, max_value=max_value))
    if max_value is not None and use_max:
        lower = drawn_min if drawn_min is not None else min_value
        drawn_max = draw(st.integers(min_value=lower, max_value=max_value))

    return {"min_value": drawn_min, "max_value": drawn_max}


class ConformanceTest(ABC):
    default_test_cases: int = 50
    modes: ClassVar[list[str] | None] = None
    registered_tests: ClassVar[set[type["ConformanceTest"]]] = set()

    def __init_subclass__(cls) -> None:
        if cls.__dict__.get("register_class", True):
            cls.registered_tests.add(cls)

    def __init__(
        self,
        binary_path: str | Path,
        test_cases: int | None = None,
        *,
        skip_server_metrics: bool = False,
    ) -> None:
        self.binary = Path(binary_path)
        assert self.binary.exists()
        self.test_cases = test_cases or self.default_test_cases
        self.skip_server_metrics = skip_server_metrics

    @abstractmethod
    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        """Return a strategy for generating test parameters."""
        ...

    @abstractmethod
    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        """Validate that the library output matches the expected constraints."""
        ...

    def extra_env(self) -> dict[str, str]:
        """Return additional environment variables for the library binary."""
        return {}

    def run(
        self,
        params: dict[str, Any],
        *,
        command_prefix: Sequence[str] | None = None,
    ) -> None:
        """Run the library binary and validate its output."""
        if command_prefix is None:
            command_prefix = [sys.executable] if self.binary.suffix == ".py" else []
        # Use delete=False because on Windows, NamedTemporaryFile holds an
        # exclusive lock that prevents the subprocess from opening the file.
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        sf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        rf = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        try:
            metrics_file = Path(f.name)
            server_metrics_file = Path(sf.name)
            run_metrics_file = Path(rf.name)
            f.close()
            sf.close()
            rf.close()
            input_json = json.dumps(params)

            result = subprocess.run(
                [*command_prefix, str(self.binary), input_json],
                env={
                    **os.environ,
                    "CONFORMANCE_METRICS_FILE": str(metrics_file),
                    "CONFORMANCE_TEST_CASES": str(self.test_cases),
                    "CONFORMANCE_SERVER_METRICS_FILE": str(server_metrics_file),
                    "CONFORMANCE_SERVER_RUN_METRICS_FILE": str(run_metrics_file),
                    **self.extra_env(),
                },
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

            if result.returncode != 0:
                raise RuntimeError(
                    f"Library binary failed with exit code {result.returncode}\n"
                    f"stdout: {result.stdout}\n"
                    f"stderr: {result.stderr}",
                )

            metrics = [
                json.loads(line)
                for line in metrics_file.read_text(encoding="utf-8").split("\n")
                if line
            ]

            server_metrics_text = server_metrics_file.read_text(encoding="utf-8")
            if server_metrics_text:
                server_metrics = [
                    json.loads(line) for line in server_metrics_text.split("\n") if line
                ]
                assert len(server_metrics) == len(metrics)
                paired = []
                for client_m, server_m in zip(metrics, server_metrics, strict=True):
                    server_m["status"] = Status(server_m["status"])
                    # skip these for now, we might do something with them in the future
                    if server_m["status"] in (Status.INVALID, Status.OVERRUN):
                        continue
                    client_m.update(server_m)
                    paired.append(client_m)
                metrics = paired
            elif not self.skip_server_metrics:
                raise RuntimeError(
                    "Server metrics file is empty. The library binary should "
                    "start the hegel server, which writes per-test-case "
                    "generate counts to CONFORMANCE_SERVER_METRICS_FILE. "
                    "If this binary does not use the hegel server, pass "
                    "skip_server_metrics=True."
                )

            run_metrics_text = run_metrics_file.read_text(encoding="utf-8").strip()
            self.run_metrics: dict[str, Any] = (
                json.loads(run_metrics_text) if run_metrics_text else {}
            )
        finally:
            metrics_file.unlink(missing_ok=True)
            server_metrics_file.unlink(missing_ok=True)
            run_metrics_file.unlink(missing_ok=True)

        self.validate(metrics, params)


class ErrorHandlingConformance(ConformanceTest):
    """Base class for error handling conformance tests.

    These tests set HEGEL_PROTOCOL_TEST_MODE to activate the test server,
    which injects specific error conditions. The library binary is run
    with empty params and must exit cleanly (exit code 0).
    """

    register_class: ClassVar[bool] = False
    test_mode: str

    def extra_env(self) -> dict[str, str]:
        return {"HEGEL_PROTOCOL_TEST_MODE": self.test_mode}

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        return st.just({})

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        pass


class StopTestOnGenerateConformance(ErrorHandlingConformance):
    """Conformance test for StopTest error on generate command."""

    test_mode = "stop_test_on_generate"


class StopTestOnMarkCompleteConformance(ErrorHandlingConformance):
    """Conformance test for StopTest error on mark_complete command."""

    test_mode = "stop_test_on_mark_complete"


class ErrorResponseConformance(ErrorHandlingConformance):
    """Conformance test for generic error response handling."""

    test_mode = "error_response"


class EmptyTestConformance(ErrorHandlingConformance):
    """Conformance test for empty test run (no test cases)."""

    test_mode = "empty_test"


class StopTestOnCollectionMoreConformance(ErrorHandlingConformance):
    """Conformance test for StopTest error on collection_more command."""

    test_mode = "stop_test_on_collection_more"


class StopTestOnNewCollectionConformance(ErrorHandlingConformance):
    """Conformance test for StopTest error on new_collection command."""

    test_mode = "stop_test_on_new_collection"


class BooleanConformance(ConformanceTest):
    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        return st.just({})

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            assert metrics["value"] in (True, False)


class IntegerConformance(ConformanceTest):
    def __init__(
        self,
        binary_path: str | Path,
        test_cases: int | None = None,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        skip_server_metrics: bool = False,
    ) -> None:
        super().__init__(
            binary_path, test_cases, skip_server_metrics=skip_server_metrics
        )
        self.min_value = min_value
        self.max_value = max_value

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        return _integer_params_strategy(self.min_value, self.max_value)

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            value = metrics["value"]
            if params["min_value"] is not None:
                assert value >= params["min_value"]
            if params["max_value"] is not None:
                assert value <= params["max_value"]


class FloatConformance(ConformanceTest):
    default_test_cases = 500  # NaN/infinity are rare, need more samples

    def __init__(
        self,
        binary_path: str | Path,
        test_cases: int = default_test_cases,
        *,
        allow_infinity: bool | None = None,
        allow_nan: bool | None = None,
        skip_server_metrics: bool = False,
    ):
        super().__init__(
            binary_path, test_cases, skip_server_metrics=skip_server_metrics
        )
        self.allow_infinity = allow_infinity
        self.allow_nan = allow_nan

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        @st.composite
        def strategy(draw: st.DrawFn) -> dict[str, Any]:
            use_min_value = draw(st.booleans())
            use_max_value = draw(st.booleans())

            min_value = None
            max_value = None

            if use_min_value:
                min_value = draw(
                    st.floats(
                        min_value=-1e6,
                        max_value=1e6,
                        allow_nan=False,
                        allow_infinity=False,
                    ),
                )

            if use_max_value:
                min_val = min_value if min_value is not None else -1e6
                max_value = draw(
                    st.floats(
                        min_value=min_val,
                        max_value=1e6,
                        allow_nan=False,
                        allow_infinity=False,
                    ),
                )

            # exclude_min/max only meaningful with bounds
            exclude_min = draw(st.booleans()) if use_min_value else False
            exclude_max = draw(st.booleans()) if use_max_value else False

            # Can't exclude both when min == max
            if (
                min_value is not None
                and max_value is not None
                and min_value == max_value
            ):
                exclude_min = False
                exclude_max = False

            # allow_nan/allow_infinity are ternary: True, False, or None.
            # None means the conformance binary should not call the setter,
            # letting the library apply its own defaults (which must match
            # Hypothesis: nan disallowed when any bound is set, infinity
            # disallowed when both bounds are set).
            allow_nan = self.allow_nan
            if allow_nan is None:
                allow_nan = draw(
                    st.sampled_from(
                        [None, False]
                        + ([] if (use_min_value or use_max_value) else [True])
                    ),
                )

            allow_infinity = self.allow_infinity
            if allow_infinity is None:
                allow_infinity = draw(
                    st.sampled_from(
                        [None, False]
                        + ([] if (use_min_value and use_max_value) else [True])
                    ),
                )

            return {
                "min_value": min_value,
                "max_value": max_value,
                "exclude_min": exclude_min,
                "exclude_max": exclude_max,
                "allow_nan": allow_nan,
                "allow_infinity": allow_infinity,
            }

        return strategy()

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        has_min = params["min_value"] is not None
        has_max = params["max_value"] is not None
        allow_nan = params["allow_nan"]

        if allow_nan is None:
            allow_nan = not has_min and not has_max
        allow_infinity = params["allow_infinity"]
        if allow_infinity is None:
            allow_infinity = not has_min or not has_max

        if allow_nan:
            assert any(m.get("is_nan") for m in metrics_list)

        if allow_infinity:
            assert any(m.get("is_infinite") for m in metrics_list)

        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            if metrics.get("is_nan") or metrics.get("is_infinite"):
                continue

            value = metrics["value"]
            if params["min_value"] is not None:
                assert value >= params["min_value"]
                if params["exclude_min"]:
                    assert value != params["min_value"]
            if params["max_value"] is not None:
                assert value <= params["max_value"]
                if params["exclude_max"]:
                    assert value != params["max_value"]


class TextConformance(ConformanceTest):
    def __init__(
        self,
        binary_path: str | Path,
        test_cases: int | None = None,
        *,
        no_surrogates: bool = False,
        skip_server_metrics: bool = False,
    ) -> None:
        super().__init__(
            binary_path, test_cases, skip_server_metrics=skip_server_metrics
        )
        self.no_surrogates = no_surrogates

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        return text_params_strategy(no_surrogates=self.no_surrogates)

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        expanded_cats = (
            set(charmap.as_general_categories(params["categories"]))
            if "categories" in params
            else set()
        )
        expanded_exclude_cats = (
            set(charmap.as_general_categories(params["exclude_categories"]))
            if "exclude_categories" in params
            else set()
        )

        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            codepoints = metrics["codepoints"]
            length = len(codepoints)
            assert length >= params["min_size"]
            if params.get("max_size") is not None:
                assert length <= params["max_size"]

            include_codepoints = {ord(c) for c in params.get("include_characters", "")}
            for cp in codepoints:
                # include_characters overrides all other character constraints
                if cp in include_codepoints:
                    continue
                if "min_codepoint" in params:
                    assert cp >= params["min_codepoint"]
                if "max_codepoint" in params:
                    assert cp <= params["max_codepoint"]
                if "codec" in params:
                    chr(cp).encode(params["codec"])
                if expanded_cats:
                    assert unicodedata.category(chr(cp)) in expanded_cats
                if expanded_exclude_cats:
                    assert unicodedata.category(chr(cp)) not in expanded_exclude_cats
                if "exclude_characters" in params:
                    assert chr(cp) not in params["exclude_characters"]


class BinaryConformance(ConformanceTest):
    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        @st.composite
        def strategy(draw: st.DrawFn) -> dict[str, Any]:
            use_min_size = draw(st.booleans())
            use_max_size = draw(st.booleans())

            min_size = draw(st.integers(0, 50)) if use_min_size else 0
            max_size = (
                draw(st.integers(min_value=min_size, max_value=100))
                if use_max_size
                else None
            )

            return {"min_size": min_size, "max_size": max_size}

        return strategy()

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            length = metrics["length"]
            assert length >= params["min_size"]
            if params["max_size"] is not None:
                assert length <= params["max_size"]


class ListConformance(ConformanceTest):
    modes: ClassVar[list[str]] = ["basic", "non_basic"]

    def __init__(
        self,
        binary_path: str | Path,
        test_cases: int | None = None,
        *,
        min_value: int | None = None,
        max_value: int | None = None,
        skip_server_metrics: bool = False,
        skip_unique: bool = False,
    ) -> None:
        super().__init__(
            binary_path, test_cases, skip_server_metrics=skip_server_metrics
        )
        self.min_value = min_value
        self.max_value = max_value
        self.skip_unique = skip_unique

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        min_value = self.min_value
        max_value = self.max_value
        skip_unique = self.skip_unique

        @st.composite
        def strategy(draw: st.DrawFn) -> dict[str, Any]:
            use_min_size = draw(st.booleans())
            use_max_size = draw(st.booleans())

            min_size = draw(st.integers(0, 100)) if use_min_size else 0
            max_size = (
                draw(st.integers(min_value=min_size, max_value=100))
                if use_max_size
                else None
            )

            integer_params = draw(_integer_params_strategy(min_value, max_value))
            unique = False if skip_unique else draw(st.booleans())

            if unique:
                lo = integer_params["min_value"]
                hi = integer_params["max_value"]
                effective_max_size = max_size if max_size is not None else min_size
                if lo is not None and hi is not None:
                    assume(hi - lo + 1 >= effective_max_size)

            return {
                "min_size": min_size,
                "max_size": max_size,
                "unique": unique,
                **integer_params,
            }

        return strategy()

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            elements = metrics["elements"]
            size = len(elements)
            assert size >= params["min_size"]
            if params["max_size"] is not None:
                assert size <= params["max_size"]

            if size > 0:
                if params["min_value"] is not None:
                    assert min(elements) >= params["min_value"]
                if params["max_value"] is not None:
                    assert max(elements) <= params["max_value"]

            if params["unique"]:
                assert len(set(elements)) == size


class SampledFromConformance(ConformanceTest):
    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        @st.composite
        def strategy(draw: st.DrawFn) -> dict[str, Any]:
            options = draw(
                st.lists(
                    st.integers(-1000, 1000),
                    min_size=1,
                    max_size=10,
                    unique=True,
                ),
            )
            return {"options": options}

        return strategy()

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            assert metrics["value"] in params["options"]


class OneOfConformance(ConformanceTest):
    """Conformance test for oneOf (choose between multiple generators).

    Uses non-overlapping integer ranges as branches so validation can
    determine which branch produced each value. Three modes exercise
    the two oneOf implementation paths:

    - basic: all branches are basic generators; uses the single combined
      schema path
    - map_negate: branches mapped through negation; still all-basic, so
      it also uses the single combined schema path (the per-branch
      transform is dispatched on the branch index returned by the
      server)
    - filter_even: branches filtered to even values only, which is
      non-basic, so it falls back to the compositional span path

    Validates both correctness (values in expected ranges) and that the
    correct protocol path was used (single schema vs. multiple requests).
    """

    modes: ClassVar[list[str]] = ["basic", "map_negate", "filter_even"]

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        @st.composite
        def strategy(draw: st.DrawFn) -> dict[str, Any]:
            n_branches = draw(st.integers(2, 5))
            ranges = []
            for i in range(n_branches):
                # Ranges spaced 1000 apart so they never overlap
                base = i * 1000
                lo = base + draw(st.integers(0, 100))
                hi = lo + draw(st.integers(1, 100))
                ranges.append({"min_value": lo, "max_value": hi})
            return {"ranges": ranges}

        return strategy()

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        ranges = params["ranges"]
        mode = params["mode"]
        branches_used: set[int] = set()

        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            value = metrics["value"]

            if "generate_call_count" in metrics:
                if mode in ("basic", "map_negate"):
                    assert metrics["generate_call_count"] == 1
                else:
                    assert metrics["generate_call_count"] >= 2

            if mode == "filter_even":
                assert value % 2 == 0

            matched = False
            for i, r in enumerate(ranges):
                lo, hi = r["min_value"], r["max_value"]
                if mode == "map_negate":
                    lo, hi = -hi, -lo
                if lo <= value <= hi:
                    branches_used.add(i)
                    matched = True
                    break
            assert matched

        # With 50 test cases and 2+ branches, both should appear
        assert len(branches_used) >= 2


class DictConformance(ConformanceTest):
    modes: ClassVar[list[str]] = ["basic", "non_basic"]

    def __init__(
        self,
        binary_path: str | Path,
        test_cases: int | None = None,
        *,
        min_key: int | None = None,
        max_key: int | None = None,
        min_value: int | None = None,
        max_value: int | None = None,
        skip_server_metrics: bool = False,
    ) -> None:
        super().__init__(
            binary_path, test_cases, skip_server_metrics=skip_server_metrics
        )
        self.min_key = min_key if min_key is not None else -1000
        self.max_key = max_key if max_key is not None else 1000
        self.min_value = min_value if min_value is not None else -1000
        self.max_value = max_value if max_value is not None else 1000

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        min_key = self.min_key
        max_key = self.max_key
        min_value = self.min_value
        max_value = self.max_value

        @st.composite
        def strategy(draw: st.DrawFn) -> dict[str, Any]:
            min_size = draw(st.integers(0, 5))
            max_size = draw(st.integers(min_value=min_size, max_value=10))
            key_type = draw(st.sampled_from(["string", "integer"]))

            # For integer keys, ensure the key range is at least as large as max_size
            # to avoid "Cannot create collection with N unique elements from M distinct"
            # Constraint: drawn_min_key + max_size - 1 <= max_key
            max_allowed_min_key = max_key - max_size + 1
            drawn_min_key = draw(
                st.integers(
                    min_value=min_key,
                    max_value=max(min_key, max_allowed_min_key),
                ),
            )
            # Ensure at least max_size distinct keys are possible
            key_range_min = drawn_min_key + max_size - 1
            drawn_max_key = draw(
                st.integers(min_value=key_range_min, max_value=max_key),
            )

            # For values, draw bounds within the allowed range
            drawn_min_value = draw(
                st.integers(min_value=min_value, max_value=max_value),
            )
            drawn_max_value = draw(
                st.integers(min_value=drawn_min_value, max_value=max_value),
            )

            return {
                "min_size": min_size,
                "max_size": max_size,
                "key_type": key_type,
                "min_key": drawn_min_key,
                "max_key": drawn_max_key,
                "min_value": drawn_min_value,
                "max_value": drawn_max_value,
            }

        return strategy()

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        for metrics in metrics_list:
            if currently_in_test_context():
                note(f"metrics: {metrics}")
            size = metrics["size"]
            assert size >= params["min_size"]
            assert size <= params["max_size"]

            if size > 0:
                # Check value bounds
                assert metrics["min_value"] >= params["min_value"]
                assert metrics["max_value"] <= params["max_value"]

                # Check key bounds for integer keys
                if params["key_type"] == "integer":
                    assert metrics["min_key"] >= params["min_key"]
                    assert metrics["max_key"] <= params["max_key"]


class OriginDeduplicationConformance(ConformanceTest):
    """Tests that origin formatting correctly deduplicates failures.

    The origin field in mark_complete is used by the server as a deduplication
    key. If origin includes too much detail (error messages containing generated
    values, or full stack traces), the server treats each failure as a distinct
    bug, breaking shrinking and producing confusing output.

    This test has two modes:
    - value_in_error_message: the test fails with the generated value in the
      error message. A correct origin (exc_type + innermost file:line) will
      deduplicate all failures to 1. An origin that includes the error message
      will produce many "distinct" failures.
    - multiple_call_sites: the same buggy function is called from multiple
      code paths. A correct origin (using the innermost frame) will deduplicate
      to 1. An origin that includes the full stack trace will produce multiple
      "distinct" failures.
    """

    modes: ClassVar[list[str]] = ["value_in_error_message", "multiple_call_sites"]

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        return st.just({})

    def validate(
        self,
        metrics_list: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> None:
        interesting = self.run_metrics["interesting_test_cases"]
        mode = params["mode"]
        assert interesting == 1, (
            f"Expected exactly 1 distinct failure for mode '{mode}', "
            f"but got {interesting}. "
            + (
                "This usually means the origin field includes the error message "
                "text, which contains the generated value and prevents deduplication."
                if mode == "value_in_error_message"
                else "This usually means the origin field includes the full stack "
                "trace, causing different call paths to the same bug to appear "
                "as distinct failures."
            )
        )


def run_conformance_tests(
    tests: Collection[ConformanceTest],
    subtests: pytest.Subtests,
    *,
    settings: Settings | None = None,
    skip_tests: Collection[type[ConformanceTest]] = frozenset(),
    command_prefix: Sequence[str] | None = None,
) -> None:
    names = {type(t).__name__ for t in tests} | {
        TestClass.__name__ for TestClass in skip_tests
    }
    assert names == {
        TestClass.__name__ for TestClass in ConformanceTest.registered_tests
    }

    for test in tests:
        for mode in test.modes if test.modes is not None else [None]:  # type: ignore
            suffix = f"[{mode}]" if mode is not None else ""
            with subtests.test(msg=f"{type(test).__name__}{suffix}"):

                @Settings(
                    parent=settings,
                    max_examples=5,
                    deadline=None,
                    phases=set(Phase) - {Phase.shrink},
                )
                @given(test.params_strategy())
                def run_test(params: dict[str, Any]) -> None:
                    if mode is not None:
                        params["mode"] = mode
                    test.run(params, command_prefix=command_prefix)

                run_test()

    # gives callers visibility into skipped tests in pytest output (and a reminder to
    # implement them).
    for TestClass in skip_tests:
        with subtests.test(msg=TestClass.__name__):
            pytest.skip("skipped by caller")
