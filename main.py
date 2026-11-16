import asyncio
from config.setting import settings
from components.index_store import IndexStore
from service.pipeline import Stage2Pipeline
from components.repo_mirror import RepoMirror
from stage3.pipeline import Stage3Pipeline

async def main():
    owner, repo, pr_number = settings.OWNER, settings.REPO, 3
    mirror = RepoMirror(
        owner=owner,
        repo=repo,
        token=settings.GITHUB_TOKEN,
        cache_dir=settings.REPO_CACHE_DIR,
    )
    store = IndexStore()
    pipeline = Stage2Pipeline(store=store, mirror=mirror, owner=owner, repo=repo)
    context_bundle = await pipeline.run(pr_number)
    stage3 = Stage3Pipeline(
        mirror=mirror,
        pr_number=pr_number,
        worktree_root=f"{settings.REPO_CACHE_DIR}/worktree",
    )
    result = await stage3.run(context_bundle)
    print(result.to_dict())
   
if __name__ == "__main__":
    asyncio.run(main())
