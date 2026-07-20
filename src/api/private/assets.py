import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from core.config import settings

router = APIRouter(tags=["assets"])


@router.get("/assets/{path:path}")
async def serve_private_asset(path: str):
    """Securely serve files from the private assets directory under authentication."""
    # Resolve the path to ensure it's within settings.PRIVATE_ASSETS_DIR
    safe_dir = os.path.abspath(settings.PRIVATE_ASSETS_DIR)
    file_path = os.path.abspath(os.path.join(safe_dir, path))
    
    # Directory traversal check
    if not file_path.startswith(safe_dir):
        raise HTTPException(status_code=403, detail="Access denied")
        
    if not os.path.exists(file_path) or not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail="File not found")
        
    return FileResponse(file_path)
