from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _state(root: Path, value: str, error: str = "") -> None:
    (root / "state.json").write_text(json.dumps({"state": value, "error": error}, ensure_ascii=False), encoding="utf-8")


def _wait_exit(pid: int, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def install(command: dict, *, launch=subprocess.Popen, timeout: float = 30, wait_exit=_wait_exit) -> str:
    install_dir, staged_dir = Path(command["install_dir"]).resolve(), Path(command["staged_dir"]).resolve()
    user_root, marker = Path(command["data_root"]).resolve(), Path(command["marker"]).resolve()
    executable = str(command["executable"])
    update_root, backup = user_root / "updates", user_root / "updates" / "backup" / "known-good"
    prepared = install_dir.parent / f".{install_dir.name}.update-new"
    if install_dir == user_root or user_root in install_dir.parents or install_dir in user_root.parents:
        raise RuntimeError("프로그램 폴더와 사용자 데이터 폴더가 안전하게 분리되지 않았습니다.")
    if not (staged_dir / executable).is_file() or not (staged_dir / "_internal").is_dir():
        raise RuntimeError("staging 검증에 실패했습니다.")
    update_root.mkdir(parents=True, exist_ok=True)
    lock = update_root / "install.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.close(fd)
    try:
        _state(update_root, "installing")
        if not wait_exit(int(command["pid"]), timeout):
            raise RuntimeError("기존 Collector가 종료되지 않았습니다.")
        shutil.rmtree(prepared, ignore_errors=True)
        shutil.copytree(staged_dir, prepared)
        if backup.exists():
            shutil.rmtree(backup)
        backup.parent.mkdir(parents=True, exist_ok=True)
        install_dir.replace(backup)
        try:
            prepared.replace(install_dir)
            _state(update_root, "installed")
            env = os.environ.copy()
            env["TAKURO_UPDATE_HEALTH_MARKER"], env["TAKURO_UPDATE_HEALTH_TOKEN"] = str(marker), str(command["token"])
            process = launch([str(install_dir / executable)], env=env, close_fds=True)
            deadline = time.time() + timeout
            while time.time() < deadline:
                if marker.is_file() and marker.read_text(encoding="utf-8") == command["token"]:
                    _state(update_root, "launch_verified")
                    return "launch_verified"
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            try:
                process.terminate()
            except Exception:
                pass
            raise RuntimeError("새 버전 시작 확인에 실패했습니다.")
        except Exception:
            failed = install_dir.parent / f".{install_dir.name}.failed"
            shutil.rmtree(failed, ignore_errors=True)
            if install_dir.exists():
                install_dir.replace(failed)
            backup.replace(install_dir)
            launch([str(install_dir / executable)], close_fds=True)
            _state(update_root, "rolled_back")
            return "rolled_back"
    except Exception as exc:
        _state(update_root, "failed", str(exc))
        raise
    finally:
        lock.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    command_path = Path(sys.argv[1]).resolve()
    install(json.loads(command_path.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
