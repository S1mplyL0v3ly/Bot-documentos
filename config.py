"""Configuration settings for autoreporte."""

from pathlib import Path

from pydantic_settings import BaseSettings

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
DATABASE_DIR = BASE_DIR / "database"
UPLOADS_DIR = BASE_DIR / "uploads"

JARVIS_DB_PATH = Path("/root/jarvis/database/jarvis_metrics.db")


class Settings(BaseSettings):
    """Application settings loaded from environment."""

    app_name: str = "autoreporte"
    app_version: str = "0.1.0"
    debug: bool = False

    # Database
    database_url: str = f"sqlite:///{DATABASE_DIR}/autoreporte.db"

    # API
    host: str = (
        "127.0.0.1"  # hardened: localhost only; use reverse proxy for external access
    )
    port: int = 8001
    api_key: str = (
        ""  # AUTOREPORT_API_KEY in .env — empty means service will refuse to start
    )

    # Claude headless
    claude_model: str = "claude-sonnet-4-6"
    claude_timeout: int = 120

    # WhatsApp (placeholder — completar en .env)
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_verify_token: str = ""

    # Email (placeholder — completar en .env)
    email_host: str = ""
    email_port: int = 587
    email_user: str = ""
    email_password: str = ""

    class Config:
        env_file = BASE_DIR / ".env"
        env_file_encoding = "utf-8"


settings = Settings()
