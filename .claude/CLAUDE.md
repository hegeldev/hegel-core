# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Hegel is a universal property-based testing protocol. A Python server (powered by Hypothesis) communicates with language-specific client libraries via Unix sockets. This repository contains the Python core server.

## Build & Test Commands

```bash
uv sync --group dev             # Install with dev dependencies
just ci                         # Run all CI checks (lint + typecheck + coverage)
just test                       # Run tests
just coverage                   # Run tests with coverage (must be 100%)
just format                     # Auto-fix lint + format (ruff + shed)
just check                      # typecheck + format + coverage
uv run pytest tests             # Run all tests directly
uv run pytest tests/test_schema.py  # Run single test file
uv run pytest -k test_name     # Run tests matching pattern
uv run mypy src/                # Type check (targets Python 3.14)
```

100% branch test coverage is required (`fail_under = 100`). Uses `uv` as package manager, `ruff` + `shed` for formatting. Coverage omits `src/hegel/conformance.py`.

Coverage runs twice in CI: once normally and once with `ANTITHESIS_OUTPUT_DIR` set (which switches the Hypothesis backend to `hypothesis-urandom`), then combines results. Both runs must be covered.

## Architecture

### Client-Server Communication

1. **Client** creates a Unix socket path and spawns the `hegel` CLI with that path
2. **The server** binds to the socket and listens for the client to connect
3. A single persistent connection supports multiple test executions
4. **Hypothesis ConjectureRunner** drives test execution on the server side, including shrinking

### Protocol

Binary protocol over Unix socket with CBOR-encoded payloads:
- 20-byte header + variable payload + terminator byte: magic `0x4845474C` (HEGL), CRC32, stream ID, message ID, payload length
- Stream 0 is the control stream; odd-numbered streams are client-created, even-numbered are server-created
- Reply bit (`1 << 31`) in message ID distinguishes requests from replies
- Commands: `generate`, `start_span`, `stop_span`, `target`, `mark_complete`, `new_collection`, `collection_more`, `collection_reject`, `new_pool`, `pool_add`, `pool_generate`, `pool_consume`

### Module Overview

- `__main__.py` - CLI entry point (`hegel` command via click), binds Unix socket and starts server
- `server.py` - Drives test execution via Hypothesis `ConjectureRunner` with a `ThreadPoolExecutor`. Contains `HegelState` (per-test-run state) and `_run_test` (orchestrates a full test including shrinking and final replay)
- `protocol/` - Binary protocol package:
  - `packet.py` - Wire format: `Packet` dataclass, `read_packet`/`write_packet` for serialization with CRC32 checksums
  - `connection.py` - `Connection` class: manages socket, reader thread, stream registry, handshake. Thread-safe writes via lock
  - `stream.py` - `Stream` class: per-stream packet queues (requests/replies), `send_request`/`handle_requests` for CBOR request-reply pattern, `PendingRequest` future
  - `utils.py` - `StreamId`/`MessageId` newtypes, `ProtocolError`/`RequestError` exceptions, `STREAM_TIMEOUT`
- `schema.py` - JSON Schema to Hypothesis strategy conversion (cached by SHA1 hash in `FROM_SCHEMA_CACHE`)
- `test_server.py` - Simplified server for error simulation testing (activated via `HEGEL_PROTOCOL_TEST_MODE`). Used by library conformance tests to validate error handling
- `conformance.py` - Framework for testing library implementations against specification (`ConformanceTest` base class with `__init_subclass__` auto-registration)
- `utils.py` - `UniqueIdentifier` sentinel factory, `not_set` sentinel

### Server Execution Flow

1. Client sends `run_test` on the control stream (stream 0)
2. `server.py` creates a `ConjectureRunner` and a per-test stream
3. For each test case, a `test_case_stream` is created and the client is notified
4. Client sends commands (`generate`, `start_span`/`stop_span`, `target`, `mark_complete`) on the test case stream
5. `generate` calls `data.draw(from_schema(schema))` to produce values
6. After all test cases, interesting (failing) examples are replayed with `is_final=True`

### Generator Modes

1. **Schema Composition** (preferred): Compose JSON schemas, single socket request. Generators that have schemas are called "basic generators."
2. **Compositional Fallback**: Multiple requests wrapped in spans when schemas unavailable (after `map`/`filter` on non-basic generators, or `flatmap`)

Key insight: `map()` on a basic generator preserves the schema by composing the transform function, rather than losing it. This is the central optimization across all libraries.

### Key Patterns

**ContextVar-based state** - `tests/client/client.py` uses `ContextVar` (`_current_stream`, `_is_final`, `_test_aborted`) so that `generate_from_schema()`, `assume()`, `start_span()` etc. work as free functions without passing streams explicitly.

**`handle_requests` pattern** - `Stream.handle_requests(handler, until)` dispatches incoming requests to a handler function, CBOR-decoding each request and encoding replies. Used in `server.py` for the main test case loop.

**Test client** - `tests/client/` is a test-local client that mirrors the real library clients:
- `tests/client/protocol.py` - `ClientConnection` and `ClientStream`: single-threaded client-side protocol (no reader thread; reads synchronously and stashes packets for other streams)
- `tests/client/client.py` - `Client` class and free functions (`generate_from_schema`, `assume`, `target`, etc.) using ContextVars
- The `client` pytest fixture (in `conftest.py`) creates a `socket.socketpair()`, runs the server in a daemon thread, and yields the client side

### Environment Variables

- `HEGEL_PROTOCOL_DEBUG=1` - Enables protocol packet tracing (set via `--verbosity debug` CLI flag)
- `HEGEL_STREAM_TIMEOUT` - Overrides the default 30-second stream timeout
- `HEGEL_PROTOCOL_TEST_MODE` - Activates the test server for error simulation (modes: `crash_after_handshake`, `crash_after_handshake_with_stderr`, `stop_test_on_generate`, `stop_test_on_mark_complete`, `error_response`, `empty_test`, etc.)
- `ANTITHESIS_OUTPUT_DIR` - When set, switches Hypothesis backend to `hypothesis-urandom`

### Library Specification

Comprehensive library specification: `docs/library-api.md`

## Release Process

PRs that change files in `src/` must include a `RELEASE.md` file in the repository root. The format is:

```
RELEASE_TYPE: patch

Description of changes for the changelog.
```

The first line must be `RELEASE_TYPE: major`, `RELEASE_TYPE: minor`, or `RELEASE_TYPE: patch`. The remaining lines are changelog text. Use `patch` for bug fixes and internal changes, `minor` for public API additions, and `major` for breaking changes (maintainers only). See `RELEASE-sample.md` for a full example. The CI `check-release` job will fail if this file is missing when source files have changed.

## Code Style

- Don't add message strings to pytest asserts (`assert x, "message"`). Pytest provides excellent error messages automatically.
- Don't reference source line numbers in test comments (e.g., "Covers client.py line 42"). Line numbers are not stable identifiers. Instead, describe the condition or branch being tested (e.g., "Tests the except TypeError branch in schema()").

### Writing Changelog Entries

When writing a `RELEASE.md`, read `.claude/changelog-guidance.md` for detailed style guidance.
