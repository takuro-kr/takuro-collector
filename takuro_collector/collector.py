from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .db import Database
from .fetcher import Fetcher, FetchFailed, LoginRequired
from .models import PropertyCandidate
from .logging_setup import get_logger
from .photos import PhotoManager
from .wordpress import WordPressSync, WordPressError

logger = get_logger("collector")
from .sites import adapter_for_url, adapters

ProgressFn = Callable[[str, str, int, int], None]


@dataclass
class ScanResult:
    sites: int = 0
    discovered: int = 0
    new_count: int = 0
    existing_count: int = 0
    errors: int = 0
    login_required: int = 0
    skipped_region_or_parse: int = 0
    listed_count: int = 0
    prechecked_count: int = 0
    duplicate_filtered: int = 0
    review_filtered: int = 0
    detail_attempted: int = 0
    inventory_snapshots: int = 0
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sites": self.sites,
            "discovered": self.discovered,
            "new_count": self.new_count,
            "existing_count": self.existing_count,
            "errors": self.errors,
            "login_required": self.login_required,
            "skipped_region_or_parse": self.skipped_region_or_parse,
            "listed_count": self.listed_count,
            "prechecked_count": self.prechecked_count,
            "duplicate_filtered": self.duplicate_filtered,
            "review_filtered": self.review_filtered,
            "detail_attempted": self.detail_attempted,
            "inventory_snapshots": self.inventory_snapshots,
            "messages": self.messages,
        }


