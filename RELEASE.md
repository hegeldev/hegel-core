RELEASE_TYPE: patch

This release adds the `allow_nan` and `allow_infinity` boolean arguments to `FloatConformance()`. This makes it possible for clients where infinity or nan is not supported to run float conformance tests.
