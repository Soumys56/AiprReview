from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Tuple

from components.repo_mirror import RepoMirror

IGNORED_SEGMENTS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".ds_store",
}


def extract_zip_to_map(zip_bytes: bytes) -> dict[str, bytes]:
    """
    Extracts a branch .zip archive into {relative_repo_path: file_bytes}.
    Normalizes Windows slashes, blocks path traversal (..), automatically strips
    a single top-level wrapper folder (e.g., repo-main/src/app.py -> src/app.py),
    and ignores .git, node_modules, __pycache__, .venv, venv, .mypy_cache, .ruff_cache,
    .pytest_cache, and .DS_Store.
    """
    if not zip_bytes:
        return {}

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for info in z.infolist():
            if info.is_dir() or info.filename.endswith("/"):
                continue

            # Normalize Windows slashes
            norm_name = info.filename.replace("\\", "/").strip()
            while norm_name.startswith(("/", "./")):
                norm_name = norm_name.lstrip("/.")

            if not norm_name:
                continue

            parts = tuple(PurePosixPath(norm_name).parts)

            # Block path traversal
            if ".." in parts or any(p.startswith("/") for p in parts):
                continue

            # Ignore cache / virtualenv / git / ds_store folders
            if any(
                p.lower() in IGNORED_SEGMENTS
                or p.lower().endswith((".pyc", ".pyo"))
                for p in parts
            ):
                continue

            entries.append((info, parts))

        if not entries:
            return {}

        # Detect and strip single top-level wrapper directory (e.g. repo-main/...)
        first_segment = entries[0][1][0]
        has_wrapper = all(len(p) > 1 and p[0] == first_segment for _, p in entries)

        file_map: dict[str, bytes] = {}
        for info, parts in entries:
            rel_parts = parts[1:] if has_wrapper else parts
            rel_path = "/".join(rel_parts)
            if not rel_path:
                continue
            file_map[rel_path] = z.read(info)

        return file_map


def parse_git_diff(diff_text: str) -> list[dict]:
    """
    Parse the output of `git diff --no-ext-diff -M -U3 <base> <head>`
    into a list of GitHub-compatible changed_file dicts:
    {
        "filename": str,
        "previous_filename": str | None,
        "status": "added" | "modified" | "removed" | "renamed",
        "additions": int,
        "deletions": int,
        "changes": int,
        "patch": str | None,
    }
    """
    if not diff_text or not diff_text.strip():
        return []

    sections = re.split(r"(?m)^diff --git ", diff_text)
    changed_files: list[dict] = []

    for sec in sections:
        if not sec.strip():
            continue

        lines = sec.splitlines()
        header = lines[0]  # e.g. "a/foo.py b/bar.py"

        is_new = any(l.startswith("new file mode") for l in lines)
        is_deleted = any(l.startswith("deleted file mode") for l in lines)
        is_binary = any(
            l.startswith("Binary files ") or "GIT binary patch" in l
            for l in lines
        )

        rename_from = None
        rename_to = None
        for l in lines:
            if l.startswith("rename from "):
                rename_from = l[len("rename from "):].strip().strip('"')
            elif l.startswith("rename to "):
                rename_to = l[len("rename to "):].strip().strip('"')

        # Find first hunk line starting with "@@ "
        hunk_start_idx = None
        for idx, l in enumerate(lines):
            if l.startswith("@@ "):
                hunk_start_idx = idx
                break

        patch_lines = lines[hunk_start_idx:] if hunk_start_idx is not None else []
        patch = "\n".join(patch_lines) + "\n" if patch_lines else None

        additions = 0
        deletions = 0
        if patch_lines:
            for l in patch_lines:
                if l.startswith("+") and not l.startswith("+++"):
                    additions += 1
                elif l.startswith("-") and not l.startswith("---"):
                    deletions += 1

        # Determine filenames and status
        if rename_from and rename_to:
            status = "renamed"
            filename = rename_to
            previous_filename = rename_from
        elif is_new:
            status = "added"
            plus_line = next((l for l in lines if l.startswith("+++ b/")), None)
            if plus_line:
                filename = plus_line[6:].strip().strip('"')
            else:
                parts = header.split(" b/", 1)
                filename = parts[1].strip().strip('"') if len(parts) == 2 else header.split()[-1].lstrip("b/")
            previous_filename = None
        elif is_deleted:
            status = "removed"
            minus_line = next((l for l in lines if l.startswith("--- a/")), None)
            if minus_line:
                filename = minus_line[6:].strip().strip('"')
            else:
                parts = header.split(" b/", 1)
                filename = parts[0].strip().lstrip("a/").strip('"') if parts else header.split()[0].lstrip("a/")
            previous_filename = None
        else:
            status = "modified"
            plus_line = next((l for l in lines if l.startswith("+++ b/")), None)
            if plus_line:
                filename = plus_line[6:].strip().strip('"')
            else:
                parts = header.split(" b/", 1)
                filename = parts[1].strip().strip('"') if len(parts) == 2 else header.split()[-1].lstrip("b/")
            previous_filename = None

        changed_files.append({
            "filename": filename,
            "previous_filename": previous_filename,
            "status": status,
            "additions": additions,
            "deletions": deletions,
            "changes": additions + deletions,
            "patch": patch if not is_binary else None,
        })

    return changed_files


