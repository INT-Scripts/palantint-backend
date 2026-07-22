import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from api.private.admin import router as admin_router
from api.private.agenda import router as agenda_router

# Auth router — mounted at root, no prefix, no auth dependency
from api.private.auth import router as auth_router
from api.private.class_groups import router as class_groups_router
from api.private.clubs import router as clubs_router

# Private router and dependencies
from api.private.deps import require_user, require_user_query_token
from api.private.graph import router as graph_router
from api.private.maps import router as maps_router
from api.private.media import router as media_router
from api.private.pay5vend import router as pay5vend_router
from api.private.relationships import router as relationships_router
from api.private.search import router as search_router
from api.private.students import router as students_router
from api.private.users import router as users_router
from api.private.assets import router as assets_router
from api.private.notifications import router as notifications_router
from api.public.class_groups import router as pub_class_groups_router
from api.public.clubs import router as pub_clubs_router
from api.public.laundry import router as pub_laundry_router
from api.public.maps import router as pub_maps_router
from api.public.search import router as pub_search_router
from api.public.students import router as pub_students_router
from core.config import settings

# Public router and dependencies
from core.rate_limit import rate_limit_dep
from db.database import init_db
from mcp_server import mcp


# Ensure necessary directories exist before mounting static files
settings.ASSETS_DIR.mkdir(parents=True, exist_ok=True)
settings.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
settings.PROFILES_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Execute on startup
    await init_db()
    
    import asyncio
    from api.private.notifications import laundry_notifier_loop
    notifier_task = asyncio.create_task(laundry_notifier_loop())
    
    async with mcp.lifespan():
        yield
        
    # Execute on shutdown
    notifier_task.cancel()
    try:
        await notifier_task
    except asyncio.CancelledError:
        pass


is_dev = settings.ENVIRONMENT == "development"

app = FastAPI(
    title="PalantINT API",
    version="1.0.0",
    lifespan=lifespan,
    root_path="/api",
    docs_url="/docs" if is_dev else None,
    redoc_url="/redoc" if is_dev else None,
    openapi_url="/openapi.json" if is_dev else None,
)

# 1. Auth router (POST /auth/login) - mounted at root
app.include_router(auth_router)

# 2. Private router (prefix /private) - requires user authentication (supports query tokens)
private_router = APIRouter(
    prefix="/private",
    dependencies=[Depends(require_user_query_token)]
)
private_router.include_router(users_router)
private_router.include_router(pay5vend_router)
private_router.include_router(admin_router)
private_router.include_router(students_router)
private_router.include_router(clubs_router)
private_router.include_router(agenda_router)
private_router.include_router(relationships_router)
private_router.include_router(media_router)
private_router.include_router(search_router)
private_router.include_router(maps_router)
private_router.include_router(graph_router)
private_router.include_router(class_groups_router)
private_router.include_router(assets_router)
private_router.include_router(notifications_router)
app.include_router(private_router)

# 3. Public router (prefix "") - rate-limited, no authentication required
public_router = APIRouter(
    prefix="",
    dependencies=[Depends(rate_limit_dep)]
)
public_router.include_router(pub_clubs_router)
public_router.include_router(pub_class_groups_router)
public_router.include_router(pub_students_router)
public_router.include_router(pub_laundry_router)
public_router.include_router(pub_search_router)
public_router.include_router(pub_maps_router)
# Mount the public static assets folder under /public/assets
app.mount("/assets", StaticFiles(directory=str(settings.ASSETS_DIR)), name="assets")
app.include_router(public_router)

app.mount("/mcp", mcp.http_app(transport="sse"))


@app.get("/")
async def root():
    return {"message": "Welcome to the PalantINT API"}


@app.get("/health")
async def health_check():
    return {"status": "ok"}
