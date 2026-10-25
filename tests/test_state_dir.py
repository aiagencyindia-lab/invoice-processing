"""Durable runtime state goes where STATE_DIR says; tracked files do not."""

from __future__ import annotations

from invoice_processing import paths
from invoice_processing.api import service
from invoice_processing.worker import queue as queue_mod
from invoice_processing.worker import runner


def test_unset_keeps_everything_under_the_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    assert paths.state_dir() == tmp_path


def test_an_empty_value_counts_as_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", "  ")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    assert paths.state_dir() == tmp_path


def test_set_moves_documents_queue_and_heartbeat(monkeypatch, tmp_path):
    state = tmp_path / "volume"
    monkeypatch.setenv("STATE_DIR", f" {state}/ ")
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path / "image")
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.delenv("PUBSUB_TOPIC", raising=False)
    assert paths.state_dir() == state
    assert service.get_blobs("t1").root == state / "blobs" / "t1"
    assert queue_mod.get_queue().root == state / "queue"
    assert runner.heartbeat_path() == state / "worker_heartbeat.json"


def test_tracked_files_stay_in_the_image(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "volume"))
    assert paths.RULES_BOOK_PATH.parent == paths.DATA_DIR


def test_reference_data_files_follow_state_dir(monkeypatch, tmp_path):
    """Work authorizations, the vendor master and purchase-order files are
    written at run time with no database branch for work authorizations;
    in the image they vanished on every deploy (review finding)."""
    state = tmp_path / "volume"
    monkeypatch.setenv("STATE_DIR", str(state))
    assert paths.work_authorization_file("t1") == (
        state / "work_authorizations" / "t1.json"
    )
    assert paths.vendor_master_file("t1") == state / "vendors" / "t1.json"
    assert paths.purchase_order_file("t1") == (
        state / "purchase_orders" / "t1.json"
    )


def test_reference_data_files_stay_put_without_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("STATE_DIR", raising=False)
    monkeypatch.setattr(paths, "WORK_AUTHORIZATION_DIR", tmp_path / "wa")
    assert paths.work_authorization_file("t1") == tmp_path / "wa" / "t1.json"
