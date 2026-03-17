import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api import router
from app.config import get_settings
from app.database import Base, engine


settings = get_settings()

app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin, "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix=settings.api_prefix)


@app.on_event("startup")
def initialize_database() -> None:
    last_error: Exception | None = None
    for _ in range(20):
        try:
            with engine.begin() as connection:
                connection.execute(text("SELECT 1"))
                Base.metadata.create_all(bind=connection)
            return
        except Exception as exc:  # pragma: no cover - startup retry path
            last_error = exc
            time.sleep(1)
    if last_error:
        raise last_error
