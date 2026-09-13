from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .db import Database
from .fetcher import Fetcher
from .paths import property_root
from .sites import adapter_for_url
from .utils import safe_filename

Progress = Callable[[str, int, int], None]
logger = logging.getLogger(__name__)


def _trim_white_margin(image: Image.Image) -> Image.Image:
    """Remove paired, uniform near-white padding without mistaking bright rooms for it."""
    rgb = image.convert("RGB")
    px = rgb.load()

    def white_col(x: int) -> bool:
        return all(min(px[x, y]) >= 247 and max(px[x, y]) - min(px[x, y]) <= 8 for y in range(rgb.height))

    def white_row(y: int) -> bool:
        return all(min(px[x, y]) >= 247 and max(px[x, y]) - min(px[x, y]) <= 8 for x in range(rgb.width))

    left = next((x for x in range(rgb.width) if not white_col(x)), 0)
    right = next((x + 1 for x in range(rgb.width - 1, -1, -1) if not white_col(x)), rgb.width)
    top = next((y for y in range(rgb.height) if not white_row(y)), 0)
    bottom = next((y + 1 for y in range(rgb.height - 1, -1, -1) if not white_row(y)), rgb.height)
    # Provider padding is paired on an axis. Requiring both sides protects white sky/walls.
    crop_x = left > 0 and right < rgb.width
    crop_y = top > 0 and bottom < rgb.height
    box = (left if crop_x else 0, top if crop_y else 0, right if crop_x else rgb.width, bottom if crop_y else rgb.height)
    if box[2] - box[0] >= 100 and box[3] - box[1] >= 100:
        return image.crop(box)
    return image


def _watermark(image: Image.Image, text: str = "TAKURO") -> Image.Image:
    base = image.convert("RGBA")
    target_width = max(80, int(base.width * 0.34))
    font_size = max(18, int(base.width * 0.10))
    font = None
    for name in (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\segoeui.ttf"):
        try:
            font = ImageFont.truetype(name, font_size)
            break
        except OSError:
            pass
    font = font or ImageFont.load_default()
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    bounds = draw.textbbox((0, 0), text, font=font)
    width = bounds[2] - bounds[0]
    height = bounds[3] - bounds[1]
    if width > target_width and width:
        font_size = max(12, int(font_size * target_width / width))
        try:
            font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", font_size)
        except OSError:
            pass
        bounds = draw.textbbox((0, 0), text, font=font)
        width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    x, y = (base.width - width) // 2, (base.height - height) // 2
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 82))
    return Image.alpha_composite(base, overlay)


def _prepare_photo(data: bytes, ext: str) -> tuple[bytes, str]:
    """Crop provider padding and add a watermark without resizing the source."""
    with Image.open(io.BytesIO(data)) as opened:
        image = _watermark(_trim_white_margin(opened))
        output = io.BytesIO()
        if ext == "png":
            image.save(output, format="PNG", optimize=True)
        elif ext == "webp":
            image.convert("RGB").save(output, format="WEBP", quality=94, method=6)
        else:
            image.convert("RGB").save(output, format="JPEG", quality=95, subsampling=0, optimize=True)
        return output.getvalue(), ext


def _kind_from_bytes(data: bytes, content_type: str, url: str) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    c = (content_type or "").lower()
    if "jpeg" in c:
        return "jpg"
    if "png" in c:
        return "png"
    if "webp" in c:
        return "webp"
    guessed, _ = mimetypes.guess_type(url)
    if guessed == "image/jpeg":
        return "jpg"
    if guessed == "image/png":
        return "png"
    if guessed == "image/webp":
        return "webp"
    return None


