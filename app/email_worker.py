"""Standalone, gracefully stoppable polling process for the email outbox.

Run with::

    python -m app.email_worker

The FastAPI application does not import or start this process. Deploy it as a
separate supervised process/container so API and delivery-worker lifecycles
remain independent.
"""

from __future__ import annotations

import argparse
import logging
import re
import signal
import socket
import threading
from collections.abc import Callable, Sequence
from typing import Any

from app.core.config import get_settings
from app.services.email_outbox_worker import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_LEASE_SECONDS,
    MAX_BATCH_SIZE,
    MAX_LEASE_SECONDS,
    MIN_LEASE_SECONDS,
    process_pending_emails_once,
)

logger = logging.getLogger("skillbridge.email_worker")
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
MIN_POLL_INTERVAL_SECONDS = 0.5
MAX_POLL_INTERVAL_SECONDS = 300.0
ERROR_BACKOFF_MAX_SECONDS = 30.0

Processor = Callable[..., dict[str, int]]


def _validate_options(
    *, batch_size: int, poll_interval_seconds: float, lease_seconds: int
) -> None:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int):
        raise ValueError("batch_size must be an integer.")
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}.")

    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
        raise ValueError("lease_seconds must be an integer.")
    if not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(
            f"lease_seconds must be between {MIN_LEASE_SECONDS} and {MAX_LEASE_SECONDS}."
        )

    if isinstance(poll_interval_seconds, bool) or not isinstance(
        poll_interval_seconds, (int, float)
    ):
        raise ValueError("poll_interval_seconds must be a number.")
    if not MIN_POLL_INTERVAL_SECONDS <= float(poll_interval_seconds) <= MAX_POLL_INTERVAL_SECONDS:
        raise ValueError(
            "poll_interval_seconds must be between "
            f"{MIN_POLL_INTERVAL_SECONDS} and {MAX_POLL_INTERVAL_SECONDS}."
        )


def _validate_worker_id(worker_id: str | None) -> None:
    if worker_id is None:
        return
    if not isinstance(worker_id, str) or not worker_id.strip():
        raise ValueError("worker_id must be a non-empty string when supplied.")
    normalized = worker_id.strip()
    if len(normalized) > 80 or any(ord(char) < 33 or ord(char) > 126 for char in normalized):
        raise ValueError("worker_id contains invalid characters or is too long.")


def _default_worker_id() -> str:
    """Return a short, non-sensitive worker label accepted by the outbox worker."""
    hostname = re.sub(r"[^A-Za-z0-9_.-]", "-", socket.gethostname())[:48] or "host"
    return f"{hostname}:{threading.get_native_id()}"


def _log_summary(summary: dict[str, int]) -> None:
    logger.info(
        "Email outbox batch completed claimed=%d sent=%d retry_scheduled=%d "
        "failed=%d errors=%d lease_lost=%d",
        summary.get("claimed", 0),
        summary.get("sent", 0),
        summary.get("retry_scheduled", 0),
        summary.get("failed", 0),
        summary.get("errors", 0),
        summary.get("lease_lost", 0),
    )


