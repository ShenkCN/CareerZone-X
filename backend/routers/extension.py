# ============================================================
# 浏览器扩展下载：把 extension/ 打包成 ZIP，用户不用去文件夹里翻。
# 权限：登录用户即可下载；ZIP 不含任何令牌/账号数据。
# ============================================================
from __future__ import annotations

import io
import zipfile

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.config import ROOT_DIR
from backend.deps import get_current_user
from backend.models import User

router = APIRouter(prefix="/api/extension", tags=["浏览器扩展"])
EXTENSION_DIR = ROOT_DIR / "extension"


@router.get("/download")
def download_extension(user: User = Depends(get_current_user)):
    """生成扩展 ZIP；解压后得到 careerzone-extension/ 文件夹。

    为什么在内存里打包：文件很小（<100KB），不需要写临时文件，
    也避免把 ZIP 暴露成静态 URL。
    """
    if not EXTENSION_DIR.exists():
        raise FileNotFoundError("extension 目录不存在")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if path.is_file() and path.suffix.lower() != ".zip":
                archive.write(path, f"careerzone-extension/{path.relative_to(EXTENSION_DIR).as_posix()}")
    payload = buffer.getvalue()
    return StreamingResponse(
        iter([payload]),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="careerzone-extension.zip"',
            "Cache-Control": "private, no-store",
        },
    )
