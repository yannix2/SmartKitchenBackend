import uuid
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.contested_order import ContestedOrder
from app.models.onboarding_form import OnboardingForm
from app.models.reported_order import ReportedOrder
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.store_refund import StoreRefund
from app.models.user import User
from app.models.user_store import STATUS_VERIFIED, UserStore
from app.services.email_service import send_onboarding_received_email
from app.services.supabase_storage import upload_file

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


KYC_BUCKET = "kyc-documents"
ALLOWED_DOC_KINDS = {"id_document", "business_proof", "bank_statement"}
ALLOWED_MIME = {
    "image/jpeg", "image/png", "image/webp", "application/pdf",
}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


# ── Schemas ────────────────────────────────────────────────────────────────────

class OnboardingFormSubmit(BaseModel):
    # KYB — Business identity
    legal_entity_name: Optional[str] = None
    business_type: Optional[str] = None
    tax_id: Optional[str] = None
    rne_number: Optional[str] = None
    years_in_business: Optional[int] = None
    business_address_rue: Optional[str] = None
    business_address_city: Optional[str] = None
    business_address_gouvernorat: Optional[str] = None
    business_address_zip_code: Optional[str] = None
    business_address_same_as_personal: Optional[str] = None

    # KYB — Operations
    store_count: Optional[int] = None
    other_platforms: Optional[List[str]] = None
    monthly_uber_revenue: Optional[str] = None
    monthly_loss_estimate: Optional[str] = None
    refund_handling_today: Optional[str] = None

    # KYC — Personal identity
    signer_role: Optional[str] = None
    cin_or_passport: Optional[str] = None
    date_of_birth: Optional[date] = None
    nationality: Optional[str] = None
    id_document_url: Optional[str] = None
    business_proof_url: Optional[str] = None

    # KYC — Banking
    bank_name: Optional[str] = None
    rib_iban: Optional[str] = None
    bank_account_holder: Optional[str] = None
    bank_statement_url: Optional[str] = None

    # Operational preferences
    preferred_call_time: Optional[str] = None
    preferred_contact_method: Optional[str] = None
    referral_source: Optional[str] = None
    notes: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_form(f: OnboardingForm) -> dict:
    return {
        "id": f.id,
        "user_id": f.user_id,
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
        "store_count": f.store_count,
        "other_platforms": f.other_platforms or [],
        "monthly_uber_revenue": f.monthly_uber_revenue,
        "monthly_loss_estimate": f.monthly_loss_estimate,
        "refund_handling_today": f.refund_handling_today,
        "signer_role": f.signer_role,
        "cin_or_passport": f.cin_or_passport,
        "date_of_birth": f.date_of_birth.isoformat() if f.date_of_birth else None,
        "nationality": f.nationality,
        "id_document_url": f.id_document_url,
        "business_proof_url": f.business_proof_url,
        "bank_name": f.bank_name,
        "rib_iban": f.rib_iban,
        "bank_account_holder": f.bank_account_holder,
        "bank_statement_url": f.bank_statement_url,
        "preferred_call_time": f.preferred_call_time,
        "preferred_contact_method": f.preferred_contact_method,
        "referral_source": f.referral_source,
        "notes": f.notes,
        "submitted_at": f.submitted_at,
        "updated_at": f.updated_at,
    }


def _validate_required(payload: OnboardingFormSubmit) -> None:
    """Enforce server-side requirements before persisting the final submission."""
    base_required = [
        ("legal_entity_name", payload.legal_entity_name),
        ("business_type", payload.business_type),
        ("tax_id", payload.tax_id),
        ("rne_number", payload.rne_number),
        ("years_in_business", payload.years_in_business),
        ("store_count", payload.store_count),
        ("monthly_uber_revenue", payload.monthly_uber_revenue),
        ("monthly_loss_estimate", payload.monthly_loss_estimate),
        ("refund_handling_today", payload.refund_handling_today),
        ("signer_role", payload.signer_role),
        ("cin_or_passport", payload.cin_or_passport),
        ("date_of_birth", payload.date_of_birth),
        ("nationality", payload.nationality),
        ("id_document_url", payload.id_document_url),
        ("business_proof_url", payload.business_proof_url),
        ("preferred_call_time", payload.preferred_call_time),
        ("preferred_contact_method", payload.preferred_contact_method),
    ]
    missing = [name for name, val in base_required if val in (None, "")]

    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Missing required fields: {', '.join(missing)}",
        )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/status")
