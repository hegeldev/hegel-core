RELEASE_TYPE: patch

This patch adds `OneOfConformance`, a new conformance test class that validates `one_of` generator correctness. It uses non-overlapping integer ranges as alternatives and runs in three modes — `basic`, `transformed`, and `non_basic` — to exercise all three `one_of` implementation paths. Existing implementations will need to either add a `test_one_of` conformance binary or add `OneOfConformance` to their `skip_tests`.

This patch also adds recommended integer bound constants (`INT32_MIN`, `INT32_MAX`, `INT64_MIN`, `INT64_MAX`, `BIGINT_MIN`, `BIGINT_MAX`) for use in conformance test setup. Languages with arbitrary-precision integers should use the `BIGINT` bounds to exercise CBOR bignum tag decoding, which is not triggered by the narrower ranges most implementations currently use.
