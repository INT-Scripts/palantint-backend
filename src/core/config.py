import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Environment
    ENVIRONMENT: str = "development"  # "development" | "production" | "test"

    # Security
    SECRET_KEY: str = "dev_secret_key_change_in_production"
    ALGORITHM: str = "HS256"
    # Fernet key (32 url-safe base64-encoded bytes) used to encrypt third-party
    # credentials (e.g. CAS) at rest in `user_credentials`. Generate with
    # `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
    FERNET_KEY: str = "H9A6BAwySrBGIGsjS2v13WK6jcyCnlyETdBAMOuXLpY="
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 5
    REFRESH_TOKEN_EXPIRE_MINUTES: int = 60
    
    # Internal Authentication
    MCP_SERVICE_TOKEN: str | None = None
    
    # Database
    DATABASE_URL: str | None = None
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "CHANGE_ME_TO_A_STRONG_PASSWORD"
    POSTGRES_DB: str = "palantint"
    POSTGRES_HOST: str = "localhost"
    
    # Storage Paths
    APP_ROOT: Path = Path(__file__).resolve().parent.parent.parent
    DATA_ROOT: Path = Path(os.getenv("DATA_ROOT", "/app" if os.path.exists("/app") else APP_ROOT.parent / "data"))
    
    ASSETS_DIR: Path = DATA_ROOT / "assets"
    PRIVATE_ASSETS_DIR: Path = DATA_ROOT / "private_assets"
    
    # Media & Profiles
    MEDIA_DIR: Path = PRIVATE_ASSETS_DIR / "media"
    PROFILES_DIR: Path = PRIVATE_ASSETS_DIR / "profiles"

    model_config = SettingsConfigDict(
        env_file=[
            str(Path(__file__).resolve().parent.parent.parent.parent / ".env"),
            str(Path(__file__).resolve().parent.parent.parent / ".env"),
            ".env"
        ],
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

settings = Settings()