def get_onboarding_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the current user's onboarding status and form (if submitted)."""
    form = db.query(OnboardingForm).filter(OnboardingForm.user_id == current_user.id).first()
    return {
        "onboarding_status": current_user.onboarding_status,
        "is_verified_bymanager": current_user.is_verified_bymanager,
        "rejection_reason": current_user.rejection_reason,
        "form": _fmt_form(form) if form else None,
    }


@router.post("/upload-document")
def upload_document(
    kind: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """
    Upload a KYC/KYB document to Supabase Storage and return its public URL.
    `kind` must be one of: id_document, business_proof, bank_statement.
    """
    if kind not in ALLOWED_DOC_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid kind. Allowed: {ALLOWED_DOC_KINDS}")

    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}")

    data = file.file.read()
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    ext = (file.filename or "").rsplit(".", 1)[-1].lower() or "bin"
    path = f"{current_user.id}/{kind}_{uuid.uuid4().hex[:8]}.{ext}"

    try:
        url = upload_file(KYC_BUCKET, path, data, file.content_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Storage error: {e}")

    return {"kind": kind, "url": url, "path": path}


@router.post("/form", status_code=status.HTTP_201_CREATED)
def submit_form(
    payload: OnboardingFormSubmit,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit or update the onboarding form. Moves status to pending_call."""
    if current_user.is_verified_bymanager:
        raise HTTPException(status_code=400, detail="Account is already approved")

    _validate_required(payload)

    form = db.query(OnboardingForm).filter(OnboardingForm.user_id == current_user.id).first()
    data = payload.model_dump(exclude_none=True)

    if form:
        for field, val in data.items():
            setattr(form, field, val)
    else:
        form = OnboardingForm(user_id=current_user.id, **data)
        db.add(form)

    current_user.onboarding_status = "pending_call"
    db.commit()
    db.refresh(form)

    try:
        send_onboarding_received_email(current_user.email, current_user.name or current_user.email)
    except Exception:
        pass

    return {"message": "Form submitted successfully", "form": _fmt_form(form)}


@router.patch("/form")
def patch_form(
    payload: OnboardingFormSubmit,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Save partial form progress without enforcing requirements (used between wizard steps).
    Does NOT change onboarding_status.
    """
    if current_user.is_verified_bymanager:
        raise HTTPException(status_code=400, detail="Account is already approved")

    form = db.query(OnboardingForm).filter(OnboardingForm.user_id == current_user.id).first()
    data = payload.model_dump(exclude_none=True)

    if form:
        for field, val in data.items():
            setattr(form, field, val)
    else:
        form = OnboardingForm(user_id=current_user.id, **data)
        db.add(form)

    db.commit()
    db.refresh(form)
    return {"form": _fmt_form(form)}


# ── Preview (post-approval, pre-subscription) ─────────────────────────────────

CONTESTED_COMMISSION = 0.20
CANCELLED_COMMISSION = 0.15
RECOVERY_RATES = (0.85, 0.90)
PREVIEW_MAX_STORES = 2


def _parse_amount(s) -> float:
    try:
        return abs(float((s or "0").replace(",", ".")))
    except (ValueError, TypeError):
        return 0.0


@router.get("/preview/stores")
def preview_stores(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return the user's verified stores that are integrated in SmartKitchen.
    Used as the picker on the preview page (max 2 selectable).
    """
    active_sk_ids = {
        s.store_id for s in
        db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
    }
    user_stores = (
        db.query(UserStore)
        .filter(UserStore.user_id == current_user.id, UserStore.status == STATUS_VERIFIED)
        .all()
    )
    stores = [
        {"store_id": s.store_id, "store_name": s.store_name or s.store_id, "integrated": s.store_id in active_sk_ids}
        for s in user_stores
    ]
    return {"stores": stores, "max_selectable": PREVIEW_MAX_STORES}


@router.get("/preview")
def preview(
    store_ids: List[str] = Query(default_factory=list, description="Up to 2 store_ids the user picked."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Show an approved-but-unsubscribed user how much they lost in the last 30 days
    and how much they would have recovered (and netted) with SmartKitchen.

    Aggregation source: ContestedOrder + ReportedOrder + StoreRefund already in the DB
    (filled by the user-triggered /order-reports/get-cancelled and /get-contested calls).

    Returns:
      - lost_total_eur
      - recovered_at_85, recovered_at_90 (gross)
      - net_at_85, net_at_90 (after SK commission per category)
      - sample_contested_order, sample_cancelled_order
      - data_ready (true if at least one order was found)
    """
    if not current_user.is_verified_bymanager:
        raise HTTPException(status_code=403, detail="Account not yet approved")

    # Resolve which stores to use
    active_sk_ids = {
        s.store_id for s in
        db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
    }
    verified = (
        db.query(UserStore)
        .filter(UserStore.user_id == current_user.id, UserStore.status == STATUS_VERIFIED)
        .all()
    )
    verified_integrated = [s for s in verified if s.store_id in active_sk_ids]

    if store_ids:
        chosen = [s for s in verified_integrated if s.store_id in store_ids][:PREVIEW_MAX_STORES]
    else:
        chosen = verified_integrated[:PREVIEW_MAX_STORES]

    if not chosen:
        return {
            "data_ready": False,
            "stores": [],
            "lost_total_eur": 0.0,
            "contested_amount": 0.0,
            "cancelled_amount": 0.0,
            "recovered_at_85": 0.0,
            "recovered_at_90": 0.0,
            "net_at_85": 0.0,
            "net_at_90": 0.0,
            "sample_contested_order": None,
            "sample_cancelled_order": None,
            "period_days": 30,
        }

    chosen_ids = [s.store_id for s in chosen]
    since = datetime.now(timezone.utc) - timedelta(days=30)

    # ── Contested (last 30 days) ──
    contested_q = (
        db.query(ContestedOrder)
        .filter(
            ContestedOrder.store_id.in_(chosen_ids),
            ContestedOrder.fetched_at >= since,
        )
    )
    contested_orders = contested_q.all()
    contested_amount = sum(_parse_amount(o.refund_covered_by_merchant) for o in contested_orders)

    # ── Cancelled (last 30 days) ──
    cancelled_q = (
        db.query(ReportedOrder)
        .filter(
            ReportedOrder.store_id.in_(chosen_ids),
            ReportedOrder.fetched_at >= since,
        )
    )
    cancelled_orders = cancelled_q.all()

    # Cancelled orders don't carry an explicit per-order amount.
    # Estimate the lost amount from matching unlinked StoreRefunds (same approach
    # the wallet uses for "cancelled" revenue).
    cancelled_amount = 0.0
    if cancelled_orders:
        unlinked_refunds = (
            db.query(StoreRefund)
            .filter(
                StoreRefund.store_id.in_(chosen_ids),
                StoreRefund.linked_order_id.is_(None),
                StoreRefund.fetched_at >= since,
            )
            .all()
        )
        cancelled_amount = sum(_parse_amount(r.amount) for r in unlinked_refunds)

    lost_total = contested_amount + cancelled_amount

    def _scenario(rate: float) -> tuple[float, float]:
        gross = lost_total * rate
        # Apply commission split proportional to source breakdown
        commission = (
            contested_amount * rate * CONTESTED_COMMISSION
            + cancelled_amount * rate * CANCELLED_COMMISSION
        )
        net = gross - commission
        return round(gross, 2), round(net, 2)

    rec85_gross, rec85_net = _scenario(RECOVERY_RATES[0])
    rec90_gross, rec90_net = _scenario(RECOVERY_RATES[1])

    # ── Pick samples (most recent of each) ──
    sample_contested = None
    if contested_orders:
        c = max(contested_orders, key=lambda o: o.fetched_at or datetime.min.replace(tzinfo=timezone.utc))
        sample_contested = {
            "order_id": c.order_id,
            "store_name": c.store_name,
            "amount": _parse_amount(c.refund_covered_by_merchant),
            "issue": c.order_issue,
            "date": c.time_customer_ordered,
        }

    sample_cancelled = None
    if cancelled_orders:
        c = max(cancelled_orders, key=lambda o: o.fetched_at or datetime.min.replace(tzinfo=timezone.utc))
        sample_cancelled = {
            "order_id": c.order_id,
            "store_name": c.store_name,
            "date": c.date_ordered,
            "status": c.order_status,
        }

    data_ready = bool(contested_orders or cancelled_orders)

    return {
        "data_ready": data_ready,
        "period_days": 30,
        "stores": [{"store_id": s.store_id, "store_name": s.store_name or s.store_id} for s in chosen],
        "contested_count": len(contested_orders),
        "cancelled_count": len(cancelled_orders),
        "contested_amount": round(contested_amount, 2),
        "cancelled_amount": round(cancelled_amount, 2),
        "lost_total_eur": round(lost_total, 2),
        "recovered_at_85": rec85_gross,
        "net_at_85": rec85_net,
        "recovered_at_90": rec90_gross,
        "net_at_90": rec90_net,
        "commission_rates": {"contested": CONTESTED_COMMISSION, "cancelled": CANCELLED_COMMISSION},
        "sample_contested_order": sample_contested,
        "sample_cancelled_order": sample_cancelled,
    }
