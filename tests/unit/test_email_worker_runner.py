from __future__ import annotations

import logging
import threading

import pytest

from app import email_worker as runner


def _summary(*, claimed: int = 0, errors: int = 0) -> dict[str, int]:
    return {
        "claimed": claimed,
        "sent": claimed,
        "retry_scheduled": 0,
        "failed": 0,
        "errors": errors,
        "lease_lost": 0,
    }


def test_validate_options_accepts_defaults():
    runner._validate_options(batch_size=20, poll_interval_seconds=5, lease_seconds=1800)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"batch_size": 0, "poll_interval_seconds": 5, "lease_seconds": 1800}, "batch_size"),
        ({"batch_size": True, "poll_interval_seconds": 5, "lease_seconds": 1800}, "batch_size"),
        ({"batch_size": 51, "poll_interval_seconds": 5, "lease_seconds": 1800}, "batch_size"),
        ({"batch_size": 20, "poll_interval_seconds": 0.1, "lease_seconds": 1800}, "poll_interval_seconds"),
        ({"batch_size": 20, "poll_interval_seconds": 301, "lease_seconds": 1800}, "poll_interval_seconds"),
        ({"batch_size": 20, "poll_interval_seconds": 5, "lease_seconds": 59}, "lease_seconds"),
    ],
)
def test_validate_options_rejects_out_of_range_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        runner._validate_options(**kwargs)


def test_run_worker_waits_after_empty_batch_and_stops():
    event = threading.Event()
    calls = []

    class StopAfterWaitEvent:
        def is_set(self):
            return event.is_set()

        def set(self):
            event.set()

        def wait(self, timeout=None):
            calls.append(timeout)
            event.set()
            return True

    def processor(**kwargs):
        assert kwargs["batch_size"] == 7
        assert kwargs["worker_id"] == "test-worker"
        assert kwargs["lease_seconds"] == 900
        return _summary(claimed=0)

    runner.run_worker(
        batch_size=7,
        poll_interval_seconds=3,
        lease_seconds=900,
        worker_id="test-worker",
        stop_event=StopAfterWaitEvent(),
        processor=processor,
    )

    assert calls == [3.0]


def test_run_worker_drains_full_batches_without_waiting():
    event = threading.Event()
    calls = []

    def processor(**_kwargs):
        calls.append(1)
        if len(calls) == 2:
            event.set()
        return _summary(claimed=4)

    class NoWaitEvent:
        def is_set(self):
            return event.is_set()

        def wait(self, timeout=None):
            pytest.fail("A full batch should be drained immediately.")

    runner.run_worker(
        batch_size=4,
        poll_interval_seconds=5,
        stop_event=NoWaitEvent(),
        processor=processor,
    )

    assert len(calls) == 2


def test_run_worker_uses_bounded_error_backoff():
    event = threading.Event()
    waits = []

    class StopAfterWaitEvent:
        def is_set(self):
            return event.is_set()

        def set(self):
            event.set()

        def wait(self, timeout=None):
            waits.append(timeout)
            event.set()
            return True

    runner.run_worker(
        batch_size=5,
        poll_interval_seconds=10,
        stop_event=StopAfterWaitEvent(),
        processor=lambda **_kwargs: _summary(claimed=1, errors=1),
    )

    assert waits == [20.0]


def test_run_worker_does_not_log_processor_exception_message(caplog):
    event = threading.Event()

    class StopAfterWaitEvent:
        def is_set(self):
            return event.is_set()

        def wait(self, timeout=None):
            event.set()
            return True

    def processor(**_kwargs):
        raise RuntimeError("postgresql://user:secret@host/db")

    with caplog.at_level(logging.ERROR):
        runner.run_worker(
            batch_size=5,
            poll_interval_seconds=1,
            stop_event=StopAfterWaitEvent(),
            processor=processor,
        )

    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert "postgresql://" not in caplog.text


@pytest.mark.parametrize("worker_id", ["", "   ", "bad\nworker", "x" * 81, 123])
def test_validate_worker_id_rejects_invalid_values(worker_id):
    with pytest.raises(ValueError, match="worker_id"):
        runner._validate_worker_id(worker_id)


def test_validate_worker_id_accepts_safe_label():
    runner._validate_worker_id("worker-01.hostname")


def test_default_worker_id_is_safe_and_bounded():
    worker_id = runner._default_worker_id()
    assert worker_id
    assert len(worker_id) <= 80
    assert all(33 <= ord(char) <= 126 for char in worker_id)
