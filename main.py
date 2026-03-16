"""autoreporte — FastAPI application entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from config import settings
from database.init_db import init_db


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    init_db()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Sistema de autoreporte: extrae campos de documentos y genera DOCX.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api/v1")

    @app.get("/health")
    def health_check():
        return {"status": "ok", "version": settings.app_version}

    return app


app = create_app()