class PhotoManager:
    def __init__(self, db: Database, fetcher: Fetcher | None = None):
        self.db = db
        self.fetcher = fetcher or Fetcher()

    @staticmethod
    def property_folder(prop: dict) -> Path:
        name = safe_filename(f"{prop.get('building_name','')} {prop.get('room','')}")
        p = property_root() / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    def download_for_property(self, property_id: int, progress: Progress | None = None) -> dict:
        prop = self.db.property(property_id)
        if not prop:
            raise RuntimeError("매물을 찾을 수 없습니다.")
        rows = self.db.photos(property_id)
        if not rows:
            self.db.set_photo_state(property_id, "no_sources")
            return {"downloaded": 0, "failed": 0, "duplicates": 0, "folder": str(self.property_folder(prop))}
        folder = self.property_folder(prop)
        base = safe_filename(f"{prop['building_name']}_{prop['room']}")
        seen_hashes: set[str] = set()
        for row in rows:
            if row.get("sha256"):
                seen_hashes.add(str(row["sha256"]))
        ordinary_counter = 0
        downloaded = failed = duplicates = 0
        floorplan_written = False
        cookies = {}
        adapter = adapter_for_url(str(prop.get("source_url") or ""))
        site_code = adapter.code if adapter else str(prop["source_site"]).split(".")[0].upper()
        raw = prop.get("raw_payload") or {}
        if isinstance(raw, dict):
            cookies = raw.get("fetch_cookies") or {}
        for idx, row in enumerate(rows, start=1):
            if progress:
                progress(f"사진 {idx}/{len(rows)} 다운로드", idx, len(rows))
            if row.get("status") == "downloaded" and row.get("local_path") and Path(str(row["local_path"])).exists():
                continue
            try:
                data, content_type = self.fetcher.download(
                    str(row["source_url"]),
                    site_code,
                    referer=str(prop.get("source_url") or ""),
                    cookies=cookies if isinstance(cookies, dict) else None,
                    # AMM is a static public site. A failed CDN request must not
                    # launch an unbounded browser process and stall the whole run.
                    browser_fallback=site_code != "AMM",
                )
                if len(data) < 100:
                    raise RuntimeError("이미지 바이트가 너무 작습니다.")
                ext = _kind_from_bytes(data, content_type, str(row["source_url"]))
                if not ext:
                    raise RuntimeError("JPEG/PNG/WebP 이미지가 아닙니다.")
                data, ext = _prepare_photo(data, ext)
                digest = hashlib.sha256(data).hexdigest()
                if digest in seen_hashes:
                    self.db.mark_photo(int(row["id"]), status="duplicate", sha256=digest)
                    duplicates += 1
                    continue
                seen_hashes.add(digest)
                is_floorplan = str(row.get("kind")) == "floorplan"
                if is_floorplan and not floorplan_written:
                    filename = f"{base}_間取り.{ext}"
                    floorplan_written = True
                elif is_floorplan:
                    # Keep additional floorplans distinct without pretending they are ordinary room photos.
                    filename = f"{base}_間取り_{idx:02d}.{ext}"
                else:
                    ordinary_counter += 1
                    filename = f"{base}_{ordinary_counter:02d}.{ext}"
                path = folder / filename
                path.write_bytes(data)
                self.db.mark_photo(int(row["id"]), status="downloaded", local_path=str(path), sha256=digest)
                downloaded += 1
            except Exception as e:
                if site_code == "AMM":
                    logger.warning(
                        "AMM photo failed url=%s phase=photo_download error=%s",
                        row.get("source_url"), e,
                    )
                self.db.mark_photo(int(row["id"]), status="error", error=str(e))
                failed += 1
        state = "ready" if downloaded or any(r.get("status") == "downloaded" for r in self.db.photos(property_id)) else "error"
        if failed and state == "ready":
            state = "partial"
        self.db.set_photo_state(property_id, state)
        return {"downloaded": downloaded, "failed": failed, "duplicates": duplicates, "folder": str(folder)}

    def local_photo_paths(self, property_id: int) -> list[Path]:
        out: list[Path] = []
        for row in self.db.photos(property_id):
            p = Path(str(row.get("local_path") or ""))
            if row.get("status") == "downloaded" and p.exists():
                out.append(p)
        return out
