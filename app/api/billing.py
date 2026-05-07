import os
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.db.session import get_db
from app.models.abonnement import Abonnement
from app.models.user import User

router = APIRouter(prefix="/billing", tags=["billing"])

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# One plan for now
PLAN = {
    "name": "Pro",
    "price": 49.99,
    "currency": "eur",
    "interval": "month",
    "features": [
        "Cancelled order refund tracking",
        "Contested order refund tracking",
        "Automated refund email campaigns",
        "Store performance dashboard",
        "Monthly revenue reports",
        "Priority support",
    ],
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_stripe():
    """Lazy import stripe and validate key."""
    try:
        import stripe as _stripe
    except ImportError:
        raise HTTPException(status_code=503, detail="Payment library not installed")
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="Payment service not configured")
    _stripe.api_key = key
    return _stripe


def _get_or_create_abonnement(user_id: str, db: Session) -> Abonnement:
    ab = db.query(Abonnement).filter(Abonnement.user_id == user_id).first()
    if not ab:
        ab = Abonnement(user_id=user_id)
        db.add(ab)
        db.commit()
        db.refresh(ab)
    return ab


STAFF_ROLES = {"admin", "agent"}

def _is_subscribed(user: User, db: Session) -> bool:
    """Staff roles are always considered subscribed; users check their abonnement."""
    if user.role in STAFF_ROLES:
        return True
    ab = db.query(Abonnement).filter(Abonnement.user_id == user.id).first()
    # "cancelling" = cancel scheduled at period end; user still has access
    return bool(ab and ab.status in ("active", "cancelling"))


# ── Public plan info ───────────────────────────────────────────────────────────

@router.get("/plans")
def get_plans():
    return {"plans": [PLAN]}


# ── Subscription status ────────────────────────────────────────────────────────

@router.get("/status")
def get_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role in STAFF_ROLES:
        return {
            "is_subscribed": True,
            "status": "active",
            "cancel_at_period_end": False,
            "can_resubscribe": False,
            "plan": {"name": current_user.role.capitalize(), "price": None, "currency": "eur", "started_at": None, "expires_at": None},
        }

    ab = db.query(Abonnement).filter(Abonnement.user_id == current_user.id).first()
    if not ab:
        return {
            "is_subscribed": False, "status": "inactive",
            "cancel_at_period_end": False, "can_resubscribe": True,
            "plan": None, "stripe_subscription_id": None,
        }

    # can_resubscribe = true only when period has fully expired
    can_resub = False
    if ab.status in ("cancelled", "inactive") or not ab.status:
        if ab.expires_at:
            exp = ab.expires_at.replace(tzinfo=timezone.utc) if ab.expires_at.tzinfo is None else ab.expires_at
            can_resub = exp <= datetime.now(timezone.utc)
        else:
            can_resub = True

    return {
        "is_subscribed": ab.status in ("active", "cancelling"),
        "status": ab.status,
        "cancel_at_period_end": ab.status == "cancelling",
        "can_resubscribe": can_resub,
        "plan": {
            "name": ab.plan_name,
            "price": ab.price,
            "currency": ab.currency,
            "started_at": ab.started_at,
            "expires_at": ab.expires_at,
        },
        "stripe_subscription_id": ab.stripe_subscription_id,
    }


# ── Create Stripe Checkout session ─────────────────────────────────────────────

@router.post("/checkout")
def create_checkout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stripe = _get_stripe()
    price_id = os.getenv("STRIPE_PRICE_ID", "")
    if not price_id:
        raise HTTPException(status_code=503, detail="Stripe price not configured")

    ab = db.query(Abonnement).filter(Abonnement.user_id == current_user.id).first()

    # Block re-subscription while a paid period is still running
    if ab and ab.status in ("active", "cancelling", "past_due"):
        raise HTTPException(
            status_code=400,
            detail="You already have an active subscription. "
                   "You can subscribe again after your current period ends.",
        )

    # Also block if cancelled but expires_at hasn't passed yet
    if ab and ab.status == "cancelled" and ab.expires_at:
        expires = ab.expires_at.replace(tzinfo=timezone.utc) if ab.expires_at.tzinfo is None else ab.expires_at
        if expires > datetime.now(timezone.utc):
            days_left = (expires - datetime.now(timezone.utc)).days
            raise HTTPException(
                status_code=400,
                detail=f"Your current period ends in {days_left} day(s). "
                       "You can subscribe again after that date.",
            )

    if not ab:
        ab = _get_or_create_abonnement(current_user.id, db)

    if not ab.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            name=f"{current_user.name or ''} {current_user.family_name or ''}".strip(),
            metadata={"user_id": current_user.id},
        )
        ab.stripe_customer_id = customer.id
        db.commit()

    session = stripe.checkout.Session.create(
        customer=ab.stripe_customer_id,
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{FRONTEND_URL}/billing?success=1",
        cancel_url=f"{FRONTEND_URL}/billing?cancelled=1",
        metadata={"user_id": current_user.id},
    )
    return {"url": session.url}


