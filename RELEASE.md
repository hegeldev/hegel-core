RELEASE_TYPE: patch

This release adds a `single_test_case` top-level protocol command. When sent
instead of `run_test`, the server immediately hands a single test case to the
client in final mode and returns the result, with no shrinking, replay, or other
exploration. This is mostly intended for callers who are running workloads in
Antithesis, but is potentially useful for anyone who wishes to use Hegel for
flexible data generation on a system that they don't have a reset button for or
that exhibits significantly non-deterministic behaviour.
