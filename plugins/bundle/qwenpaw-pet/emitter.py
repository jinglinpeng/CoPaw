# -*- coding: utf-8 -*-
"""Fire-and-forget desktop event emitter."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("qwenpaw.pet_desktop")

# ``True`` once the plugin has either spawned the pet desktop *or*
# observed a healthy one during startup (see ``ensure_desktop_available``
# below). The shutdown hook only kills processes that the plugin has
# "adopted" this way — that covers both the autostart case and the case
# where a previous QwenPaw run left the pet behind. A user who never
# lets QwenPaw see the pet (e.g. ``QWENPAW_PET_AUTOSTART=0`` and the pet
# is not running at startup) will not have their standalone desktop
# killed on QwenPaw exit. Hard opt-out: ``QWENPAW_PET_STOP_ON_SHUTDOWN=0``.
_DESKTOP_OWNED = False

# Last URL that successfully answered ``GET /health`` (or the URL we
# chose when spawning after a port collision).
_active_desktop_base: str | None = None

# After a failed health probe or POST, skip further probes until this
# monotonic deadline to avoid hammering localhost on every chat token.
_DESKTOP_UNREACHABLE_UNTIL = 0.0
_HEALTH_RETRY_SEC = 3.0
_EVENT_SERIAL = 0

# Serialize manual/autostart spawns so rapid UI clicks cannot launch copies.
_SPAWN_LOCK = threading.RLock()


def _is_frozen_environment() -> bool:
    """Return True when running inside a PyInstaller / frozen bundle."""
    try:
        from qwenpaw.utils.frozen_env import is_frozen_environment

        return is_frozen_environment()
    except ImportError:
        # Standalone execution outside the host package.
        return getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")


# Cache: ``None`` = not searched yet, ``""`` = searched but not found.
_cached_system_python: str | None = None


def _find_bundled_python() -> str | None:
    """Locate the bundled Python shipped with the Tauri package.

    In a frozen (PyInstaller / Tauri) build the standalone Python
    embedded distribution lives at ``python-embed/`` next to the
    executable.  This is the preferred interpreter for spawning the
    desktop pet because it has PySide6 and other dependencies
    pre-installed and does not pollute the user's system Python.

    Returns the path to ``python.exe`` or ``None``.
    """
    try:
        from qwenpaw.plugins.bundled_python import (
            get_bundled_python_executable,
        )

        return get_bundled_python_executable()
    except ImportError:
        pass

    # Fallback: manual probe next to the frozen exe
    if not _is_frozen_environment():
        return None
    meipass = Path(getattr(sys, "_MEIPASS", ""))
    exe_dir = Path(sys.executable).parent
    exe_name = "python.exe" if sys.platform == "win32" else "python3"
    for candidate in [
        exe_dir / "python-embed" / exe_name,
        meipass.parent / "python-embed" / exe_name,
    ]:
        if candidate.is_file():
            logger.info("Found bundled Python at %s", candidate)
            return str(candidate)
    return None


def _scan_path_for_python() -> str | None:
    """Scan PATH for a Python (≥3.10) with PySide6 installed.

    Returns the first qualifying interpreter path, or ``None``.
    """
    import shutil

    creation_flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if sys.platform == "win32"
        else 0
    )

    candidates: list[str] = []
    if sys.platform == "win32":
        candidates.extend(["python", "python3"])
        candidates.extend(f"python3.{m}" for m in range(13, 9, -1))
        candidates.append("py")
    else:
        candidates.extend(f"python3.{m}" for m in range(13, 9, -1))
        candidates.extend(["python3", "python"])

    check_script = (
        "import sys; "
        "v=sys.version_info[:2]; "
        "ps6=False; "
        "exec('try:\\n import PySide6; ps6=True\\nexcept ImportError: pass'); "
        "print(v, ps6)"
    )

    for name in candidates:
        exe = shutil.which(name)
        if not exe:
            continue
        found = _probe_python_candidate(
            name,
            exe,
            check_script,
            creation_flags,
        )
        if found:
            return found
    return None


def _probe_python_candidate(
    name: str,
    exe: str,
    check_script: str,
    creation_flags: int,
) -> str | None:
    """Test whether *exe* is Python ≥3.10 with PySide6. Return path or None."""
    try:
        result = subprocess.run(
            [exe, "-c", check_script],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creation_flags,
            check=False,
        )
        if result.returncode != 0:
            logger.debug(
                "Python candidate '%s' (%s) failed check (rc=%d)",
                name,
                exe,
                result.returncode,
            )
            return None
        parts = result.stdout.strip().rsplit(maxsplit=1)
        if len(parts) != 2:
            return None
        version_tuple = eval(parts[0])  # noqa: S307
        has_pyside6 = parts[1] == "True"
        if version_tuple < (3, 10):
            logger.debug(
                "Python candidate '%s' (%s) too old: %s",
                name,
                exe,
                version_tuple,
            )
            return None
        if not has_pyside6:
            logger.info(
                "Python candidate '%s' (%s, v%s.%s) lacks PySide6 — "
                "skipping. Install with: %s -m pip install "
                "pyside6-essentials",
                name,
                exe,
                *version_tuple,
                exe,
            )
            return None
        logger.info(
            "Found system Python for pet desktop: %s (v%s.%s, PySide6 OK)",
            exe,
            *version_tuple,
        )
        return exe
    except Exception:
        return None


def _find_system_python() -> str | None:
    """Locate a Python interpreter for spawning the desktop pet.

    **Priority**: bundled Python (shipped with Tauri) is tried first.
    Only falls back to scanning PATH when the bundled Python is absent.

    The result is cached so repeated calls do not re-scan every time.
    """
    global _cached_system_python
    if _cached_system_python is not None:
        return _cached_system_python or None

    # ── Try bundled Python first ──────────────────────────────────
    bundled = _find_bundled_python()
    if bundled:
        _cached_system_python = bundled
        return bundled

    # ── Fall back to scanning PATH ────────────────────────────────
    found = _scan_path_for_python()
    if found:
        _cached_system_python = found
        return found

    logger.warning(
        "No suitable Python (≥3.10 with PySide6) found. "
        "Desktop pet cannot start. The bundled Python was not found "
        "and no system Python with PySide6 is on PATH.",
    )
    _cached_system_python = ""
    return None


_FROZEN_ENV_HINT = (
    "QwenPaw is running in a packaged (Tauri/PyInstaller) environment "
    "and no suitable Python interpreter was found. The bundled Python "
    "embed directory is missing and no system Python (≥3.10) with "
    "PySide6 is available on PATH."
)


def _mark_desktop_owned() -> None:
    """Mark the desktop as managed by this QwenPaw process."""
    global _DESKTOP_OWNED
    _DESKTOP_OWNED = True


def _clear_desktop_base_url_cache() -> None:
    global _active_desktop_base
    _active_desktop_base = None


def _reset_desktop_reachability_probe() -> None:
    global _DESKTOP_UNREACHABLE_UNTIL
    _DESKTOP_UNREACHABLE_UNTIL = 0.0


def _mark_desktop_unreachable() -> None:
    global _DESKTOP_UNREACHABLE_UNTIL
    _clear_desktop_base_url_cache()
    _DESKTOP_UNREACHABLE_UNTIL = time.monotonic() + _HEALTH_RETRY_SEC


def _desktop_is_reachable() -> bool:
    """Return whether the pet desktop HTTP bridge is currently up."""
    if _active_desktop_base:
        return True
    if time.monotonic() < _DESKTOP_UNREACHABLE_UNTIL:
        return False
    if desktop_health() is not None:
        _reset_desktop_reachability_probe()
        return True
    _mark_desktop_unreachable()
    return False


TOKEN_PATH = Path(
    os.environ.get(
        "QWENPAW_PET_TOKEN_PATH",
        str(Path.home() / ".qwenpaw-pet/runtime/update-token"),
    ),
)


def _read_token() -> str | None:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def _headers() -> dict[str, str]:
    token = _read_token()
    if not token:
        return {}
    return {"X-QwenPaw-Pet-Token": token}


def _httpx_client_kwargs() -> dict[str, Any]:
    """Options for calls to the local pet desktop.

    ``trust_env=False`` avoids routing ``127.0.0.1`` through HTTP(S)_PROXY
    (e.g. Clash on 7890), which would time out and break all pet events.
    """
    return {"trust_env": False, "timeout": 0.35}


def _spawn_host_port_from_env() -> tuple[str, int]:
    """Host + preferred TCP port for ``qwenpaw_pet_desktop.app``."""
    url = (os.environ.get("QWENPAW_PET_DESKTOP_URL") or "").strip()
    if url:
        u = urlparse(url)
        host = (u.hostname or "127.0.0.1").strip() or "127.0.0.1"
        if u.port is not None:
            return host, int(u.port)
        if (u.scheme or "http").lower() == "https":
            return host, 443
        return host, 8765
    host = (os.environ.get("QWENPAW_PET_DESKTOP_HOST") or "127.0.0.1").strip()
    host = host or "127.0.0.1"
    port = int(os.environ.get("QWENPAW_PET_DESKTOP_PORT", "8765"))
    return host, port


def _tcp_bind_test(host: str, port: int) -> bool:
    """Return whether ``(host, port)`` is free for a new listener."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _pick_listen_port(host: str, preferred: int) -> int:
    """Use ``preferred`` if free; otherwise scan upward, then ask the OS.

    Set ``QWENPAW_PET_DESKTOP_STRICT_PORT=1`` to disable fallback (spawn
    may still fail with EADDRINUSE if the port is taken between the probe
    and the child bind — rare on localhost).

    If ``QWENPAW_PET_DESKTOP_URL`` pins an explicit ``host:port``, we never
    pick another port — otherwise the running bridge would not match the
    URL the user configured.
    """
    if os.environ.get("QWENPAW_PET_DESKTOP_STRICT_PORT", "0") == "1":
        return preferred
    if (os.environ.get("QWENPAW_PET_DESKTOP_URL") or "").strip():
        return preferred
    if _tcp_bind_test(host, preferred):
        return preferred
    for p in range(preferred + 1, preferred + 128):
        if _tcp_bind_test(host, p):
            logger.info(
                "QwenPaw Pet: preferred desktop port %s busy on %s; using %s",
                preferred,
                host,
                p,
            )
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        ephem = int(s.getsockname()[1])
        logger.warning(
            "QwenPaw Pet: desktop using ephemeral port %s on %s",
            ephem,
            host,
        )
        return ephem


