import sys
import threading
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from typing import Any

from hegel.utils import UniqueIdentifier, not_set

try:
    ExceptionGroup
except NameError:  # pragma: no cover
    from exceptiongroup import ExceptionGroup

import cbor2

from hegel.protocol import RequestError
from tests.client.protocol import ClientConnection, ClientStream

# Context variables for the current test case
_current_stream: ContextVar[ClientStream | None] = ContextVar(
    "_current_stream",
    default=None,
)
_is_final: ContextVar[bool] = ContextVar("_is_final", default=False)
_test_aborted: ContextVar[bool] = ContextVar("_test_aborted", default=False)


class AssumeRejected(Exception):
    """Raised when assume() condition is False."""


class DataExhausted(Exception):
    """Raised when the server runs out of test data (StopTest)."""


class HealthCheckFailure(Exception):
    """Raised when a health check fires during test execution."""


class FlakyTest(Exception):
    """Raised when the test or its data generation is non-deterministic."""


class Client:
    """Test client for connecting to a Hegel server."""

    def __init__(self, connection: ClientConnection):
        _version = connection.send_handshake()
        self.connection = connection
        self._control = connection.control_stream
        self.__lock = threading.Lock()
        self.last_result: dict | None = None

    def single_test_case(
        self,
        test_fn: Callable[[], None],
        *,
        seed: int | None = None,
    ) -> None:
        """Run a single test case."""

        test_stream = self.connection.new_stream()

        message: dict[str, Any] = {
            "command": "single_test_case",
            "seed": seed,
            "stream_id": test_stream.stream_id,
        }

        with self.__lock:
            self._control.send_request(message)

        result_data = None

        while True:
            packet = test_stream.read_request()
            message = cbor2.loads(packet.payload)
            event = message.get("event")

            if event == "test_case":
                stream_id = message["stream_id"]
                is_final = message.get("is_final", False)
                test_stream.write_reply(packet.message_id, None)
                test_case_stream = self.connection.connect_stream(stream_id)
                self._run_test_case(test_case_stream, test_fn, is_final=is_final)
            elif event == "test_done":
                test_stream.write_reply(packet.message_id, True)
                result_data = message["results"]
                break
            else:
                test_stream.write_reply_error(
                    packet.message_id,
                    error=f"Unrecognised event {event}",
                    error_type="InvalidMessage",
                )

        assert result_data is not None
        self.last_result = result_data

        if "error" in result_data:
            raise ValueError(result_data["error"])

        n_interesting = result_data["interesting_test_cases"]

        if n_interesting == 0:
            return

        # single_test_case runs in final mode, so the exception was already
        # raised during the test_case handling above. This path handles the
        # case where is_final was set but the exception wasn't propagated.
        raise ValueError("single_test_case failed but exception was not propagated")

    def run_test(
        self,
        test_fn: Callable[[], None],
        *,
        test_cases: int = 100,
        seed: int | None = None,
        failure_blob: bytes | None = None,
        suppress_health_check: list[str] | None = None,
        database_key: bytes | None = None,
        derandomize: bool = False,
        database: str | None | UniqueIdentifier = not_set,
        phases: list[str] | None = None,
        report_multiple_failures: bool | None = None,
    ) -> None:
        """Run a property test."""

        test_stream = self.connection.new_stream()

        message: dict[str, Any] = {
            "command": "run_test",
            "test_cases": test_cases,
            "seed": seed,
            "stream_id": test_stream.stream_id,
            "database_key": database_key,
            "derandomize": derandomize,
            "failure_blob": failure_blob,
        }
        if database is not not_set:
            message["database"] = database
        if suppress_health_check:
            message["suppress_health_check"] = suppress_health_check
        if phases is not None:
            message["phases"] = phases
        if report_multiple_failures is not None:
            message["report_multiple_failures"] = report_multiple_failures

        with self.__lock:
            self._control.send_request(message)

        result_data = None

        while True:
            packet = test_stream.read_request()
            message = cbor2.loads(packet.payload)
            event = message.get("event")

            if event == "test_case":
                stream_id = message["stream_id"]
                is_final = message.get("is_final", False)
                test_stream.write_reply(packet.message_id, None)
                test_case_stream = self.connection.connect_stream(stream_id)
                self._run_test_case(test_case_stream, test_fn, is_final=is_final)
            elif event == "test_done":
                test_stream.write_reply(packet.message_id, True)
                result_data = message["results"]
                break
            else:
                test_stream.write_reply_error(
                    packet.message_id,
                    error=f"Unrecognised event {event}",
                    error_type="InvalidMessage",
                )

        assert result_data is not None
        self.last_result = result_data

        if "error" in result_data:
            raise ValueError(result_data["error"])

        if "health_check_failure" in result_data:
            raise HealthCheckFailure(result_data["health_check_failure"])

        if "flaky" in result_data:
            raise FlakyTest(result_data["flaky"])

        n_interesting = result_data["interesting_test_cases"]

        if result_data["passed"] and failure_blob:
            raise AssertionError("failure blob did not reproduce")
        if n_interesting == 0:
            return

        exceptions: list[Exception] = []
        for i in range(n_interesting):
            try:
                packet = test_stream.read_request()
                message = cbor2.loads(packet.payload)
                assert message["event"] == "test_case"

                stream_id = message["stream_id"]
                test_stream.write_reply(packet.message_id, None)
                test_case_stream = self.connection.connect_stream(stream_id)
                self._run_test_case(test_case_stream, test_fn, is_final=True)
                if n_interesting > 1:
                    raise ValueError(
                        f"Expected test case {i} to fail but it didn't",
                    )
                else:
                    raise ValueError("Expected test case to fail but it didn't")
            except Exception as e:
                if n_interesting == 1:
                    raise
                exceptions.append(e)
        raise ExceptionGroup("multiple failures", exceptions)

    def _run_test_case(
        self,
        stream: ClientStream,
        test_fn: Callable[[], None],
        *,
        is_final: bool,
    ) -> None:
        """Run a single test case."""
        if _current_stream.get() is not None:
            raise RuntimeError(
                "Cannot nest test cases - already inside a test case",
            )
        _current_stream.set(stream)
        _is_final.set(is_final)
        _test_aborted.set(False)
        already_complete = False
        status = "VALID"
        origin = None
        try:
            test_fn()
        except AssumeRejected:
            status = "INVALID"
        except (DataExhausted, FlakyTest):
            already_complete = True
        except ConnectionError:
            raise
        except Exception as e:
            status = "INTERESTING"
            tb = e.__traceback__
            origin = _extract_origin(e, tb)
            if is_final:
                raise
        finally:
            _current_stream.set(None)
            _is_final.set(False)
            _test_aborted.set(False)
            if not already_complete:
                stream.write_request(
                    cbor2.dumps(
                        {
                            "command": "mark_complete",
                            "status": status,
                            "origin": origin,
                        }
                    )
                )
            stream.close()


