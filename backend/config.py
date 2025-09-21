# backend/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # --- Application Secrets ---
    # The default is insecure; you MUST override this in the .env file
    secret_key: str = "a_default_insecure_secret_key"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 1 day

    # --- Database Configuration ---
    database_url: str = "sqlite:///./bugzilla_tracker.db"

    # --- Bugzilla API Configuration ---
    bugzilla_api_key: str | None = None
    bugzilla_url: str = "https://bugzilla.mozilla.org"

    class Config:
        env_file = ".env"


# Create a single, reusable settings instance
settings = Settings()