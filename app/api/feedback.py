from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, conint
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.feedback import Feedback
from app.models.user import User

router = APIRouter(prefix="/feedback", tags=["feedback"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class FeedbackCreate(BaseModel):
    rating: conint(ge=1, le=5)  # type: ignore[valid-type]
    comment: Optional[str] = Field(default=None, max_length=1000)


class FeedbackUpdate(BaseModel):
    rating: Optional[conint(ge=1, le=5)] = None  # type: ignore[valid-type]
    comment: Optional[str] = Field(default=None, max_length=1000)


class AdminPublishUpdate(BaseModel):
    is_published: bool


def _public_response(fb: Feedback, user: User) -> dict:
    return {
        "id": fb.id,
        "rating": fb.rating,
        "comment": fb.comment,
        "created_at": fb.created_at,
        "user": {
            "name": user.name,
            "family_name": user.family_name,
            "avatar_url": user.avatar_url,
            "city": user.address_city,
        },
    }


def _self_response(fb: Feedback) -> dict:
    return {
        "id": fb.id,
        "rating": fb.rating,
        "comment": fb.comment,
        "is_published": fb.is_published,
        "created_at": fb.created_at,
        "updated_at": fb.updated_at,
    }


def _admin_response(fb: Feedback, user: User) -> dict:
    return {
        "id": fb.id,
        "rating": fb.rating,
        "comment": fb.comment,
        "is_published": fb.is_published,
        "created_at": fb.created_at,
        "updated_at": fb.updated_at,
        "user": {
            "id": user.id,
            "name": user.name,
            "family_name": user.family_name,
            "email": user.email,
            "avatar_url": user.avatar_url,
        },
    }


# ── Public ───────────────────────────────────────────────────────────────────

@router.get("/public")
def list_public_feedbacks(
    limit: int = Query(20, ge=1, le=100),
    min_rating: int = Query(1, ge=1, le=5),
    db: Session = Depends(get_db),
):
    """Top published feedbacks for the landing page (no auth)."""
    rows = (
        db.query(Feedback, User)
        .join(User, User.id == Feedback.user_id)
        .filter(Feedback.is_published.is_(True))
        .filter(Feedback.rating >= min_rating)
        .order_by(desc(Feedback.created_at))
        .limit(limit)
        .all()
    )

    agg = (
        db.query(Feedback)
        .filter(Feedback.is_published.is_(True))
        .all()
    )
    total = len(agg)
    avg = round(sum(f.rating for f in agg) / total, 2) if total else 0

    return {
        "total": total,
        "average": avg,
        "feedbacks": [_public_response(fb, u) for fb, u in rows],
    }


# ── Authenticated user (multi-feedback) ──────────────────────────────────────

@router.get("/me")
def list_my_feedbacks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all feedbacks left by the current user, newest first."""
    rows = (
        db.query(Feedback)
        .filter(Feedback.user_id == current_user.id)
        .order_by(desc(Feedback.created_at))
        .all()
    )
    return [_self_response(fb) for fb in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_my_feedback(
    payload: FeedbackCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new feedback. A user can leave as many as they like."""
    fb = Feedback(
        user_id=current_user.id,
        rating=payload.rating,
        comment=payload.comment,
        is_published=True,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return _self_response(fb)


@router.patch("/me/{feedback_id}")
def update_my_feedback(
    feedback_id: str,
    payload: FeedbackUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Edit one of your own feedbacks."""
    fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not fb:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    if fb.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your feedback")

    if payload.rating is not None:
        fb.rating = payload.rating
    if payload.comment is not None:
        fb.comment = payload.comment

    db.commit()
    db.refresh(fb)
    return _self_response(fb)


@router.delete("/me/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_my_feedback(
    feedback_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete one of your own feedbacks by id."""
    fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not fb:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    if fb.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your feedback")
    db.delete(fb)
    db.commit()
    return None


# ── Admin ────────────────────────────────────────────────────────────────────

def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")


@router.get("/admin")
def list_all_feedbacks(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    is_published: Optional[bool] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)

    q = db.query(Feedback, User).join(User, User.id == Feedback.user_id)
    if is_published is not None:
        q = q.filter(Feedback.is_published.is_(is_published))

    total = q.count()
    rows = q.order_by(desc(Feedback.created_at)).offset(skip).limit(limit).all()

    return {
        "total": total,
        "feedbacks": [_admin_response(fb, u) for fb, u in rows],
    }


@router.patch("/admin/{feedback_id}")
def update_feedback_publication(
    feedback_id: str,
    payload: AdminPublishUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)
    fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not fb:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    fb.is_published = payload.is_published
    db.commit()
    db.refresh(fb)
    return _self_response(fb)


@router.delete("/admin/{feedback_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_feedback(
    feedback_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(current_user)
    fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
    if not fb:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found")
    db.delete(fb)
    db.commit()
    return None
