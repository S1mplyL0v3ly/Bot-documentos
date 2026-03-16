"""Initialize the autoreporte SQLite database."""

from pathlib import Path

from sqlalchemy import create_engine, text

from config import DATABASE_DIR, settings
from database.models import Base


def _migrate_existing_db(engine) -> None:
    """Add columns that may not exist in older installs (safe no-op if already present)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE documents ADD COLUMN transcript_text TEXT"))
            conn.commit()
    except Exception:
        pass  # Column already exists


def init_db() -> None:
    """Create all tables if they don't exist and run migrations."""
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.database_url, echo=settings.debug)
    Base.metadata.create_all(engine)
    _migrate_existing_db(engine)
    print(f"Database initialized at: {settings.database_url}")


if __name__ == "__main__":
    init_db()
