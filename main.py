import asyncio
from config.setting import settings
from components.index_store import IndexStore
from service.pipeline import Stage2Pipeline
from components.repo_mirror import RepoMirror
from components.stage3 import stage3

async def main():
    owner, repo, pr_number = settings.OWNER,settings.REPO,1
    mirror = RepoMirror(
        owner=owner,
        repo=repo,
        token=settings.GITHUB_TOKEN,
        cache_dir=settings.REPO_CACHE_DIR,
    )
    store = IndexStore()
    pipeline = Stage2Pipeline(store=store, mirror=mirror, owner=owner, repo=repo)

    context_bundle = await pipeline.run(pr_number)
    # stage3.export_worktree()
   
if __name__ == "__main__":
    asyncio.run(main())