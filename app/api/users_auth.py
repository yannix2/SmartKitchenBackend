import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.db.session import get_db
from app.models.user import User
from app.models.user_store import UserStore
from app.services.email_service import send_verification_email, send_reset_password_email

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class AddressSchema(BaseModel):
    rue: Optional[str] = None
    city: Optional[str] = None
    gouvernorat: Optional[str] = None
    zip_code: Optional[str] = None


class RegisterRequest(BaseModel):
    name: str
    family_name: str
    email: EmailStr
    password: str
    phone_number: Optional[str] = None
    phone_code: Optional[str] = None  # e.g. "+216"
    address: Optional[AddressSchema] = None
    role: str = "user"  # user | admin | manager


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


# ── Admin bulk schemas ─────────────────────────────────────────────────────────

class BulkUserCreateItem(BaseModel):
    name: str
    family_name: str
    email: EmailStr
    password: str
    phone_number: Optional[str] = None
    phone_code: Optional[str] = None
    address: Optional[AddressSchema] = None
    role: str = "user"


class BulkUpdateItem(BaseModel):
    user_id: str
    name: Optional[str] = None
    family_name: Optional[str] = None
    phone_number: Optional[str] = None
    phone_code: Optional[str] = None
    address: Optional[AddressSchema] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class BulkIdsRequest(BaseModel):
    user_ids: List[str]


# ── Dependency: admin only ─────────────────────────────────────────────────────

def _require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


# ── Swagger-compatible token endpoint ─────────────────────────────────────────

@router.post("/token", include_in_schema=False)
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    OAuth2 password flow endpoint used by Swagger UI.
    Accepts form fields: username (= email) + password.
    """
    user = db.query(User).filter(User.email == form.username).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your inbox.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Please contact support.",
        )

    data = {"sub": str(user.id)}
    return {
        "access_token": create_access_token(data),
        "refresh_token": create_refresh_token(data),
        "token_type": "bearer",
    }


# ── Register ───────────────────────────────────────────────────────────────────

@router.post("/register", status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """
    Create a new user account.
    The account starts as inactive and unverified.
    A verification link is sent to the provided email.
    """
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    verification_token = secrets.token_urlsafe(32)
    verification_expires = datetime.now(timezone.utc) + timedelta(hours=24)

    user = User(
        name=payload.name,
        family_name=payload.family_name,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        phone_number=payload.phone_number,
        phone_code=payload.phone_code,
        address_rue=payload.address.rue if payload.address else None,
        address_city=payload.address.city if payload.address else None,
        address_gouvernorat=payload.address.gouvernorat if payload.address else None,
        address_zip_code=payload.address.zip_code if payload.address else None,
        role=payload.role,
        is_active=False,
        is_verified=False,
        verification_token=verification_token,
        verification_token_expires=verification_expires,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    send_verification_email(user.email, user.name, verification_token)

    return {
        "message": "Registration successful. Please check your email to verify your account."
    }


# ── Verify email ───────────────────────────────────────────────────────────────

@router.get("/verify-email")
def verify_email(token: str = Query(...), db: Session = Depends(get_db)):
    """
    Activate a user account via the token sent by email.
    Once verified, is_active and is_verified are set to True.
    """
    user = db.query(User).filter(User.verification_token == token).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification token",
        )

    expires = user.verification_token_expires
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    if not expires or datetime.now(timezone.utc) > expires:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification token has expired. Please register again or request a new link.",
        )

    user.is_verified = True
    user.is_active = True
    user.verification_token = None
    user.verification_token_expires = None
    db.commit()

    return {"message": "Email verified successfully. Your account is now active."}


# ── Login ──────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate a user and return access + refresh tokens.
    Only verified and active accounts can log in.
    """
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified. Please check your inbox.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Please contact support.",
        )

    data = {"sub": str(user.id)}
    return TokenResponse(
        access_token=create_access_token(data),
        refresh_token=create_refresh_token(data),
    )


# ── Refresh token ──────────────────────────────────────────────────────────────

@router.post("/refresh", response_model=AccessTokenResponse)
def refresh_access_token(payload: RefreshRequest, db: Session = Depends(get_db)):
    """
    Issue a new access token using a valid refresh token.
    """
    token_data = decode_refresh_token(payload.refresh_token)
    user_id = token_data.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_verified or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is not active or verified",
        )

    return AccessTokenResponse(access_token=create_access_token({"sub": str(user.id)}))


# ── Logout ─────────────────────────────────────────────────────────────────────

@router.post("/logout")
def logout():
    """
    Stateless logout — the client should discard both tokens.
    """
    return {"message": "Logged out successfully"}


# ── Forgot password ────────────────────────────────────────────────────────────

@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Send a password-reset link to the user's email.
    Always returns 200 to prevent email enumeration.
    """
    user = db.query(User).filter(User.email == payload.email).first()
    if user:
        reset_token = secrets.token_urlsafe(32)
        user.reset_password_token = reset_token
        user.reset_password_token_expires = datetime.now(timezone.utc) + timedelta(hours=1)
        db.commit()
        send_reset_password_email(user.email, user.name, reset_token)

    return {
        "message": "If this email is registered, a password reset link has been sent."
    }


# ── Reset password ─────────────────────────────────────────────────────────────

@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    """
    Set a new password using the token received by email.
    The token expires after 1 hour.
    """
    user = db.query(User).filter(User.reset_password_token == payload.token).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    expires = user.reset_password_token_expires
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    if not expires or datetime.now(timezone.utc) > expires:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token has expired. Please request a new one.",
        )

    user.hashed_password = hash_password(payload.new_password)
    user.reset_password_token = None
    user.reset_password_token_expires = None
    db.commit()

    return {"message": "Password reset successfully. You can now log in with your new password."}


# ── Admin: activate / deactivate accounts ─────────────────────────────────────

# ── Admin: bulk CRUD ───────────────────────────────────────────────────────────

@router.get("/admin/users")
def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    role: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — list all users with optional filters and pagination."""
    q = db.query(User)
    if role is not None:
        q = q.filter(User.role == role)
    if is_active is not None:
        q = q.filter(User.is_active == is_active)
    total = q.count()
    users = q.offset(skip).limit(limit).all()
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "users": [
            {
                "id": u.id,
                "name": u.name,
                "family_name": u.family_name,
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "is_verified": u.is_verified,
                "created_at": u.created_at,
            }
            for u in users
        ],
    }


