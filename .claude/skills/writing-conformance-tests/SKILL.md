---
name: Writing Hegel Conformance Tests
description: This skill should be used when creating or modifying conformance tests in hegel-core, when adding a new ConformanceTest subclass, when writing a conformance binary for tests/conformance/tests/, or when instrumenting the hegel server to report metrics for conformance validation. Also use when debugging conformance test failures or understanding how library implementations (hegel-go, hegel-rust) are validated.
---

# Writing Hegel Conformance Tests

Conformance tests validate that hegel library implementations (hegel-go, hegel-rust, etc.) correctly implement the hegel protocol. They run compiled library binaries against the real hegel server and validate the results.

## Architecture: Three Parts

### 1. ConformanceTest Subclass (hegel-core, `src/hegel/conformance.py`)

Defines what to test and how to validate:

- `params_strategy()` — Hypothesis strategy generating random test parameters
- `validate(metrics_list, params)` — Assertions on the collected metrics
- `modes` class var — Optional list of modes; framework iterates and adds `mode` to params
- `extra_env()` — Optional extra env vars for the binary
- `default_test_cases` — Override for default number of test cases

The framework (`run_conformance_tests`) runs each test 5 times with random params via `@given`.

### 2. Conformance Binary (library side)

A compiled executable (Go/Rust/Python) that:

1. Parses JSON params from `argv[1]` (including `mode` if the test has modes)
2. Reads `CONFORMANCE_TEST_CASES` and `CONFORMANCE_METRICS_FILE` from env
3. Connects to the hegel server (the library spawns the server internally)
4. Runs a test through the library that exercises the feature being tested
5. Writes **per-test-case** client metrics (one JSON line per test case) to `CONFORMANCE_METRICS_FILE`

**Critical principle: the binary is the library's code.** It uses the library's API (`hegel.MustRun` in Go, `Hegel::new().run()` in Rust). It does NOT reimplement the protocol.

### 3. Server Instrumentation (hegel-core, `src/hegel/server.py`)

The server writes per-test-case server metrics to `CONFORMANCE_SERVER_METRICS_FILE` on each `mark_complete`. These are things only the server can know (e.g., `generate_call_count`). The framework merges server metrics with client metrics 1:1 and passes the merged list to `validate()`.

**The server is the source of truth.** Server-written metrics cannot be faked by the library binary. Use server metrics for anything that validates server-side behavior.

## Metrics Flow

```
Library Binary              Hegel Server                  Framework
    |                            |                            |
    |-- spawns server ---------->|                            |
    |-- run_test --------------->|                            |
    |                            |                            |
    |<-- test_case event --------|                            |
    | writes to METRICS_FILE     | writes to SERVER_METRICS   |
    |  (per test case)           |  (per mark_complete)       |
    |                            |                            |
    |<-- test_done event --------|                            |
    |                            |                            |
    |                            |         reads both files   |
    |                            |         merges 1:1         |
    |                            |         calls validate()   |
```

The 1:1 merge requires the same number of lines in both files. Each `mark_complete` produces one server line. Each test function invocation produces one client line. They naturally align.

## Self-Test Binaries (Python reference, `tests/conformance/tests/`)

Hegel-core includes Python reference binaries that use Hypothesis directly (NOT the server):

- Pass `skip_server_metrics=True` to skip the 1:1 server merge
- Verify the conformance validation logic is correct
- Do NOT test actual server behavior (that's what real library tests do)

Pattern: `@given(strategy)` generates values, writes metrics in the test function body.

## How to Add a New Conformance Test

### Step 1: Identify what to validate

Determine what property of a library implementation to test. Identify whether validation requires:
- **Client metrics only** (e.g., value constraints) — library reports what it observed
- **Server metrics** (e.g., generate_call_count, interesting_test_cases) — server reports independently

### Step 2: Instrument the server (if needed)

If validation requires server-side data, add instrumentation in `server.py`. Write to `CONFORMANCE_SERVER_METRICS_FILE` (per-test-case) or `CONFORMANCE_SERVER_RUN_METRICS_FILE` (per-run). Add a unit test in `tests/test_server.py` verifying the server writes the expected data.

### Step 3: Define the ConformanceTest subclass

In `src/hegel/conformance.py`, create a subclass:

```python
class MyConformance(ConformanceTest):
    modes: ClassVar[list[str]] = ["mode_a", "mode_b"]  # optional

    def params_strategy(self) -> st.SearchStrategy[dict[str, Any]]:
        return st.just({})  # or a composite strategy

    def validate(self, metrics_list, params):
        for metrics in metrics_list:
            # Check client metrics and/or merged server metrics
            assert metrics["some_field"] == expected
```

### Step 4: Write the self-test binary

In `tests/conformance/tests/my_test.py`:

```python
#!/usr/bin/env python3
import json, os, sys
from hypothesis import given, settings, strategies as st

def main():
    params = json.loads(sys.argv[1])
    metrics_file = os.environ["CONFORMANCE_METRICS_FILE"]
    test_cases = int(os.environ["CONFORMANCE_TEST_CASES"])

    @settings(max_examples=test_cases, database=None)
    @given(st.integers(...))
    def run(value):
        with open(metrics_file, "a") as f:
            f.write(json.dumps({"value": value}) + "\n")

    run()

if __name__ == "__main__":
    main()
```

### Step 5: Register in test_conformance.py

```python
MyConformance(TESTS_DIR / "my_test.py", skip_server_metrics=True),
```

The self-test uses `skip_server_metrics=True`. Real library repos (hegel-go, hegel-rust) create their own binaries that go through the actual server.

### Step 6: Library binaries implement the test

A hegel-go binary looks like:
```go
func main() {
    params := parseParams()
    n := conformance.GetTestCases()
    hegel.MustRun(func(s *hegel.TestCase) {
        v := hegel.Draw(s, hegel.Integers[int](0, 100))
        conformance.WriteMetrics(map[string]any{"value": v})
    }, hegel.WithTestCases(n))
}
```

## Common Mistakes

1. **Writing a custom client in the binary.** The binary should use the library's API, not reimplement the protocol. The library spawns the server.

2. **Having the client write server-side metrics.** If a metric represents server behavior (like `interesting_test_cases`), the SERVER must write it. A client can say whatever it wants.

3. **Confusing self-test with real test.** The Python self-test only validates the validation logic. Real testing happens when hegel-go/hegel-rust binaries run through the actual server.

4. **Breaking the 1:1 line count.** Client metrics lines must match server metrics lines. Each `mark_complete` = one server line. Each test function call = one client line.

## Reference Repos

- **hegel-go**: `internal/conformance/cmd/` has Go binaries, `tests/conformance/` has pytest
- **hegel-rust**: `tests/conformance/rust/src/bin/` has Rust binaries
- Both use `CONFORMANCE_METRICS_FILE`, `CONFORMANCE_TEST_CASES`, `CONFORMANCE_SERVER_METRICS_FILE`
