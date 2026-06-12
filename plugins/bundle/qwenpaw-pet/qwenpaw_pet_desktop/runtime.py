# -*- coding: utf-8 -*-
"""Runtime paths, process state, and small JSON helpers."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# On Windows, prevent console-subsystem child processes (tasklist,
# taskkill …) from flashing a black CMD window.
_WIN_CREATION_FLAGS: int = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if sys.platform == "win32"
    else 0
)


def home_dir() -> Path:
    return Path(
        os.environ.get(
            "QWENPAW_PET_HOME",
            str(Path.home() / ".qwenpaw-pet"),
        ),
    )


def qwenpaw_working_dir() -> Path:
    """Return QwenPaw's working directory using QwenPaw's precedence."""
    explicit = os.environ.get("QWENPAW_WORKING_DIR") or os.environ.get(
        "COPAW_WORKING_DIR",
    )
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        from qwenpaw.constant import WORKING_DIR  # type: ignore

        return Path(WORKING_DIR).expanduser().resolve()
    except Exception:
        legacy_copaw = Path("~/.copaw").expanduser()
        if legacy_copaw.exists():
            return legacy_copaw.resolve()
        return Path("~/.qwenpaw").expanduser().resolve()


def runtime_dir() -> Path:
    return home_dir() / "runtime"


def pets_dir() -> Path:
    return qwenpaw_working_dir() / "pets"


def state_path() -> Path:
    return runtime_dir() / "state.json"


def bubble_path() -> Path:
    return runtime_dir() / "bubble.json"


def token_path() -> Path:
    return runtime_dir() / "update-token"


def pid_path() -> Path:
    return runtime_dir() / "pet-desktop.pid"


def log_path() -> Path:
    return runtime_dir() / "pet-desktop.log"


def bridge_url_path() -> Path:
    """Path to JSON holding the bridge ``url`` for plugin HTTP clients."""
    return runtime_dir() / "desktop-bridge.json"


def spawn_claim_path() -> Path:
    """Short-lived marker written by the plugin before spawning desktop."""
    return runtime_dir() / "desktop-spawn-claim.json"


def instance_lock_path() -> Path:
    """Exclusive lock file held for the lifetime of a desktop process."""
    return runtime_dir() / "pet-desktop.instance.lock"


_SPAWN_CLAIM_TTL_MS = 180_000


def read_bridge_url() -> str | None:
    data = read_json(bridge_url_path(), {})
    u = data.get("url")
    if not isinstance(u, str):
        return None
    u = u.strip().rstrip("/")
    return u or None


def write_bridge_url(base_url: str) -> None:
    """Persist the HTTP bridge base URL (may include a non-default port)."""
    ensure_runtime()
    write_json(
        bridge_url_path(),
        {
            "url": base_url.rstrip("/"),
            "updatedAt": int(time.time() * 1000),
        },
    )


def write_spawn_claim(host: str, port: int) -> None:
    """Record that the plugin is launching (or has launched) the desktop."""
    ensure_runtime()
    write_json(
        spawn_claim_path(),
        {
            "host": host,
            "port": port,
            "sinceMs": int(time.time() * 1000),
        },
    )


def clear_spawn_claim() -> None:
    try:
        spawn_claim_path().unlink(missing_ok=True)
    except OSError:
        pass


def spawn_claim_active() -> bool:
    """True while a recent spawn claim exists and no stale TTL has elapsed."""
    data = read_json(spawn_claim_path(), {})
    since = data.get("sinceMs")
    if not isinstance(since, int):
        return False
    if int(time.time() * 1000) - since > _SPAWN_CLAIM_TTL_MS:
        clear_spawn_claim()
        return False
    pid = read_pid()
    if pid and is_pet_desktop_pid(pid):
        return True
    # Child may not have written pid / bound the port yet (Windows cold start).
    return True


def try_acquire_instance_lock() -> bool:
    """Claim a single running desktop instance (cross-platform file lock)."""
    ensure_runtime()
    path = instance_lock_path()
    for _attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                old_pid = int(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                old_pid = 0
            if old_pid and old_pid != os.getpid() and is_pid_running(old_pid):
                return False
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return False
    return False


def release_instance_lock() -> None:
    try:
        path = instance_lock_path()
        if not path.exists():
            return
        if path.read_text(encoding="ascii").strip() == str(os.getpid()):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_runtime() -> None:
    runtime_dir().mkdir(parents=True, exist_ok=True)
    pets_dir().mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)


def ensure_token() -> str:
    ensure_runtime()
    existing = read_token()
    if existing:
        return existing
    token = secrets.token_hex(32)
    token_path().write_text(token, encoding="utf-8")
    try:
        token_path().chmod(0o600)
    except OSError:
        pass
    return token


def read_token() -> str | None:
    try:
        token = token_path().read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def write_pid(pid: int) -> None:
    ensure_runtime()
    write_json(
        pid_path(),
        {
            "pid": pid,
            "updatedAt": int(time.time() * 1000),
        },
    )


def read_pid() -> int | None:
    data = read_json(pid_path(), {})
    pid = data.get("pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def _tasklist_has_no_matching_pid(stdout: str) -> bool:
    text = stdout.strip()
    if not text:
        return True
    lower = text.lower()
    if lower.startswith("info:"):
        return True
    if "no tasks are running" in lower:
        return True
    # Localized Windows (e.g. zh-CN) "no matching tasks" tasklist message.
    if "没有" in text and "任务" in text:
        return True
    return False


def _pid_exists_win32(pid: int) -> bool:
    """Return whether *pid* is still running on Windows.

    Uses ``tasklist /fi`` to query the process table without requiring
    elevated privileges.  On any error (tasklist not found, timeout,
    permission denied) returns ``True`` so that downstream callers such
    as ``terminate_process_tree`` still attempt a ``taskkill`` rather
    than silently skipping a live process.
    """
    try:
        result = subprocess.run(
            ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=_WIN_CREATION_FLAGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # If we cannot probe, assume it exists so shutdown still tries
        # ``taskkill`` rather than skipping a live pet window.
        return True
    stdout = result.stdout or ""
    if _tasklist_has_no_matching_pid(stdout):
        return False
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("info:"):
            continue
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                if int(parts[1].strip().strip('"')) == pid:
                    return True
            except (IndexError, ValueError):
                pass
        if str(pid) in line:
            return True
    return False


def _pid_exists_posix(pid: int) -> bool:
    """Return whether *pid* is still running on POSIX systems.

    Sends signal 0 to *pid* via ``os.kill``, which performs the kernel
    permission check without delivering an actual signal:
    - ``ProcessLookupError`` (ESRCH) → process does not exist → False
    - ``PermissionError`` (EPERM)  → process exists, we lack permission → True
    - Any other ``OSError``        → treat as not running → False
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_exists(pid: int) -> bool:
    """Return whether ``pid`` is still running (cross-platform)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        return _pid_exists_win32(pid)
    return _pid_exists_posix(pid)


def is_pid_running(pid: int) -> bool:
    return pid_exists(pid)


# Process names that the pet desktop may appear as.  The desktop is
# spawned via ``python.exe -m qwenpaw_pet_desktop.app`` or via
# ``python.exe -c "…"``, so on Windows the image name is always some
# variant of ``python`` (possibly versioned, e.g. ``python3.13.exe``).
_PET_PROCESS_NAMES = frozenset({"python.exe", "python3.exe", "py.exe"})


def _get_pid_process_name_win32(pid: int) -> str | None:
    """Return the image name of *pid* on Windows, or ``None``."""
    try:
        result = subprocess.run(
            ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=_WIN_CREATION_FLAGS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    stdout = result.stdout or ""
    if _tasklist_has_no_matching_pid(stdout):
        return None
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("info:"):
            continue
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                found_pid = int(parts[1].strip().strip('"'))
            except (IndexError, ValueError):
                continue
            if found_pid == pid:
                return parts[0].strip().strip('"')
    return None


def _is_python_process_name(name: str) -> bool:
    """Return True if *name* looks like a Python interpreter executable."""
    lower = name.lower()
    if lower in _PET_PROCESS_NAMES:
        return True
    # Accept versioned variants like python3.13.exe
    return lower.startswith("python") and lower.endswith(".exe")


def is_pet_desktop_pid(pid: int) -> bool:
    """Return True only if *pid* is alive **and** looks like a Python process.

    Plain ``is_pid_running`` cannot distinguish the pet desktop from an
    unrelated process that inherited the same PID after the pet exited.
    On Windows, PID reuse is common and fast — a stale PID file can
    easily point to a completely different application (e.g. a system
    service), causing the status page to permanently show
    ``starting=True`` and disable the launch button.

    On POSIX, ``/proc/<pid>/comm`` is checked; on Windows, ``tasklist``
    output is inspected for the image name.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False

    if sys.platform == "win32":
        name = _get_pid_process_name_win32(pid)
        return _is_python_process_name(name) if name else False

    # POSIX: check /proc/<pid>/comm
    # Note: /proc/<pid>/comm may not reflect "python" if the process
    # called prctl(PR_SET_NAME, ...) to rename itself, or if the
    # interpreter was invoked via a symlink with a non-standard name
    # (e.g. "qwenpaw-pet").  In such edge cases we fall back to
    # _pid_exists_posix(), which only confirms the PID is alive but
    # cannot verify it belongs to the pet desktop.  This is acceptable
    # because PID reuse is far less aggressive on Linux than on Windows.
    try:
        comm = (
            Path(f"/proc/{pid}/comm")
            .read_text(encoding="utf-8")
            .strip()
            .lower()
        )
        return "python" in comm
    except OSError:
        # /proc not available (e.g. macOS, FreeBSD) — fall back to
        # kill-based existence check.  False positives are possible
        # but unlikely on POSIX systems with large PID spaces.
        return _pid_exists_posix(pid)


