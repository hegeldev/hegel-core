RELEASE_TYPE: patch

This release adds a `one_shot` option to the `run_test` protocol command. When set,
the server runs exactly one test case in final mode and returns the result, with no
shrinking, replay, or other exploration. This is mostly intended for callers who
are running workloads in Antithesis, but is potentially useful for anyone who wishes
to use Hegel for flexible data generation on a system that they don't have a reset
button for or that exhibits significantly non-deterministic behaviour.