def _desktop_url_candidates() -> list[str]:
    """Ordered URLs to try for ``GET /health`` (deduped)."""
    explicit = (os.environ.get("QWENPAW_PET_DESKTOP_URL") or "").strip()
    if explicit:
        return [explicit.rstrip("/")]

    out: list[str] = []
    try:
        from qwenpaw_pet_desktop import runtime as pet_rt

        bu = pet_rt.read_bridge_url()
        if bu:
            out.append(bu.rstrip("/"))
    except Exception:
        pass

    host = (os.environ.get("QWENPAW_PET_DESKTOP_HOST") or "127.0.0.1").strip()
    host = host or "127.0.0.1"
    port = int(os.environ.get("QWENPAW_PET_DESKTOP_PORT", "8765"))
    default_u = f"http://{host}:{port}"
    out.append(default_u.rstrip("/"))

    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _resolved_desktop_base_url() -> str:
    """Base URL for mutating HTTP calls (``/event``, ``/pet``).

    Prefer the last healthy endpoint; otherwise probe once via
    ``desktop_health``; fall back to the first candidate.
    """
    global _active_desktop_base
    if _active_desktop_base:
        return _active_desktop_base.rstrip("/")
    desktop_health()
    if _active_desktop_base:
        return _active_desktop_base.rstrip("/")
    cands = _desktop_url_candidates()
    return cands[0].rstrip("/") if cands else "http://127.0.0.1:8765"


