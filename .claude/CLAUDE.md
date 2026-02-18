# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Hegel is a universal property-based testing framework. A Python server (powered by Hypothesis) communicates with language-specific SDKs via Unix sockets. This repository contains the Python core (CLI + server) and the reference Python SDK (in `sdk/`).

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

The Python SDK lives in `sdk/` with its own justfile:
```bash
just sdk test                   # Run SDK tests
just sdk coverage               # Run SDK tests with coverage
just sdk typecheck              # Type check SDK
just sdk format                 # Format SDK code
```

100% branch test coverage is required (`fail_under = 100`). Uses `uv` as package manager, `ruff` + `shed` for formatting.

## Architecture

### Client-Server Communication

1. **SDK** creates a Unix socket path and spawns the `hegel` CLI with that path
2. **The server** binds to the socket and listens for the SDK to connect
3. A single persistent connection supports multiple test executions
4. **Hypothesis ConjectureRunner** drives test execution on the server side, including shrinking

### Protocol

Binary protocol over Unix socket with CBOR-encoded payloads:
- 20-byte header: magic `0x4845474C` (HEGL), CRC32, channel ID, message ID, payload length
- Channel 0 is the control channel; odd-numbered channels for test communication
- Reply bit (`1 << 31`) in message ID distinguishes requests from responses
- Commands: `generate`, `start_span`, `stop_span`, `target`, `mark_complete`, `new_collection`, `collection_more`, `collection_reject`

### Module Overview

- `__main__.py` - CLI entry point (`hegel` command via click), binds Unix socket and starts server
- `server.py` - Drives test execution via Hypothesis `ConjectureRunner` with a `ThreadPoolExecutor`
- `protocol.py` - Binary protocol with CBOR encoding, multiplexed channels, demand-driven reader (no background thread)
- `schema.py` - JSON Schema to Hypothesis strategy conversion (cached by SHA1 hash in `FROM_SCHEMA_CACHE`)
- `conformance.py` - Framework for testing SDK implementations against specification (`ConformanceTest` base class with `__init_subclass__` auto-registration)

### Server Execution Flow

1. SDK sends `run_test` on the control channel (channel 0)
2. `server.py` creates a `ConjectureRunner` and a per-test channel
3. For each test case, a `test_case_channel` is created and the SDK is notified
4. SDK sends commands (`generate`, `start_span`/`stop_span`, `target`, `mark_complete`) on the test case channel
5. `generate` calls `data.draw(cached_from_schema(schema))` to produce values
6. After all test cases, interesting (failing) examples are replayed with `is_final=True`

### Generator Modes

1. **Schema Composition** (preferred): Compose JSON schemas, single socket request. Generators that have schemas are called "basic generators."
2. **Compositional Fallback**: Multiple requests wrapped in spans when schemas unavailable (after `map`/`filter` on non-basic generators, or `flatmap`)

Key insight: `map()` on a basic generator preserves the schema by composing the transform function, rather than losing it. This is the central optimization across all SDKs.

### Key Patterns

**ContextVar-based state** - `tests/client.py` and `sdk/src/hegel_sdk/client.py` use `ContextVar` (`_current_channel`, `_is_final`, `_test_aborted`) so that `generate_from_schema()`, `assume()`, `start_span()` etc. work as free functions without passing channels explicitly.

**`handle_requests` decorator** - `Channel.handle_requests(handler, until)` dispatches incoming requests to a handler function. Used as `@channel.handle_requests` in both server and test code.

**Test client** - `tests/client.py` is a test-local client that mirrors the SDK's `client.py`. It's used by the `client` pytest fixture (in `conftest.py`) which creates a `socket.socketpair()`, runs the server in a daemon thread, and yields the client side.

### Environment Variables

- `HEGEL_PROTOCOL_DEBUG=1` - Enables protocol packet tracing (set via `--verbosity debug` CLI flag)
- `HEGEL_CHANNEL_TIMEOUT` - Overrides the default 30-second channel timeout

### SDK Specification

Comprehensive SDK specification: `docs/sdk-api.md`

## Code Style

- Don't add message strings to pytest asserts (`assert x, "message"`). Pytest provides excellent error messages automatically.
- Don't reference source line numbers in test comments (e.g., "Covers sdk.py line 42"). Line numbers are not stable identifiers. Instead, describe the condition or branch being tested (e.g., "Tests the except TypeError branch in schema()").
