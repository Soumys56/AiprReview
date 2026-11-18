from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    GITHUB_TOKEN: str = ""
    OWNER: str = ""
    REPO: str = ""
    GITHUB_API: str = "https://api.github.com"
    REPO_CACHE_DIR: str = ".cache/repos"
    INDEX_DB_PATH: str = ".cache/index.sqlite"
    GIT_HOST: str = "github.com"
    GROQ_API: str = "https://api.groq.com/openai/v1"
    GROQ_TOKEN: str = ""

    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH) if ENV_PATH.exists() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
