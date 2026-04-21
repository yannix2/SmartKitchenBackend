from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.user import User
from app.services.supabase_storage import upload_file

router = APIRouter(prefix="/profile", tags=["profile"])

_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5 MB


# ── Schemas ────────────────────────────────────────────────────────────────────

class AddressUpdate(BaseModel):
    rue: Optional[str] = None
    city: Optional[str] = None
    gouvernorat: Optional[str] = None
    zip_code: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    family_name: Optional[str] = None
    phone_number: Optional[str] = None
    phone_code: Optional[str] = None
    address: Optional[AddressUpdate] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _profile_response(user: User) -> dict:
    return {
        "id": user.id,
        "name": user.name,
        "family_name": user.family_name,
        "email": user.email,
        "phone_number": user.phone_number,
        "phone_code": user.phone_code,
        "address": {
            "rue": user.address_rue,
            "city": user.address_city,
            "gouvernorat": user.address_gouvernorat,
            "zip_code": user.address_zip_code,
        },
        "avatar_url": user.avatar_url,
        "role": user.role,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("")
def get_profile(current_user: User = Depends(get_current_user)):
    """Return the current user's profile."""
    return _profile_response(current_user)


@router.patch("")
def update_profile(
    payload: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update editable profile fields. Email cannot be changed."""
    if payload.name is not None:
        current_user.name = payload.name
    if payload.family_name is not None:
        current_user.family_name = payload.family_name
    if payload.phone_number is not None:
        current_user.phone_number = payload.phone_number
    if payload.phone_code is not None:
        current_user.phone_code = payload.phone_code
    if payload.address is not None:
        if payload.address.rue is not None:
            current_user.address_rue = payload.address.rue
        if payload.address.city is not None:
            current_user.address_city = payload.address.city
        if payload.address.gouvernorat is not None:
            current_user.address_gouvernorat = payload.address.gouvernorat
        if payload.address.zip_code is not None:
            current_user.address_zip_code = payload.address.zip_code

    db.commit()
    db.refresh(current_user)
    return _profile_response(current_user)


@router.post("/avatar", status_code=status.HTTP_200_OK)
async def upload_avatar(
    file: UploadFile,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload or replace the current user's profile photo."""
    content_type = file.content_type or ""
    if content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{content_type}'. Allowed: jpeg, png, webp",
        )

    data = await file.read()
    if len(data) > _MAX_AVATAR_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File too large. Maximum size is 5 MB.",
        )

    ext = content_type.split("/")[-1]
    path = f"{current_user.id}.{ext}"

    try:
        public_url = upload_file(bucket="avatars", path=path, data=data, content_type=content_type)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Storage upload failed: {e}",
        )

    current_user.avatar_url = public_url
    db.commit()

    return {"avatar_url": public_url}
