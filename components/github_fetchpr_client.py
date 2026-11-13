from service.githubclinet import fetch_repository_pull_requests
from service.githubclinet import fetch_pr_diff
from service.githubclinet import fetch_pull_request
class githubFetch:
    def __init__(self,repo,owner):
        self.repo=repo
        self.owner=owner
    async def github_fetch_all_pr(self):
        try:
            print(self.owner,self.repo)
            all_prs=await fetch_repository_pull_requests(self.owner,self.repo) 
            return all_prs
        except Exception as e:
            print(f"Error while fetching GitHub pull requests: {e}")
            return None
 
class GitHubPRDiff(githubFetch):

    def __init__(self, repo: str, owner: str, pr_number: int):
        super().__init__(repo, owner)
        self.pr_number = pr_number
        
    async def github_fetch_pr(self):
        try:
            return await fetch_pull_request(self.owner, self.repo, self.pr_number)
        except Exception as e:
            print(f"Error while fetching GitHub pull request #{self.pr_number}: {e}")
            return None

    async def github_fetch_pr_diff(self):
        try:
            return await fetch_pr_diff(
                self.owner,
                self.repo,
                self.pr_number
            )

        except Exception as e:
            print(f"Error while fetching GitHub pull requests: {e}")
            return None            
        
    
        
    

    