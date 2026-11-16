from pathlib import Path
import shutil
import subprocess
from components.worktree_deps import ensure_worktree_deps


class Stage3:
    def __init__(self, mirror, sha, pr_number, worktree_root):
        self.mirror = mirror
        self.sha=sha
        self.pr_number=pr_number
        self.worktree_root=worktree_root
        
    def export_worktree(
        self,
    ) -> Path:
        """Create (or reuse) a read-only detached worktree at `sha` for static tools."""
        target = Path(self.worktree_root) / self.sha[:12]
        # 1. Cache hit: reuse existing directory if already checked out
        if target.exists() and (target / ".git").exists():
            self.dependency_status = ensure_worktree_deps(target)
            return target
        # 2. Ensure the bare mirror contains `sha` before exporting
        if not self._has(self.sha):
            if self.pr_number is not None:
                self._git("fetch", "origin", f"+refs/pull/{self.pr_number}/head:refs/pr/{self.pr_number}")
            if not self._has(self.sha):
                self._git("fetch", "origin", self.sha)
        # 3. Clean up any broken partial folder and materialize files onto disk
        if target.exists():
            shutil.rmtree(target)
        self._git("worktree", "prune", check=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--detach", "--force", str(target.resolve()), self.sha)
        self.dependency_status = ensure_worktree_deps(target)
        return target
    
    def _git(self, *args, check=True):
        """Run a raw Git command quietly in the background."""
        return subprocess.run(
            ["git", *args],
            cwd=self.mirror.path,
            capture_output=True,
            check=check,
        )
    
    def _has(self, sha: str) -> bool:
        """Check if Git's internal database already contains this specific commit."""
        return self._git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0
    
    
        