def _extract_origin(exc: Exception, tb: Any) -> str:
    """Extract InterestingOrigin from an exception."""
    filename = ""
    lineno = 0

    if tb is not None:
        while tb.tb_next is not None:
            tb = tb.tb_next
        filename = tb.tb_frame.f_code.co_filename
        lineno = tb.tb_lineno

    return f"{type(exc).__name__} at {filename}:{lineno}"


def _get_stream() -> ClientStream:
    """Get the current test stream, raising if not in a test."""
    stream = _current_stream.get()
    if stream is None:
        raise RuntimeError(
            "Not in a test context - must be called from within a test function",
        )
    return stream


def _request(payload: dict) -> Any:
    """Send a request on the current test stream, handling server-side errors.

    Converts server-side StopTest/UnsatisfiedAssumption errors into
    DataExhausted so the client knows the test case is already complete.
    Converts flaky errors into FlakyTest with clear messages.
    """
    try:
        return _get_stream().send_request(payload)
    except RequestError as e:
        if e.error_type == "StopTest":
            _test_aborted.set(True)
            raise DataExhausted("Server ran out of data") from e
        if e.error_type == "FlakyStrategyDefinition":
            _test_aborted.set(True)
            raise FlakyTest(
                "Your data generation is non-deterministic: a call to "
                "generate() produced different results when replayed with "
                "the same random choices. This usually means your test "
                "depends on external state such as global variables, system "
                "time, or external random number generators."
            ) from e
        if e.error_type == "FlakyReplay":
            _test_aborted.set(True)
            raise FlakyTest(
                "Your test produced different outcomes when run with the "
                "same generated data. This usually means your test depends "
                "on external state such as global variables, system time, "
                "or network calls."
            ) from e
        raise


def generate_from_schema(schema: dict) -> Any:
    """Generate a value from a schema."""
    return _request({"command": "generate", "schema": schema})


def assume(condition: bool) -> None:
    """Reject the current test case if condition is False."""
    if not condition:
        raise AssumeRejected


def note(message: str) -> None:
    """Record a message that will be printed on the final (failing) run."""
    if _is_final.get():
        print(message, file=sys.stderr)


def target(value: float, label: str = "") -> None:
    """Guide the search toward higher values."""
    _request({"command": "target", "value": value, "label": label})


def start_span(label: int = 0) -> None:
    """Start a generation span for better shrinking."""
    if _test_aborted.get():
        return
    _request({"command": "start_span", "label": label})


def stop_span(*, discard: bool = False) -> None:
    """End the current generation span."""
    if _test_aborted.get():
        return
    _request({"command": "stop_span", "discard": discard})


def run_state_machine(
    rules: Sequence[Callable[[], None]],
    *,
    invariants: Sequence[Callable[[], None]] = (),
    steps: int,
) -> None:
    state_machine_id = _request(
        {
            "command": "new_state_machine",
            "rules": [{"name": rule.__name__} for rule in rules],
            "invariants": [{"name": invariant.__name__} for invariant in invariants],
        }
    )

    def check_invariants() -> None:
        for invariant in invariants:
            invariant()

    check_invariants()
    for _ in range(steps):
        index = _request({"command": "next_rule", "state_machine_id": state_machine_id})
        try:
            rules[index]()
        except AssumeRejected:
            continue
        check_invariants()


class collection:
    def __init__(self, min_size: int = 0, max_size: int | None = None):
        self.__collection_id = None
        self.__finished = False
        self.min_size = min_size
        self.max_size = max_size

    @property
    def _collection_id(self):
        if self.__collection_id is None:
            self.__collection_id = _request(
                {
                    "command": "new_collection",
                    "min_size": self.min_size,
                    "max_size": self.max_size,
                }
            )
        return self.__collection_id

    def more(self) -> bool:
        """Should we generate another element?"""
        if self.__finished:
            return False

        result = _request(
            {"command": "collection_more", "collection_id": self._collection_id}
        )
        if not result:
            self.__finished = True
        return result

    def reject(self, why: str | None = None) -> None:
        """We did not add the last element to the collection,
        don't count it towards our size budget."""
        if not self.__finished:
            _request(
                {
                    "command": "collection_reject",
                    "collection_id": self._collection_id,
                    "why": why,
                }
            )
