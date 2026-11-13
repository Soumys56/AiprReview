
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    GITHUB_TOKEN: str
    OWNER: str
    REPO: str
    GITHUB_API:str
    REPO_CACHE_DIR:str
    INDEX_DB_PATH:str
    GIT_HOST:str
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8"
    )

settings = Settings()
