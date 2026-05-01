RELEASE_TYPE: minor

This release adds support for the `phases` parameter in the `run_test` protocol message,
allowing clients to control which Hypothesis phases run (e.g. `generate`, `shrink`,
`reuse`, `target`, `explicit`, `explain`).
