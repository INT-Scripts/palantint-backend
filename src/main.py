from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.routes import router as auth_router
from db.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Execute on startup
    await init_db()
    yield
    # Execute on shutdown


app = FastAPI(
    title="PalantINT API",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/api",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)

from api.api_agenda import router as agenda_router
from api.api_clubs import router as club_router
from api.api_media import router as media_router
from api.api_relationships import router as relationship_router
from api.api_students import router as student_router
from api.api_search import router as search_router
from api.api_maps import router as maps_router
from api.api_graph import router as graph_router

app.include_router(student_router)
app.include_router(relationship_router)
app.include_router(club_router)
app.include_router(media_router)
app.include_router(agenda_router)
app.include_router(search_router)
app.include_router(maps_router)
app.include_router(graph_router)

# Mount the static assets folder
import os
ASSETS_DIR = "/app/assets"
if not os.path.exists(ASSETS_DIR):
    os.makedirs(ASSETS_DIR, exist_ok=True)
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

SCRAPS_DIR = "/app/scraps"
if not os.path.exists(SCRAPS_DIR):
    os.makedirs(SCRAPS_DIR, exist_ok=True)
app.mount("/scraps", StaticFiles(directory=SCRAPS_DIR), name="scraps")


@app.get("/")
async def root():
    return {"message": "Welcome to the PalantINT API"}


@app.get("/health")
async def health_check():
    return {"status": "ok"}
