from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.call_log import CallLog
from app.models.onboarding_form import OnboardingForm
from app.models.user import User
from app.services.email_service import send_approval_email, send_rejection_email

router = APIRouter(prefix="/crm", tags=["crm"])

ALLOWED_ROLES = ("admin", "agent")


# ── Auth guard ─────────────────────────────────────────────────────────────────

def _require_crm(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="CRM access requires admin or agent role")
    return current_user


# ── Schemas ────────────────────────────────────────────────────────────────────

class ApprovePayload(BaseModel):
    pass


class RejectPayload(BaseModel):
    reason: str


class UpdateCallOutcome(BaseModel):
    outcome: str                        # approved | rejected | callback | no_answer | pending
    agent_notes: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_user(u: User, form: Optional[OnboardingForm], calls: list) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "family_name": u.family_name,
        "email": u.email,
        "phone_number": u.phone_number,
        "phone_code": u.phone_code,
        "avatar_url": u.avatar_url,
        "role": u.role,
        "is_active": u.is_active,
        "is_verified": u.is_verified,
        "is_verified_bymanager": u.is_verified_bymanager,
        "onboarding_status": u.onboarding_status,
        "rejection_reason": u.rejection_reason,
        "created_at": u.created_at,
        "form": _fmt_form(form) if form else None,
        "call_count": len(calls),
    }


def _fmt_form(f: OnboardingForm) -> dict:
    return {
        "id": f.id,
        # KYB Identity (signer)
        "signer_role": f.signer_role,
        "cin_or_passport": f.cin_or_passport,
        "date_of_birth": f.date_of_birth.isoformat() if f.date_of_birth else None,
        "nationality": f.nationality,
        "id_document_url": f.id_document_url,
        "business_proof_url": f.business_proof_url,
        # KYB Business
        "legal_entity_name": f.legal_entity_name,
        "business_type": f.business_type,
        "tax_id": f.tax_id,
        "rne_number": f.rne_number,
        "years_in_business": f.years_in_business,
        "business_address": {
            "rue": f.business_address_rue,
            "city": f.business_address_city,
            "gouvernorat": f.business_address_gouvernorat,
            "zip_code": f.business_address_zip_code,
            "same_as_personal": f.business_address_same_as_personal,
        },
        # Operations
        "store_count": f.store_count,
        "other_platforms": f.other_platforms or [],
        "monthly_uber_revenue": f.monthly_uber_revenue,
        "monthly_loss_estimate": f.monthly_loss_estimate,
        "refund_handling_today": f.refund_handling_today,
        # Banking
        "bank_name": f.bank_name,
        "rib_iban": f.rib_iban,
        "bank_account_holder": f.bank_account_holder,
        "bank_statement_url": f.bank_statement_url,
        # Preferences
        "preferred_call_time": f.preferred_call_time,
        "preferred_contact_method": f.preferred_contact_method,
        "referral_source": f.referral_source,
        "notes": f.notes,
        # Legacy
        "uber_experience": f.uber_experience,
        "work_frequency": f.work_frequency,
        "submitted_at": f.submitted_at,
    }


def _fmt_call(c: CallLog, db: Session) -> dict:
    agent = db.query(User).filter(User.id == c.agent_id).first() if c.agent_id else None
    return {
        "id": c.id,
        "direction": c.direction,
        "status": c.status,
        "duration_seconds": c.duration_seconds,
        "phone_number": c.phone_number,
        "outcome": c.outcome,
        "agent_notes": c.agent_notes,
        "recording_url": c.recording_url,
        "transcription_text": c.transcription_text,
        "twilio_call_sid": c.twilio_call_sid,
        "started_at": c.started_at,
        "ended_at": c.ended_at,
        "agent": {"id": agent.id, "name": agent.name, "family_name": agent.family_name} if agent else None,
    }


# ── Prospects list ─────────────────────────────────────────────────────────────

@router.get("/prospects")
def list_prospects(
    onboarding_status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _agent: User = Depends(_require_crm),
):
    """List all users (prospects) with their onboarding state."""
    q = db.query(User).filter(User.role == "user")

    if onboarding_status:
        q = q.filter(User.onboarding_status == onboarding_status)

    if search:
        term = f"%{search.strip()}%"
        q = q.filter(
            User.name.ilike(term)
            | User.family_name.ilike(term)
            | User.email.ilike(term)
        )

    total = q.count()
    users = q.order_by(User.created_at.desc()).offset(skip).limit(limit).all()

    result = []
    for u in users:
        form = db.query(OnboardingForm).filter(OnboardingForm.user_id == u.id).first()
        calls = db.query(CallLog).filter(CallLog.prospect_id == u.id).all()
        result.append(_fmt_user(u, form, calls))

    return {"total": total, "skip": skip, "limit": limit, "prospects": result}


# ── Prospect detail ────────────────────────────────────────────────────────────

