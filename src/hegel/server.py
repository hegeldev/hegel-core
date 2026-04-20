import contextlib
import itertools
import json
import os
import random
import traceback
from concurrent.futures import ThreadPoolExecutor
from random import Random
from typing import Any

import cbor2
from hypothesis import HealthCheck, settings
from hypothesis.control import BuildContext
from hypothesis.core import decode_failure, encode_failure
from hypothesis.database import DirectoryBasedExampleDatabase
from hypothesis.errors import (
    FailedHealthCheck,
    Flaky,
    FlakyStrategyDefinition,
    StopTest,
    UnsatisfiedAssumption,
)
from hypothesis.internal.conjecture.data import ConjectureData, Status
from hypothesis.internal.conjecture.engine import ConjectureRunner, ExitReason
from hypothesis.internal.conjecture.shrinker import sort_key
from hypothesis.internal.conjecture.utils import calc_label_from_name, many

from hegel.protocol import Connection, ProtocolError, Stream
from hegel.schema import _encode_value, from_schema
from hegel.utils import UniqueIdentifier, not_set

VARIABLES_LABEL = calc_label_from_name("Variables")

USUALLY_MEANS = (
    "This usually means your test "
    "depends on external state such as global variables, system "
    "time, or external random number generators."
)

FLAKY_GENERATION_MSG = (
    "Your data generation is non-deterministic: a different call to "
    "draw() happened than expected, or your test errored before data "
    "generation that previously completed successfully had finished. "
) + USUALLY_MEANS

FLAKY_TEST_RESULT_MSG = (
    "Your test produced different outcomes when run with the "
    "same generated data - it failed when it previously succeeded, or "
    "succeeded when it previously failed."
) + USUALLY_MEANS


def _flaky_message(error: Flaky) -> str:
    if isinstance(error, FlakyStrategyDefinition):
        return FLAKY_GENERATION_MSG
    return FLAKY_TEST_RESULT_MSG


def _write_run_metrics(result: dict[str, Any]) -> None:
    """Write per-run metrics (interesting_test_cases) to the conformance
    server run metrics file, if set."""
    run_metrics_file = os.environ.get("CONFORMANCE_SERVER_RUN_METRICS_FILE")
    if run_metrics_file is not None:
        with open(run_metrics_file, "w", encoding="utf-8") as mf:
            mf.write(
                json.dumps({"interesting_test_cases": result["interesting_test_cases"]})
            )


def _flaky_result(
    runner: ConjectureRunner,
    seed: int,
    error: Flaky,
    captured_error: Flaky | None,
) -> dict[str, Any]:
    """Build a test_done result dict for a flaky test."""
    return {
        "passed": False,
        "test_cases": runner.call_count,
        "valid_test_cases": runner.valid_examples,
        "invalid_test_cases": runner.invalid_examples,
        "interesting_test_cases": 0,
        "seed": str(seed),
        "flaky": _flaky_message(captured_error or error),
    }


# Health checks that are relevant to the Hegel wire protocol.
# Hypothesis has additional health checks (function_scoped_fixture,
# differing_executors, nested_given) that are pytest/Hypothesis-specific
# and don't apply here.
#
# We also rename some of the health checks here because the Hypothesis
# names are either not great in the first place or clash with Hegel
# naming conventions.
SUPPORTED_HEALTH_CHECKS: dict[str, HealthCheck] = {
    "test_cases_too_large": HealthCheck.data_too_large,
    "filter_too_much": HealthCheck.filter_too_much,
    "too_slow": HealthCheck.too_slow,
    "large_initial_test_case": HealthCheck.large_base_example,
}


