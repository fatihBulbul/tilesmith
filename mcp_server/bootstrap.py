#!/usr/bin/env python3
"""Tilesmith MCP bootstrap.

Goal: make the plugin self-contained. Claude Code launches this script
with the system `python3`; we then create a plugin-local `.venv`, install
`requirements.txt` into it, and `exec` the real MCP server using the
venv's interpreter. This sidesteps PEP 668 (externally-managed-environment
on Homebrew / Debian) because dependencies live in an isolated venv, not
in the system site-packages.

Design notes:
    * All log output goes to stderr. stdout is reserved for the MCP
      stdio protocol once `server.py` takes over.
    * A `.installed` marker is written after a successful install. On
      subsequent launches, if the marker is newer than `requirements.txt`,
      we skip the `pip install` step entirely — startup becomes near-instant.
    * `os.execv` replaces this bootstrap process with `server.py`, so
      Claude Code's stdio pipes connect directly to the MCP server.
      Using a subprocess would break the stdio handshake.
    * Cross-platform: `venv_python()` returns the right interpreter path
      on both POSIX and Windows.
    * Python 3.10+ is required (matches what the MCP SDK needs).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = PLUGIN_ROOT / ".venv"
REQ_FILE = PLUGIN_ROOT / "requirements.txt"
MARKER = VENV_DIR / ".installed"
SERVER_PY = PLUGIN_ROOT / "mcp_server" / "server.py"

FRONTEND_DIR = PLUGIN_ROOT / "studio" / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist"
FRONTEND_INDEX = FRONTEND_DIST / "index.html"
FRONTEND_MARKER = FRONTEND_DIR / ".built"

MIN_PY = (3, 10)


def log(msg: str) -> None:
    print(f"[tilesmith-bootstrap] {msg}", file=sys.stderr, flush=True)


def venv_python() -> Path:
    """Path to the interpreter inside the plugin-local venv."""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def needs_install() -> bool:
    """True if venv doesn't exist, marker missing, or requirements changed."""
    if not VENV_DIR.exists():
        return True
    if not venv_python().exists():
        return True
    if not MARKER.exists():
        return True
    if not REQ_FILE.exists():
        # No requirements file — nothing to install, consider fresh.
        return False
    return REQ_FILE.stat().st_mtime > MARKER.stat().st_mtime


def ensure_venv() -> None:
    """Create the plugin-local venv if missing. Idempotent."""
    if not VENV_DIR.exists() or not venv_python().exists():
        log(f"creating venv at {VENV_DIR}")
        builder = venv.EnvBuilder(
            with_pip=True,
            clear=False,
            upgrade_deps=False,
            symlinks=(os.name != "nt"),
        )
        builder.create(VENV_DIR)


def pip_install() -> None:
    """Install requirements.txt into the venv."""
    if not REQ_FILE.exists():
        log("requirements.txt not found; skipping pip install")
        return
    log(f"installing deps from {REQ_FILE.name} (first run may take ~30s)")
    subprocess.check_call(
        [
            str(venv_python()),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--quiet",
            "-r",
            str(REQ_FILE),
        ]
    )
    MARKER.touch()
    log("deps installed")


def ensure_frontend() -> None:
    """If the Studio frontend build is missing AND Node is available,
    try to build it. This is a belt-and-suspenders fallback — the plugin
    normally ships with a prebuilt `studio/frontend/dist/`. If Node is
    absent we silently skip; the bridge server will serve a readable HTML
    error page when the user tries to open Studio.
    """
    if FRONTEND_INDEX.exists():
        return  # Prebuilt dist already present (expected path).
    npm = shutil.which("npm")
    if npm is None:
        log("npm not found; Studio UI will not be available until you "
            "install Node.js 18+ and run `npm install && npm run build` "
            f"in {FRONTEND_DIR}")
        return
    try:
        log("Studio frontend missing; building from source (~30-60s)")
        # `npm ci` if lockfile exists, else `npm install`.
        if (FRONTEND_DIR / "package-lock.json").exists():
            subprocess.check_call([npm, "ci", "--silent"], cwd=str(FRONTEND_DIR))
        else:
            subprocess.check_call([npm, "install", "--silent"],
                                  cwd=str(FRONTEND_DIR))
        subprocess.check_call([npm, "run", "build", "--silent"],
                              cwd=str(FRONTEND_DIR))
        FRONTEND_MARKER.touch()
        log("Studio frontend built")
    except subprocess.CalledProcessError as e:
        log(f"WARN: frontend build failed (exit {e.returncode}); "
            "Studio UI will be unavailable until the build succeeds. "
            "Core MCP tools still work.")
    except Exception as e:  # noqa: BLE001
        log(f"WARN: frontend build skipped: {e}")


def check_python_version() -> None:
    if sys.version_info < MIN_PY:
        log(
            f"ERROR: Python {MIN_PY[0]}.{MIN_PY[1]}+ required, "
            f"found {sys.version_info.major}.{sys.version_info.minor}"
        )
        sys.exit(1)


def main() -> None:
    check_python_version()

    if needs_install():
        try:
            ensure_venv()
            pip_install()
        except subprocess.CalledProcessError as e:
            log(f"ERROR: pip install failed (exit {e.returncode})")
            sys.exit(1)
        except Exception as e:  # noqa: BLE001
            log(f"ERROR: bootstrap failed: {e}")
            sys.exit(1)

    # Frontend is non-fatal: a missing Studio UI does not break core MCP
    # tools (scan_folder, generate_map, consolidate_map, ...). We only
    # fail the whole bootstrap on Python dep issues above.
    ensure_frontend()

    if not SERVER_PY.exists():
        log(f"ERROR: server.py not found at {SERVER_PY}")
        sys.exit(1)

    interp = str(venv_python())
    # Replace this process with the MCP server so Claude Code's stdio
    # handshake connects directly to it.
    os.execv(interp, [interp, str(SERVER_PY), *sys.argv[1:]])


if __name__ == "__main__":
    main()