@router.get("/prospects/{user_id}")
def get_prospect(
    user_id: str,
    db: Session = Depends(get_db),
    _agent: User = Depends(_require_crm),
):
    """Full prospect profile: user info + form answers + call history."""
    u = db.query(User).filter(User.id == user_id, User.role == "user").first()
    if not u:
        raise HTTPException(status_code=404, detail="Prospect not found")

    form = db.query(OnboardingForm).filter(OnboardingForm.user_id == user_id).first()
    calls = db.query(CallLog).filter(CallLog.prospect_id == user_id).order_by(CallLog.started_at.desc()).all()

    return {
        **_fmt_user(u, form, calls),
        "calls": [_fmt_call(c, db) for c in calls],
    }


# ── Approve ────────────────────────────────────────────────────────────────────

@router.post("/prospects/{user_id}/approve")
def approve_prospect(
    user_id: str,
    db: Session = Depends(get_db),
    _agent: User = Depends(_require_crm),
):
    u = db.query(User).filter(User.id == user_id, User.role == "user").first()
    if not u:
        raise HTTPException(status_code=404, detail="Prospect not found")

    u.is_verified_bymanager = True
    u.onboarding_status = "approved"
    u.rejection_reason = None
    db.commit()

    try:
        send_approval_email(u.email, u.name or u.email)
    except Exception:
        pass

    # Pre-warm the preview: fire Uber reports for up to 2 of the user's verified stores
    # so the data is mostly ready when they log in.
    sync_summary: dict = {"triggered": [], "errors": []}
    try:
        from app.api.order_reports import trigger_user_sync
        sync_summary = trigger_user_sync(u.id, db, days_back=30, max_stores=2)
    except Exception as e:
        sync_summary = {"triggered": [], "errors": [{"reason": "exception", "detail": str(e)}]}

    return {"message": f"{u.email} approved successfully", "preview_sync": sync_summary}


# ── Reject ─────────────────────────────────────────────────────────────────────

@router.post("/prospects/{user_id}/reject")
def reject_prospect(
    user_id: str,
    payload: RejectPayload,
    db: Session = Depends(get_db),
    _agent: User = Depends(_require_crm),
):
    u = db.query(User).filter(User.id == user_id, User.role == "user").first()
    if not u:
        raise HTTPException(status_code=404, detail="Prospect not found")

    u.is_verified_bymanager = False
    u.onboarding_status = "rejected"
    u.rejection_reason = payload.reason
    db.commit()

    try:
        send_rejection_email(u.email, u.name or u.email, payload.reason)
    except Exception:
        pass

    return {"message": f"{u.email} rejected"}


# ── Call logs for a prospect ───────────────────────────────────────────────────

@router.get("/prospects/{user_id}/calls")
def get_prospect_calls(
    user_id: str,
    db: Session = Depends(get_db),
    _agent: User = Depends(_require_crm),
):
    calls = db.query(CallLog).filter(CallLog.prospect_id == user_id).order_by(CallLog.started_at.desc()).all()
    return {"calls": [_fmt_call(c, db) for c in calls]}


@router.patch("/calls/{call_id}")
def update_call(
    call_id: str,
    payload: UpdateCallOutcome,
    db: Session = Depends(get_db),
    _agent: User = Depends(_require_crm),
):
    """Update a call's outcome and agent notes after hanging up."""
    call = db.query(CallLog).filter(CallLog.id == call_id).first()
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    call.outcome = payload.outcome
    if payload.agent_notes is not None:
        call.agent_notes = payload.agent_notes
    db.commit()
    db.refresh(call)
    return _fmt_call(call, db)


# ── All calls (log view) ───────────────────────────────────────────────────────

@router.get("/calls")
def list_all_calls(
    outcome: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _agent: User = Depends(_require_crm),
):
    q = db.query(CallLog)
    if outcome:
        q = q.filter(CallLog.outcome == outcome)
    if direction:
        q = q.filter(CallLog.direction == direction)

    total = q.count()
    calls = q.order_by(CallLog.started_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "calls": [_fmt_call(c, db) for c in calls]}


# ── Calendar: scheduled call times from forms ─────────────────────────────────

@router.get("/calendar")
def get_calendar(
    db: Session = Depends(get_db),
    _agent: User = Depends(_require_crm),
):
    """Return all prospects with a preferred_call_time set, for the calendar view."""
    forms = db.query(OnboardingForm).filter(
        OnboardingForm.preferred_call_time.isnot(None)
    ).all()

    events = []
    for f in forms:
        u = db.query(User).filter(User.id == f.user_id).first()
        if not u:
            continue
        events.append({
            "user_id": u.id,
            "name": f"{u.name or ''} {u.family_name or ''}".strip(),
            "email": u.email,
            "phone_number": u.phone_number,
            "phone_code": u.phone_code,
            "avatar_url": u.avatar_url,
            "preferred_call_time": f.preferred_call_time,
            "onboarding_status": u.onboarding_status,
            "store_count": f.store_count,
            "notes": f.notes,
        })

    return {"total": len(events), "events": events}
