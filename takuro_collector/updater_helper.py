from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .update_diagnostics import log_phase, use_log, write_state


def _wait_exit_windows(pid: int, timeout: float) -> bool:
    """Wait for a Windows process handle; PID 87 means it is already gone."""
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0, wait_timeout, wait_failed = 0, 258, 0xFFFFFFFF
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return True
        raise ctypes.WinError(error)
    try:
        milliseconds = max(0, min(int(max(timeout, 0) * 1000), 0xFFFFFFFE))
        result = kernel32.WaitForSingleObject(handle, milliseconds)
        if result == wait_object_0:
            return True
        if result == wait_timeout:
            return False
        if result == wait_failed:
            raise ctypes.WinError(ctypes.get_last_error())
        raise OSError(f"예상하지 못한 Windows process wait 결과: {result}")
    finally:
        kernel32.CloseHandle(handle)


def _wait_exit(pid: int, timeout: float = 30) -> bool:
    if os.name == "nt":
        return _wait_exit_windows(pid, timeout)
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
    update_root = user_root / "updates"
    backup = install_dir.parent / f".{install_dir.name}.known-good"
    prepared = install_dir.parent / f".{install_dir.name}.update-new"
    from_version, to_version = str(command.get("from_version") or ""), str(command.get("version") or "")
    fallback_log = user_root / "updates" / "logs" / f"update-helper-{time.time_ns()}.log"
    use_log(command.get("log_path") or fallback_log)
    context = {
        "from_version": from_version, "to_version": to_version, "install_dir": install_dir,
        "prepared_dir": prepared, "backup_dir": backup, "helper_cwd": Path.cwd(),
        "parent_pid": int(command["pid"]),
    }
    phase = "validation"
    lock = update_root / "install.lock"
    lock_created = False
    try:
        if install_dir == user_root or user_root in install_dir.parents or install_dir in user_root.parents:
            raise RuntimeError("프로그램 폴더와 사용자 데이터 폴더가 안전하게 분리되지 않았습니다.")
        if not (staged_dir / executable).is_file() or not (staged_dir / "_internal").is_dir():
            raise RuntimeError("staging 검증에 실패했습니다.")
        update_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        lock_created = True
        phase = "parent_wait"
        write_state("in_progress", "parent_wait", root=user_root, **context)
        log_phase("parent_wait", **context)
        if not wait_exit(int(command["pid"]), timeout):
            raise RuntimeError("기존 Collector가 종료되지 않았습니다.")
        log_phase("parent_exited", **context)
        phase = "prepared_copy"
        shutil.rmtree(prepared, ignore_errors=True)
        log_phase("prepared_copy", source_path=staged_dir, destination_path=prepared, **context)
        shutil.copytree(staged_dir, prepared)
        if backup.exists():
            log_phase("stale_backup_remove", destination_path=backup, **context)
            shutil.rmtree(backup)
        backup.parent.mkdir(parents=True, exist_ok=True)
        phase = "known_good_backup"
        log_phase("known_good_backup", source_path=install_dir, destination_path=backup, **context)
        install_dir.replace(backup)
        try:
            phase = "install_replace"
            log_phase("install_replace", source_path=prepared, destination_path=install_dir, **context)
            prepared.replace(install_dir)
            phase = "relaunch"
            write_state("in_progress", "relaunch", root=user_root, **context)
            env = os.environ.copy()
            env["TAKURO_UPDATE_HEALTH_MARKER"], env["TAKURO_UPDATE_HEALTH_TOKEN"] = str(marker), str(command["token"])
            log_phase("relaunch", destination_path=install_dir / executable, **context)
            process = launch([str(install_dir / executable)], env=env, close_fds=True)
            phase = "health_marker"
            deadline = time.time() + timeout
            while time.time() < deadline:
                if marker.is_file() and marker.read_text(encoding="utf-8") == command["token"]:
                    log_phase("health_marker", **context)
                    write_state("success", "complete", root=user_root, **context)
                    log_phase("complete", **context)
                    return "launch_verified"
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            try:
                process.terminate()
            except Exception:
                pass
            raise RuntimeError("새 버전 시작 확인에 실패했습니다.")
        except Exception as install_error:
            phase = "rollback"
            log_phase("rollback", str(install_error), error_type=type(install_error).__name__,
                      winerror=getattr(install_error, "winerror", None), **context)
            failed = install_dir.parent / f".{install_dir.name}.failed"
            try:
                shutil.rmtree(failed, ignore_errors=True)
                if install_dir.exists():
                    install_dir.replace(failed)
                backup.replace(install_dir)
                launch([str(install_dir / executable)], close_fds=True)
                write_state("rolled_back", "rollback", error=install_error, root=user_root, **context)
                log_phase("rollback_complete", **context)
                return "rolled_back"
            except Exception as rollback_error:
                log_phase("rollback_failed", str(rollback_error), error_type=type(rollback_error).__name__,
                          winerror=getattr(rollback_error, "winerror", None), **context)
                write_state("failed", "rollback", error=rollback_error, root=user_root, **context)
                raise
    except Exception as exc:
        log_phase("failed", str(exc), error_type=type(exc).__name__, winerror=getattr(exc, "winerror", None),
                  errno=getattr(exc, "errno", None), **context)
        write_state("failed", phase, error=exc, root=user_root, **context)
        raise
    finally:
        if lock_created:
            lock.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    command_path = Path(sys.argv[1]).resolve()
    install(json.loads(command_path.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
