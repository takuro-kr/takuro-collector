from __future__ import annotations

import os
import re
import subprocess
from urllib.parse import urlencode
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .paths import browser_profile_dir

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class LoginRequired(RuntimeError):
    pass


class FetchFailed(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    html: str
    status_code: int
    via_browser: bool = False
    cookies: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


class Fetcher:
    def __init__(self, timeout: int = 20, visible_browser: bool = False):
        self.timeout = timeout
        self.visible_browser = visible_browser
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "ja,en-US;q=0.8,en;q=0.6"})

    @staticmethod
    def _looks_blocked(status: int, html: str) -> bool:
        if status in {401, 403, 429, 503}:
            return True
        t = (html or "").lower()
        # Do not treat the mere word "cloudflare" as a block signal. Normal pages
        # commonly load assets from cdnjs.cloudflare.com, which caused false positives
        # on Kinoshita's valid search-result HTML. Match challenge-specific markers instead.
        needles = [
            "enable javascript", "javascriptを有効", "access denied", "forbidden",
            "captcha", "ログインしてください", "ログインが必要",
            "sign in", "authentication required",
            "cf-chl-", "challenge-platform", "cloudflare ray id",
            "checking your browser", "just a moment...",
        ]
        return any(n in t for n in needles)

    @staticmethod
    def _looks_like_kin_search_result(html: str) -> bool:
        t = html or ""
        return (
            "search_result_housing_list" in t
            or "change_list_count" in t
            or ("name=\"page_num\"" in t and "search_result.html" in t)
        )

    def post_form(
        self,
        url: str,
        site_code: str,
        data,
        *,
        login_expected: bool = False,
        referer: str = "",
        success_marker: str = "",
        browser_fallback: bool = True,
    ) -> FetchResult:
        """POST a form with the shared site session, then retry through a browser session.

        KIN's search endpoint can return HTTP 200 for a response that is not the actual
        search-result document.  Success is therefore based on site-specific result markers,
        not status/length alone.  If the plain requests session does not yield a valid result,
        retry in a persistent Chromium context after visiting the referer page so site cookies
        and navigation state are established.
        """
        headers = {}
        if referer:
            headers["Referer"] = referer
        r = None
        request_error = None
        try:
            r = self.session.post(url, data=data, headers=headers or None, timeout=self.timeout, allow_redirects=True)
            html = r.text or ""
            marker_ok = success_marker in html if success_marker else True
            if site_code == "KIN" and not success_marker:
                marker_ok = self._looks_like_kin_search_result(html)
            if r.ok and marker_ok and not self._looks_blocked(r.status_code, html):
                return FetchResult(r.url, html, r.status_code, False, self.session.cookies.get_dict(), dict(r.headers))
            if login_expected and (r.status_code in {401, 403} or "login" in r.url.lower() or "ログイン" in html[:5000]):
                raise LoginRequired(f"{site_code}: 로그인/세션 확인이 필요합니다.")
        except requests.RequestException as e:
            request_error = e

        if browser_fallback:
            try:
                return self.browser_post_form(
                    url, site_code, data, referer=referer, login_expected=login_expected, success_marker=success_marker
                )
            except LoginRequired:
                raise
            except Exception as e:
                detail = self._post_diagnostic(r, request_error)
                raise FetchFailed(f"{site_code}: {url} POST 수집 실패 ({detail}); 브라우저 재시도 실패: {e}") from e

        detail = self._post_diagnostic(r, request_error)
        raise FetchFailed(f"{site_code}: {url} POST 수집 실패 ({detail})")

    @staticmethod
    def _post_diagnostic(response, error: Exception | None = None) -> str:
        if response is None:
            return f"request_error={error}" if error else "response 없음"
        html = response.text or ""
        title = ""
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
        return f"HTTP {response.status_code}, bytes={len(response.content or b'')}, final_url={response.url}, title={title!r}"

    def browser_post_form(
        self,
        url: str,
        site_code: str,
        data,
        *,
        referer: str = "",
        login_expected: bool = False,
        success_marker: str = "",
    ) -> FetchResult:
        from playwright.sync_api import sync_playwright

        profile = browser_profile_dir(site_code)
        with sync_playwright() as p:
            context = None
            last = None
            for channel in ("msedge", "chrome", None):
                try:
                    kwargs: dict[str, Any] = {
                        "user_data_dir": str(profile),
                        "headless": not self.visible_browser,
                        "locale": "ja-JP",
                        "args": ["--disable-blink-features=AutomationControlled"],
                    }
                    if channel:
                        kwargs["channel"] = channel
                    context = p.chromium.launch_persistent_context(**kwargs)
                    break
                except Exception as e:
                    last = e
            if context is None:
                raise FetchFailed(f"Edge/Chrome을 열 수 없습니다: {last}")
            try:
                if referer:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(referer, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                headers = {"Referer": referer} if referer else None
                encoded = urlencode(list(data), doseq=True)
                req_headers = {"Content-Type": "application/x-www-form-urlencoded"}
                if headers:
                    req_headers.update(headers)
                response = context.request.post(url, data=encoded, headers=req_headers, timeout=self.timeout * 1000)
                html = response.text()
                marker_ok = success_marker in html if success_marker else True
                if site_code == "KIN" and not success_marker:
                    marker_ok = self._looks_like_kin_search_result(html)
                if login_expected and (response.status in {401, 403} or "ログイン" in html[:5000]):
                    raise LoginRequired(f"{site_code}: 로그인/세션 확인이 필요합니다.")
                if not response.ok or not marker_ok or self._looks_blocked(response.status, html):
                    raise FetchFailed(
                        f"HTTP {response.status}, bytes={len(html.encode('utf-8', errors='ignore'))}, marker={marker_ok}"
                    )
                cookies_list = context.cookies()
                cookies = {str(c.get("name")): str(c.get("value")) for c in cookies_list}
                return FetchResult(response.url, html, response.status, True, cookies, dict(response.headers))
            finally:
                context.close()

    def fetch(self, url: str, site_code: str, *, force_browser: bool = False, login_expected: bool = False) -> FetchResult:
        if not force_browser:
            try:
                r = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                html = r.text or ""
                if r.ok and len(html) > 500 and not self._looks_blocked(r.status_code, html):
                    return FetchResult(r.url, html, r.status_code, False, self.session.cookies.get_dict(), dict(r.headers))
                if login_expected and (r.status_code in {401, 403} or "login" in r.url.lower() or "ログイン" in html[:5000]):
                    # Browser profile may already contain a valid session; try it before calling this login-required.
                    pass
            except requests.RequestException:
                pass
        try:
            return self.browser_fetch(url, site_code, login_expected=login_expected)
        except LoginRequired:
            raise
        except Exception as e:
            raise FetchFailed(f"{site_code}: {url} 수집 실패: {e}") from e

    def browser_fetch(self, url: str, site_code: str, *, login_expected: bool = False) -> FetchResult:
        from playwright.sync_api import sync_playwright

        profile = browser_profile_dir(site_code)
        last_error: Exception | None = None
        with sync_playwright() as p:
            context = None
            for channel in ("msedge", "chrome", None):
                try:
                    kwargs: dict[str, Any] = {
                        "user_data_dir": str(profile),
                        "headless": not self.visible_browser,
                        "viewport": {"width": 1440, "height": 1000},
                        "locale": "ja-JP",
                        "args": ["--disable-blink-features=AutomationControlled"],
                    }
                    if channel:
                        kwargs["channel"] = channel
                    context = p.chromium.launch_persistent_context(**kwargs)
                    break
                except Exception as e:
                    last_error = e
            if context is None:
                raise FetchFailed(
                    "Edge/Chrome을 열 수 없습니다. Microsoft Edge가 설치되어 있는지 확인하세요. "
                    f"({last_error})"
                )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                html = page.content()
                final_url = page.url
                status = response.status if response else 200
                cookies_list = context.cookies()
                cookies = {str(c.get("name")): str(c.get("value")) for c in cookies_list}
                visible_text = ""
                try:
                    visible_text = page.locator("body").inner_text(timeout=3000)[:10000]
                except Exception:
                    pass
                if login_expected and (status in {401, 403} or self._appears_logged_out(final_url, html, visible_text)):
                    raise LoginRequired(f"{site_code}: 로그인 필요")
                if self._looks_blocked(status, html) and "captcha" in (html + visible_text).lower():
                    raise LoginRequired(f"{site_code}: CAPTCHA/사람 확인이 필요합니다. [브라우저 열기/로그인]으로 직접 확인하세요.")
                return FetchResult(final_url, html, status, True, cookies, {})
            finally:
                context.close()

    @staticmethod
    def _appears_logged_out(url: str, html: str, text: str) -> bool:
        sample = (url + "\n" + html[:12000] + "\n" + text[:6000]).lower()
        login_terms = ["login", "signin", "ログイン", "ユーザー名", "パスワード", "メールアドレス"]
        # A mere login link in a footer is insufficient; require a form-ish indicator too.
        score = sum(1 for x in login_terms if x.lower() in sample)
        return score >= 2 and ("password" in sample or "パスワード" in sample or "type=\"password\"" in sample)

    def download(self, url: str, site_code: str, *, referer: str = "", cookies: dict[str, str] | None = None) -> tuple[bytes, str]:
        headers = {"User-Agent": DEFAULT_UA}
        if referer:
            headers["Referer"] = referer
        try:
            r = self.session.get(url, headers=headers, cookies=cookies or {}, timeout=self.timeout, allow_redirects=True)
            if r.ok and r.content:
                return r.content, str(r.headers.get("Content-Type", ""))
        except requests.RequestException:
            pass
        return self._browser_download(url, site_code, referer=referer)

    def _browser_download(self, url: str, site_code: str, *, referer: str = "") -> tuple[bytes, str]:
        from playwright.sync_api import sync_playwright

        profile = browser_profile_dir(site_code)
        with sync_playwright() as p:
            context = None
            last = None
            for channel in ("msedge", "chrome", None):
                try:
                    kwargs: dict[str, Any] = {"user_data_dir": str(profile), "headless": True, "locale": "ja-JP"}
                    if channel:
                        kwargs["channel"] = channel
                    context = p.chromium.launch_persistent_context(**kwargs)
                    break
                except Exception as e:
                    last = e
            if context is None:
                raise FetchFailed(f"이미지 다운로드용 브라우저를 열 수 없습니다: {last}")
            try:
                headers = {"Referer": referer} if referer else None
                response = context.request.get(url, headers=headers, timeout=self.timeout * 1000)
                if not response.ok:
                    raise FetchFailed(f"HTTP {response.status}")
                return response.body(), str(response.headers.get("content-type", ""))
            finally:
                context.close()


def _edge_candidates() -> list[Path]:
    candidates: list[Path] = []
    pf86 = os.environ.get("PROGRAMFILES(X86)", "")
    pf = os.environ.get("PROGRAMFILES", "")
    local = os.environ.get("LOCALAPPDATA", "")
    for base, rel in [
        (pf86, r"Microsoft\Edge\Application\msedge.exe"),
        (pf, r"Microsoft\Edge\Application\msedge.exe"),
        (local, r"Microsoft\Edge\Application\msedge.exe"),
        (pf, r"Google\Chrome\Application\chrome.exe"),
        (pf86, r"Google\Chrome\Application\chrome.exe"),
        (local, r"Google\Chrome\Application\chrome.exe"),
    ]:
        if base:
            candidates.append(Path(base) / rel)
    return candidates


def open_login_browser(site_code: str, url: str) -> None:
    if os.name != "nt":
        import webbrowser
        webbrowser.open(url)
        return
    exe = next((p for p in _edge_candidates() if p.exists()), None)
    if not exe:
        raise RuntimeError("Microsoft Edge 또는 Chrome 실행 파일을 찾지 못했습니다.")
    profile = browser_profile_dir(site_code)
    subprocess.Popen([str(exe), f"--user-data-dir={profile}", "--no-first-run", url])