@router.post("/admin/users/bulk-create", status_code=status.HTTP_201_CREATED)
def bulk_create_users(
    payload: List[BulkUserCreateItem],
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — create multiple users at once. Skips duplicates and reports them."""
    created, skipped = [], []
    for item in payload:
        if db.query(User).filter(User.email == item.email).first():
            skipped.append(item.email)
            continue
        user = User(
            name=item.name,
            family_name=item.family_name,
            email=item.email,
            hashed_password=hash_password(item.password),
            phone_number=item.phone_number,
            phone_code=item.phone_code,
            address_rue=item.address.rue if item.address else None,
            address_city=item.address.city if item.address else None,
            address_gouvernorat=item.address.gouvernorat if item.address else None,
            address_zip_code=item.address.zip_code if item.address else None,
            role=item.role,
            is_active=True,
            is_verified=True,
        )
        db.add(user)
        created.append(item.email)
    db.commit()
    return {"created": created, "skipped_duplicates": skipped}


@router.patch("/admin/users/bulk-update")
def bulk_update_users(
    payload: List[BulkUpdateItem],
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — update fields on multiple users at once."""
    updated, not_found = [], []
    for item in payload:
        user = db.query(User).filter(User.id == item.user_id).first()
        if not user:
            not_found.append(item.user_id)
            continue
        if item.name is not None:
            user.name = item.name
        if item.family_name is not None:
            user.family_name = item.family_name
        if item.phone_number is not None:
            user.phone_number = item.phone_number
        if item.phone_code is not None:
            user.phone_code = item.phone_code
        if item.role is not None:
            user.role = item.role
        if item.is_active is not None:
            user.is_active = item.is_active
        if item.address is not None:
            user.address_rue = item.address.rue
            user.address_city = item.address.city
            user.address_gouvernorat = item.address.gouvernorat
            user.address_zip_code = item.address.zip_code
        updated.append(item.user_id)
    db.commit()
    return {"updated": updated, "not_found": not_found}


@router.post("/admin/users/bulk-activate")
def bulk_activate_users(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — activate multiple user accounts at once."""
    updated = db.query(User).filter(User.id.in_(payload.user_ids)).all()
    for u in updated:
        u.is_active = True
    db.commit()
    return {"activated": [u.id for u in updated], "count": len(updated)}


@router.post("/admin/users/bulk-deactivate")
def bulk_deactivate_users(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — deactivate multiple user accounts at once."""
    updated = db.query(User).filter(User.id.in_(payload.user_ids)).all()
    for u in updated:
        u.is_active = False
    db.commit()
    return {"deactivated": [u.id for u in updated], "count": len(updated)}


@router.delete("/admin/users/bulk-delete", status_code=status.HTTP_200_OK)
def bulk_delete_users(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — permanently delete multiple users."""
    users = db.query(User).filter(User.id.in_(payload.user_ids)).all()
    deleted = [u.id for u in users]
    for u in users:
        db.delete(u)
    db.commit()
    not_found = [uid for uid in payload.user_ids if uid not in deleted]
    return {"deleted": deleted, "not_found": not_found}


@router.get("/admin/users/{user_id}")
def get_user_detail(
    user_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — full profile for a single user including their linked stores."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    stores = db.query(UserStore).filter(UserStore.user_id == user_id).all()

    return {
        "id": user.id,
        "name": user.name,
        "family_name": user.family_name,
        "email": user.email,
        "phone_number": user.phone_number,
        "phone_code": user.phone_code,
        "avatar_url": user.avatar_url,
        "abonnement_id": user.abonnement_id,
        "role": user.role,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "address": {
            "rue": user.address_rue,
            "city": user.address_city,
            "gouvernorat": user.address_gouvernorat,
            "zip_code": user.address_zip_code,
        },
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "stores": [
            {
                "id": s.id,
                "store_id": s.store_id,
                "store_name": s.store_name,
                "status": s.status,
                "linked_at": s.linked_at,
            }
            for s in stores
        ],
    }


@router.post("/admin/users/{user_id}/activate")
def activate_user(
    user_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — activate a user account."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.is_active = True
    db.commit()
    return {"message": f"User {user.email} has been activated"}


@router.post("/admin/users/{user_id}/deactivate")
def deactivate_user(
    user_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — deactivate a user account."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.is_active = False
    db.commit()
    return {"message": f"User {user_id} has been deactivated"}

@router.get("/me")
def get_current_user(current_user: User = Depends(get_current_user)):
    """Get the current authenticated user's profile."""
    return {
        "id": current_user.id,
        "name": current_user.name,
        "family_name": current_user.family_name,
        "email": current_user.email,
        "phone_number": current_user.phone_number,
        "phone_code": current_user.phone_code,
        "address": {
            "rue": current_user.address_rue,
            "city": current_user.address_city,
            "gouvernorat": current_user.address_gouvernorat,
            "zip_code": current_user.address_zip_code,
        },
        "role": current_user.role,
        "is_active": current_user.is_active,
        "is_verified": current_user.is_verified,
        "created_at": current_user.created_at,
    }