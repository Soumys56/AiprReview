
import httpx
import base64
from urllib.parse import quote
from config.setting import settings
if not settings.GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN is missing from .env")

HEADERS = {
    "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def fetch_repository_pull_requests(
    owner: str,
    repo: str,
    state: str = "open",
) -> list[dict]:
    if state not in {"open", "closed", "all"}:
        raise ValueError("state must be open, closed, or all")
    pull_requests = []
    async with httpx.AsyncClient(timeout=30) as client:
        page = 1
        while True:
            response = await client.get(
                f"{settings.GITHUB_API}/repos/{owner}/{repo}/pulls",
                headers=HEADERS,
                params={
                    "state": state,
                    "per_page": 100,
                    "page": page,
                    "sort": "updated",
                    "direction": "desc",
                },
            )
            if response.status_code == 404:
                raise RuntimeError(
                    f"Repository not found or access denied: "
                    f"{owner}/{repo}"
                )
            if response.status_code == 401:
                raise RuntimeError("Invalid or expired GitHub token")
            if response.status_code == 403:
                raise RuntimeError(
                    f"GitHub permission or rate-limit error: "
                    f"{response.text}"
                )
            response.raise_for_status()
            data = response.json()
            if not data:
                break
            for pr in data:
                pull_requests.append({
                    "number": pr["number"],
                    "title": pr["title"],
                    "author": pr["user"]["login"],
                    "state": pr["state"],
                    "head_branch": pr["head"]["ref"],
                    "base_branch": pr["base"]["ref"],
                    "head_sha": pr["head"]["sha"],
                    "base_sha": pr["base"]["sha"],
                    "created_at": pr["created_at"],
                    "updated_at": pr["updated_at"],
                    "url": pr["html_url"],
                })

            page += 1
    return pull_requests


async def fetch_pr_diff(
    owner: str,
    repo: str,
    pr_number: int,
) -> list[dict]:
    """
    Fetch all changed files and their code patches
    for a particular pull request.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{settings.GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files",
            headers=HEADERS,
            params={"per_page": 100} # Get up to 100 files per page
        )
        # Call this directly, do not assign it back to response
        response.raise_for_status() 
        files = response.json()
        
        if not files:
            return []
            
        changed_files = []
        for f in files:
            changed_files.append({
                "filename": f["filename"],
                # Capture previous filename for renames
                "previous_filename": f.get("previous_filename"), 
                "status": f["status"],
                "additions": f["additions"],
                "deletions": f["deletions"],
                "changes": f["changes"],
                # Use None instead of a string for missing patches
                "patch": f.get("patch", None), 
            })
            
        return changed_files
    
def _raise_for_github_error(response: httpx.Response, owner: str, repo: str) -> None:
    if response.status_code == 404:
        raise RuntimeError(f"Repository or PR not found: {owner}/{repo}")
    if response.status_code == 401:
        raise RuntimeError("Invalid or expired GitHub token")
    if response.status_code == 403:
        raise RuntimeError(f"GitHub permission or rate-limit error: {response.text}")
    response.raise_for_status()
    
    
    
async def fetch_pull_request(owner: str, repo: str, pr_number: int) -> dict:
    """Fetch metadata (including base_sha and head_sha) for a single pull request."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{settings.GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=HEADERS,
        )
        _raise_for_github_error(response, owner, repo)
        pr = response.json()
        return {
            "number": pr["number"],
            "title": pr["title"],
            "author": pr["user"]["login"],
            "state": pr["state"],
            "head_branch": pr["head"]["ref"],
            "base_branch": pr["base"]["ref"],
            "head_sha": pr["head"]["sha"],
            "base_sha": pr["base"]["sha"],
            "created_at": pr["created_at"],
            "updated_at": pr["updated_at"],
            "url": pr["html_url"],
        }