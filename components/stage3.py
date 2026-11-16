from pathlib import Path
import shutil
class stage3:
    def __init__(self,sha,pr_number,worktree_root):
        self.sha=sha
        self.pr_number=pr_number
        self.worktree_root=worktree_root
    def export_worktree(
        self,
        sha: str,
        pr_number: int | None = None,
        worktree_root: str | Path = ".cache/worktrees",
    ) -> Path:
        """Create (or reuse) a read-only detached worktree at `sha` for static tools."""
        target = Path(worktree_root) / sha[:12]
        # 1. Cache hit: reuse existing directory if already checked out
        if target.exists() and (target / ".git").exists():
            return target
        # 2. Ensure the bare mirror contains `sha` before exporting
        if not self._has(sha):
            if pr_number is not None:
                self._git("fetch", "origin", f"+refs/pull/{pr_number}/head:refs/pr/{pr_number}")
            if not self._has(sha):
                self._git("fetch", "origin", sha)
        # 3. Clean up any broken partial folder and materialize files onto disk
        if target.exists():
            shutil.rmtree(target)
        self._git("worktree", "prune", check=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--detach", "--force", str(target.resolve()), sha)
        return target
    
        