def desktop_health(*, timeout: float | None = None) -> dict[str, Any] | None:
    global _active_desktop_base
    kwargs = _httpx_client_kwargs()
    if timeout is not None:
        kwargs["timeout"] = timeout
    for base in _desktop_url_candidates():
        try:
            response = httpx.get(
                f"{base.rstrip('/')}/health",
                **kwargs,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                _active_desktop_base = base.rstrip("/")
                _clear_desktop_spawn_markers()
                return data
        except Exception:
            continue
    _active_desktop_base = None
    return None


def _living_desktop_present(host: str, port: int) -> bool:
    """True when a pet desktop is up or still starting on the bridge port."""
    health = desktop_health()
    if health and health.get("ok"):
        return True
    try:
        from qwenpaw_pet_desktop import runtime as pet_rt

        if pet_rt.spawn_claim_active():
            return True
        pid = pet_rt.read_pid()
        if pid and pet_rt.is_pet_desktop_pid(pid):
            return True
    except ImportError:
        pass
    # Uvicorn may have bound the port before /health responds.
    if not _tcp_bind_test(host, port):
        return True
    return False


def _clear_desktop_spawn_markers() -> None:
    try:
        from qwenpaw_pet_desktop import runtime as pet_rt

        pet_rt.clear_spawn_claim()
    except ImportError:
        pass


def desktop_status_summary() -> dict[str, Any]:
    """Plugin-side desktop status (works before /health is ready)."""
    # Use a longer timeout for the status page — the default 0.35s is
    # optimized for fire-and-forget event pushes but too aggressive for
    # interactive status queries, especially on Windows where the first
    # TCP connection in a frozen (PyInstaller) process can be slow due
    # to Windows Defender / firewall checks.
    health = desktop_health(timeout=2.0)
    if health and health.get("ok"):
        return {**health, "ready": True, "starting": False}
    try:
        from qwenpaw_pet_desktop import runtime as pet_rt

        pid = pet_rt.read_pid()
        running = bool(pid and pet_rt.is_pet_desktop_pid(pid))
        starting = pet_rt.spawn_claim_active() or running
        return {
            "ok": False,
            "ready": False,
            "starting": starting,
            "running": running,
            "pid": pid if running else None,
        }
    except ImportError:
        return {
            "ok": False,
            "ready": False,
            "starting": False,
            "running": False,
        }


def _desktop_start_response(
    *,
    already_running: bool,
    launch_attempted: bool,
    message: str,
    hint: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "alreadyRunning": already_running,
        "launchAttempted": launch_attempted,
        "desktop": desktop_health(),
        "message": message,
        **({"hint": hint} if hint else {}),
    }


_MISSING_DEPS_HINT = (
    "Desktop runtime import failed (likely a missing dependency in "
    "QwenPaw's interpreter). Install into the same environment: "
    'pip install "fastapi>=0.110" "uvicorn>=0.27" "pillow>=10.0" '
    '"pyside6-essentials>=6.6" (PySide6 wheels exist for Python 3.10-3.13).'
)


def _spawn_desktop_background() -> tuple[bool, str | None]:
    """Start the pet desktop in a detached process.

    Runs ``sys.executable -m qwenpaw_pet_desktop.app``. The package lives
    next to this plugin and is on the *parent's* ``sys.path`` because
    ``plugin.py`` injects the plugin directory; the child process gets
    that location via ``PYTHONPATH`` so ``python -m qwenpaw_pet_desktop.app``
    resolves without any ``pip install``. Third-party deps (fastapi,
    uvicorn, pillow, PySide6) still need to be available to ``sys.executable``.

    Returns:
        ``(True, None)`` if a process was spawned, else
        ``(False, user-facing reason)``.
    """
    with _SPAWN_LOCK:
        return _spawn_desktop_background_impl()


def _build_spawn_command(
    python_exe: str,
    host: str,
    port: int,
    env: dict[str, str],
) -> list[str]:
    """Build the command line for spawning the pet desktop subprocess.

    Python embedded distributions ship a ``._pth`` file that **disables**
    ``PYTHONPATH`` processing.  For bundled Python we use ``-c`` to inject
    ``sys.path`` explicitly; for system Python we use ``-m`` with
    ``PYTHONPATH`` as a safety net.
    """
    plugin_dir = str(Path(__file__).resolve().parent)
    is_bundled_python = _find_bundled_python() == python_exe

    extra_args: list[str] = ["--host", host, "--port", str(port)]
    scale = os.environ.get("QWENPAW_PET_DESKTOP_SCALE")
    if scale:
        extra_args.extend(["--scale", str(scale)])
    pet_dir = os.environ.get("QWENPAW_PET_DESKTOP_PET_DIR")
    if pet_dir:
        extra_args.extend(["--pet-dir", pet_dir])

    if is_bundled_python:
        bootstrap_code = (
            f"import sys; sys.path.insert(0, {plugin_dir!r}); "
            f"from qwenpaw_pet_desktop.app import main; main()"
        )
        return [python_exe, "-c", bootstrap_code, *extra_args]

    # Standard interpreter: use -m with PYTHONPATH fallback.
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        plugin_dir + os.pathsep + existing_pp if existing_pp else plugin_dir
    )
    if sys.platform == "win32" and Path(python_exe).stem.lower() == "py":
        return [python_exe, "-3", "-m", "qwenpaw_pet_desktop.app", *extra_args]
    return [python_exe, "-m", "qwenpaw_pet_desktop.app", *extra_args]


