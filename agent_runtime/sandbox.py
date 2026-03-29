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
    return config.WORKDIR


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
