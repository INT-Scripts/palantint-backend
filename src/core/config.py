import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Security
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 5
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60
    
    # Internal Authentication
    MCP_SERVICE_TOKEN: str | None = None
    MCP_PALANTINT_URL: str = "http://localhost:3000"
    
    # Database
    DATABASE_URL: str | None = None
    
    # Storage Paths
    # Defaulting to paths that work both in dev and in docker (via env overrides)
    APP_ROOT: Path = Path(__file__).resolve().parent.parent.parent
    DATA_ROOT: Path = Path(os.getenv("DATA_ROOT", "/app" if os.path.exists("/app") else APP_ROOT.parent / "data"))
    
    ASSETS_DIR: Path = DATA_ROOT / "assets"
    PRIVATE_ASSETS_DIR: Path = DATA_ROOT / "private_assets"
    
    MEDIA_DIR: Path = PRIVATE_ASSETS_DIR / "media"
    PROFILES_DIR: Path = PRIVATE_ASSETS_DIR / "profiles"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

settings = Settings()
