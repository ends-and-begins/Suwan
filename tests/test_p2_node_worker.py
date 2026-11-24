"""Worker outcomes use coordinator receipts and preserve an uncertain result."""
from __future__ import annotations

from threading import Event
import unittest
from unittest.mock import Mock, patch

from suwan.tasks.node_protocol import NodeClient, NodeProtocolError
from suwan.tasks.node_worker import ReportNodeWorker


class ReportNodeWorkerTest(unittest.TestCase):
    def setUp(self):
        self.lease = {
            "task_id": "report-step",
            "version": 1,
            "node_id": "first",
            "instance_id": "run",
            "generation": 2,
            "payload": {"source_id": "S01"},
        }
        self.client = Mock(spec=NodeClient)
        self.client.claim.return_value = self.lease
        self.client.renew.return_value = True
        self.client.submit.return_value = True
        self.client.fail = Mock(return_value=True)
        self.processor = Mock(return_value={"facts": ["确认 24 人"]})
        self.worker = ReportNodeWorker(self.client, self.processor)

    def test_one_local_lease_can_be_renewed_and_completed(self):
        lease = self.worker.claim()
        self.assertEqual(lease, self.lease)
        lease["generation"] = 99
        self.assertEqual(self.worker.claim(), self.lease)
        self.client.claim.assert_called_once()
        self.client.renew.assert_called_once_with(self.lease)

        self.assertTrue(self.worker.renew())
        self.assertTrue(self.worker.complete())
        self.assertEqual(self.client.renew.call_count, 3)
        self.client.submit.assert_called_once_with(self.lease, {"facts": ["确认 24 人"]})
        self.assertEqual(self.worker.status, "completed")
        self.assertIsNone(self.worker.lease)

    def test_disconnect_during_claim_and_renew_remains_truthful(self):
        self.client.claim.side_effect = [ConnectionRefusedError(), self.lease]
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status, "waiting_for_coordinator")
        self.assertEqual(self.worker.claim(), self.lease)

        self.client.renew.side_effect = ConnectionRefusedError
        self.assertFalse(self.worker.renew())
        self.assertEqual(self.worker.status, "waiting_for_coordinator")
        self.assertEqual(self.worker.lease, self.lease)
        self.client.submit.assert_not_called()

    def test_lost_submission_confirmation_resends_same_result_only(self):
        self.client.submit.side_effect = [NodeProtocolError("invalid_response"), True]
        self.worker.claim()
        self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "result_confirmation_pending")
        self.assertEqual(self.worker.lease, self.lease)
        self.assertFalse(self.worker.renew())
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status, "result_confirmation_pending")
        self.client.renew.assert_called_once_with(self.lease)

        self.assertTrue(self.worker.complete())
        self.assertEqual(self.worker.status, "completed")
        self.assertEqual(self.client.submit.call_args_list[0], self.client.submit.call_args_list[1])
        self.processor.assert_called_once_with({"source_id": "S01"})

    def test_explicit_rejection_clears_lease_without_showing_completion(self):
        self.client.submit.return_value = False
        self.worker.claim()
        self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "lease_rejected")
        self.assertIsNone(self.worker.lease)
        self.assertFalse(self.worker.complete())
        self.client.submit.assert_called_once()

    def test_protocol_failure_is_visible_and_never_reported_as_idle(self):
        self.client.claim.side_effect = NodeProtocolError("protocol_mismatch")
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status, "protocol_error")
        self.assertEqual(self.worker.error_code, "protocol_mismatch")

    def test_claim_timeout_and_storage_busy_have_distinct_statuses(self):
        self.client.claim.side_effect = [NodeProtocolError("storage_busy"), TimeoutError(), ConnectionRefusedError()]
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status, "storage_busy")
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status, "claim_confirmation_pending")
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status, "claim_confirmation_pending")
        self.assertEqual(self.worker.error_code, "coordinator_unreachable")

    def test_unavailable_connection_before_send_reports_coordinator_wait(self):
        self.client.claim.side_effect = NodeProtocolError("connection_unavailable")
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status, "waiting_for_coordinator")
        self.assertEqual(self.worker.error_code, "connection_unavailable")

    def test_processor_failure_is_fenced_and_releases_worker(self):
        self.processor.side_effect = ValueError
        self.worker.claim()
        self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "processing_failed")
        self.assertIsNone(self.worker.lease)
        self.client.fail.assert_called_once_with(self.lease, "processor_failed", retryable=True)
        self.client.submit.assert_not_called()

    def test_uncertain_failure_receipt_keeps_the_same_failure_to_confirm(self):
        self.processor.side_effect = ValueError
        self.client.fail.side_effect = [TimeoutError(), True]
        self.worker.claim()
        self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "failure_confirmation_pending")
        self.assertIsNone(self.worker.claim())
        self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "processing_failed")
        self.assertIsNone(self.worker.lease)
        self.processor.assert_called_once()
        self.assertEqual(self.client.fail.call_args_list[0], self.client.fail.call_args_list[1])

    def test_definite_invalid_result_closes_the_lease_and_allows_a_new_claim(self):
        self.client.submit.side_effect = NodeProtocolError("result_too_large")
        self.worker.claim()
        self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "invalid_result")
        self.assertIsNone(self.worker.lease)
        self.client.fail.assert_called_once_with(self.lease, "result_too_large", retryable=False)
        self.assertFalse(self.worker.complete())
        self.client.submit.assert_called_once()
        self.client.claim.return_value = {**self.lease, "task_id": "next-step"}
        self.assertEqual(self.worker.claim()["task_id"], "next-step")

    def test_non_object_result_is_failed_without_entering_submit(self):
        self.processor.return_value = []
        self.worker.claim()
        self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "invalid_result")
        self.client.fail.assert_called_once_with(self.lease, "invalid_result", retryable=False)
        self.client.submit.assert_not_called()

    def test_heartbeat_start_failure_settles_without_starting_processor(self):
        self.worker.claim()
        with patch("suwan.tasks.node_worker.Thread.start", side_effect=RuntimeError("no thread available")):
            self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "processing_failed")
        self.assertEqual(self.worker.error_code, "heartbeat_unavailable")
        self.client.fail.assert_called_once_with(self.lease, "heartbeat_unavailable", retryable=True)
        self.processor.assert_not_called()

    def test_expired_cached_lease_is_not_returned_as_a_claim(self):
        self.worker.claim()
        self.client.renew.return_value = False
        self.assertIsNone(self.worker.claim())
        self.assertEqual(self.worker.status, "lease_rejected")
        self.assertIsNone(self.worker.lease)
        self.processor.assert_not_called()

    def test_expired_lease_is_rejected_before_computation(self):
        self.worker.claim()
        self.client.renew.return_value = False
        self.assertFalse(self.worker.complete())
        self.assertEqual(self.worker.status, "lease_rejected")
        self.processor.assert_not_called()
        self.client.submit.assert_not_called()

    def test_slow_processing_renews_in_parallel_and_can_submit_after_original_deadline(self):
        started, renewed_during_work = Event(), Event()
        simulated_time = 0.0
        deadline = 2.5

        def renew(_lease):
            nonlocal deadline
            if simulated_time >= deadline:
                return False
            deadline = simulated_time + 2.5
            if started.is_set():
                renewed_during_work.set()
            return True

        def slow_compute(_payload):
            nonlocal simulated_time
            simulated_time = 2.0
            started.set()
            if not renewed_during_work.wait(1):
                raise RuntimeError("heartbeat never ran")
            simulated_time = 3.0
            return {"facts": ["确认 24 人"]}

        client = self.client
        client.renew.side_effect = renew
        client.submit.side_effect = lambda _lease, _result: simulated_time < deadline
        worker = ReportNodeWorker(client, slow_compute, heartbeat_interval=0.01)
        worker.claim()
        self.assertTrue(worker.complete())
        self.assertTrue(renewed_during_work.is_set())
        self.assertEqual(worker.status, "completed")
        self.assertEqual(simulated_time, 3.0)

    def test_loss_during_processing_discards_computed_result(self):
        renewal_called = Event()

        def reject():
            renewal_called.set()
            return False

        renewals = iter((lambda: True, reject))
        self.client.renew.side_effect = lambda _lease: next(renewals)()

        def compute(_payload):
            if not renewal_called.wait(1):
                raise RuntimeError("heartbeat never ran")
            return {"facts": ["确认 24 人"]}

        worker = ReportNodeWorker(self.client, compute, heartbeat_interval=0.01)
        worker.claim()
        self.assertFalse(worker.complete())
        self.assertEqual(worker.status, "lease_rejected")
        self.assertIsNone(worker.lease)
        self.client.submit.assert_not_called()
        self.client.fail.assert_not_called()