class CollectorEngine:
    def __init__(self, db: Database, *, visible_browser: bool = False):
        self.db = db
        self.fetcher = Fetcher(visible_browser=visible_browser)

    def scan_all(self, progress: ProgressFn | None = None) -> ScanResult:
        result = ScanResult()
        logger.info("full scan started")
        scan_id = self.db.start_scan("all")
        all_adapters = adapters()
        enabled = [a for a in all_adapters if self.db.get_bool(f"site_enabled_{a.code}", True)]
        for index, adapter in enumerate(enabled, start=1):
            result.sites += 1
            if progress:
                progress(adapter.code, f"{adapter.label} 검색 시작", index, len(enabled))
            site_new = 0
            site_total = 0
            try:
                discovery = adapter.discover(self.fetcher)
                urls = list(discovery.urls)
                hints = dict(getattr(discovery, "hints", {}) or {})
                result.listed_count += int(getattr(discovery, "listed_count", 0) or len(urls))
                result.messages.extend(list(getattr(discovery, "messages", []) or []))
                if not urls:
                    raise FetchFailed("상세 매물 링크를 찾지 못했습니다. URL 수집은 계속 사용할 수 있습니다.")

                # Outgoing detection is allowed only from an adapter that can prove
                # a complete current inventory.  Save this *before* duplicate
                # filtering so already-registered live listings remain present in
                # the snapshot and are never falsely marked as gone.
                if bool(getattr(discovery, "inventory_complete", False)):
                    snapshot_items = [hints[u] for u in urls if u in hints]
                    if len(snapshot_items) == len(urls):
                        snap = self.db.save_inventory_snapshot(
                            adapter.code,
                            str(getattr(discovery, "inventory_site", "") or ""),
                            snapshot_items,
                        )
                        result.inventory_snapshots += 1
                        result.messages.append(
                            f"{adapter.code}: 나간매물 비교용 완전 재고 저장 {snap['expected_count']}건"
                        )
                    else:
                        result.messages.append(
                            f"{adapter.code}: 재고 스냅샷 보류 - 상세 URL {len(urls)}건 중 식별정보 {len(snapshot_items)}건"
                        )

                # KIN and future adapters may provide lightweight list-page identity
                # hints.  When WordPress is configured, reject exact building+room
                # duplicates before any detail page is fetched.
                if hints:
                    wp = WordPressSync(self.db)
                    if wp.configured():
                        allowed: list[str] = []
                        lookup_rows = [hints[u] for u in urls if u in hints]
                        verdict_by_url: dict[str, dict] = {}
                        try:
                            client = wp.client()
                            for off in range(0, len(lookup_rows), 100):
                                chunk = lookup_rows[off:off + 100]
                                reply = client.duplicate_check(chunk)
                                results = list(reply.get("results") or [])
                                result.prechecked_count += len(chunk)
                                for row, verdict in zip(chunk, results):
                                    verdict_by_url[str(row.get("url") or "")] = dict(verdict or {})
                        except Exception as e:
                            # Do not silently treat a failed duplicate lookup as safe.
                            raise FetchFailed(f"WordPress 매물 중복 사전조회 실패: {e}") from e

                        for url in urls:
                            verdict = verdict_by_url.get(url)
                            if not verdict:
                                # Missing server verdict is review-worthy, not safe-to-fetch.
                                result.review_filtered += 1
                                result.messages.append(f"{adapter.code} {url}: 중복조회 응답 누락 - 확인 필요")
                                continue
                            status = str(verdict.get("status") or "needs_review")
                            if status == "registered":
                                result.duplicate_filtered += 1
                                continue
                            if status == "needs_review":
                                result.review_filtered += 1
                                result.messages.append(
                                    f"{adapter.code} {hints[url].get('building_name','')} {hints[url].get('room','')}: 중복 가능성 확인 필요"
                                )
                                continue
                            allowed.append(url)
                        urls = allowed

                pending_candidates: list[PropertyCandidate] = []

                def flush_candidates() -> None:
                    nonlocal site_total, site_new
                    if not pending_candidates:
                        return
                    saved = self.db.upsert_properties_batch(pending_candidates)
                    manager = PhotoManager(self.db, self.fetcher)
                    for (property_id, is_new), candidate in zip(saved, pending_candidates):
                        site_total += 1
                        result.discovered += 1
                        if is_new:
                            site_new += 1
                            result.new_count += 1
                        else:
                            result.existing_count += 1
                        # A WordPress candidate must arrive with its TXT and photos
                        # already staged. Download only when this local property has
                        # no completed photos, so later scans do not repeat the work.
                        if not manager.local_photo_paths(property_id):
                            photo_result = manager.download_for_property(property_id)
                            if photo_result.get("failed"):
                                result.messages.append(
                                    f"{adapter.code} {candidate.building_name} {candidate.room}: "
                                    f"사진 일부 실패 {photo_result.get('failed')}개"
                                )
                    pending_candidates.clear()

                for uidx, url in enumerate(urls, start=1):
                    if progress:
                        progress(adapter.code, f"{adapter.label} {uidx}/{len(urls)}", index, len(enabled))
                    try:
                        result.detail_attempted += 1
                        candidate = adapter.collect_url(self.fetcher, url)
                        pending_candidates.append(candidate)
                        if len(pending_candidates) >= 25:
                            flush_candidates()
                    except LoginRequired as e:
                        flush_candidates()
                        result.login_required += 1
                        result.messages.append(str(e))
                        # One login failure normally applies to the whole site.
                        raise
                    except ValueError as e:
                        result.skipped_region_or_parse += 1
                        result.messages.append(f"{adapter.code} {url}: {e}")
                    except Exception as e:
                        result.errors += 1
                        result.messages.append(f"{adapter.code} {url}: {e}")
                flush_candidates()
                self.db.set_site_status(adapter.code, "ok", new_count=site_new, total_count=site_total)
                logger.info("site %s ok total=%s new=%s", adapter.code, site_total, site_new)
            except LoginRequired as e:
                logger.warning("site %s login required: %s", adapter.code, e)
                self.db.set_site_status(adapter.code, "login_required", error=str(e), new_count=site_new, total_count=site_total)
            except Exception as e:
                logger.exception("site %s failed", adapter.code)
                result.errors += 1
                result.messages.append(f"{adapter.code}: {e}")
                self.db.set_site_status(adapter.code, "error", error=str(e), new_count=site_new, total_count=site_total)
        self.db.finish_scan(scan_id, result.to_dict())
        logger.info("full scan finished %s", result.to_dict())
        return result

    def collect_urls(self, urls: list[str], progress: ProgressFn | None = None) -> ScanResult:
        result = ScanResult(sites=0)
        scan_id = self.db.start_scan("urls")
        seen_sites: set[str] = set()
        for index, url in enumerate(urls, start=1):
            adapter = adapter_for_url(url)
            if not adapter:
                result.errors += 1
                result.messages.append(f"지원하지 않는 사이트: {url}")
                continue
            seen_sites.add(adapter.code)
            if progress:
                progress(adapter.code, f"URL 수집 {index}/{len(urls)}", index, len(urls))
            try:
                candidate = adapter.collect_url(self.fetcher, url)
                property_id, is_new = self.db.upsert_property(candidate)
                result.discovered += 1
                result.new_count += 1 if is_new else 0
                result.existing_count += 0 if is_new else 1

                # URL 수동 수집은 직원이 링크를 넣는 즉시 사진까지 준비되는 흐름이다.
                # 전체 사이트 스캔은 대량 다운로드를 피하기 위해 기존 동작을 유지한다.
                manager = PhotoManager(self.db, self.fetcher)
                photo_result = manager.download_for_property(
                    property_id,
                    (lambda msg, cur, total: progress(adapter.code, f"사진 {msg}", index, len(urls))) if progress else None,
                )
                if photo_result.get("failed"):
                    result.messages.append(
                        f"{adapter.code} 사진 일부 실패: {photo_result.get('failed')}개 (다운로드 {photo_result.get('downloaded', 0)}개)"
                    )
                self.db.set_site_status(adapter.code, "ok", new_count=1 if is_new else 0, total_count=1)
            except LoginRequired as e:
                result.login_required += 1
                result.messages.append(str(e))
                self.db.set_site_status(adapter.code, "login_required", error=str(e))
            except Exception as e:
                result.errors += 1
                result.messages.append(f"{adapter.code}: {e}")
                self.db.set_site_status(adapter.code, "error", error=str(e))
        result.sites = len(seen_sites)
        self.db.finish_scan(scan_id, result.to_dict())
        return result
