"""Run one pure local report step at a time under the coordinator's lease."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import math
from threading import Event, Thread
from typing import Any

from suwan.tasks.node_protocol import NodeClient, NodeProtocolError


_PENDING_STATUS = {
    "claim": "claim_confirmation_pending",
    "renew": "lease_confirmation_pending",
    "submit": "result_confirmation_pending",
    "fail": "failure_confirmation_pending",
}
_AMBIGUOUS_ERRORS = frozenset({"invalid_response", "transport_timeout", "transport_interrupted"})
_INVALID_RESULTS = frozenset({"invalid_result", "result_too_large", "invalid_request", "request_too_large"})


class ReportNodeWorker:
    """A single driver claims, renews and completes work.

    The supplied processor computes a result locally without side effects.
    Only the coordinator can grant a lease or confirm that a result was accepted.
    """

    def __init__(
        self,
        client: NodeClient,
        processor: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        heartbeat_interval: float = 0.5,
    ) -> None:
        if type(heartbeat_interval) not in (int, float) or not 0 < heartbeat_interval < math.inf:
            raise ValueError("invalid worker heartbeat interval")
        self._client = client
        self._processor = processor
        self._heartbeat_interval = heartbeat_interval
        self._lease: dict[str, Any] | None = None
        self._result: dict[str, Any] | None = None
        self._failure: tuple[str, bool] | None = None
        self._status = "idle"
        self._error_code: str | None = None

    @property
    def lease(self) -> dict[str, Any] | None:
        """Return a copy of the local receipt; status describes its confirmation."""
        return deepcopy(self._lease)

    @property
    def status(self) -> str:
        return self._status

    @property
    def error_code(self) -> str | None:
        return self._error_code

    def _set_status(self, status: str, error_code: str | None = None) -> None:
        self._status = status
        self._error_code = error_code

    def _request_failed(self, exc: OSError | NodeProtocolError, operation: str) -> None:
        pending = _PENDING_STATUS[operation]
        if isinstance(exc, NodeProtocolError):
            code = exc.code
        elif isinstance(exc, ConnectionRefusedError):
            code = "coordinator_unreachable"
        elif isinstance(exc, TimeoutError):
            code = "transport_timeout"
        else:
            code = "transport_interrupted"

        if code in _AMBIGUOUS_ERRORS or self._status == pending:
            self._set_status(pending, code)
        elif code in {"coordinator_unreachable", "connection_unavailable"}:
            self._set_status("waiting_for_coordinator", code)
        elif code in {"storage_busy", "storage_unavailable"}:
            self._set_status(code, code)
        else:
            self._set_status("protocol_error", code)

    def _release(self, status: str, error_code: str | None = None) -> None:
        self._lease = None
        self._result = None
        self._failure = None
        self._set_status(status, error_code)

    def claim(self, *, checkpoint_seq: int | None = None) -> dict[str, Any] | None:
        """Ask for work or return a held lease only after renewing it."""
        if self._lease is not None:
            if checkpoint_seq is not None and self._lease["payload"].get("checkpoint_seq") != checkpoint_seq:
                return None
            return self.lease if self.renew() else None
        try:
            self._lease = self._client.claim() if checkpoint_seq is None else self._client.claim(
                checkpoint_seq=checkpoint_seq
            )
        except (OSError, NodeProtocolError) as exc:
            self._request_failed(exc, "claim")
            return None
        self._set_status("idle" if self._lease is None else "leased")
        return self.lease

    def renew(self) -> bool:
        """Renew held work before submitting its first result."""
        if self._lease is None or self._result is not None or self._failure is not None:
            # Replaying a pending receipt distinguishes acceptance from expiry.
            return False
        try:
            renewed = self._client.renew(self._lease)
        except (OSError, NodeProtocolError) as exc:
            self._request_failed(exc, "renew")
            return False
        if renewed:
            self._set_status("leased")
        else:
            self._release("lease_rejected")
        return renewed

    def _keep_lease(self, lease: dict[str, Any], stop: Event, lost: Event) -> None:
        interval = self._heartbeat_interval
        lease_seconds = lease.get("lease_seconds")
        if type(lease_seconds) in (int, float) and 0 < lease_seconds < math.inf:
            interval = min(interval, lease_seconds / 3)
        while not stop.wait(interval):
            try:
                renewed = self._client.renew(lease)
            except (OSError, NodeProtocolError):
                # A transport error cannot prove loss of authority. The final
                # submit or fail still has to pass the coordinator's fence.
                continue
            if not renewed:
                lost.set()
                return

    def _process(self, lease: dict[str, Any]) -> bool:
        stop, lost = Event(), Event()
        heartbeat = Thread(target=self._keep_lease, args=(lease, stop, lost), name="p2-lease-heartbeat", daemon=True)
        try:
            heartbeat.start()
        except RuntimeError:
            self._failure = ("heartbeat_unavailable", True)
            return True
        try:
            result = self._processor(deepcopy(lease["payload"]))
            if isinstance(result, dict):
                self._result = deepcopy(result)
            else:
                self._failure = ("invalid_result", False)
        except Exception:
            self._failure = ("processor_failed", True)
        finally:
            stop.set()
            heartbeat.join()
        if lost.is_set():
            self._release("lease_rejected")
            return False
        return True

    def _confirm_failure(self, lease: dict[str, Any], failure: tuple[str, bool]) -> None:
        reason, retryable = failure
        try:
            accepted = self._client.fail(lease, reason, retryable=retryable)
        except (OSError, NodeProtocolError) as exc:
            self._request_failed(exc, "fail")
            return
        if accepted:
            self._release("processing_failed" if retryable else "invalid_result", reason)
        else:
            self._release("lease_rejected")

    def complete(self) -> bool:
        """Compute once under the lease and settle the same result or failure."""
        lease = self._lease
        if lease is None:
            return False
        if self._result is None and self._failure is None:
            if not self.renew() or not self._process(lease):
                return False
        if self._failure is not None:
            self._confirm_failure(lease, self._failure)
            return False
        assert self._result is not None
        try:
            accepted = self._client.submit(lease, self._result)
        except NodeProtocolError as exc:
            if exc.code in _INVALID_RESULTS:
                self._result = None
                reason = exc.code if exc.code == "result_too_large" else "invalid_result"
                self._failure = (reason, False)
                self._confirm_failure(lease, self._failure)
            else:
                self._request_failed(exc, "submit")
            return False
        except OSError as exc:
            self._request_failed(exc, "submit")
            return False
        self._release("completed" if accepted else "lease_rejected")
        return accepted