class Variables:
    def __init__(self):
        self.last_id = 0
        self.variables = []
        self.removed = set()

    def generate(self, data: ConjectureData) -> int:
        if not self.variables:
            data.mark_invalid()
        else:
            for _ in range(3):
                data.start_span(VARIABLES_LABEL)
                i = data.draw_integer(
                    min_value=0,
                    max_value=len(self.variables) - 1,
                    # Follows convention from hypothesis.stateful.Bundle.
                    # Apparently this shrinks better because it means that
                    # problems found later on are easier to shrink because
                    # there's no padding.
                    shrink_towards=len(self.variables),
                )
                v = self.variables[i]
                if v not in self.removed:
                    data.stop_span()
                    return v
                else:
                    data.stop_span(discard=True)
            i = len(self.variables) - 1
            assert i >= 0
            v = self.variables[i]
            data.draw_integer(
                min_value=0,
                max_value=len(self.variables) - 1,
                forced=i,
            )
            return v

    def consume(self, variable_id: int) -> None:
        self.removed.add(variable_id)
        while self.variables and self.variables[-1] in self.removed:
            self.variables.pop()

    def next(self) -> int:
        self.last_id += 1
        self.variables.append(self.last_id)
        return self.last_id


class HegelState:
    """State for a test run that communicates with the client.

    The test_function method handles a single test case by:
    1. Creating a stream for communication
    2. Sending a test_case event to the client
    3. Handling generate/span/target requests from the client until mark_complete
    4. Applying the final status to the ConjectureData
    """

    def __init__(
        self,
        connection: Connection,
        stream: Stream,
        *,
        is_final: bool = False,
    ):
        self._connection = connection
        self._stream = stream
        self._is_final = is_final
        self.flaky_error: Flaky | None = None

    def test_function(self, data: ConjectureData) -> None:
        collections: dict[int, many] = {}
        variable_pools: list[Variables] = []
        collection_id_counter = itertools.count()
        generate_count = 0

        with BuildContext(data, is_final=self._is_final, wrapped_test=None):  # type: ignore
            test_case_stream = self._connection.new_stream(role="Test Case")
            self._stream.send_request(
                {
                    "event": "test_case",
                    "stream_id": test_case_stream.stream_id,
                    "is_final": self._is_final,
                },
            ).get()

            done = False

            def handle_client_request(message: dict) -> Any:
                nonlocal done, generate_count
                try:
                    command = message["command"]

                    if command == "generate":
                        generate_count += 1
                        schema = message["schema"]
                        strategy = from_schema(schema)
                        result = data.draw(strategy)
                        return _encode_value(result)
                    elif command == "start_span":
                        label = message.get("label", 0)
                        data.start_span(label)
                        return None
                    elif command == "stop_span":
                        discard = message.get("discard", False)
                        data.stop_span(discard=discard)
                        return None
                    elif command == "target":
                        value = message["value"]
                        label = message["label"]
                        data.target_observations[label] = value
                        return None
                    elif command == "mark_complete":
                        done = True
                        server_metrics_file = os.environ.get(
                            "CONFORMANCE_SERVER_METRICS_FILE"
                        )
                        if server_metrics_file is not None:
                            with open(server_metrics_file, "a", encoding="utf-8") as mf:
                                mf.write(
                                    json.dumps({"generate_call_count": generate_count})
                                    + "\n"
                                )
                        status = Status[message["status"]]
                        origin = message.get("origin")
                        if status is Status.VALID:
                            data.conclude_test(Status.VALID)
                        elif status is Status.INVALID:
                            data.mark_invalid()
                        else:
                            assert status is Status.INTERESTING
                            data.mark_interesting(
                                origin,  # type: ignore[arg-type]
                            )
                    elif command == "new_collection":
                        collection_id = next(collection_id_counter)
                        assert collection_id not in collections
                        min_size = message.get("min_size", 0)
                        max_size = message.get("max_size", float("inf"))
                        if max_size is None:
                            max_size = float("inf")
                        # Standard formula for Hypothesis collections.
                        average_size = min(
                            max(min_size * 2, min_size + 5),
                            0.5 * (min_size + max_size),
                        )
                        collections[collection_id] = many(
                            data,
                            min_size=min_size,
                            max_size=max_size,
                            average_size=average_size,
                        )
                        return collection_id
                    elif command == "collection_more":
                        collection = collections[message["collection_id"]]
                        return collection.more()
                    elif command == "collection_reject":
                        collection = collections[message["collection_id"]]
                        return collection.reject(why=message.get("why"))
                    elif command == "new_pool":
                        i = len(variable_pools)
                        v = Variables()
                        variable_pools.append(v)
                        return i
                    elif command == "pool_consume":
                        pool_id = message["pool_id"]
                        variable_id = message["variable_id"]
                        variable_pools[pool_id].consume(variable_id)
                        return None
                    elif command == "pool_add":
                        pool_id = message["pool_id"]
                        return variable_pools[pool_id].next()
                    elif command == "pool_generate":
                        pool_id = message["pool_id"]
                        consume = message.get("consume", False)
                        pool = variable_pools[pool_id]
                        v = pool.generate(data)
                        if consume:
                            pool.consume(v)
                        return v
                    else:
                        raise ValueError(f"Unknown command: {command}")
                except UnsatisfiedAssumption:
                    done = True
                    data.mark_invalid()
                except StopTest:
                    done = True
                    raise
                except Flaky as e:
                    done = True
                    self.flaky_error = e
                    raise

            test_case_stream.handle_requests(handle_client_request, until=lambda: done)


