from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from takuro_collector.updater import EXECUTABLE_NAME, SCHEMA_VERSION, UPDATE_HOST, _safe_member, _signed_payload, _version


def _outside_repository(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise SystemExit("private key는 repository 밖의 경로를 사용해야 합니다.")
    return resolved


def generate_key(private_key_path: Path, public_key_path: Path | None) -> None:
    private_key_path = _outside_repository(private_key_path)
    if private_key_path.exists():
        raise SystemExit(f"기존 private key를 덮어쓰지 않습니다: {private_key_path}")
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    private_key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    try:
        private_key_path.chmod(0o600)
    except OSError:
        pass
    public_b64 = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )).decode("ascii")
    if public_key_path:
        public_key_path.expanduser().resolve().write_text(public_b64 + "\n", encoding="ascii")
    print(f"Private key created outside repository: {private_key_path}")
    print(f"Public key (safe to embed): {public_b64}")


def prepare(args) -> tuple[Path, Path]:
    private_path = _outside_repository(args.private_key)
    archive = args.zip.resolve()
    if not archive.is_file():
        raise SystemExit(f"ZIP을 찾을 수 없습니다: {archive}")
    _version(args.version)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if not names or any(not _safe_member(name) for name in names):
            raise SystemExit("안전하지 않은 ZIP 경로가 있습니다.")
        required = {EXECUTABLE_NAME, "TAKURO Updater.exe", "update-package.json"}
        if not required.issubset(names) or not any(name.startswith("_internal/") for name in names):
            raise SystemExit("PyInstaller onedir update package 구조가 아닙니다.")
        metadata = json.loads(bundle.read("update-package.json").decode("utf-8"))
        if metadata != {"schema_version": SCHEMA_VERSION, "version": args.version}:
            raise SystemExit("ZIP package metadata와 release version이 일치하지 않습니다.")
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("Ed25519 private key가 아닙니다.")
    output = args.output.resolve()
    release_dir = output / "releases" / args.version
    release_dir.mkdir(parents=True, exist_ok=True)
    release_name = f"TAKURO-Collector-{args.version}.zip"
    release_zip = release_dir / release_name
    shutil.copy2(archive, release_zip)
    content = release_zip.read_bytes()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "version": args.version,
        "published_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "download_url": f"https://{UPDATE_HOST}/releases/{args.version}/{release_name}",
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "release_notes": list(args.note),
        "package": {"format": "pyinstaller-onedir-zip", "executable": EXECUTABLE_NAME,
                    "metadata": {"schema_version": SCHEMA_VERSION, "version": args.version}},
    }
    manifest["signature"] = base64.b64encode(key.sign(_signed_payload(manifest))).decode("ascii")
    latest = output / "latest.json"
    latest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Upload: {latest}")
    print(f"Upload: {release_zip}")
    return latest, release_zip


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Create signed static TAKURO update files")
    commands = root.add_subparsers(dest="command", required=True)
    key = commands.add_parser("generate-key")
    key.add_argument("--private-key", type=Path, required=True)
    key.add_argument("--public-key-output", type=Path)
    release = commands.add_parser("prepare")
    release.add_argument("--version", required=True)
    release.add_argument("--zip", type=Path, required=True)
    release.add_argument("--private-key", type=Path, required=True)
    release.add_argument("--output", type=Path, default=ROOT / "release-output")
    release.add_argument("--note", action="append", default=[])
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "generate-key":
        generate_key(args.private_key, args.public_key_output)
    else:
        prepare(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
