# Trio Port Progress

## Status: COMPLETE

Ported hegel-core from threading to trio structured concurrency.

## Completed

- [x] Infrastructure: add `trio>=0.27.0` runtime dep, remove `pytest-asyncio`
- [x] packet.py — async `aread_packet` / `aread_exact` added
- [x] stream.py + connection.py — trio primitives (MemoryChannel, Lock, nursery-based reader)
- [x] server.py — async `_run_test`, Hypothesis `ConjectureRunner` in trio worker thread
- [x] test_server.py — async `run_test_server`
- [x] __main__.py — async entry point with `_StdioStream` trio wrapper
- [x] conftest.py — trio server in background thread via `run_trio_server` helper
- [x] Final validation: 100% branch coverage, ruff clean, mypy clean, all 193 tests passing