def _spawn_desktop_background_impl() -> tuple[bool, str | None]:
    # Determine the Python interpreter to use for spawning.
    frozen = _is_frozen_environment()
    if frozen:
        python_exe = _find_system_python()
        if not python_exe:
            return False, _FROZEN_ENV_HINT
        is_bundled = _find_bundled_python() == python_exe
        logger.info(
            "Frozen environment: using %s Python '%s' to launch "
            "pet desktop.",
            "bundled" if is_bundled else "system",
            python_exe,
        )
    else:
        python_exe = sys.executable

    try:
        from qwenpaw_pet_desktop import runtime as pet_rt
    except ImportError as exc:
        return False, f"{_MISSING_DEPS_HINT} ({exc})"

    host, preferred_port = _spawn_host_port_from_env()
    if _living_desktop_present(host, preferred_port):
        return False, "Desktop pet is already running or starting."

    try:
        pet_rt.ensure_runtime()
        try:
            pet_rt.ensure_token()
        except Exception:
            logger.warning(
                "Could not pre-create pet bridge token",
                exc_info=True,
            )
        if not _tcp_bind_test(host, preferred_port):
            return False, "Desktop pet is already running or starting."
        port = preferred_port
        pet_rt.write_spawn_claim(host, port)
        display_host = (
            "127.0.0.1" if host in ("0.0.0.0", "::", "[::]") else host
        )
        listen_url = f"http://{display_host}:{port}"
        pet_rt.write_bridge_url(listen_url)

        env = os.environ.copy()
        cmd = _build_spawn_command(python_exe, host, port, env)
        # ``Popen`` duplicates the log FD into the child's stdout/stderr,
        # so the parent's handle is safe to close as soon as the spawn
        # returns. Using ``with`` here both fixes the FD leak and lets
        # the detached child keep writing to the same file.
        with pet_rt.log_path().open("ab") as log_file:
            proc = pet_rt.detached_popen(
                cmd,
                stdout=log_file,
                stderr=log_file,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        pet_rt.write_pid(proc.pid)
        global _active_desktop_base
        _active_desktop_base = listen_url
        _mark_desktop_owned()
        _reset_desktop_reachability_probe()
        return True, None
    except OSError as exc:
        return False, f"failed to start desktop: {exc}"


def _stop_pid(
    pid: int,
    *,
    grace: float = 2.0,
    aggressive: bool = False,
) -> bool:
    """Best-effort terminate ``pid`` (delegates to runtime helpers)."""
    try:
        from qwenpaw_pet_desktop import runtime as pet_rt
    except ImportError:
        return False
    return pet_rt.terminate_process_tree(
        pid,
        grace=grace,
        aggressive=aggressive,
    )


def _stop_desktop_skip_reason(*, force: bool) -> str | None:
    if os.environ.get("QWENPAW_PET_STOP_ON_SHUTDOWN", "1") == "0":
        return "opted out"
    if not _DESKTOP_OWNED and not force:
        return "not autostarted"
    return None


def stop_desktop(
    *,
    force: bool = False,
    aggressive: bool = False,
    grace: float = 2.0,
) -> dict[str, Any]:
    """Stop the pet desktop process that this QwenPaw process manages.

    Called from ``_shutdown`` so the floating pet exits together with
    QwenPaw — the default mental model is that the desktop is a child
    of QwenPaw, not a long-running independent service. Users who want
    to keep the pet alive across QwenPaw restarts can opt out with
    ``QWENPAW_PET_STOP_ON_SHUTDOWN=0``.

    By default this only acts on a desktop that QwenPaw has *adopted*
    (``_DESKTOP_OWNED``): either we spawned it, or ``/health`` was
    responding at startup / explicit ``desktop/start`` time. Pass
    ``force=True`` to stop a desktop that QwenPaw never observed (e.g.
    started just now by another tool while QwenPaw was already up).

    ``aggressive=True`` skips gentle shutdown (important on Windows
    where Qt may ignore ``taskkill`` without ``/F``).
    """
    skip = _stop_desktop_skip_reason(force=force)
    if skip is not None:
        return {"ok": True, "stopped": False, "reason": skip}

    try:
        from qwenpaw_pet_desktop import runtime as pet_rt
    except ImportError as exc:
        return {
            "ok": True,
            "stopped": False,
            "reason": f"runtime not importable: {exc}",
        }

    pid = pet_rt.read_pid()
    if not pid and force:
        health = desktop_health()
        if isinstance(health, dict):
            health_pid = health.get("pid")
            if isinstance(health_pid, int) and health_pid > 0:
                pid = health_pid
    if not pid:
        _clear_desktop_base_url_cache()
        return {"ok": True, "stopped": False, "reason": "no pid file"}
    if pid == os.getpid():
        logger.warning(
            "QwenPaw Pet: refusing to stop desktop pid=%s (matches QwenPaw)",
            pid,
        )
        _clear_desktop_base_url_cache()
        return {"ok": True, "stopped": False, "reason": "pid is qwenpaw"}
    running = pet_rt.is_pid_running(pid)
    if not running and not force:
        _clear_desktop_base_url_cache()
        return {"ok": True, "stopped": False, "reason": "not running"}

    stopped = _stop_pid(
        pid,
        grace=grace,
        aggressive=aggressive or not running,
    )
    _clear_desktop_spawn_markers()
    _clear_desktop_base_url_cache()
    return {"ok": True, "stopped": stopped, "pid": pid}


def _wait_for_desktop_ready(timeout: float, interval: float = 0.1) -> bool:
    """Poll ``/health`` until the desktop responds or ``timeout`` elapses.

    Uses ``time.sleep`` so this is safe to run only from threads — call
    sites that may execute in an asyncio context should dispatch it via
    ``asyncio.to_thread``.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if desktop_health():
            return True
        time.sleep(interval)
    return False


def ensure_desktop_available() -> None:
    """Best-effort start of the desktop runtime.

    If the executable is not installed or the user prefers manual startup,
    this stays quiet. QwenPaw should never fail because the pet is absent.

    Adoption rule: if a desktop is *already* responding to ``/health`` at
    startup (e.g. left over from a previous QwenPaw run that crashed
    before its shutdown hook ran), the plugin claims it via
    ``_mark_desktop_owned()`` so the shutdown hook will stop it on the
    way out — otherwise the pet would slowly accumulate orphan
    processes that the next QwenPaw run merely "skips spawning".

    The plugin startup hook is registered as a regular callable, but the
    plugin system may invoke us either from an asyncio event loop (during
    async startup) or from a plain thread. We detect the running loop and
    drop the blocking ``_wait_for_desktop_ready`` poll into a worker
    thread so the event loop never stalls for up to two seconds at
    startup.
    """
    if desktop_health():
        _mark_desktop_owned()
        _reset_desktop_reachability_probe()
        return
    if _is_frozen_environment():
        python_for_pet = _find_system_python()
        if not python_for_pet:
            logger.info(
                "Frozen environment detected and no suitable Python found "
                "(bundled or system) — skipping pet desktop autostart.",
            )
            return
        is_bundled = _find_bundled_python() == python_for_pet
        logger.info(
            "Frozen environment: will try to autostart pet desktop "
            "via %s Python '%s'.",
            "bundled" if is_bundled else "system",
            python_for_pet,
        )
        # Fall through to normal spawn logic which now uses system Python.
    if os.environ.get("QWENPAW_PET_AUTOSTART", "0") == "0":
        return
    ok, hint = _spawn_desktop_background()
    if not ok:
        logger.warning("Could not autostart pet desktop: %s", hint)
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        wait_sec = 5.0 if sys.platform == "win32" else 2.0
        _wait_for_desktop_ready(wait_sec)
        return

    async def _poll() -> None:
        # Windows desktop cold start (PySide6 + uvicorn) can exceed 2s.
        wait_sec = 5.0 if sys.platform == "win32" else 2.0
        await asyncio.to_thread(_wait_for_desktop_ready, wait_sec)

    task = loop.create_task(_poll())

    def _done(t: asyncio.Task) -> None:
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning(
                "ensure_desktop_available poll task failed",
                exc_info=True,
            )

    task.add_done_callback(_done)


def start_desktop_interactive() -> dict[str, Any]:
    """Explicit start from HTTP/UI.

    Always tries to spawn (ignores ``QWENPAW_PET_AUTOSTART``). Returns
    a JSON-friendly dict so the console can show *why* start failed.
    """
    with _SPAWN_LOCK:
        health = desktop_health()
        if health and health.get("ok"):
            _mark_desktop_owned()
            _reset_desktop_reachability_probe()
            return _desktop_start_response(
                already_running=True,
                launch_attempted=False,
                message="Desktop pet is already running.",
            )

        host, preferred_port = _spawn_host_port_from_env()
        wait_sec = 5.0 if sys.platform == "win32" else 3.0
        if _living_desktop_present(host, preferred_port):
            _mark_desktop_owned()
            _reset_desktop_reachability_probe()
            _wait_for_desktop_ready(wait_sec, interval=0.12)
            if desktop_health():
                return _desktop_start_response(
                    already_running=True,
                    launch_attempted=False,
                    message="Desktop pet is already running.",
                )
            return _desktop_start_response(
                already_running=False,
                launch_attempted=False,
                message="Desktop pet is already starting.",
            )

        ok, hint = _spawn_desktop_background_impl()
        if not ok:
            return _desktop_start_response(
                already_running=False,
                launch_attempted=False,
                message=hint or "Could not start the desktop pet process.",
                hint=_MISSING_DEPS_HINT,
            )

        # ``start_desktop_interactive`` is wired to ``POST /desktop/start`` as
        # a sync FastAPI route, so FastAPI dispatches it in a worker thread —
        # blocking ``time.sleep`` here is fine and does not stall the loop.
        if _wait_for_desktop_ready(wait_sec, interval=0.12):
            return _desktop_start_response(
                already_running=False,
                launch_attempted=True,
                message="Desktop pet started.",
            )

        return _desktop_start_response(
            already_running=False,
            launch_attempted=True,
            message=(
                "A desktop process was spawned but /health is not ready yet "
                "(it may still be starting, or it exited immediately)."
            ),
            hint=(
                "See log file under QwenPaw pet runtime "
                "(often ~/.qwenpaw-pet/runtime/pet-desktop.log)."
            ),
        )


def schedule_emit_pet_event(event: str, **payload: Any) -> None:
    """Notify desktop from async QwenPaw code without blocking the event loop.

    Calling sync ``httpx`` inside an ``async def`` blocks the entire asyncio
    runner (including the request that is waiting on tool approval).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        emit_pet_event(event, **payload)
        return

    async def _run() -> None:
        await asyncio.to_thread(
            functools.partial(emit_pet_event, event, **payload),
        )

    task = loop.create_task(_run())

    def _done(t: asyncio.Task) -> None:
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning(
                "schedule_emit_pet_event task failed",
                exc_info=True,
            )

    task.add_done_callback(_done)


def emit_pet_event(event: str, **payload: Any) -> None:
    """Send a lifecycle event to QwenPaw Pet Desktop.

    No-op when the desktop pet is not running. Otherwise fire-and-forget:
    short timeout, no exception escapes into QwenPaw's main request path.
    """
    if not _desktop_is_reachable():
        logger.debug(
            "QwenPaw Pet Desktop not running; skip event=%s",
            event,
        )
        return

    global _EVENT_SERIAL
    _EVENT_SERIAL += 1
    # Do not pre-resolve ``state`` here — the desktop pet maps ``event`` via
    # its own ``EVENT_TO_STATE`` so animation tweaks in ``sprites.py`` take
    # effect after restarting the pet desktop only.
    body = {
        "event": event,
        "source": "qwenpaw",
        "serial": _EVENT_SERIAL,
        **payload,
    }
    base = _active_desktop_base
    if not base:
        return
    try:
        response = httpx.post(
            f"{base.rstrip('/')}/event",
            json=body,
            headers=_headers(),
            **_httpx_client_kwargs(),
        )
        if response.status_code >= 400:
            logger.warning(
                "QwenPaw Pet Desktop POST /event HTTP %s "
                "event=%s detail=%s",
                response.status_code,
                event,
                (response.text or "")[:200],
            )
        else:
            _mark_desktop_owned()
    except Exception:
        _mark_desktop_unreachable()
        logger.warning(
            "QwenPaw Pet Desktop POST /event failed (url=%s event=%s)",
            base,
            event,
            exc_info=True,
        )


def switch_pet_desktop(
    *,
    pet_dir: str | None = None,
    pet_id: str | None = None,
) -> dict[str, Any]:
    """Hot-switch the running pet via ``POST /pet`` (no desktop restart)."""
    body: dict[str, str] = {}
    if pet_dir and str(pet_dir).strip():
        body["pet_dir"] = str(pet_dir).strip()
    elif pet_id and str(pet_id).strip():
        body["pet_id"] = str(pet_id).strip()
    else:
        return {"ok": False, "error": "missing pet_dir or pet_id"}
    client_kw = dict(_httpx_client_kwargs())
    client_kw["timeout"] = 3.0
    try:
        response = httpx.post(
            f"{_resolved_desktop_base_url()}/pet",
            json=body,
            headers=_headers(),
            **client_kw,
        )
        try:
            data = response.json()
        except Exception:
            data = {"ok": response.is_success}
        if not isinstance(data, dict):
            data = {"ok": response.is_success}
        if response.status_code >= 400:
            logger.warning(
                "QwenPaw Pet Desktop POST /pet HTTP %s detail=%s",
                response.status_code,
                (response.text or "")[:300],
            )
        return data
    except Exception as exc:
        logger.warning("QwenPaw Pet Desktop POST /pet failed: %s", exc)
        return {"ok": False, "error": str(exc)}
