"""Initialize the autoreporte SQLite database."""

from pathlib import Path

from sqlalchemy import create_engine

from config import DATABASE_DIR, settings
from database.models import Base


def init_db() -> None:
    """Create all tables if they don't exist."""
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, echo=settings.debug)
    Base.metadata.create_all(engine)
    print(f"Database initialized at: {settings.database_url}")


if __name__ == "__main__":
    init_db()
