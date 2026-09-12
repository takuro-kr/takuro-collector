from __future__ import annotations


def photo_state_label(value: str) -> str:
    labels = {
        "not_started": "⚪ 미다운로드",
        "ready": "🟢 다운로드 완료",
        "partial": "🟡 일부 완료",
        "error": "🔴 다운로드 오류",
        "no_sources": "⚪ 사진 없음",
    }
    return labels.get((value or "").strip(), value or "-")


def property_status_label(value: str) -> str:
    labels = {
        "new": "신규",
        "ready": "등록 준비",
        "packaged": "ZIP 완료",
    }
    return labels.get((value or "").strip(), value or "-")


def wp_state_label(value: str) -> str:
    labels = {
        "pending": "대기",
        "synced": "🟢 동기화됨",
        "error": "🔴 오류",
    }
    return labels.get((value or "").strip(), value or "-")