def _safe_write_tree(root: Path, file_map: dict[str, bytes]) -> None:
    """Safely writes file_map into root, validating against path traversal."""
    root_resolved = root.resolve()
    for rel_path, data in file_map.items():
        norm = rel_path.replace("\\", "/").strip().lstrip("/")
        if not norm:
            continue
        target = (root_resolved / norm).resolve()
        if not target.is_relative_to(root_resolved):
            raise ValueError(f"Unsafe file path detected escaping repository: {rel_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def build_offline_branch_pr(
    base_files: dict[str, bytes],
    head_files: dict[str, bytes],
    project_name: str = "offline-repo",
    cache_dir: str = ".cache/repos",
    pr_title: str = "Offline Branch PR Review",
) -> Tuple[RepoMirror, dict, list[dict]]:
    """
    Initializes a clean bare RepoMirror(owner='local', repo=safe_project, token='offline', cache_dir=cache_dir).
    Uses a temporary working directory to commit base_files as base_sha (main), overwrite the working
    tree with head_files and commit as head_sha (pr-branch), and push both commits into the bare
    RepoMirror (refs/heads/main, refs/heads/pr-branch, and refs/pr/1).
    Runs git diff --no-ext-diff -M -U3 <base_sha> <head_sha> on the bare mirror and parses the
    unified diff into GitHub-compatible changed_files list.
    """
    safe_project = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", project_name).strip("._")
    if not safe_project:
        safe_project = "offline-repo"

    cache_path = Path(cache_dir).resolve()
    cache_path.mkdir(parents=True, exist_ok=True)

    mirror = RepoMirror(
        owner="local",
        repo=safe_project,
        token="offline",
        cache_dir=str(cache_path),
    )
    mirror_path = mirror.path.resolve()
    mirror_path.parent.mkdir(parents=True, exist_ok=True)

    if not (mirror_path / "HEAD").exists():
        mirror_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare"],
            cwd=mirror_path,
            check=True,
            capture_output=True,
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir).resolve()

        # Initialize local working directory
        subprocess.run(["git", "init"], cwd=temp_path, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=temp_path, check=False, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Offline Reviewer"], cwd=temp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "offline@aiprreview.local"], cwd=temp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=temp_path, check=True, capture_output=True)

        # 1. Base files -> main branch commit
        _safe_write_tree(temp_path, base_files or {".gitkeep": b""})
        subprocess.run(["git", "add", "-A"], cwd=temp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Base commit (main)", "--no-gpg-sign", "--allow-empty"],
            cwd=temp_path,
            check=True,
            capture_output=True,
        )
        base_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_path,
        ).decode().strip()

        # 2. PR branch -> pr-branch commit
        subprocess.run(["git", "checkout", "-b", "pr-branch"], cwd=temp_path, check=True, capture_output=True)

        # Clean old working files except .git
        for item in temp_path.iterdir():
            if item.name == ".git":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()

        # Write head files
        _safe_write_tree(temp_path, head_files or {".gitkeep": b""})
        subprocess.run(["git", "add", "-A"], cwd=temp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "PR commit (pr-branch)", "--no-gpg-sign", "--allow-empty"],
            cwd=temp_path,
            check=True,
            capture_output=True,
        )
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=temp_path,
        ).decode().strip()

        # 3. Push both commits into bare mirror
        remote_mirror_url = str(mirror_path)
        subprocess.run(
            ["git", "remote", "add", "mirror", remote_mirror_url],
            cwd=temp_path,
            check=False,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "mirror", "main:refs/heads/main", "--force"],
            cwd=temp_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "mirror", "pr-branch:refs/heads/pr-branch", "--force"],
            cwd=temp_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "mirror", "pr-branch:refs/pr/1", "--force"],
            cwd=temp_path,
            check=True,
            capture_output=True,
        )

    # Compute diff on bare mirror
    diff_proc = mirror._git(
        "-c",
        "core.quotepath=false",
        "diff",
        "--no-ext-diff",
        "-M",
        "-U3",
        base_sha,
        head_sha,
        check=True,
    )
    diff_text = diff_proc.stdout.decode("utf-8", errors="replace")
    changed_files = parse_git_diff(diff_text)

    pr_meta = {
        "number": 1,
        "title": pr_title,
        "body": "Offline branch diff generated locally without GitHub token.",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "base_branch": "main",
        "head_branch": "pr-branch",
        "state": "open",
        "author": "offline-user",
        "url": "",
        "owner": "local",
        "repo": safe_project,
    }

    return mirror, pr_meta, changed_files
