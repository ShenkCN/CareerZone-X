# ============================================================
# 画像路由：读取/保存求职画像、显示名、头像。
# 头像单独上传，不塞进 JSON；数据库只存路径，文件放 storage。
# ============================================================
from __future__ import annotations

import io
import re

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from backend.config import Settings, get_settings
from backend.database import get_db
from backend.deps import get_client_ip, get_current_user, write_audit
from backend.models import User
from backend.routers.auth import _user_out
from backend.schemas import (
    MessageOut,
    ProfileBundle,
    ProfileIn,
    ProfileOut,
    UpdateDisplayNameIn,
    UserOut,
)
from backend.security import new_avatar_filename
from backend.services.profile_service import (
    calculate_completeness,
    ensure_profile,
    get_profile,
    profile_to_dict,
)
from backend.services.text import clean_text

router = APIRouter(prefix="/api/profile", tags=["求职画像"])

# Pillow 防解压炸弹：头像最多 2000 万像素，超过直接拒绝。
Image.MAX_IMAGE_PIXELS = 20_000_000


@router.get("", response_model=ProfileBundle)
def read_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> ProfileBundle:
    """返回画像 + 显示名 + 头像地址；前端据此决定是否引导填写。"""
    profile = get_profile(db, user)
    data = profile_to_dict(profile)
    return ProfileBundle(
        profile=ProfileOut(**data) if profile is not None else None,
        display_name=user.display_name or user.username,
        avatar_url="/api/profile/avatar" if user.avatar_path else "",
        completeness=calculate_completeness(profile),
    )


@router.put("", response_model=ProfileOut)
def save_profile(
    payload: ProfileIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ProfileOut:
    """整表保存；字段允许为空，由前端提示完善度，不在这里强制全填。"""
    profile = ensure_profile(db, user)
    for field, value in payload.model_dump().items():
        setattr(profile, field, value)
    profile.completeness = calculate_completeness(profile)
    write_audit(
        db,
        "profile.save",
        actor_id=user.id,
        target=f"user:{user.id}",
        detail={"completeness": profile.completeness},
        ip=get_client_ip(request),
    )
    db.commit()
    db.refresh(profile)
    return ProfileOut(**profile_to_dict(profile))


@router.put("/display-name", response_model=UserOut)
def update_display_name(
    payload: UpdateDisplayNameIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserOut:
    """显示名 ≠ 登录账号；允许中文/空格，但不能只有空白或控制字符。"""
    name = clean_text(payload.display_name, 64)
    if not name or re.search(r"[\x00-\x1f\x7f]", name):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="显示名不能为空或含控制字符")
    user.display_name = name
    write_audit(db, "profile.display_name", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return _user_out(db, user)


@router.post("/avatar", response_model=MessageOut)
async def upload_avatar(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MessageOut:
    """头像统一裁成 256x256 WebP，既省空间，也避免 SVG 携带脚本。"""
    content = await file.read(settings.max_avatar_bytes + 1)
    await file.close()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件为空")
    if len(content) > settings.max_avatar_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="头像不能超过 2MB")

    try:
        image = Image.open(io.BytesIO(content))
        image.verify()
        # verify 后图像对象不可继续使用，需要重新打开。
        image = Image.open(io.BytesIO(content))
        image = ImageOps.exif_transpose(image)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGB")
        image = ImageOps.fit(image, (256, 256), method=Image.Resampling.LANCZOS)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="无法识别该图片，请上传 PNG/JPG/WebP") from exc

    user_dir = settings.avatar_dir / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = new_avatar_filename("webp")
    target = user_dir / filename
    image.save(target, format="WEBP", quality=88, method=6)

    old = user.avatar_path
    user.avatar_path = target.relative_to(settings.storage_dir).as_posix()
    # 保存成功后再删旧头像，避免新文件写失败导致用户没头像。
    if old:
        old_path = settings.storage_dir / old
        if old_path.exists() and old_path != target:
            old_path.unlink(missing_ok=True)
    write_audit(db, "profile.avatar", actor_id=user.id, target=f"user:{user.id}", ip=get_client_ip(request))
    db.commit()
    return MessageOut(message="头像已更新")


@router.get("/avatar")
def get_avatar(
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
):
    """返回当前用户头像；前端用 requests 取 bytes 再 base64 展示。"""
    if not user.avatar_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="尚未设置头像")
    path = (settings.storage_dir / user.avatar_path).resolve()
    if settings.storage_dir.resolve() not in path.parents or not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="头像文件不存在")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, max-age=300"})