def run_server_on_connection(connection: Connection) -> None:
    connection.receive_handshake()

    pending_futures = []
    try:
        with ThreadPoolExecutor(max_workers=os.cpu_count()) as thread_pool:
            while True:
                packet = connection.control_stream.read_request(timeout=None)
                message = cbor2.loads(packet.payload)
                command = message["command"]
                if command == "run_test":
                    stream = connection.register_client_stream(
                        message["stream_id"], role="Test stream"
                    )

                    pending_futures.append(
                        thread_pool.submit(
                            _run_test,
                            connection,
                            stream,
                            test_cases=message["test_cases"],
                            database_key=message.get("database_key"),
                            seed=message.get("seed"),
                            failure_blob=message.get("failure_blob"),
                            suppress_health_check=message.get(
                                "suppress_health_check", []
                            ),
                            derandomize=message.get("derandomize", False),
                            database=message.get("database", not_set),
                        ),
                    )
                    connection.control_stream.write_reply(packet.message_id, True)
                else:
                    raise ValueError(f"Unknown command: {command}")
    except (ConnectionError, ProtocolError):
        pass
    except BaseException:
        traceback.print_exc()
    finally:
        connection.close()

    for f in pending_futures:
        try:
            f.result(timeout=0.5)
        except (ConnectionError, TimeoutError):
            f.cancel()


