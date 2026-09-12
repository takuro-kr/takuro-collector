from pathlib import Path
from zipfile import ZipFile

from takuro_collector import package


def test_ordered_photo_archive_names(tmp_path, monkeypatch):
    class FakeDB:
        def property(self, _pid): return {"building_name": "테스트", "room": "101", "pdf_path": ""}
        def set_zip(self, *_args): pass
        def set_status(self, *_args): pass

    folder = tmp_path / "property"
    folder.mkdir()
    exterior = folder / "building_01.jpg"
    floorplan = folder / "building_間取り.jpg"
    exterior.write_bytes(b"outside")
    floorplan.write_bytes(b"plan")
    monkeypatch.setattr(package.PhotoManager, "property_folder", staticmethod(lambda _p: folder))
    monkeypatch.setattr(package.PhotoManager, "local_photo_paths", lambda _self, _pid: [exterior, floorplan])
    monkeypatch.setattr(package, "write_text", lambda _p, _f: (folder / "property.txt"))
    monkeypatch.setattr(package, "write_collected_files", lambda _p, _f: (folder / "collected.txt", folder / "collected.json"))
    monkeypatch.setattr(package, "export_root", lambda: tmp_path)
    for name in ("property.txt", "collected.txt", "collected.json"):
        (folder / name).write_text("x", encoding="utf-8")
    out = package.create_zip(FakeDB(), 1, require_pdf=False)
    with ZipFile(out) as zf:
        photos = [n for n in zf.namelist() if n.endswith(".jpg")]
    assert photos == ["photo-001.jpg", "photo-002.jpg"]