# ── Stripe Customer Portal ─────────────────────────────────────────────────────

@router.get("/portal")
def create_portal(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stripe = _get_stripe()

    ab = db.query(Abonnement).filter(Abonnement.user_id == current_user.id).first()
    if not ab or not ab.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No billing account found. Please subscribe first.")

    portal = stripe.billing_portal.Session.create(
        customer=ab.stripe_customer_id,
        return_url=f"{FRONTEND_URL}/billing",
    )
    return {"url": portal.url}


# ── Stripe Webhook ─────────────────────────────────────────────────────────────

@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        import stripe as _stripe
        _stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    except ImportError:
        raise HTTPException(status_code=503, detail="Payment library not installed")

    import json as _json

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    if secret:
        try:
            event = _stripe.Webhook.construct_event(payload, sig, secret)
        except _stripe.SignatureVerificationError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook signature")
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        # Stripe v5+ — convert to plain dict for uniform key access
        try:
            event_dict = event.to_dict_recursive()
        except AttributeError:
            event_dict = _json.loads(payload.decode())
        etype = event_dict.get("type", "")
        obj   = event_dict.get("data", {}).get("object", {})
    else:
        # No secret configured — skip verification (local dev only)
        try:
            event_dict = _json.loads(payload)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook payload")
        etype = event_dict.get("type", "")
        obj   = event_dict.get("data", {}).get("object", {})

    if etype == "checkout.session.completed":
        user_id = (obj.get("metadata") or {}).get("user_id")
        sub_id  = obj.get("subscription")
        if user_id:
            ab = _get_or_create_abonnement(user_id, db)
            if sub_id:
                ab.stripe_subscription_id = sub_id
            ab.status    = "active"
            ab.plan_name = PLAN["name"]
            ab.price     = PLAN["price"]
            ab.currency  = PLAN["currency"]
            ab.started_at  = datetime.now(timezone.utc)
            ab.expires_at  = datetime.now(timezone.utc) + timedelta(days=30)
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.abonnement_id = ab.id
            db.commit()

    elif etype == "invoice.payment_succeeded":
        sub_id = obj.get("subscription")
        if sub_id:
            ab = db.query(Abonnement).filter(Abonnement.stripe_subscription_id == sub_id).first()
            if ab:
                ab.status = "active"
                # period_end from invoice lines if available
                lines = obj.get("lines", {})
                lines_data = lines.get("data", []) if isinstance(lines, dict) else []
                period_end = lines_data[0].get("period", {}).get("end") if lines_data else None
                ab.expires_at = (
                    datetime.fromtimestamp(period_end, tz=timezone.utc)
                    if period_end
                    else datetime.now(timezone.utc) + timedelta(days=30)
                )
                db.commit()

    elif etype == "invoice.payment_failed":
        sub_id = obj.get("subscription")
        if sub_id:
            ab = db.query(Abonnement).filter(Abonnement.stripe_subscription_id == sub_id).first()
            if ab:
                ab.status = "past_due"
                db.commit()

    elif etype == "customer.subscription.deleted":
        sub_id = obj.get("id")
        if sub_id:
            ab = db.query(Abonnement).filter(Abonnement.stripe_subscription_id == sub_id).first()
            if ab:
                ab.status = "cancelled"
                user = db.query(User).filter(User.id == ab.user_id).first()
                if user:
                    user.abonnement_id = None
                db.commit()

    return {"received": True}


# ── Sync subscription from Stripe (called after redirect back from Checkout) ──

@router.post("/sync")
def sync_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Pull the latest subscription state from Stripe and persist it.
    Call this when the user lands on /billing?success=1 as a fallback
    in case the webhook hasn't fired yet.
    """
    stripe = _get_stripe()

    ab = db.query(Abonnement).filter(Abonnement.user_id == current_user.id).first()
    if not ab or not ab.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No billing account found")

    # Fetch subscriptions for this customer directly from Stripe
    subs = stripe.Subscription.list(customer=ab.stripe_customer_id, status="all", limit=1)
    if not subs.data:
        return {"synced": False, "detail": "No Stripe subscription found yet"}

    sub = subs.data[0]
    # Use to_dict() — in Stripe v15 None-valued fields raise AttributeError on attribute access
    sub_dict = sub.to_dict()

    ab.stripe_subscription_id = sub_dict.get("id") or sub.id
    sub_status = sub_dict.get("status", "")
    ab.status    = "active" if sub_status in ("active", "trialing") else sub_status
    ab.plan_name = PLAN["name"]
    ab.price     = PLAN["price"]
    ab.currency  = PLAN["currency"]

    start_ts = sub_dict.get("start_date") or sub_dict.get("billing_cycle_anchor")
    ab.started_at = datetime.fromtimestamp(start_ts, tz=timezone.utc) if start_ts else datetime.now(timezone.utc)

    # In Stripe API 2026-03-25.dahlia, current_period_end moved to items.data[0]
    period_end = sub_dict.get("current_period_end")
    if not period_end:
        items = sub_dict.get("items", {})
        items_data = items.get("data", []) if isinstance(items, dict) else []
        if items_data:
            period_end = items_data[0].get("current_period_end")

    ab.expires_at = (
        datetime.fromtimestamp(period_end, tz=timezone.utc)
        if period_end
        else datetime.now(timezone.utc) + timedelta(days=30)
    )

    user = db.query(User).filter(User.id == current_user.id).first()
    if user:
        user.abonnement_id = ab.id

    db.commit()
    db.refresh(ab)

    return {
        "synced": True,
        "status": ab.status,
        "stripe_subscription_id": ab.stripe_subscription_id,
        "started_at": ab.started_at,
        "expires_at": ab.expires_at,
    }


# ── Cancel subscription (3-day window rule) ───────────────────────────────────

CANCEL_LOCK_DAYS = 3  # cannot cancel within this many days of renewal

@router.post("/cancel")
def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ab = db.query(Abonnement).filter(Abonnement.user_id == current_user.id).first()
    if not ab or ab.status not in ("active", "cancelling"):
        raise HTTPException(status_code=400, detail="No active subscription found")

    if ab.status == "cancelling":
        raise HTTPException(status_code=400, detail="Subscription is already scheduled for cancellation")

    # Enforce 3-day cancellation window
    if ab.expires_at:
        days_left = (ab.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        if days_left <= CANCEL_LOCK_DAYS:
            raise HTTPException(
                status_code=400,
                detail=f"Cancellation is locked within {CANCEL_LOCK_DAYS} days of renewal. "
                       f"Your subscription renews in {days_left} day(s).",
            )

    if not ab.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No Stripe subscription linked to this account")

    stripe = _get_stripe()
    stripe.Subscription.modify(ab.stripe_subscription_id, cancel_at_period_end=True)

    ab.status = "cancelling"
    db.commit()

    return {
        "cancelled": True,
        "access_until": ab.expires_at,
        "message": f"Your subscription will end on {ab.expires_at.strftime('%d/%m/%Y') if ab.expires_at else 'period end'}. "
                   "You keep full access until then.",
    }


# ── Reactivate auto-renewal (undo a cancellation) ─────────────────────────────

@router.post("/reactivate")
def reactivate_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Re-enable auto-renewal for a subscription that was set to cancel at period end."""
    ab = db.query(Abonnement).filter(Abonnement.user_id == current_user.id).first()
    if not ab or ab.status != "cancelling":
        raise HTTPException(status_code=400, detail="No pending cancellation to reactivate")

    if not ab.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No Stripe subscription linked to this account")

    stripe = _get_stripe()
    stripe.Subscription.modify(ab.stripe_subscription_id, cancel_at_period_end=False)

    ab.status = "active"
    db.commit()

    return {
        "reactivated": True,
        "message": "Auto-renewal restored. Your subscription will continue automatically.",
        "expires_at": ab.expires_at,
    }


# ── User: invoice history from Stripe ─────────────────────────────────────────

@router.get("/invoices")
def get_invoices(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ab = db.query(Abonnement).filter(Abonnement.user_id == current_user.id).first()
    if not ab or not ab.stripe_customer_id:
        return {"invoices": []}

    stripe = _get_stripe()
    invoices = stripe.Invoice.list(customer=ab.stripe_customer_id, limit=24)

    result = []
    for inv in invoices.data:
        inv_dict = inv.to_dict()
        result.append({
            "id":          inv_dict.get("id"),
            "number":      inv_dict.get("number"),
            "status":      inv_dict.get("status"),          # paid | open | void | draft
            "amount_paid": inv_dict.get("amount_paid", 0),  # cents
            "currency":    inv_dict.get("currency", "eur"),
            "created":     inv_dict.get("created"),
            "period_start": inv_dict.get("period_start"),
            "period_end":   inv_dict.get("period_end"),
            "hosted_invoice_url": inv_dict.get("hosted_invoice_url"),
            "invoice_pdf":  inv_dict.get("invoice_pdf"),
        })
    return {"invoices": result}


# ── Admin: list all subscriptions ─────────────────────────────────────────────

@router.get("/admin/subscriptions")
def admin_list_subscriptions(
    skip: int = 0,
    limit: int = 50,
    status: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ("admin", "agent"):
        raise HTTPException(status_code=403, detail="Admin or agent only")

    query = db.query(Abonnement, User).join(User, Abonnement.user_id == User.id)
    if status:
        query = query.filter(Abonnement.status == status)

    total = query.count()
    rows  = query.order_by(Abonnement.created_at.desc()).offset(skip).limit(limit).all()

    # Aggregate counts per status across the entire dataset (ignores filters /
    # pagination) so the summary tiles always show the true picture.
    from sqlalchemy import func
    grouped = (
        db.query(Abonnement.status, func.count(Abonnement.id))
        .group_by(Abonnement.status)
        .all()
    )
    counts = {s or "unknown": c for s, c in grouped}

    return {
        "total": total,
        "counts": {
            "total":      sum(counts.values()),
            "active":     counts.get("active", 0),
            "cancelling": counts.get("cancelling", 0),
            "past_due":   counts.get("past_due", 0),
            "cancelled":  counts.get("cancelled", 0),
        },
        "subscriptions": [
            {
                "id":                     ab.id,
                "user_id":                ab.user_id,
                "user_name":              f"{u.name or ''} {u.family_name or ''}".strip(),
                "user_email":             u.email,
                "status":                 ab.status,
                "plan_name":              ab.plan_name,
                "price":                  ab.price,
                "currency":               ab.currency,
                "started_at":             ab.started_at,
                "expires_at":             ab.expires_at,
                "stripe_customer_id":     ab.stripe_customer_id,
                "stripe_subscription_id": ab.stripe_subscription_id,
                "created_at":             ab.created_at,
            }
            for ab, u in rows
        ],
    }


# ── Admin: manually activate a user subscription ──────────────────────────────

@router.post("/admin/activate/{user_id}")
def admin_activate(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Admin or agent — manually mark a user as subscribed (e.g. after phone/CRM sale)."""
    if current_user.role not in ("admin", "agent"):
        raise HTTPException(status_code=403, detail="Admin or agent only")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    ab = _get_or_create_abonnement(user_id, db)
    ab.status = "active"
    ab.plan_name = PLAN["name"]
    ab.price = PLAN["price"]
    ab.currency = PLAN["currency"]
    ab.started_at = datetime.now(timezone.utc)
    ab.expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    user.abonnement_id = ab.id
    db.commit()
    return {"message": f"Subscription activated for user {user_id}", "expires_at": ab.expires_at}


@router.post("/admin/deactivate/{user_id}")
def admin_deactivate(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Admin or agent — revoke a user's subscription."""
    if current_user.role not in ("admin", "agent"):
        raise HTTPException(status_code=403, detail="Admin or agent only")

    ab = db.query(Abonnement).filter(Abonnement.user_id == user_id).first()
    if not ab:
        raise HTTPException(status_code=404, detail="No subscription found")

    ab.status = "cancelled"
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.abonnement_id = None
    db.commit()
    return {"message": f"Subscription cancelled for user {user_id}"}