def _run_test(
    connection: Connection,
    stream: Stream,
    *,
    test_cases: int,
    database_key: bytes | None,
    seed: int | None,
    failure_blob: bytes | None = None,
    suppress_health_check: list[str] | None,
    derandomize: bool,
    database: str | UniqueIdentifier | None,
) -> dict[str, Any]:
    """Run a single test using ConjectureRunner.

    Returns a dict with test results including:
    - passed: bool
    - test_cases: int
    - valid_examples: int
    - invalid_examples: int
    - failure: optional dict with failure details
    """
    try:
        # seed takes precendence over derandomize, like Hypothesis
        if derandomize and seed is None:
            seed = (
                int.from_bytes(database_key, "big") if database_key is not None else 0
            )

        seed = random.getrandbits(128) if seed is None else seed

        suppress = []
        for name in suppress_health_check or []:
            check = SUPPORTED_HEALTH_CHECKS.get(name)
            if check is not None:
                suppress.append(check)
            else:
                valid = list(SUPPORTED_HEALTH_CHECKS.keys())
                result: dict[str, Any] = {
                    "passed": False,
                    "test_cases": 0,
                    "valid_test_cases": 0,
                    "invalid_test_cases": 0,
                    "interesting_test_cases": 0,
                    "seed": str(seed),
                    "error": (
                        f"Unknown health check: {name!r}. "
                        f"Valid health checks are: {valid}"
                    ),
                }
                stream.send_request({"event": "test_done", "results": result}).get()
                return result

        if database is None:
            database_key = None

        if isinstance(database, str):
            database = DirectoryBasedExampleDatabase(database)  # type: ignore

        settings_kwargs = {
            "deadline": None,
            "max_examples": test_cases,
            "suppress_health_check": suppress,
            "backend": (
                "hypothesis-urandom"
                if os.environ.get("ANTITHESIS_OUTPUT_DIR")
                else "hypothesis"
            ),
            **({} if database is not_set else {"database": database}),
        }

        state = HegelState(connection, stream, is_final=False)
        runner = ConjectureRunner(
            state.test_function,
            settings=settings(**settings_kwargs),  # type: ignore
            random=Random(seed),
            database_key=database_key,
        )
        try:
            if failure_blob is not None:
                choices = decode_failure(failure_blob)
                data = ConjectureData.for_choices(choices)
                with contextlib.suppress(StopTest):
                    state.test_function(data)

                is_interesting = data.status is Status.INTERESTING
                result = {
                    "passed": not is_interesting,
                    "test_cases": 1,
                    "valid_test_cases": 0,
                    "invalid_test_cases": 0,
                    "interesting_test_cases": int(is_interesting),
                }
                if is_interesting:
                    result["failure_blobs"] = [failure_blob]
                    interesting_choices = [choices]
                else:
                    result["failure_blobs"] = []
                    interesting_choices = []
            else:
                runner.run()

                result = {
                    "passed": len(runner.interesting_examples) == 0,
                    "test_cases": runner.call_count,
                    "valid_test_cases": runner.valid_examples,
                    "invalid_test_cases": runner.invalid_examples,
                    "interesting_test_cases": len(runner.interesting_examples),
                    "seed": str(seed),
                }
                interesting_examples = sorted(
                    runner.interesting_examples.values(),
                    key=lambda d: sort_key(d.nodes),
                )

                interesting_choices = [v.choices for v in interesting_examples]

                result["failure_blobs"] = [
                    encode_failure(choices) for choices in interesting_choices
                ]
        except FailedHealthCheck as e:
            result = {
                "passed": False,
                "test_cases": runner.call_count,
                "valid_test_cases": runner.valid_examples,
                "invalid_test_cases": runner.invalid_examples,
                "interesting_test_cases": 0,
                "seed": str(seed),
                "health_check_failure": str(e),
            }
            _write_run_metrics(result)
            stream.send_request({"event": "test_done", "results": result}).get()
            return result
        except Flaky as e:
            result = _flaky_result(runner, seed, e, state.flaky_error)
            _write_run_metrics(result)
            stream.send_request({"event": "test_done", "results": result}).get()
            return result

        # Check for flaky behavior detected during test execution
        flaky_error = state.flaky_error
        if flaky_error is not None:
            result["passed"] = False
            result["flaky"] = _flaky_message(flaky_error)
        elif hasattr(runner, "exit_reason") and runner.exit_reason == ExitReason.flaky:
            result["passed"] = False
            result["flaky"] = FLAKY_TEST_RESULT_MSG

        _write_run_metrics(result)
        stream.send_request({"event": "test_done", "results": result}).get()

        final_state = HegelState(connection, stream, is_final=True)

        for choices in interesting_choices:
            with contextlib.suppress(StopTest):
                final_state.test_function(ConjectureData.for_choices(choices))

        return result
    except Exception:
        # We don't actually await the futures and just sortof run them fire and
        # forget in the background, so we won't see any exceptions that are
        # thrown unless we print them here.
        traceback.print_exc()
        raise
