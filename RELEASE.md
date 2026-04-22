RELEASE_TYPE: minor

This release ports the hegel server internals from threading to trio, replacing the reader thread and `ThreadPoolExecutor`-based concurrency model with trio's structured concurrency primitives.

The `Connection` class now requires a `nursery` keyword argument (a `trio.Nursery`) and accepts a `trio.abc.Stream` instead of a raw socket. The `run_server_on_connection` and `run_test_server` functions are now `async`.
