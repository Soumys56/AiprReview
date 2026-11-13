import asyncio
from config.setting import settings
from components.index_store import IndexStore
from service.pipeline import Stage2Pipeline
from components.repo_mirror import RepoMirror


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
    print(f"Chunks: {len(context_bundle['chunks'])}, Links: {len(context_bundle['cross_file_links'])}")
    print(context_bundle)

if __name__ == "__main__":
    asyncio.run(main())