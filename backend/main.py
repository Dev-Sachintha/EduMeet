"""FastAPI application entry point.  Run from the project root:  python main.py server"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend import admin, ai, attendance, auth, emailcheck, files, mailer, meetings, models, signaling  # noqa: F401  (models registers tables)
from backend.config import BASE_DIR, settings
from backend.database import Base, SessionLocal, engine, ensure_columns


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Starter convenience. For production schema changes use Alembic migrations instead.
    Base.metadata.create_all(bind=engine)
    ensure_columns(engine)  # upgrades an older database file in place
    settings.upload_path  # creates the uploads folder
    with SessionLocal() as db:
        auth.ensure_first_admin(db)
        emailcheck.backfill_canonical(db)
    env_file = BASE_DIR / ".env"
    print(f"[EduMeet] Settings file: {env_file} -> {'found' if env_file.exists() else 'NOT FOUND (using built-in defaults; copy .env.example to .env)'}", flush=True)
    print(f"[EduMeet] {mailer.status_line()}", flush=True)
    yield


app = FastAPI(title="EduMeet API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,  # bearer tokens, not cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(auth.admin_router)
app.include_router(admin.router)
app.include_router(ai.router)
app.include_router(attendance.router)
app.include_router(attendance.page_router)
app.include_router(meetings.router)
app.include_router(files.router)
app.include_router(signaling.router)
app.include_router(signaling.page_router)


@app.get("/api/health", tags=["health"])
def health():
    return {"status": "ok", "service": "edumeet"}
