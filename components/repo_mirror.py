import subprocess
from pathlib import Path

class RepoMirror:
    """Bare, blob-less clone. Reads any file at any commit without a checkout."""

    def __init__(self, owner: str, repo: str, token: str, cache_dir: str, host: str = "github.com"):
        self.path = Path(cache_dir) / f"{owner}__{repo}.git"
        # Fixed URL format: Authenticats via token without triggering markdown bugs
        self.url = f"https://x-access-token:{token}@{host}/{owner}/{repo}.git"

    def _git(self, *args, check=True):
        """Helper to run a raw Git command quietly in the background."""
        return subprocess.run(
            ["git", *args], 
            cwd=self.path, 
            capture_output=True, 
            check=check
        )

    def _has(self, sha: str) -> bool:
        """Check if Git's internal database already contains this specific commit."""
        return self._git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def ensure(self, base_sha: str, head_sha: str, pr_number: int) -> None:
        """Download the bare repo and fetch the exact base and PR commits."""
        # Step 1: Create the hidden blob-less clone if it doesn't exist yet
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--bare", "--filter=blob:none", self.url, str(self.path)],
                check=True, 
                capture_output=True
            )
            
        # Step 2: Fetch the main branch commit (Before PR)
        if not self._has(base_sha):
            self._git("fetch", "origin", base_sha)
            
        # Step 3: Fetch the new PR code (Even if it comes from a personal fork)
        if not self._has(head_sha):
            self._git("fetch", "origin", f"+refs/pull/{pr_number}/head:refs/pr/{pr_number}")

    def list_files(self, sha: str) -> list[str]:
        """Get a fast text list of all files present at this commit."""
        result = self._git("ls-tree", "-r", "--name-only", sha)
        return result.stdout.decode().splitlines()

    def read(self, sha: str, path: str) -> bytes | None:
        """Pluck the raw text of a file directly from Git's memory."""
        result = self._git("show", f"{sha}:{path}", check=False)
        return result.stdout if result.returncode == 0 else None