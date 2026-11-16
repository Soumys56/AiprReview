import hashlib
import shutil
import subprocess
from pathlib import Path

INSTALL_TIMEOUT_SEC = 300


def _sync_lockfile(
    worktree_dir: Path,
    lockfile: Path,
    marker: Path,
    tool: str,
    cmd: list[str],
) -> dict:
    """Run a frozen dependency install cached by the lockfile's SHA-256 hash."""
    if not shutil.which(tool):
        return {
            "installed": False,
            "cached": False,
            "tool": tool,
            "reason": f"{tool} not installed",
        }

    lock_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == lock_hash:
        return {"installed": True, "cached": True, "tool": tool}

    try:
        subprocess.run(
            cmd,
            cwd=worktree_dir,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SEC,
            check=True,
        )
    except subprocess.TimeoutExpired:
        return {
            "installed": False,
            "cached": False,
            "tool": tool,
            "reason": f"{tool} install timed out",
        }
    except subprocess.CalledProcessError as exc:
        output = (exc.stderr or exc.stdout or "").strip().splitlines()
        reason = output[-1] if output else "install failed"
        return {
            "installed": False,
            "cached": False,
            "tool": tool,
            "reason": reason[:200],
        }

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(lock_hash, encoding="utf-8")
    return {"installed": True, "cached": False, "tool": tool}


def ensure_worktree_deps(worktree_dir: Path) -> dict[str, dict]:
    """Install locked Python and/or Node dependencies found in a worktree."""
    worktree_dir = Path(worktree_dir)
    results: dict[str, dict] = {}

    uv_lock = worktree_dir / "uv.lock"
    if uv_lock.exists():
        results["python"] = _sync_lockfile(
            worktree_dir=worktree_dir,
            lockfile=uv_lock,
            marker=worktree_dir / ".venv" / ".lock_hash",
            tool="uv",
            cmd=["uv", "sync", "--frozen", "--no-install-project", "--no-build"],
        )

    npm_lock = worktree_dir / "package-lock.json"
    pnpm_lock = worktree_dir / "pnpm-lock.yaml"
    if npm_lock.exists():
        results["node"] = _sync_lockfile(
            worktree_dir=worktree_dir,
            lockfile=npm_lock,
            marker=worktree_dir / "node_modules" / ".lock_hash",
            tool="npm",
            cmd=["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        )
    elif pnpm_lock.exists():
        results["node"] = _sync_lockfile(
            worktree_dir=worktree_dir,
            lockfile=pnpm_lock,
            marker=worktree_dir / "node_modules" / ".lock_hash",
            tool="pnpm",
            cmd=["pnpm", "install", "--frozen-lockfile", "--ignore-scripts"],
        )

    return results
