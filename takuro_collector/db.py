from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import PropertyCandidate
from .paths import database_path
from .utils import extract_prefecture, json_dumps, normalize_room, now_iso

SCHEMA_VERSION = 6


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=20, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Worker connections used to execute CREATE/INSERT migration statements on
        # every task start. Under load that needlessly competes with the GUI reader.
        if not self._schema_is_current():
            self.install()
        self.recover_sending_inventory_runs()

    def _schema_is_current(self) -> bool:
        try:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key='schema_version'"
            ).fetchone()
            return bool(row and str(row[0]) == str(SCHEMA_VERSION))
        except sqlite3.OperationalError:
            return False

    def install(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS properties (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_site TEXT NOT NULL,
                    source_property_id TEXT NOT NULL,
                    management_company TEXT NOT NULL DEFAULT '',
                    building_name TEXT NOT NULL,
                    room TEXT NOT NULL,
                    prefecture TEXT NOT NULL DEFAULT '',
                    address TEXT NOT NULL DEFAULT '',
                    rent INTEGER NOT NULL DEFAULT 0,
                    management_fee INTEGER NOT NULL DEFAULT 0,
                    deposit TEXT NOT NULL DEFAULT '',
                    key_money TEXT NOT NULL DEFAULT '',
                    area REAL,
                    layout TEXT NOT NULL DEFAULT '',
                    built_date TEXT NOT NULL DEFAULT '',
                    floor TEXT NOT NULL DEFAULT '',
                    total_floors TEXT NOT NULL DEFAULT '',
                    structure TEXT NOT NULL DEFAULT '',
                    orientation TEXT NOT NULL DEFAULT '',
                    move_in_date TEXT NOT NULL DEFAULT '',
                    transport_json TEXT NOT NULL DEFAULT '[]',
                    equipment_json TEXT NOT NULL DEFAULT '[]',
                    raw_payload TEXT NOT NULL DEFAULT '{}',
                    source_url TEXT NOT NULL DEFAULT '',
                    source_id_kind TEXT NOT NULL DEFAULT 'site',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    photo_state TEXT NOT NULL DEFAULT 'not_started',
                    pdf_path TEXT NOT NULL DEFAULT '',
                    zip_path TEXT NOT NULL DEFAULT '',
                    wp_candidate_id INTEGER NOT NULL DEFAULT 0,
                    wp_sync_state TEXT NOT NULL DEFAULT 'pending',
                    wp_last_error TEXT NOT NULL DEFAULT '',
                    wp_package_job_id TEXT NOT NULL DEFAULT '',
                    wp_package_state TEXT NOT NULL DEFAULT '',
                    wp_package_error TEXT NOT NULL DEFAULT '',
                    wp_asset_state TEXT NOT NULL DEFAULT '',
                    wp_asset_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_site, source_property_id)
                );
                CREATE INDEX IF NOT EXISTS idx_properties_status_seen ON properties(status, last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_properties_wp_sync ON properties(wp_sync_state, last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_properties_seen ON properties(last_seen_at DESC, id DESC);

                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    property_id INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    alt_text TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'photo',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    local_path TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(property_id, source_url),
                    FOREIGN KEY(property_id) REFERENCES properties(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_photos_property ON photos(property_id, status);

                CREATE TABLE IF NOT EXISTS site_status (
                    site_code TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'never',
                    last_checked_at TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_new_count INTEGER NOT NULL DEFAULT 0,
                    last_total_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    property_id INTEGER NOT NULL,
                    action TEXT NOT NULL DEFAULT 'upsert_candidate',
                    sync_status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    UNIQUE(property_id, action),
                    FOREIGN KEY(property_id) REFERENCES properties(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scan_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL,
                    sites INTEGER NOT NULL DEFAULT 0,
                    discovered INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    existing_count INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    login_required INTEGER NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS inventory_snapshots (
                    site_code TEXT PRIMARY KEY,
                    source_site TEXT NOT NULL,
                    snapshot_token TEXT NOT NULL,
                    expected_count INTEGER NOT NULL,
                    items_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    sync_status TEXT NOT NULL DEFAULT 'pending',
                    synced_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_snapshots_sync ON inventory_snapshots(sync_status, created_at);

                CREATE TABLE IF NOT EXISTS inventory_delivery_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    site_code TEXT NOT NULL,
                    source_site TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    complete INTEGER NOT NULL DEFAULT 1,
                    expected_count INTEGER NOT NULL,
                    items_json TEXT NOT NULL,
                    payload_checksum TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'pending',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    rejected_reason_code TEXT NOT NULL DEFAULT '',
                    rejected_reason_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_attempt_at TEXT NOT NULL DEFAULT '',
                    accepted_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_inventory_outbox_delivery
                    ON inventory_delivery_outbox(delivery_status, site_code, completed_at, id);
                """
            )
            self._ensure_property_column("wp_package_job_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_property_column("wp_package_state", "TEXT NOT NULL DEFAULT ''")
            self._ensure_property_column("wp_package_error", "TEXT NOT NULL DEFAULT ''")
            self._ensure_property_column("wp_asset_state", "TEXT NOT NULL DEFAULT ''")
            self._ensure_property_column("wp_asset_error", "TEXT NOT NULL DEFAULT ''")
            self._ensure_table_column("photos", "sort_order", "INTEGER NOT NULL DEFAULT 0")
            self._migrate_legacy_inventory_outbox()
            self.conn.execute(
                "INSERT OR REPLACE INTO settings(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _valid_run_id(value: str) -> bool:
        text = str(value or "")
        if not re.fullmatch(r"[0-9a-f]{32}", text):
            return False
        try:
            return uuid.UUID(hex=text).version == 4
        except ValueError:
            return False

    @staticmethod
    def _utc_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _as_utc_z(value: str) -> str:
        text = str(value or "").strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        except ValueError:
            return ""

    @staticmethod
    def _canonical_inventory(
        source_site: str, items: list[dict], *, run_id: str, completed_at: str
    ) -> tuple[list[dict], str]:
        unique: dict[str, dict] = {}
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            source_id = str(raw.get("source_property_id") or "").strip()[:300]
            if not source_id:
                continue
            unique[source_id] = {
                "source_property_id": source_id,
                "building_name": str(raw.get("building_name") or "")[:1000],
                "room": str(raw.get("room") or "")[:300],
                "source_url": str(raw.get("source_url") or raw.get("url") or "")[:2000],
            }
        rows = [unique[key] for key in sorted(unique)]
        canonical = {
            "schema_version": 1,
            "source_site": source_site,
            "discovery_status": "complete",
            "complete": True,
            "errors": 0,
            "parse_errors": 0,
            "blockers": [],
            "source_property_ids": [row["source_property_id"] for row in rows],
            "run_id": run_id,
            "completed_at": completed_at,
        }
        raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return rows, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _migrate_legacy_inventory_outbox(self) -> None:
        rows = self.conn.execute(
            "SELECT * FROM inventory_snapshots WHERE sync_status IN ('pending','error')"
        ).fetchall()
        for row in rows:
            run_id = str(row["snapshot_token"] or "")
            if not self._valid_run_id(run_id):
                self.conn.execute(
                    "UPDATE inventory_snapshots SET last_error=? WHERE site_code=?",
                    ("legacy snapshot_token 형식이 유효하지 않아 outbox 이관 보류", row["site_code"]),
                )
                continue
            try:
                items = json.loads(str(row["items_json"] or "[]"))
            except Exception:
                items = []
            completed_at = self._as_utc_z(str(row["created_at"] or ""))
            if not completed_at:
                self.conn.execute(
                    "UPDATE inventory_snapshots SET last_error=? WHERE site_code=?",
                    ("legacy completed_at 형식이 유효하지 않아 outbox 이관 보류", row["site_code"]),
                )
                continue
            canonical_rows, checksum = self._canonical_inventory(
                str(row["source_site"] or ""), items, run_id=run_id, completed_at=completed_at
            )
            if not canonical_rows or len(canonical_rows) != int(row["expected_count"] or 0):
                self.conn.execute(
                    "UPDATE inventory_snapshots SET last_error=? WHERE site_code=?",
                    ("legacy snapshot payload가 유효하지 않아 outbox 이관 보류", row["site_code"]),
                )
                continue
            self.conn.execute(
                "INSERT OR IGNORE INTO inventory_delivery_outbox("
                "run_id,site_code,source_site,completed_at,complete,expected_count,items_json,payload_checksum,"
                "delivery_status,retry_count,last_error,created_at) VALUES(?,?,?,?,1,?,?,?,?,0,?,?)",
                (
                    run_id, row["site_code"], row["source_site"], completed_at,
                    len(canonical_rows), json.dumps(canonical_rows, ensure_ascii=False, separators=(",", ":")),
                    checksum, "retryable_error" if row["sync_status"] == "error" else "pending",
                    str(row["last_error"] or ""), completed_at,
                ),
            )

    def _ensure_property_column(self, name: str, ddl: str) -> None:
        self._ensure_table_column("properties", name, ddl)

    def _ensure_table_column(self, table: str, name: str, ddl: str) -> None:
        cols = {str(row[1]) for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if name not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def set_package_state(self, property_id: int, *, state: str, job_id: str = "", error: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE properties SET wp_package_state=?, wp_package_job_id=?, wp_package_error=? WHERE id=?",
                (state[:80], job_id[:128], error[:2000], int(property_id)),
            )

    def set_asset_state(self, property_id: int, *, state: str, error: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE properties SET wp_asset_state=?, wp_asset_error=? WHERE id=?",
                (state[:80], error[:2000], int(property_id)),
            )

    def close(self) -> None:
        self.conn.close()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

    def get_bool(self, key: str, default: bool = False) -> bool:
        return self.get_setting(key, "1" if default else "0") in {"1", "true", "yes", "on"}

    def set_bool(self, key: str, value: bool) -> None:
        self.set_setting(key, "1" if value else "0")

    def _upsert_property_in_transaction(self, p: PropertyCandidate) -> tuple[int, bool]:
        """Upsert one property assuming the caller owns the transaction."""
        now = now_iso()
        room = normalize_room(p.room)
        pref = extract_prefecture(p.address, p.prefecture)
        existing = self.conn.execute(
            "SELECT id,raw_payload,wp_sync_state FROM properties WHERE source_site=? AND source_property_id=?",
            (p.source_site, p.source_property_id),
        ).fetchone()
        payload = p.to_dict()
        payload_json = json_dumps(payload)
        payload_changed = (not existing) or str(existing["raw_payload"] or "") != payload_json
        values = {
            "management_company": p.management_company,
            "building_name": p.building_name,
            "room": room,
            "prefecture": pref,
            "address": p.address,
            "rent": int(p.rent or 0),
            "management_fee": int(p.management_fee or 0),
            "deposit": p.deposit,
            "key_money": p.key_money,
            "area": p.area,
            "layout": p.layout,
            "built_date": p.built_date,
            "floor": p.floor,
            "total_floors": p.total_floors,
            "structure": p.structure,
            "orientation": p.orientation,
            "move_in_date": p.move_in_date,
            "transport_json": json_dumps(p.transport),
            "equipment_json": json_dumps(p.equipment),
            "raw_payload": payload_json,
            "source_url": p.source_url,
            "source_id_kind": p.source_id_kind,
            "last_seen_at": now,
        }
        if existing:
            pid = int(existing["id"])
            sets = ",".join(f"{k}=?" for k in values)
            self.conn.execute(
                f"UPDATE properties SET {sets}, wp_sync_state=CASE WHEN ?=1 AND wp_sync_state='synced' THEN 'pending' ELSE wp_sync_state END WHERE id=?",
                (*values.values(), 1 if payload_changed else 0, pid),
            )
            is_new = False
        else:
            cols = ["source_site", "source_property_id", *values.keys(), "first_seen_at"]
            vals = [p.source_site, p.source_property_id, *values.values(), now]
            marks = ",".join("?" for _ in cols)
            cur = self.conn.execute(
                f"INSERT INTO properties({','.join(cols)}) VALUES({marks})",
                vals,
            )
            pid = int(cur.lastrowid)
            is_new = True
        photo_changed = self._replace_photo_sources(pid, p.photo_sources)
        if photo_changed and not is_new:
            self.conn.execute("UPDATE properties SET wp_asset_state='', wp_asset_error='' WHERE id=?", (pid,))
        if is_new or payload_changed or photo_changed:
            self.conn.execute(
                "INSERT INTO sync_queue(property_id,action,sync_status,retry_count,last_error,updated_at) VALUES(?, 'upsert_candidate','pending',0,'',?) "
                "ON CONFLICT(property_id,action) DO UPDATE SET sync_status='pending', last_error='', updated_at=excluded.updated_at",
                (pid, now),
            )
        return pid, is_new

    def upsert_property(self, p: PropertyCandidate) -> tuple[int, bool]:
        with self.conn:
            return self._upsert_property_in_transaction(p)

    def upsert_properties_batch(self, candidates: Iterable[PropertyCandidate]) -> list[tuple[int, bool]]:
        """Persist a collection chunk in one SQLite transaction.

        Full-site crawls can produce hundreds of rows.  Committing a small chunk
        at a time reduces writer-lock/GIL churn while keeping recovery granular.
        """
        rows = list(candidates)
        if not rows:
            return []
        with self.conn:
            return [self._upsert_property_in_transaction(p) for p in rows]

    def _replace_photo_sources(self, property_id: int, sources: Iterable[dict]) -> bool:
        now = now_iso()
        source_rows = [src for src in sources if str(src.get("url", "")).strip()]
        urls = [str(src.get("url", "")).strip() for src in source_rows]
        before = [str(r[0]) for r in self.conn.execute(
            "SELECT source_url FROM photos WHERE property_id=? ORDER BY sort_order,id", (property_id,)
        )]
        # A fresh scrape is authoritative. This removes former thumbnail URLs and
        # lets URL collection redownload the new full-size, correctly ordered set.
        if urls:
            marks = ",".join("?" for _ in urls)
            self.conn.execute(
                f"DELETE FROM photos WHERE property_id=? AND source_url NOT IN ({marks})",
                (property_id, *urls),
            )
        for sort_order, src in enumerate(source_rows):
            url = str(src.get("url", "")).strip()
            if not url:
                continue
            self.conn.execute(
                "INSERT INTO photos(property_id,source_url,alt_text,kind,sort_order,created_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(property_id,source_url) DO UPDATE SET alt_text=excluded.alt_text, kind=excluded.kind, sort_order=excluded.sort_order",
                (property_id, url, str(src.get("alt", "")), str(src.get("kind", "photo")), sort_order, now),
            )
        return before != urls

    def property(self, property_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone()
        return self._decode_property(row) if row else None

    def property_by_identity(self, source_site: str, source_property_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM properties WHERE source_site=? AND source_property_id=?",
            (source_site, source_property_id),
        ).fetchone()
        return self._decode_property(row) if row else None

    def properties_by_source_site(self, source_site: str) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM properties WHERE source_site=?", (source_site,)).fetchall()
        return {str(row["source_property_id"]): self._decode_property(row) for row in rows}

    def property_by_ready_fallback(self, source_site: str, building_name: str, room: str) -> dict | None:
        """Return a unique same-site building+room match for a ready candidate.

        This fallback is intentionally conservative. It is used only when the
        primary source_site + source_property_id identity no longer matches
        (for example after a local Collector reset changed the source id).
        Ambiguous matches return None instead of guessing.
        """
        site = (source_site or "").strip().lower()
        name = (building_name or "").strip()
        room_norm = normalize_room(room)
        if not site or not name or not room_norm:
            return None
        rows = self.conn.execute(
            "SELECT * FROM properties WHERE LOWER(source_site)=? AND building_name=?",
            (site, name),
        ).fetchall()
        matches = [r for r in rows if normalize_room(str(r["room"] or "")) == room_norm]
        return self._decode_property(matches[0]) if len(matches) == 1 else None

    def list_properties(self, query: str = "", limit: int = 500) -> list[dict]:
        params: list = []
        where = ""
        if query.strip():
            like = f"%{query.strip()}%"
            where = "WHERE building_name LIKE ? OR room LIKE ? OR address LIKE ? OR management_company LIKE ? OR source_site LIKE ?"
            params.extend([like] * 5)
        params.append(int(limit))
        rows = self.conn.execute(
            f"SELECT * FROM properties {where} ORDER BY last_seen_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._decode_property(r) for r in rows]

    def list_property_summaries(self, query: str = "", limit: int = 500) -> list[dict]:
        """Return only columns rendered by the main table.

        Avoid decoding transport/equipment/raw_payload JSON for up to 500 rows on
        the GUI thread. Full payloads are loaded only when one property is selected.
        """
        params: list = []
        where = ""
        if query.strip():
            like = f"%{query.strip()}%"
            where = "WHERE building_name LIKE ? OR room LIKE ? OR address LIKE ? OR management_company LIKE ? OR source_site LIKE ?"
            params.extend([like] * 5)
        params.append(int(limit))
        rows = self.conn.execute(
            f"SELECT id,status,management_company,building_name,room,address,rent,management_fee,"
            f"photo_state,pdf_path,zip_path,wp_package_state,wp_sync_state "
            f"FROM properties {where} ORDER BY last_seen_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_property_states(self, query: str = "", limit: int = 500) -> list[dict]:
        """Return only columns needed for the post-sync state refresh.

        Avoid decoding transport/equipment/raw_payload JSON for hundreds of rows
        on the GUI thread after WordPress synchronization.
        """
        params: list = []
        where = ""
        if query.strip():
            like = f"%{query.strip()}%"
            where = "WHERE building_name LIKE ? OR room LIKE ? OR address LIKE ? OR management_company LIKE ? OR source_site LIKE ?"
            params.extend([like] * 5)
        params.append(int(limit))
        rows = self.conn.execute(
            f"SELECT id,status,photo_state,pdf_path,zip_path,wp_package_state,wp_sync_state "
            f"FROM properties {where} ORDER BY last_seen_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def _decode_property(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in ("transport_json", "equipment_json", "raw_payload"):
            try:
                d[key[:-5] if key.endswith("_json") else key] = json.loads(d.get(key) or ("{}" if key == "raw_payload" else "[]"))
            except Exception:
                d[key[:-5] if key.endswith("_json") else key] = {} if key == "raw_payload" else []
        return d

    def photos(self, property_id: int) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM photos WHERE property_id=? ORDER BY sort_order,id", (property_id,))]

    def mark_photo(self, photo_id: int, *, status: str, local_path: str = "", sha256: str = "", error: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE photos SET status=?, local_path=?, sha256=?, error=? WHERE id=?",
                (status, local_path, sha256, error, photo_id),
            )

    def reset_photo_downloads(self, property_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE photos SET status='pending', local_path='', sha256='', error='' WHERE property_id=?",
                (property_id,),
            )
            self.conn.execute("UPDATE properties SET photo_state='not_started' WHERE id=?", (property_id,))

    def set_photo_state(self, property_id: int, state: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE properties SET photo_state=? WHERE id=?", (state, property_id))

    def set_pdf(self, property_id: int, path: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE properties SET pdf_path=? WHERE id=?", (path, property_id))

    def set_zip(self, property_id: int, path: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE properties SET zip_path=? WHERE id=?", (path, property_id))

    def set_status(self, property_id: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE properties SET status=? WHERE id=?", (status, property_id))

    def update_wp_state(self, property_id: int, *, state: str, candidate_id: int = 0, error: str = "") -> None:
        now = now_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE properties SET wp_sync_state=?, wp_candidate_id=CASE WHEN ?>0 THEN ? ELSE wp_candidate_id END, wp_last_error=? WHERE id=?",
                (state, candidate_id, candidate_id, error, property_id),
            )
            self.conn.execute(
                "UPDATE sync_queue SET sync_status=?, retry_count=CASE WHEN ?='error' THEN retry_count+1 ELSE retry_count END, last_error=?, updated_at=? WHERE property_id=? AND action='upsert_candidate'",
                (state, state, error, now, property_id),
            )

    def update_wp_states_batch(self, updates: list[dict]) -> None:
        """Persist one server batch in a single SQLite transaction.

        Large syncs previously committed once per property, which could hold the
        Python GIL/SQLite writer lock often enough for Windows to label the Qt
        window as unresponsive even though the worker thread was progressing.
        """
        if not updates:
            return
        now = now_iso()
        with self.conn:
            for item in updates:
                property_id = int(item.get("property_id") or item.get("id") or 0)
                if not property_id:
                    continue
                state = str(item.get("state") or "error")[:32]
                candidate_id = int(item.get("candidate_id") or 0)
                error = str(item.get("error") or "")[:2000]
                self.conn.execute(
                    "UPDATE properties SET wp_sync_state=?, wp_candidate_id=CASE WHEN ?>0 THEN ? ELSE wp_candidate_id END, wp_last_error=? WHERE id=?",
                    (state, candidate_id, candidate_id, error, property_id),
                )
                self.conn.execute(
                    "UPDATE sync_queue SET sync_status=?, retry_count=CASE WHEN ?='error' THEN retry_count+1 ELSE retry_count END, last_error=?, updated_at=? WHERE property_id=? AND action='upsert_candidate'",
                    (state, state, error, now, property_id),
                )

    def save_inventory_snapshot(
        self,
        site_code: str,
        source_site: str,
        items: list[dict],
        *,
        run_id: str = "",
        completed_at: str = "",
    ) -> dict:
        """Atomically update the current inventory and append its delivery run."""
        site_code = str(site_code or "").strip().upper()[:32]
        source_site = str(source_site or "").strip().lower()[:300]
        run_id = str(run_id or uuid.uuid4().hex)
        completed_at = self._as_utc_z(str(completed_at)) if completed_at else self._utc_iso()
        if not self._valid_run_id(run_id):
            raise ValueError("inventory run_id는 32자리 lowercase UUIDv4 hex여야 합니다.")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", completed_at):
            raise ValueError("inventory completed_at은 UTC ISO-8601 Z 형식이어야 합니다.")
        rows, checksum = self._canonical_inventory(
            source_site, items, run_id=run_id, completed_at=completed_at
        )
        if not site_code or not source_site or not rows:
            raise ValueError("완전 재고 스냅샷의 사이트/매물 식별자가 비어 있습니다.")
        created_at = self._utc_iso()
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        with self.conn:
            existing = self.conn.execute(
                "SELECT site_code,source_site,completed_at,payload_checksum "
                "FROM inventory_delivery_outbox WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing and (
                str(existing[0]) != site_code
                or str(existing[1]) != source_site
                or str(existing[2]) != completed_at
                or str(existing[3]) != checksum
            ):
                raise ValueError("동일 inventory run_id에 서로 다른 payload를 저장할 수 없습니다.")
            self.conn.execute(
                "INSERT INTO inventory_snapshots(site_code,source_site,snapshot_token,expected_count,items_json,created_at,sync_status,synced_at,last_error) "
                "VALUES(?,?,?,?,?,?,'pending','','') "
                "ON CONFLICT(site_code) DO UPDATE SET source_site=excluded.source_site,snapshot_token=excluded.snapshot_token,expected_count=excluded.expected_count,items_json=excluded.items_json,created_at=excluded.created_at,sync_status='pending',synced_at='',last_error=''",
                (site_code, source_site, run_id, len(rows), payload, completed_at),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO inventory_delivery_outbox("
                "run_id,site_code,source_site,completed_at,complete,expected_count,items_json,payload_checksum,"
                "delivery_status,retry_count,last_error,created_at) VALUES(?,?,?,?,1,?,?,?,?,0,'',?)",
                (run_id, site_code, source_site, completed_at, len(rows), payload, checksum, "pending", created_at),
            )
        return {
            "site_code": site_code,
            "source_site": source_site,
            "snapshot_token": run_id,
            "run_id": run_id,
            "completed_at": completed_at,
            "payload_checksum": checksum,
            "expected_count": len(rows),
        }

    def pending_inventory_snapshots(self) -> list[dict]:
        """Compatibility view of deliverable append-only inventory runs."""
        return self.pending_inventory_runs()

    def recover_sending_inventory_runs(self) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE inventory_delivery_outbox SET delivery_status='retryable_error', "
                    "last_error=CASE WHEN last_error='' THEN '이전 실행 중 전송이 중단되어 재시도 대기' ELSE last_error END "
                    "WHERE delivery_status='sending'"
                )
        except sqlite3.OperationalError:
            return

    def pending_inventory_runs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT o.* FROM inventory_delivery_outbox o "
            "WHERE o.delivery_status IN ('pending','retryable_error') "
            "AND NOT EXISTS (SELECT 1 FROM inventory_delivery_outbox prior "
            "WHERE prior.site_code=o.site_code "
            "AND prior.delivery_status IN ('pending','sending','retryable_error') "
            "AND (prior.completed_at<o.completed_at OR (prior.completed_at=o.completed_at AND prior.id<o.id))) "
            "ORDER BY o.completed_at ASC,o.id ASC"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            d = dict(row)
            try:
                d["items"] = json.loads(str(d.get("items_json") or "[]"))
            except Exception:
                d["items"] = []
            out.append(d)
        return out

    def inventory_outbox_rows(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM inventory_delivery_outbox ORDER BY completed_at,id"
        ).fetchall()
        return [dict(row) for row in rows]

    def inventory_run_checksum(self, run: dict) -> str:
        _rows, checksum = self._canonical_inventory(
            str(run.get("source_site") or ""),
            list(run.get("items") or []),
            run_id=str(run.get("run_id") or ""),
            completed_at=str(run.get("completed_at") or ""),
        )
        return checksum

    def mark_inventory_run(
        self,
        run_id: str,
        *,
        state: str,
        error: str = "",
        rejected_code: str = "",
        rejected_message: str = "",
    ) -> None:
        allowed = {"pending", "sending", "accepted", "retryable_error", "rejected_terminal"}
        if state not in allowed:
            raise ValueError(f"inventory delivery 상태가 올바르지 않습니다: {state}")
        now = self._utc_iso()
        with self.conn:
            self.conn.execute(
                "UPDATE inventory_delivery_outbox SET delivery_status=?,"
                "retry_count=retry_count+CASE WHEN ? IN ('retryable_error','rejected_terminal') THEN 1 ELSE 0 END,"
                "last_error=?,rejected_reason_code=?,rejected_reason_message=?,"
                "last_attempt_at=CASE WHEN ?='sending' THEN ? ELSE last_attempt_at END,"
                "accepted_at=CASE WHEN ?='accepted' THEN ? ELSE accepted_at END WHERE run_id=?",
                (
                    state, state, str(error or "")[:2000], str(rejected_code or "")[:128],
                    str(rejected_message or "")[:2000], state, now, state, now, run_id,
                ),
            )
            if state == "accepted":
                self.conn.execute(
                    "UPDATE inventory_snapshots SET sync_status='synced',synced_at=?,last_error='' "
                    "WHERE snapshot_token=?",
                    (now, run_id),
                )
            elif state in {"retryable_error", "rejected_terminal"}:
                self.conn.execute(
                    "UPDATE inventory_snapshots SET sync_status='error',last_error=? WHERE snapshot_token=?",
                    (str(error or rejected_message or "")[:2000], run_id),
                )

    def mark_inventory_snapshot(self, site_code: str, *, state: str, error: str = "") -> None:
        """Legacy current-view state helper retained for compatibility tests/tools."""
        state = str(state or "error")[:32]
        with self.conn:
            self.conn.execute(
                "UPDATE inventory_snapshots SET sync_status=?, synced_at=CASE WHEN ?='synced' THEN ? ELSE synced_at END, last_error=? WHERE site_code=?",
                (state, state, now_iso(), str(error or "")[:2000], str(site_code or "").strip().upper()),
            )

    def pending_sync(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT p.* FROM properties p JOIN sync_queue q ON q.property_id=p.id AND q.action='upsert_candidate' WHERE q.sync_status IN ('pending','error') ORDER BY p.last_seen_at ASC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [self._decode_property(r) for r in rows]

    def set_site_status(self, site_code: str, status: str, *, error: str = "", new_count: int = 0, total_count: int = 0) -> None:
        now = now_iso()
        success = now if status == "ok" else ""
        with self.conn:
            self.conn.execute(
                "INSERT INTO site_status(site_code,status,last_checked_at,last_success_at,last_error,last_new_count,last_total_count) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(site_code) DO UPDATE SET status=excluded.status,last_checked_at=excluded.last_checked_at,last_success_at=CASE WHEN excluded.last_success_at<>'' THEN excluded.last_success_at ELSE site_status.last_success_at END,last_error=excluded.last_error,last_new_count=excluded.last_new_count,last_total_count=excluded.last_total_count",
                (site_code, status, now, success, error, int(new_count), int(total_count)),
            )

    def site_statuses(self) -> dict[str, dict]:
        return {str(r["site_code"]): dict(r) for r in self.conn.execute("SELECT * FROM site_status ORDER BY site_code")}

    def start_scan(self, mode: str) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO scan_history(started_at,mode) VALUES(?,?)", (now_iso(), mode))
            return int(cur.lastrowid)

    def finish_scan(self, scan_id: int, result: dict) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE scan_history SET finished_at=?,sites=?,discovered=?,new_count=?,existing_count=?,errors=?,login_required=?,detail=? WHERE id=?",
                (
                    now_iso(),
                    int(result.get("sites", 0)),
                    int(result.get("discovered", 0)),
                    int(result.get("new_count", 0)),
                    int(result.get("existing_count", 0)),
                    int(result.get("errors", 0)),
                    int(result.get("login_required", 0)),
                    json_dumps(result),
                    scan_id,
                ),
            )
