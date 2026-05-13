RELEASE_TYPE: minor

This release adds `allow_nan` and `allow_infinity` boolean arguments to `FloatConformance()`. This makes it possible for clients where infinity or nan is not supported (e.g. Erlang) to run float conformance tests.
