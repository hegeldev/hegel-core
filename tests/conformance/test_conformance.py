from pathlib import Path

from hegel.conformance import (
    BinaryConformance,
    BooleanConformance,
    DictConformance,
    EmptyTestConformance,
    ErrorResponseConformance,
    FloatConformance,
    IntegerConformance,
    ListConformance,
    OneOfConformance,
    SampledFromConformance,
    StopTestOnCollectionMoreConformance,
    StopTestOnGenerateConformance,
    StopTestOnMarkCompleteConformance,
    StopTestOnNewCollectionConformance,
    TextConformance,
    run_conformance_tests,
)

# We pretend that Hypothesis is a "hegel library", and have it implement our conformance
# tests. This makes sure we don't write an assertion in a conformance test that is violated
# by the Hypothesis ground truth.
#
# This is imperfect, because the conformance test itself sometimes contains somewhat complex
# logic, and a hegel library which misimplements a conformance test will not be caught by
# this. But this serves its job as catching a misimplementation of the hegel-core conformance
# test validation itself.

TESTS_DIR = Path(__file__).parent / "tests"


def test_conformance(subtests):
    # These binaries use Hypothesis directly, not the hegel server,
    # so server-side metrics (generate_count) are not available.
    skip = {"skip_server_metrics": True}

    run_conformance_tests(
        [
            BooleanConformance(TESTS_DIR / "boolean.py", **skip),
            IntegerConformance(TESTS_DIR / "integer.py", **skip),
            FloatConformance(TESTS_DIR / "float.py", **skip),
            TextConformance(TESTS_DIR / "text.py", **skip),
            BinaryConformance(TESTS_DIR / "binary.py", **skip),
            ListConformance(TESTS_DIR / "list.py", **skip),
            SampledFromConformance(TESTS_DIR / "sampled_from.py", **skip),
            OneOfConformance(TESTS_DIR / "one_of.py", **skip),
            DictConformance(TESTS_DIR / "dict.py", **skip),
        ],
        subtests,
        skip_tests=[
            StopTestOnGenerateConformance,
            StopTestOnMarkCompleteConformance,
            ErrorResponseConformance,
            EmptyTestConformance,
            StopTestOnCollectionMoreConformance,
            StopTestOnNewCollectionConformance,
        ],
    )
