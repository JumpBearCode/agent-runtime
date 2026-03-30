"""Docker sandbox lifecycle management."""

import shutil
import subprocess
from pathlib import Path

from . import config


def setup_workspace(workspace_arg: str | None) -> Path:
    """Resolve workspace path and optionally start Docker sandbox."""
    if workspace_arg is None:
        config.WORKDIR = Path.cwd()
        config.SANDBOX_ENABLED = False
        return config.WORKDIR

    ws = Path(workspace_arg).resolve() if workspace_arg != "." else Path.cwd()
    if not ws.exists():
        ws.mkdir(parents=True)
        print(f"  Created workspace: {ws}")

    config.WORKDIR = ws

    if not shutil.which("docker"):
        print("\033[33m  [warn] Docker not found, sandbox disabled.\033[0m")
        config.SANDBOX_ENABLED = False
        return config.WORKDIR

    config.SANDBOX_ENABLED = _ensure_container(ws)
    if config.SANDBOX_ENABLED:
        _init_workspace()
    return config.WORKDIR


def teardown_sandbox() -> None:
    """Stop and remove the sandbox container."""
    try:
        subprocess.run(
            ["docker", "rm", "-f", config.CONTAINER_NAME],
            capture_output=True, timeout=10,
        )
        print(f"  Sandbox: removed container '{config.CONTAINER_NAME}'")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _init_workspace() -> None:
    """Detect project type and install dependencies inside the sandbox."""
    detect = (
        "cd /workspace && "
        "if [ -f pyproject.toml ]; then echo 'pyproject'; "
        "elif [ -f requirements.txt ]; then echo 'requirements'; "
        "elif [ -f package.json ]; then echo 'node'; "
        "else echo 'none'; fi"
    )
    r = subprocess.run(
        ["docker", "exec", "--workdir", "/workspace",
         config.CONTAINER_NAME, "bash", "-c", detect],
        capture_output=True, text=True, timeout=10,
    )
    project_type = r.stdout.strip()

    install_cmd: str | None = None
    if project_type == "pyproject":
        install_cmd = "uv pip install --system -e '.[dev]' 2>/dev/null || uv pip install --system -e . 2>/dev/null || pip install -e . 2>/dev/null"
    elif project_type == "requirements":
        install_cmd = "uv pip install --system -r requirements.txt 2>/dev/null || pip install -r requirements.txt"
    elif project_type == "node":
        install_cmd = "which npm >/dev/null 2>&1 && npm install --no-fund --no-audit 2>/dev/null || true"

    if install_cmd is None:
        print("  Workspace init: no known project files detected, skipping.")
        return

    print(f"  Workspace init: detected '{project_type}', installing deps...")
    r = subprocess.run(
        ["docker", "exec", "--workdir", "/workspace",
         config.CONTAINER_NAME, "bash", "-c", install_cmd],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode == 0:
        print("  Workspace init: done.")
    else:
        stderr = r.stderr.strip()[-200:] if r.stderr else ""
        print(f"\033[33m  [warn] Workspace init failed (non-fatal): {stderr}\033[0m")


def _ensure_container(workspace: Path) -> bool:
    """Start sandbox container if not running. Returns True if sandbox is active."""
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", config.CONTAINER_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip() == "true":
            print(f"  Sandbox: reusing container '{config.CONTAINER_NAME}'")
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    r = subprocess.run(
        ["docker", "images", "-q", config.SANDBOX_IMAGE],
        capture_output=True, text=True, timeout=5,
    )
    if not r.stdout.strip():
        print(f"\033[33m  [warn] Docker image '{config.SANDBOX_IMAGE}' not found. Run: docker build -t {config.SANDBOX_IMAGE} .\033[0m")
        print("\033[33m  Sandbox disabled.\033[0m")
        return False

    subprocess.run(["docker", "rm", "-f", config.CONTAINER_NAME],
                   capture_output=True, timeout=5)

    r = subprocess.run([
        "docker", "run", "-d",
        "--name", config.CONTAINER_NAME,
        "-v", f"{workspace}:/workspace",
        config.SANDBOX_IMAGE,
    ], capture_output=True, text=True, timeout=30)

    if r.returncode == 0:
        print(f"  Sandbox: started container '{config.CONTAINER_NAME}' -> /workspace")
        return True

    print(f"\033[33m  [warn] Failed to start container: {r.stderr.strip()}\033[0m")
    print("\033[33m  Sandbox disabled.\033[0m")
    return False
