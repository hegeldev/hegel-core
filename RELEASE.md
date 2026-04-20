RELEASE_TYPE: minor

This release adds a `one_shot` option to the `run_test` protocol command. When set,
the server runs exactly one test case in final mode and returns the result, with no
shrinking, replay, or other exploration. This is intended for callers that want to
execute a single property-test pass with full reporting and then exit immediately.