def run_worker(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    worker_id: str | None = None,
    stop_event: threading.Event | None = None,
    processor: Processor | None = None,
) -> None:
    """Poll and process bounded batches until ``stop_event`` is set.

    The loop waits between empty/partial batches, immediately drains a full
    batch, and uses a bounded backoff after a batch reports infrastructure or
    persistence errors. SIGINT/SIGTERM are translated to a stop event by
    ``main``. An in-progress synchronous batch is allowed to finish before exit.
    """
    _validate_options(
        batch_size=batch_size,
        poll_interval_seconds=poll_interval_seconds,
        lease_seconds=lease_seconds,
    )
    _validate_worker_id(worker_id)

    event = stop_event or threading.Event()
    process_batch = processor or process_pending_emails_once
    effective_worker_id = worker_id or _default_worker_id()
    poll_interval = float(poll_interval_seconds)

    logger.info(
        "Email outbox worker started worker_id=%s batch_size=%d poll_interval_seconds=%s lease_seconds=%d",
        effective_worker_id,
        batch_size,
        poll_interval,
        lease_seconds,
    )

    while not event.is_set():
        try:
            summary = process_batch(
                batch_size=batch_size,
                worker_id=effective_worker_id,
                lease_seconds=lease_seconds,
            )
            if not isinstance(summary, dict):
                raise TypeError("Outbox processor returned an invalid summary.")
            _log_summary(summary)
        except Exception as exc:
            # Do not log exception text: database/provider errors can contain
            # connection details or other operational secrets.
            logger.error(
                "Email outbox batch raised an unexpected error error_type=%s",
                type(exc).__name__,
            )
            event.wait(min(max(poll_interval, 2.0), ERROR_BACKOFF_MAX_SECONDS))
            continue

        if event.is_set():
            break

        claimed = summary.get("claimed", 0)
        errors = summary.get("errors", 0)
        if isinstance(claimed, int) and claimed >= batch_size and not errors:
            # There may be more immediately-eligible rows; drain without an
            # unnecessary sleep while keeping every individual batch bounded.
            continue

        wait_seconds = poll_interval
        if errors:
            wait_seconds = min(max(poll_interval * 2, 2.0), ERROR_BACKOFF_MAX_SECONDS)
        event.wait(wait_seconds)

    logger.info("Email outbox worker stopped worker_id=%s", effective_worker_id)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the SkillBridge transactional email outbox worker."
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Maximum messages per batch (1-{MAX_BATCH_SIZE}; default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help=(
            "Seconds to wait after an empty/partial batch "
            f"({MIN_POLL_INTERVAL_SECONDS}-{MAX_POLL_INTERVAL_SECONDS}); "
            "uses OUTBOX_POLL_INTERVAL_SECONDS when omitted."
        ),
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        default=None,
        help=(
            f"Processing lease duration ({MIN_LEASE_SECONDS}-{MAX_LEASE_SECONDS}); "
            "uses OUTBOX_LEASE_SECONDS when omitted."
        ),
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="Optional non-sensitive worker label (a unique lease owner is generated per batch).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one batch and exit instead of starting the polling loop.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; suitable for ``python -m app.email_worker``."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
        poll_interval_seconds = (
            args.poll_interval
            if args.poll_interval is not None
            else settings.OUTBOX_POLL_INTERVAL_SECONDS
        )
        lease_seconds = (
            args.lease_seconds
            if args.lease_seconds is not None
            else settings.OUTBOX_LEASE_SECONDS
        )
        _validate_options(
            batch_size=args.batch_size,
            poll_interval_seconds=poll_interval_seconds,
            lease_seconds=lease_seconds,
        )
        _validate_worker_id(args.worker_id)
    except ValueError as exc:
        parser.error(str(exc))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    worker_id = args.worker_id or _default_worker_id()

    if args.once:
        try:
            summary = process_pending_emails_once(
                batch_size=args.batch_size,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )
            _log_summary(summary)
            return 1 if summary.get("errors", 0) else 0
        except Exception as exc:
            logger.error(
                "One-shot email outbox execution failed error_type=%s",
                type(exc).__name__,
            )
            return 1

    stop_event = threading.Event()
    previous_handlers: dict[signal.Signals, Any] = {}

    def _request_stop(signum: int, _frame: Any) -> None:
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = "UNKNOWN"
        logger.info("Email outbox worker received shutdown signal=%s", signal_name)
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, _request_stop)
        except (OSError, RuntimeError, ValueError):
            # Signal handlers can only be installed from the main interpreter
            # thread; service managers still control process termination.
            logger.warning("Unable to install signal handler signal=%s", signum.name)

    try:
        run_worker(
            batch_size=args.batch_size,
            poll_interval_seconds=poll_interval_seconds,
            lease_seconds=lease_seconds,
            worker_id=worker_id,
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        stop_event.set()
        logger.info("Email outbox worker interrupted; shutting down.")
    finally:
        for signum, previous in previous_handlers.items():
            try:
                signal.signal(signum, previous)
            except (OSError, RuntimeError, ValueError):
                pass

    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by CLI invocation
    raise SystemExit(main())
