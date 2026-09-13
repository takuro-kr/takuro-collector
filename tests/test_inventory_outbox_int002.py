from __future__ import annotations

import inspect
import json
import re
import sqlite3
import uuid

import pytest

import takuro_collector.collector as collector_module
from takuro_collector.collector import CollectorEngine
from takuro_collector.db import Database, SCHEMA_VERSION
from takuro_collector.models import PropertyCandidate
from takuro_collector.sites.base import DiscoveryResult
from takuro_collector.ui import MainWindow
from takuro_collector.wordpress import WordPressClient, WordPressError, WordPressSync


def item(source_id: str) -> dict:
    return {
        "source_property_id": source_id,
        "building_name": f"Building {source_id}",
        "room": source_id,
        "source_url": f"https://example.test/{source_id}",
    }


def accepted(kwargs: dict, *, duplicate: bool = False) -> dict:
    return {
        "accepted": True,
        "run_id": kwargs["run_id"],
        "accepted_count": len(kwargs["source_property_ids"]),
        "duplicate_run": duplicate,
        "transport": "registration_v2_inventory_rest",
    }


def test_new_database_has_append_only_inventory_outbox(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    assert SCHEMA_VERSION == 6
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(inventory_delivery_outbox)")}
    assert {"run_id", "completed_at", "payload_checksum", "delivery_status", "accepted_at"} <= columns
    db.close()


def test_legacy_pending_snapshot_migrates_once_and_synced_does_not(tmp_path):
    path = tmp_path / "db.sqlite3"
    db = Database(path)
    property_id, _ = db.upsert_property(PropertyCandidate(
        source_site="legacy.example", source_property_id="property-one", management_company="legacy",
        building_name="Legacy Building", room="101", prefecture="東京都", address="東京都新宿区1-1",
        source_url="https://legacy.example/one", rent=50000,
    ))
    db.set_setting("migration_sentinel", "preserved")
    pending_id = uuid.uuid4().hex
    synced_id = uuid.uuid4().hex
    payload = json.dumps([item("one")], separators=(",", ":"))
    with db.conn:
        db.conn.execute("DELETE FROM inventory_delivery_outbox")
        db.conn.execute("DELETE FROM inventory_snapshots")
        db.conn.execute(
            "INSERT INTO inventory_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            ("KIN", "kin.example", pending_id, 1, payload, "2026-09-14T09:00:00+09:00", "pending", "", ""),
        )
        db.conn.execute(
            "INSERT INTO inventory_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            ("AMB", "amb.example", synced_id, 1, payload, "2026-09-14T09:00:00+09:00", "synced", "x", ""),
        )
        db.conn.execute("UPDATE settings SET value='5' WHERE key='schema_version'")
    db.close()

    upgraded = Database(path)
    rows = upgraded.inventory_outbox_rows()
    assert [row["run_id"] for row in rows] == [pending_id]
    assert rows[0]["completed_at"] == "2026-09-14T00:00:00.000000Z"
    upgraded.install()
    assert len(upgraded.inventory_outbox_rows()) == 1
    assert upgraded.conn.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()[0] == "6"
    assert upgraded.get_setting("migration_sentinel") == "preserved"
    assert upgraded.property(property_id)["source_property_id"] == "property-one"
    upgraded.close()


def test_invalid_legacy_token_is_preserved_but_not_delivered(tmp_path):
    path = tmp_path / "db.sqlite3"
    db = Database(path)
    with db.conn:
        db.conn.execute("DELETE FROM inventory_delivery_outbox")
        db.conn.execute("DELETE FROM inventory_snapshots")
        db.conn.execute(
            "INSERT INTO inventory_snapshots VALUES(?,?,?,?,?,?,?,?,?)",
            ("KIN", "kin.example", "legacy-token", 1, json.dumps([item("one")]),
             "2026-09-14T00:00:00Z", "pending", "", ""),
        )
        db.conn.execute("UPDATE settings SET value='5' WHERE key='schema_version'")
    db.close()
    upgraded = Database(path)
    assert upgraded.inventory_outbox_rows() == []
    legacy = upgraded.conn.execute("SELECT * FROM inventory_snapshots").fetchone()
    assert legacy["snapshot_token"] == "legacy-token"
    assert "이관 보류" in legacy["last_error"]
    upgraded.close()


def test_run_is_uuid4_utc_and_canonical_checksum_is_stable(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    first = db.save_inventory_snapshot("KIN", "EXAMPLE.COM", [item("b"), item("a"), item("a")])
    parsed = uuid.UUID(hex=first["run_id"])
    assert parsed.version == 4
    assert re.fullmatch(r"[0-9a-f]{32}", first["run_id"])
    assert first["completed_at"].endswith("Z")
    fixed_run = uuid.uuid4().hex
    one = db.save_inventory_snapshot(
        "AMB", "amb.example", [item("b"), item("a"), item("a")],
        run_id=fixed_run, completed_at="2026-09-14T00:00:00Z",
    )
    two = db.save_inventory_snapshot(
        "AMB", "amb.example", [item("a"), item("b")],
        run_id=fixed_run, completed_at="2026-09-14T00:00:00Z",
    )
    assert one["payload_checksum"] == two["payload_checksum"]
    assert len([row for row in db.inventory_outbox_rows() if row["run_id"] == fixed_run]) == 1
    db.close()


def test_same_run_different_payload_conflicts_without_partial_current_update(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    run_id = uuid.uuid4().hex
    db.save_inventory_snapshot(
        "KIN", "kin.example", [item("a")], run_id=run_id,
        completed_at="2026-09-14T00:00:00Z",
    )
    with pytest.raises(ValueError, match="서로 다른 payload"):
        db.save_inventory_snapshot(
            "KIN", "kin.example", [item("b")], run_id=run_id,
            completed_at="2026-09-14T00:00:00Z",
        )
    current = db.conn.execute("SELECT items_json FROM inventory_snapshots WHERE site_code='KIN'").fetchone()
    assert json.loads(current[0])[0]["source_property_id"] == "a"
    db.close()


def test_outbox_insert_failure_rolls_back_current_snapshot(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.conn.execute(
        "CREATE TRIGGER reject_inventory_outbox BEFORE INSERT ON inventory_delivery_outbox "
        "BEGIN SELECT RAISE(ABORT, 'injected outbox failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected outbox failure"):
        db.save_inventory_snapshot("KIN", "kin.example", [item("one")])
    assert db.conn.execute("SELECT COUNT(*) FROM inventory_snapshots").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM inventory_delivery_outbox").fetchone()[0] == 0
    db.close()


def test_new_discovery_appends_without_overwriting_pending_run(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    first = db.save_inventory_snapshot("KIN", "kin.example", [item("a")])
    second = db.save_inventory_snapshot("KIN", "kin.example", [item("a"), item("b")])
    assert first["run_id"] != second["run_id"]
    assert len(db.inventory_outbox_rows()) == 2
    assert db.conn.execute("SELECT snapshot_token FROM inventory_snapshots WHERE site_code='KIN'").fetchone()[0] == second["run_id"]
    db.close()


def test_incomplete_discovery_does_not_create_inventory_run(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    url = "https://example.test/one"

    class Adapter:
        code = "TEST"
        label = "Test"

        def discover(self, _fetcher):
            return DiscoveryResult(
                [url], listed_count=1, inventory_complete=False,
                inventory_site="example.test", inventory_items={url: item("one")},
            )

        def collect_url(self, _fetcher, _url):
            return PropertyCandidate(
                source_site="example.test", source_property_id="one", management_company="test",
                building_name="Building", room="101", prefecture="東京都", address="東京都新宿区1-1",
                source_url=url, rent=60000,
            )

    monkeypatch.setattr(collector_module, "adapters", lambda: [Adapter()])
    monkeypatch.setattr(collector_module.PhotoManager, "download_for_property", lambda *_args: None)
    result = CollectorEngine(db).scan_all()
    assert result.inventory_snapshots == 0
    assert db.inventory_outbox_rows() == []
    db.close()


def test_sending_run_recovers_on_database_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    db = Database(path)
    run = db.save_inventory_snapshot("KIN", "kin.example", [item("a")])
    db.mark_inventory_run(run["run_id"], state="sending")
    db.close()
    reopened = Database(path)
    row = reopened.inventory_outbox_rows()[0]
    assert row["delivery_status"] == "retryable_error"
    assert reopened.pending_inventory_runs()[0]["run_id"] == run["run_id"]
    reopened.close()


def test_same_site_fifo_and_other_site_continue_after_retryable_failure(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    k1 = db.save_inventory_snapshot("KIN", "kin.example", [item("k1")], completed_at="2026-09-14T00:00:00Z")
    k2 = db.save_inventory_snapshot("KIN", "kin.example", [item("k2")], completed_at="2026-09-14T00:01:00Z")
    a1 = db.save_inventory_snapshot("AMB", "amb.example", [item("a1")], completed_at="2026-09-14T00:02:00Z")
    calls = []

    class Client:
        def send_inventory_run(self, **kwargs):
            calls.append(kwargs["run_id"])
            if kwargs["run_id"] == k1["run_id"]:
                raise WordPressError("HTTP 503: retry")
            return accepted(kwargs)

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, "client", lambda: Client())
    out = sync.sync_inventory_snapshots()
    assert calls == [k1["run_id"], a1["run_id"]]
    assert k2["run_id"] not in calls
    assert out["halted"] is True
    states = {row["run_id"]: row["delivery_status"] for row in db.inventory_outbox_rows()}
    assert states == {k1["run_id"]: "retryable_error", k2["run_id"]: "pending", a1["run_id"]: "accepted"}
    db.close()


def test_terminal_rejection_does_not_block_next_same_site_run(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    first = db.save_inventory_snapshot("KIN", "kin.example", [item("one")], completed_at="2026-09-14T00:00:00Z")
    second = db.save_inventory_snapshot("KIN", "kin.example", [item("two")], completed_at="2026-09-14T00:01:00Z")
    calls = []

    class Client:
        def send_inventory_run(self, **kwargs):
            calls.append(kwargs["run_id"])
            if kwargs["run_id"] == first["run_id"]:
                return {"accepted": False, "run_id": kwargs["run_id"], "accepted_count": 0,
                        "duplicate_run": False, "rejected_reason": {
                            "code": "RUN_ID_PAYLOAD_CONFLICT", "message": "conflict", "retryable": False}}
            return accepted(kwargs)

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, "client", lambda: Client())
    sync.sync_inventory_snapshots()
    assert calls == [first["run_id"], second["run_id"]]
    states = [row["delivery_status"] for row in db.inventory_outbox_rows()]
    assert states == ["rejected_terminal", "accepted"]
    db.close()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_http_failure_keeps_same_run(tmp_path, monkeypatch, status):
    db = Database(tmp_path / "db.sqlite3")
    run = db.save_inventory_snapshot("KIN", "kin.example", [item("one")])
    calls = []

    class Client:
        def send_inventory_run(self, **kwargs):
            calls.append(kwargs["run_id"])
            if len(calls) == 1:
                raise WordPressError(f"HTTP {status}: temporary")
            return accepted(kwargs, duplicate=True)

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, "client", lambda: Client())
    first = sync.sync_inventory_snapshots()
    second = sync.sync_inventory_snapshots()
    assert first["halted"] is True
    assert second["synced_snapshots"] == 1
    assert calls == [run["run_id"], run["run_id"]]
    assert db.inventory_outbox_rows()[0]["delivery_status"] == "accepted"
    db.close()


@pytest.mark.parametrize("retryable,expected", [(True, "retryable_error"), (False, "rejected_terminal")])
def test_structured_rejection_state(tmp_path, monkeypatch, retryable, expected):
    db = Database(tmp_path / "db.sqlite3")
    run = db.save_inventory_snapshot("KIN", "kin.example", [item("one")])

    class Client:
        def send_inventory_run(self, **kwargs):
            return {
                "accepted": False, "run_id": kwargs["run_id"], "accepted_count": 0,
                "duplicate_run": False, "rejected_reason": {
                    "code": "TEMP" if retryable else "BAD_PAYLOAD",
                    "message": "rejected", "retryable": retryable,
                },
            }

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, "client", lambda: Client())
    sync.sync_inventory_snapshots()
    row = db.inventory_outbox_rows()[0]
    assert row["delivery_status"] == expected
    assert row["rejected_reason_code"] == ("TEMP" if retryable else "BAD_PAYLOAD")
    assert row["run_id"] == run["run_id"]
    db.close()


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda reply: reply.pop("run_id"), "run_id"),
        (lambda reply: reply.__setitem__("run_id", uuid.uuid4().hex), "run_id"),
        (lambda reply: reply.pop("accepted_count"), "accepted_count"),
        (lambda reply: reply.__setitem__("accepted_count", 999), "accepted_count"),
        (lambda reply: reply.__setitem__("accepted", "true"), "accepted"),
        (lambda reply: reply.__setitem__("duplicate_run", "false"), "duplicate_run"),
    ],
)
def test_invalid_success_response_remains_retryable(tmp_path, monkeypatch, mutate, fragment):
    db = Database(tmp_path / "db.sqlite3")
    run = db.save_inventory_snapshot("KIN", "kin.example", [item("one")])

    class Client:
        def send_inventory_run(self, **kwargs):
            reply = accepted(kwargs)
            mutate(reply)
            return reply

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, "client", lambda: Client())
    out = sync.sync_inventory_snapshots()
    row = db.inventory_outbox_rows()[0]
    assert row["delivery_status"] == "retryable_error"
    assert fragment in row["last_error"]
    assert out["synced_snapshots"] == 0
    db.close()


