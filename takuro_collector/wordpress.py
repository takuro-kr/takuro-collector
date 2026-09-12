from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import base64
import hashlib
import json
import re
from urllib.parse import urljoin

import requests

from . import __version__
from .db import Database
from .package import attach_pdf, create_zip
from .secrets import protect, unprotect
from .utils import canonical_host


class WordPressError(RuntimeError):
    pass


class WordPressClient:
    def __init__(self, site_url: str, collector_key: str, timeout: int = 20):
        self.site_url = self.normalize_site(site_url)
        self.key = collector_key.strip()
        if not re.fullmatch(r"[A-Fa-f0-9]{64}", self.key):
            raise WordPressError("Collector 연동 키는 WordPress에서 발급한 64자리 영문(a-f)·숫자(0-9) 키만 입력해 주세요.")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Takuro-Collector-Key": self.key, "User-Agent": f"TAKURO-Collector/{__version__}"})

    @staticmethod
    def normalize_site(url: str) -> str:
        url = (url or "").strip().rstrip("/")
        for suffix in ("/wp-json", "/wp-json/takuro/v1"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        return url.rstrip("/")

    def api(self, path: str) -> str:
        return f"{self.site_url}/wp-json/takuro/v1/{path.lstrip('/')}"

    def registration_api(self, path: str) -> str:
        return f"{self.site_url}/wp-json/takuro-registration/v1/{path.lstrip('/')}"

    def front_intake(self) -> str:
        return f"{self.site_url}/?takuro_collector_candidates=1"

    def front_inventory(self) -> str:
        return f"{self.site_url}/?takuro_collector_inventory=1"

    def admin_post(self, action: str) -> str:
        return f"{self.site_url}/wp-admin/admin-post.php?action={action}"

    def _json(self, response: requests.Response) -> Any:
        try:
            data = response.json()
        except Exception:
            raw = str(getattr(response, "text", "") or "")
            excerpt = re.sub(r"<[^>]+>", " ", raw)
            excerpt = re.sub(r"\s+", " ", excerpt).strip()[:240]
            suffix = f": {excerpt}" if excerpt else ""
            raise WordPressError(f"HTTP {response.status_code} (non-JSON){suffix}")
        if not response.ok:
            message = data.get("message") if isinstance(data, dict) else str(data)
            raise WordPressError(f"HTTP {response.status_code}: {message}")
        return data

    def status(self) -> dict:
        r = self.session.get(self.registration_api("status"), timeout=self.timeout)
        data = self._json(r)
        return dict(data) if isinstance(data, dict) else {}

    @staticmethod
    def _opaque_form(payload: dict) -> dict[str, str]:
        # SiteGuard can reject large JSON bodies before WordPress routing.  The
        # authenticated fallback therefore sends a compact base64url envelope as
        # a normal form field.  The Collector key remains in the request header.
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return {"payload_b64": encoded}

    @staticmethod
    def _non_json_403(response: requests.Response) -> bool:
        if int(getattr(response, "status_code", 0) or 0) != 403:
            return False
        try:
            response.json()
        except Exception:
            return True
        return False

    def send_candidates(self, items: list[dict]) -> dict:
        if not 1 <= len(items) <= 100:
            raise WordPressError("한 번에 1~100개 후보만 전송할 수 있습니다.")
        r = self.session.post(self.registration_api("candidates"), json={"items": items}, timeout=self.timeout)
        data = self._json(r)
        if isinstance(data, dict):
            out = dict(data)
            out.setdefault("transport", "registration_v2_rest")
            return out
        return {}

    def send_inventory_snapshot(
        self,
        *,
        source_site: str,
        source_property_ids: list[str],
        complete: bool = True,
        errors: int = 0,
        parse_errors: int = 0,
        blockers: list[str] | None = None,
    ) -> dict:
        """Send one proven-complete room-level inventory snapshot.

        The WordPress 0.36.86+ contract evaluates outgoing transitions only from
        the complete set in a single request.  Partial/chunked payloads are not
        accepted because they could make still-live listings look missing.
        """
        if not complete:
            raise WordPressError("불완전 재고 스냅샷은 WordPress로 전송하지 않습니다.")
        if int(errors) > 0 or int(parse_errors) > 0:
            raise WordPressError("오류가 있는 재고 스냅샷은 WordPress로 전송하지 않습니다.")
        unsafe = {"403", "429", "5xx", "http_403", "http_429", "http_5xx", "timeout", "waf", "siteguard", "login", "parser", "parse", "incomplete"}
        clean_blockers = []
        for value in blockers or []:
            key = str(value or "").strip().lower().replace("-", "_")[:64]
            if key:
                clean_blockers.append(key)
        if unsafe.intersection(clean_blockers):
            raise WordPressError("안전 차단 사유가 있는 재고 스냅샷은 WordPress로 전송하지 않습니다.")

        ids = sorted({str(v or "").strip()[:300] for v in source_property_ids if str(v or "").strip()})
        if not ids:
            raise WordPressError("재고 스냅샷 source_property_ids가 비어 있습니다.")
        if len(ids) > 10000:
            raise WordPressError("재고 스냅샷 전체 건수를 확인해 주세요.")
        payload = {
            "source_site": str(source_site or "")[:300],
            "complete": True,
            "errors": 0,
            "parse_errors": 0,
            "blockers": clean_blockers,
            "source_property_ids": ids,
        }
        r = self.session.post(self.api("collection/inventory-snapshot"), json=payload, timeout=self.timeout)
        data = self._json(r)
        if not isinstance(data, dict):
            raise WordPressError("재고 스냅샷 응답 형식을 확인할 수 없습니다.")
        out = dict(data)
        out.setdefault("transport", "rest_json")
        return out

    def duplicate_check(self, items: list[dict]) -> dict:
        """Read-only WordPress duplicate lookup using the Collector integration key."""
        if not 1 <= len(items) <= 100:
            raise WordPressError("한 번에 1~100개 중복 후보만 조회할 수 있습니다.")
        payload = []
        for item in items:
            payload.append({
                "management_company": str(item.get("management_company") or "")[:300],
                "source_site": str(item.get("source_site") or "")[:300],
                "source_property_id": str(item.get("source_property_id") or "")[:300],
                "building_name": str(item.get("building_name") or "")[:1000],
                "room": str(item.get("room") or "")[:300],
            })
        r = self.session.post(self.api("duplicate-check"), json={"items": payload}, timeout=self.timeout)
        data = self._json(r)
        return dict(data) if isinstance(data, dict) else {}

    def ready(self) -> list[dict]:
        r = self.session.get(self.api("collection/ready"), timeout=self.timeout)
        data = self._json(r)
        return list(data.get("rows") or []) if isinstance(data, dict) else []

    def upload_candidate_assets(self, candidate_id: int, zip_path: str | Path) -> dict:
        p = Path(zip_path)
        if not p.is_file() or p.suffix.lower() != ".zip":
            raise WordPressError("업로드할 사진·TXT ZIP을 찾을 수 없습니다.")
        if p.stat().st_size > 50 * 1024 * 1024:
            raise WordPressError("사진·TXT ZIP은 50MiB 이하만 업로드할 수 있습니다.")
        with p.open("rb") as fh:
            r = self.session.post(
                self.registration_api(f"candidates/{int(candidate_id)}/assets"),
                files={"package": (p.name, fh, "application/zip")},
                timeout=max(self.timeout, 120),
            )
        data = self._json(r)
        return dict(data) if isinstance(data, dict) else {}


    def upload_package(self, zip_path: str | Path, *, source_property_id: str = "", auto_fast: bool = False) -> dict:
        """Upload one finished Collector ZIP and request server-side FAST processing.

        Server contract: POST /collection/packages with multipart field ``package``.
        The Collector key is reused for authentication. The server must store the ZIP,
        deduplicate by SHA-256, and when auto_fast is true enqueue GPT/FAST processing
        in draft-only mode.
        """
        p = Path(zip_path)
        if not p.is_file():
            raise WordPressError("업로드할 ZIP 파일을 찾을 수 없습니다.")
        if p.suffix.lower() != ".zip":
            raise WordPressError("TAKURO 자동 등록에는 ZIP 파일만 업로드할 수 있습니다.")
        size = p.stat().st_size
        if size < 4 or size > 50 * 1024 * 1024:
            raise WordPressError("TAKURO ZIP은 50MiB 이하만 업로드할 수 있습니다.")
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        headers = {
            "X-Takuro-Package-SHA256": digest,
            "X-Takuro-Draft-Only": "1",
        }
        data = {
            "source_property_id": (source_property_id or "")[:300],
            "auto_fast": "1" if auto_fast else "0",
            "draft_only": "1",
        }
        with p.open("rb") as fh:
            files = {"package": (p.name, fh, "application/zip")}
            r = self.session.post(self.api("collection/packages"), headers=headers, data=data, files=files, timeout=max(self.timeout, 120))
        result = self._json(r)
        if not isinstance(result, dict):
            raise WordPressError("TAKURO ZIP 업로드 응답 형식이 올바르지 않습니다.")
        result.setdefault("sha256", digest)
        result.setdefault("bytes", size)
        return dict(result)

    def package_status(self, job_id: str) -> dict:
        job_id = str(job_id or "").strip()
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise WordPressError("TAKURO ZIP 작업 ID가 올바르지 않습니다.")
        r = self.session.get(self.api(f"collection/packages/{job_id}"), timeout=self.timeout)
        data = self._json(r)
        return dict(data) if isinstance(data, dict) else {}

    def download_drawing(self, candidate_id: int, destination: str | Path) -> Path:
        r = self.session.get(self.api(f"collection/candidates/{int(candidate_id)}/drawing"), timeout=self.timeout)
        if not r.ok:
            try:
                message = r.json().get("message", "")
            except Exception:
                message = ""
            raise WordPressError(f"REINS PDF 다운로드 실패 HTTP {r.status_code}: {message}")
        data = r.content
        if not data.startswith(b"%PDF-"):
            raise WordPressError("WordPress에서 받은 파일이 PDF가 아닙니다.")
        p = Path(destination)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


class WordPressSync:
    def __init__(self, db: Database):
        self.db = db

    def configured(self) -> bool:
        return self.db.get_bool("wp_enabled", False) and bool(self.db.get_setting("wp_site_url", "")) and bool(self.key())

    def key(self) -> str:
        return unprotect(self.db.get_setting("wp_collector_key", ""))

    def save_config(self, enabled: bool, site_url: str, collector_key: str, auto_sync: bool = True) -> None:
        self.db.set_bool("wp_enabled", enabled)
        self.db.set_setting("wp_site_url", WordPressClient.normalize_site(site_url))
        if collector_key.strip():
            self.db.set_setting("wp_collector_key", protect(collector_key.strip()))
        self.db.set_bool("wp_auto_sync", auto_sync)

    def client(self) -> WordPressClient:
        site = self.db.get_setting("wp_site_url", "")
        key = self.key()
        if not site or not key:
            raise WordPressError("TAKURO WordPress 주소와 Collector 연동 키를 설정해 주세요.")
        return WordPressClient(site, key)

    @staticmethod
    def payload_from_row(row: dict) -> dict:
        # Candidate intake only needs the indexed identity/summary fields.  Keep
        # the rich Collector payload locally for TXT/ZIP generation instead of
        # shipping table_fields/photo metadata through a WAF on every sync.
        return {
            "source_site": str(row.get("source_site") or "")[:300],
            "source_property_id": str(row.get("source_property_id") or "")[:300],
            "management_company": str(row.get("management_company") or "")[:300],
            "building_name": str(row.get("building_name") or "")[:1000],
            "room": str(row.get("room") or "")[:300],
            "prefecture": str(row.get("prefecture") or "")[:100],
            "address": str(row.get("address") or "")[:1000],
            "rent": int(row.get("rent") or 0),
            "management_fee": int(row.get("management_fee") or 0),
            "source_url": str(row.get("source_url") or "")[:2000],
        }

    def sync_pending(
        self,
        *,
        batch_size: int = 25,
        max_rows: int = 500,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Sync queued candidates in bounded batches.

        A request-level failure (403, timeout, malformed response, etc.) is *not*
        attributed to every property in that batch. Successfully completed prior
        batches stay synced, while the failed batch and all later rows remain
        retryable in their current pending/error state.
        """
        client = self.client()
        # Registration V2 owns an independent candidate table. Requeue legacy
        # Connect mappings exactly once so every local candidate receives a new
        # V2 candidate id before its TXT/photos are uploaded.
        if not self.db.get_bool("wp_registration_v2_migrated", False):
            with self.db.conn:
                self.db.conn.execute("UPDATE properties SET wp_sync_state='pending', wp_candidate_id=0, wp_asset_state='', wp_asset_error='' WHERE wp_sync_state='synced'")
                self.db.conn.execute("UPDATE sync_queue SET sync_status='pending', last_error='' WHERE action='upsert_candidate'")
            self.db.set_bool("wp_registration_v2_migrated", True)
        # 0.2.4 incorrectly marked every item in a request-level 403 batch as
        # a property error. Repair only that known legacy signature so those
        # rows return to a retryable pending state on first 0.2.5 sync.
        legacy_rows = self.db.conn.execute(
            "SELECT id FROM properties WHERE wp_sync_state='error' "
            "AND (wp_last_error LIKE '%HTTP 403%' OR wp_last_error LIKE '%JSON이 아닙니다%')"
        ).fetchall()
        legacy_ids = [int(row[0]) for row in legacy_rows]
        repaired_legacy_403 = len(legacy_ids)
        if legacy_ids:
            marks = ",".join("?" for _ in legacy_ids)
            with self.db.conn:
                self.db.conn.execute(
                    f"UPDATE properties SET wp_sync_state='pending', wp_last_error='' WHERE id IN ({marks})",
                    legacy_ids,
                )
                self.db.conn.execute(
                    f"UPDATE sync_queue SET sync_status='pending', last_error='' "
                    f"WHERE action='upsert_candidate' AND property_id IN ({marks})",
                    legacy_ids,
                )
        batch_size = max(1, min(100, int(batch_size)))
        max_rows = max(batch_size, int(max_rows))
        sent = synced = errors = batches = assets_uploaded = asset_errors = 0
        halted = False
        halt_error = ""
        http_status = 0
        transports: dict[str, int] = {}
        total_row = self.db.conn.execute(
            "SELECT COUNT(*) FROM sync_queue WHERE action='upsert_candidate' AND sync_status IN ('pending','error')"
        ).fetchone()
        total_target = min(max_rows, int(total_row[0] if total_row else 0))
        if progress_callback:
            progress_callback(0, total_target, f"후보 동기화 준비 · {total_target}건")

        while sent < max_rows:
            pending = self.db.pending_sync(min(batch_size, max_rows - sent))
            if not pending:
                break
            payloads = [self.payload_from_row(r) for r in pending]
            if progress_callback:
                progress_callback(sent, total_target, f"후보 {sent + 1}~{sent + len(pending)}/{total_target} 전송 중")
            try:
                result = client.send_candidates(payloads)
            except Exception as e:
                halted = True
                halt_error = str(e)
                m = re.search(r"HTTP\s+(\d{3})", halt_error, re.I)
                http_status = int(m.group(1)) if m else 0
                break

            batches += 1
            sent += len(pending)
            transport = str(result.get("transport") or "unknown")
            transports[transport] = transports.get(transport, 0) + 1
            if progress_callback:
                progress_callback(sent, total_target, f"후보 {sent}/{total_target} 전송 완료 · {transport}")
            results = list(result.get("results") or [])
            state_updates: list[dict] = []
            for idx, row in enumerate(pending):
                item = results[idx] if idx < len(results) else {"status": "error", "error": "응답 누락"}
                item_status = str(item.get("status") or "error")
                if item_status == "error":
                    errors += 1
                    state_updates.append({"property_id": int(row["id"]), "state": "error", "error": str(item.get("error") or "전송 실패")})
                elif item_status == "ignored_region":
                    errors += 1
                    state_updates.append({"property_id": int(row["id"]), "state": "error", "error": "WordPress 지역 필터 제외"})
                else:
                    synced += 1
                    state_updates.append({"property_id": int(row["id"]), "state": "synced", "candidate_id": int(item.get("id") or 0), "error": ""})
            self.db.update_wp_states_batch(state_updates)
            # The Collector sends TXT/photos once. WordPress keeps them privately
            # and later combines the staff-selected REINS PDF without a round trip.
            for idx, row in enumerate(pending):
                item = results[idx] if idx < len(results) else {}
                candidate_id = int(item.get("id") or 0)
                if not candidate_id or str(item.get("status") or "") in {"error", "ignored_region"}:
                    continue
                pid = int(row["id"])
                current = self.db.property(pid) or row
                if str(current.get("wp_asset_state") or "") == "uploaded":
                    continue
                try:
                    from .photos import PhotoManager
                    if not PhotoManager(self.db).local_photo_paths(pid):
                        raise WordPressError("사진이 아직 다운로드되지 않았습니다.")
                    asset_zip = create_zip(self.db, pid, require_pdf=False)
                    client.upload_candidate_assets(candidate_id, asset_zip)
                    self.db.set_asset_state(pid, state="uploaded", error="")
                    assets_uploaded += 1
                except Exception as exc:
                    self.db.set_asset_state(pid, state="error", error=str(exc))
                    asset_errors += 1

        # Backfill TXT/photos for candidates that older Collector versions had
        # already marked as synced before the separate server asset store existed.
        # Only rows with downloaded local photos are eligible, so this never
        # re-downloads or invents missing candidate material.
        backfill_rows = self.db.conn.execute(
            "SELECT id,wp_candidate_id FROM properties "
            "WHERE wp_sync_state='synced' AND wp_candidate_id>0 "
            "AND COALESCE(wp_asset_state,'')<>'uploaded' "
            "AND EXISTS (SELECT 1 FROM photos WHERE photos.property_id=properties.id "
            "AND local_path<>'') ORDER BY id DESC LIMIT ?",
            (max_rows,),
        ).fetchall()
        for backfill in backfill_rows:
            pid = int(backfill["id"])
            candidate_id = int(backfill["wp_candidate_id"])
            try:
                asset_zip = create_zip(self.db, pid, require_pdf=False)
                client.upload_candidate_assets(candidate_id, asset_zip)
                self.db.set_asset_state(pid, state="uploaded", error="")
                assets_uploaded += 1
            except Exception as exc:
                self.db.set_asset_state(pid, state="error", error=str(exc))
                asset_errors += 1

        remaining_row = self.db.conn.execute(
            "SELECT COUNT(*) FROM sync_queue WHERE action='upsert_candidate' AND sync_status IN ('pending','error')"
        ).fetchone()
        remaining = int(remaining_row[0] if remaining_row else 0)
        return {
            "sent": sent,
            "synced": synced,
            "errors": errors,
            "batches": batches,
            "remaining": remaining,
            "halted": halted,
            "http_status": http_status,
            "error": halt_error,
            "repaired_legacy_403": repaired_legacy_403,
            "transports": transports,
            "assets_uploaded": assets_uploaded,
            "asset_errors": asset_errors,
        }

    def sync_inventory_snapshots(
        self,
        *,
        batch_size: int = 100,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Send only proven-complete site inventories used for outgoing detection.

        ``batch_size`` is retained for API compatibility but inventory snapshots
        are intentionally sent as one atomic ID set.  Chunking a snapshot would
        make the server compare against an incomplete inventory.
        """
        client = self.client()
        snapshots = self.db.pending_inventory_snapshots()
        synced = 0
        halted = False
        error = ""
        http_status = 0
        transports: dict[str, int] = {}
        results: list[dict] = []
        for snap in snapshots:
            site_code = str(snap.get("site_code") or "")
            source_site = str(snap.get("source_site") or "")
            expected = int(snap.get("expected_count") or 0)
            items = list(snap.get("items") or [])
            ids = [str(row.get("source_property_id") or "").strip() for row in items if isinstance(row, dict)]
            ids = sorted({v for v in ids if v})
            if expected < 1 or len(items) != expected or len(ids) != expected:
                error = f"{site_code} 재고 스냅샷 로컬 건수 불일치: rows={len(items)} ids={len(ids)} expected={expected}"
                self.db.mark_inventory_snapshot(site_code, state="error", error=error)
                results.append({"site_code": site_code, "ok": False, "error": error})
                continue
            if progress_callback:
                progress_callback(0, expected, f"{site_code} 나간매물 재고 동기화 준비 · {expected}건")
            try:
                reply = client.send_inventory_snapshot(
                    source_site=source_site,
                    source_property_ids=ids,
                    complete=True,
                    errors=0,
                    parse_errors=0,
                    blockers=[],
                )
                transport = str(reply.get("transport") or "unknown")
                transports[transport] = transports.get(transport, 0) + 1
                if not bool(reply.get("accepted")):
                    raise WordPressError("재고 스냅샷 서버 승인 응답이 없습니다.")
                self.db.mark_inventory_snapshot(site_code, state="synced", error="")
                synced += 1
                results.append({
                    "site_code": site_code,
                    "ok": True,
                    "expected_count": expected,
                    "baseline_only": bool(reply.get("baseline_only")),
                    "missing": int(reply.get("missing") or 0),
                    "suspected": int(reply.get("suspected") or 0),
                    "confirmed": int(reply.get("confirmed") or 0),
                    "restored": int(reply.get("restored") or 0),
                })
                if progress_callback:
                    progress_callback(expected, expected, f"{site_code} 재고 {expected}/{expected} 전송 완료 · {transport}")
            except Exception as e:
                halted = True
                error = str(e)
                m = re.search(r"HTTP\s+(\d{3})", error, re.I)
                http_status = int(m.group(1)) if m else 0
                self.db.mark_inventory_snapshot(site_code, state="error", error=error)
                results.append({"site_code": site_code, "ok": False, "error": error})
                break
        return {
            "pending_snapshots": len(snapshots),
            "synced_snapshots": synced,
            "halted": halted,
            "http_status": http_status,
            "error": error,
            "transports": transports,
            "results": results,
        }

    def auto_submit_package(self, property_id: int, zip_path: str | Path, *, auto_fast: bool = False) -> dict:
        prop = self.db.property(property_id)
        if not prop:
            raise WordPressError("매물을 찾을 수 없습니다.")
        result = self.client().upload_package(
            zip_path,
            source_property_id=str(prop.get("source_property_id") or ""),
            auto_fast=auto_fast,
        )
        job_id = str(result.get("job_id") or result.get("child_job_id") or "")
        state = str(result.get("status") or result.get("state") or "uploaded")
        self.db.set_package_state(property_id, state=state, job_id=job_id, error="")
        return result

    def pull_ready_drawings(self, *, auto_package: bool = True, auto_fast: bool = True) -> dict:
        """Pull staff-selected REINS PDFs and feed them into the existing FAST ZIP path.

        This is deliberately draft-only: upload_package always sends the server's
        X-Takuro-Draft-Only contract, and auto_fast=True may create a WordPress draft
        but must never publish it. Existing server job IDs are checked first so a
        ready candidate is not uploaded again on every poll.
        """
        client = self.client()
        rows = client.ready()
        matched = downloaded = skipped = packaged = submitted = already_submitted = errors = 0
        skip_details: list[dict] = []
        error_details: list[dict] = []
        for remote in rows:
            raw_site = str(remote.get("source_site") or "").strip()
            # WordPress stores Collector source_site as the bare hostname
            # (e.g. ``kinoshita-chintai.com``). canonical_host() expects a URL
            # and returns an empty string for a bare hostname, which made every
            # ready candidate miss after a fresh local setup. Preserve bare
            # hostnames instead of collapsing them to an empty identity.
            site = canonical_host(raw_site)
            if not site and raw_site:
                site = canonical_host("https://" + raw_site.lstrip("/")) or raw_site.lower().strip(".")
            source_id = str(remote.get("source_property_id") or "").strip()
            local = self.db.property_by_identity(site, source_id) if site and source_id else None
            if not local:
                local = self.db.property_by_ready_fallback(
                    site,
                    str(remote.get("building_name") or ""),
                    str(remote.get("room") or ""),
                )
            if not local:
                skipped += 1
                if len(skip_details) < 10:
                    skip_details.append({
                        "candidate_id": int(remote.get("id") or 0),
                        "source_site": raw_site,
                        "normalized_site": site,
                        "source_property_id": source_id,
                        "building_name": str(remote.get("building_name") or "")[:120],
                        "room": str(remote.get("room") or "")[:40],
                        "reason": "local_identity_not_found",
                    })
                continue
            matched += 1
            pid = int(local["id"])
            try:
                self.db.update_wp_state(pid, state="synced", candidate_id=int(remote.get("id") or 0), error="")
                self.db.set_status(pid, "ready")
                existing_pdf = Path(str(local.get("pdf_path") or ""))
                if not existing_pdf.is_file():
                    from .photos import PhotoManager
                    folder = PhotoManager.property_folder(local)
                    temp = folder / "REINS.from-wordpress.pdf"
                    client.download_drawing(int(remote["id"]), temp)
                    attach_pdf(self.db, pid, temp)
                    temp.unlink(missing_ok=True)
                    downloaded += 1
                if not auto_package:
                    continue

                current = self.db.property(pid) or local
                existing_job = str(current.get("wp_package_job_id") or "")
                if existing_job:
                    try:
                        remote_job = client.package_status(existing_job)
                        state = str(remote_job.get("status") or remote_job.get("job_state") or "submitted")
                        error = str(remote_job.get("error") or "")
                        self.db.set_package_state(pid, state=state, job_id=existing_job, error=error)
                    except Exception as exc:
                        # A recorded server job is authoritative for idempotency.
                        # Never upload it again merely because the status lookup failed.
                        self.db.set_package_state(pid, state="submitted", job_id=existing_job, error=str(exc))
                    already_submitted += 1
                    continue

                from .fetcher import Fetcher
                from .photos import PhotoManager
                manager = PhotoManager(self.db, Fetcher(visible_browser=False))
                photo_paths = manager.local_photo_paths(pid)
                if not photo_paths:
                    manager.download_for_property(pid)
                    photo_paths = manager.local_photo_paths(pid)
                if not photo_paths:
                    raise WordPressError("후보 사진을 확보하지 못해 자동 ZIP 등록을 중단했습니다.")
                out = create_zip(self.db, pid, require_pdf=True)
                packaged += 1
                result = self.auto_submit_package(pid, out, auto_fast=bool(auto_fast))
                if result.get("draft_only") is not True:
                    raise WordPressError("서버가 draft-only 계약을 확인하지 않았습니다.")
                submitted += 1
            except Exception as exc:
                errors += 1
                message = str(exc)
                self.db.set_package_state(pid, state="error", error=message)
                if len(error_details) < 10:
                    error_details.append({
                        "candidate_id": int(remote.get("id") or 0),
                        "property_id": pid,
                        "source_site": raw_site,
                        "source_property_id": source_id,
                        "building_name": str(remote.get("building_name") or "")[:120],
                        "room": str(remote.get("room") or "")[:40],
                        "error": message[:500],
                    })
        return {
            "remote_ready": len(rows), "matched": matched, "downloaded": downloaded,
            "packaged": packaged, "submitted": submitted, "already_submitted": already_submitted,
            "skipped": skipped, "errors": errors, "draft_only": True, "auto_fast": bool(auto_fast),
            "skip_details": skip_details, "error_details": error_details,
        }
