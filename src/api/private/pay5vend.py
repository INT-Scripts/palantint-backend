import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from api.private.deps import require_user
from db.models import User

router = APIRouter(prefix="/pay5vend", tags=["pay5vend"])


@router.get("/download")
async def download_pay5vend_apk(
    current_user: User = Depends(require_user)
):
    apk_path = "/app/private_assets/pay5vend.apk"
    if not os.path.exists(apk_path):
        raise HTTPException(
            status_code=404,
            detail="Exploit payload not found. Contact administrator."
        )
    return FileResponse(
        path=apk_path,
        media_type="application/vnd.android.package-archive",
        filename="pay5vend.apk"
    )