def test_duplicate_run_is_idempotent_success(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    run = db.save_inventory_snapshot("KIN", "kin.example", [item("one")])

    class Client:
        def send_inventory_run(self, **kwargs):
            return accepted(kwargs, duplicate=True)

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, "client", lambda: Client())
    out = sync.sync_inventory_snapshots()
    assert out["synced_snapshots"] == 1
    assert out["results"][0]["duplicate_run"] is True
    assert db.inventory_outbox_rows()[0]["delivery_status"] == "accepted"
    db.close()


class Response:
    ok = True
    status_code = 200

    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class Session:
    def __init__(self):
        self.calls = []
        self.headers = {}

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = kwargs["json"]
        return Response({"accepted": True, "run_id": payload["run_id"],
                         "accepted_count": len(payload["source_property_ids"]), "duplicate_run": False})


def test_registration_inventory_endpoint_payload_and_legacy_method_remain():
    client = WordPressClient("https://homes.example", "a" * 64)
    session = Session()
    client.session = session
    run_id = uuid.uuid4().hex
    result = client.send_inventory_run(
        run_id=run_id, completed_at="2026-09-14T00:00:00Z",
        source_site="kin.example", source_property_ids=["b", "a", "a"],
    )
    url, options = session.calls[0]
    assert url.endswith("/wp-json/takuro-registration/v1/inventory-snapshots")
    assert options["json"] == {
        "schema_version": 1, "source_site": "kin.example", "discovery_status": "complete",
        "complete": True, "errors": 0, "parse_errors": 0,
        "blockers": [], "source_property_ids": ["a", "b"], "run_id": run_id,
        "completed_at": "2026-09-14T00:00:00Z",
    }
    assert result["accepted"] is True
    assert callable(client.send_inventory_snapshot)