def _wait_for_pid_exit(pid: int, grace: float) -> bool:
    deadline = time.time() + grace
    while time.time() < deadline:
        if not pid_exists(pid):
            return True
        time.sleep(0.1)
    return not pid_exists(pid)


def _terminate_process_tree_win32(
    pid: int,
    *,
    grace: float,
    aggressive: bool = False,
) -> bool:
    if aggressive:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=_WIN_CREATION_FLAGS,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return not pid_exists(pid)

    try:
        subprocess.run(
            ["taskkill", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=_WIN_CREATION_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    if _wait_for_pid_exit(pid, grace):
        return True
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            creationflags=_WIN_CREATION_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return not pid_exists(pid)


def _terminate_process_tree_unix(  # pylint: disable=too-many-return-statements
    pid: int,
    *,
    grace: float,
) -> bool:
    sent_term = False
    pgid: int | None = None
    if callable(getattr(os, "getpgid", None)) and callable(
        getattr(os, "killpg", None),
    ):
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None

    try:
        if pgid is not None and pgid != os.getpgid(os.getpid()):
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        sent_term = True
    except ProcessLookupError:
        return True
    except OSError:
        pass

    if sent_term and _wait_for_pid_exit(pid, grace):
        return True

    try:
        if pgid is not None and pgid != os.getpgid(os.getpid()):
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def terminate_process_tree(
    pid: int,
    *,
    grace: float = 2.0,
    aggressive: bool = False,
) -> bool:
    """Best-effort terminate ``pid`` (and children on Windows).

    Returns ``True`` when the process is no longer running by the end.
    """
    if not pid_exists(pid):
        return True
    if sys.platform == "win32":
        return _terminate_process_tree_win32(
            pid,
            grace=grace,
            aggressive=aggressive,
        )
    return _terminate_process_tree_unix(pid, grace=grace)


def detached_popen(
    cmd: list[str],
    *,
    stdout: Any,
    stderr: Any,
    stdin: Any,
    env: dict[str, str],
) -> subprocess.Popen[Any]:
    """Spawn a detached child process (no console on Windows)."""
    kwargs: dict[str, Any] = {
        "stdout": stdout,
        "stderr": stderr,
        "stdin": stdin,
        "env": env,
    }
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        if creationflags:
            kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        cmd,
        **kwargs,
    )  # pylint: disable=consider-using-with


def current_process_status() -> dict[str, Any]:
    pid = read_pid()
    running = bool(pid and is_pid_running(pid))
    return {
        "running": running,
        "pid": pid if running else None,
        "home": str(home_dir()),
        "qwenpawWorkingDir": str(qwenpaw_working_dir()),
        "petsDir": str(pets_dir()),
        "statePath": str(state_path()),
        "bubblePath": str(bubble_path()),
    }


def stop_process() -> dict[str, Any]:
    pid = read_pid()
    if not pid:
        return {"ok": True, "stopped": False, "reason": "no pid file"}
    if not is_pid_running(pid):
        return {"ok": True, "stopped": False, "reason": "not running"}
    stopped = terminate_process_tree(pid)
    return {"ok": True, "stopped": stopped, "pid": pid}
