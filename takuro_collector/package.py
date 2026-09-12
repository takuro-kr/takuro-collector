from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

from .db import Database
from .paths import export_root
from .photos import PhotoManager
from .txt_export import write_text
from .collected_export import write_collected_files
from .utils import safe_filename

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_ZIP_BYTES = 50 * 1024 * 1024
SAFE_TARGET_BYTES = 49 * 1024 * 1024


def attach_pdf(db: Database, property_id: int, source_pdf: str | Path) -> Path:
    prop = db.property(property_id)
    if not prop:
        raise RuntimeError("매물을 찾을 수 없습니다.")
    src = Path(source_pdf)
    if not src.is_file():
        raise RuntimeError("PDF 파일을 찾을 수 없습니다.")
    size = src.stat().st_size
    if size < 5 or size > MAX_PDF_BYTES:
        raise RuntimeError("REINS PDF는 20MiB 이하만 사용할 수 있습니다.")
    with src.open("rb") as fh:
        if fh.read(5) != b"%PDF-":
            raise RuntimeError("선택한 파일은 PDF 형식이 아닙니다.")
    folder = PhotoManager.property_folder(prop)
    dest = folder / "REINS.pdf"
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    db.set_pdf(property_id, str(dest))
    return dest


def create_zip(db: Database, property_id: int, *, require_pdf: bool = True) -> Path:
    prop = db.property(property_id)
    if not prop:
        raise RuntimeError("매물을 찾을 수 없습니다.")
    folder = PhotoManager.property_folder(prop)
    txt = write_text(prop, folder)
    collected_txt, collected_json = write_collected_files(prop, folder)
    pdf = Path(str(prop.get("pdf_path") or ""))
    if require_pdf and (not pdf.is_file()):
        raise RuntimeError("REINS PDF를 먼저 추가해 주세요.")
    photo_paths = PhotoManager(db).local_photo_paths(property_id)
    members: list[Path] = [txt, collected_txt, collected_json, *photo_paths]
    if pdf.is_file():
        members.append(pdf)
    if not members:
        raise RuntimeError("ZIP에 넣을 파일이 없습니다.")
    raw_size = sum(p.stat().st_size for p in members if p.is_file())
    if raw_size > SAFE_TARGET_BYTES:
        raise RuntimeError(f"ZIP 원본 파일 합계가 {raw_size / 1048576:.1f}MiB입니다. TAKURO 50MiB 업로드 한도를 넘을 수 있으므로 사진 수를 줄여주세요.")
    filename = safe_filename(f"{prop['building_name']} {prop['room']}") + ".zip"
    out = export_root() / filename
    photo_set = {p.resolve() for p in photo_paths}
    photo_number = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in members:
            if p.is_file():
                # Import V2 naturally sorts ZIP photo names. Prefixing the intended
                # order keeps exterior first and floorplan second on WordPress.
                if p.resolve() in photo_set:
                    photo_number += 1
                    arcname = f"photo-{photo_number:03d}{p.suffix.lower()}"
                else:
                    arcname = p.name
                zf.write(p, arcname=arcname)
    if out.stat().st_size > MAX_ZIP_BYTES:
        out.unlink(missing_ok=True)
        raise RuntimeError("생성된 ZIP이 50MiB를 초과했습니다. 사진 수를 줄여주세요.")
    db.set_zip(property_id, str(out))
    db.set_status(property_id, "packaged")
    return out