def test_local_registration_endpoint_delivery_e2e(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    run = db.save_inventory_snapshot("GOO", "goodcom.example", [item("2"), item("1")])
    client = WordPressClient("https://registration.local", "a" * 64)
    session = Session()
    client.session = session
    sync = WordPressSync(db)
    monkeypatch.setattr(sync, "client", lambda: client)
    result = sync.sync_inventory_snapshots()
    assert result["synced_snapshots"] == 1
    assert db.inventory_outbox_rows()[0]["delivery_status"] == "accepted"
    url, request = session.calls[0]
    assert url == "https://registration.local/wp-json/takuro-registration/v1/inventory-snapshots"
    assert request["json"]["run_id"] == run["run_id"]
    assert request["json"]["source_property_ids"] == ["1", "2"]
    db.close()


def test_wordpress_retry_schedules_sync_only_not_collect_all():
    source = inspect.getsource(MainWindow.schedule_sync_retry)
    assert "sync_wordpress" in source
    assert "collect_all" not in source


@pytest.mark.parametrize(
    "mode",
    ["missing_id", "missing_result", "candidate_storage_error", "http_500"],
)
def test_candidate_failure_never_marks_synced(tmp_path, monkeypatch, mode):
    db = Database(tmp_path / "db.sqlite3")
    property_id, _created = db.upsert_property(PropertyCandidate(
        source_site="kin.example", source_property_id="one", management_company="kin",
        building_name="Building", room="101", prefecture="東京都", address="東京都新宿区1-1",
        source_url="https://kin.example/one", rent=60000, management_fee=3000,
    ))

    class Client:
        def send_candidates(self, items):
            if mode in {"candidate_storage_error", "http_500"}:
                raise WordPressError("HTTP 500: candidate_storage_error")
            if mode == "missing_result":
                return {"results": []}
            return {"results": [{"status": "new"}]}

    sync = WordPressSync(db)
    monkeypatch.setattr(sync, "client", lambda: Client())
    result = sync.sync_pending(batch_size=1, max_rows=1)
    prop = db.property(property_id)
    assert prop["wp_sync_state"] in {"pending", "error"}
    assert prop["wp_sync_state"] != "synced"
    assert db.pending_sync(10)
    if mode in {"candidate_storage_error", "http_500"}:
        assert result["halted"] is True
    db.close()
