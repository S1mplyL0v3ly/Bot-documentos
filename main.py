"""autoreporte — FastAPI application entry point."""

import sys

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from config import settings
from database.init_db import init_db


async def verify_api_key(x_api_key: str = Header(...)) -> None:
    """Dependency: require X-Api-Key header matching AUTOREPORT_API_KEY."""
    if not settings.api_key or x_api_key != settings.api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing API key.")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    # Fail-closed: refuse to start without an API key configured
    if not settings.api_key:
        print(
            "[autoreporte] FATAL: AUTOREPORT_API_KEY is not set. "
            "Set it in .env before starting the service.",
            file=sys.stderr,
        )
        sys.exit(1)

    init_db()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="Sistema de autoreporte: extrae campos de documentos y genera DOCX.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:8000",
            "http://localhost:8001",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8001",
        ],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "X-Api-Key"],
    )

    app.include_router(
        router,
        prefix="/api/v1",
        dependencies=[Depends(verify_api_key)],
    )

    @app.get("/health")
    def health_check():
        return {"status": "ok", "version": settings.app_version}

    return app


app = create_app()
