"""FastAPI app. Binds to 127.0.0.1 by default (run with `uvicorn askdb.main:app`)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from askdb.health import HealthReport, run_health_check
from askdb.schema import SchemaInfo
from askdb.settings import Settings, get_settings

log = logging.getLogger("askdb")


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings or get_settings()
        app.state.health = run_health_check(app.state.settings)
        for p in app.state.health.problems:
            log.warning("startup health check: %s", p.message)
        yield

    app = FastAPI(title="AskDB", lifespan=lifespan)

    @app.get("/api/health", response_model=HealthReport)
    def health(request: Request):
        return request.app.state.health

    @app.get("/api/schema-info", response_model=SchemaInfo)
    def schema_info(request: Request):
        report: HealthReport = request.app.state.health
        if report.schema_info is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "Schema is unavailable. Fix the problems below and restart.",
                    "problems": [p.model_dump() for p in report.problems],
                },
            )
        return report.schema_info

    return app


app = create_app()
