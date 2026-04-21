from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.users_auth import _require_admin
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.contested_order import ContestedOrder
from app.models.reported_order import ReportedOrder
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.store_refund import StoreRefund
from app.models.user import User
from app.models.user_store import STATUS_VERIFIED, UserStore

router = APIRouter(prefix="/store-refunds", tags=["store-refunds"])


class LinkOrderRequest(BaseModel):
    linked_order_id: str


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt(r: StoreRefund) -> dict:
    return {
        "id": r.id,
        "store_name": r.store_name,
        "store_id": r.store_id,
        "refund_date": r.refund_date,
        "amount": r.amount,
        "payout_reference_id": r.payout_reference_id,
        "linked_order_id": r.linked_order_id,
        "report_job_id": r.report_job_id,
        "fetched_at": r.fetched_at,
    }


def _user_store_ids(user: User, db: Session) -> list[str] | None:
    """Returns verified store_ids for a regular user, None for admin (= no restriction)."""
    if user.role == "admin":
        return None
    rows = (
        db.query(UserStore)
        .filter(UserStore.user_id == user.id, UserStore.status == STATUS_VERIFIED)
        .all()
    )
    return [r.store_id for r in rows]



# ── List refunds ────────────────────────────────────────────────────────────────

@router.get("")
def list_refunds(
    store_id: Optional[str] = Query(None),
    linked: Optional[bool] = Query(None, description="true = linked, false = unlinked only"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List store refunds.
    Admin: all stores. User: only their verified stores.
    """
    allowed = _user_store_ids(current_user, db)

    q = db.query(StoreRefund)
    if allowed is not None:
        q = q.filter(StoreRefund.store_id.in_(allowed))
    if store_id:
        q = q.filter(StoreRefund.store_id == store_id)
    if linked is True:
        q = q.filter(StoreRefund.linked_order_id.isnot(None))
    elif linked is False:
        q = q.filter(StoreRefund.linked_order_id.is_(None))
    if date_from:
        q = q.filter(func.substr(StoreRefund.refund_date, 1, 10) >= date_from)
    if date_to:
        q = q.filter(func.substr(StoreRefund.refund_date, 1, 10) <= date_to)

    total = q.count()
    rows = q.order_by(StoreRefund.fetched_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "skip": skip, "limit": limit, "refunds": [_fmt(r) for r in rows]}


# ── Suggest matching orders ────────────────────────────────────────────────────

@router.get("/suggest-orders")
def suggest_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    For every refund of the user's stores, find contested orders where
    refund_covered_by_merchant == refund amount exactly (same store).
    Admin gets all active SK stores.
    """
    allowed = _user_store_ids(current_user, db)

    if allowed is not None:
        store_ids = allowed
    else:
        store_ids = [
            s.store_id for s in
            db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
        ]

    refunds = (
        db.query(StoreRefund)
        .filter(StoreRefund.store_id.in_(store_ids))
        .order_by(StoreRefund.fetched_at.desc())
        .all()
    )

    result = []
    for refund in refunds:
        if not refund.store_id or not refund.amount:
            continue

        contested_candidates = [
            {
                "order_id": o.order_id,
                "order_uuid": o.order_uuid,
                "refund_covered_by_merchant": o.refund_covered_by_merchant,
                "date": o.time_customer_ordered,
            }
            for o in db.query(ContestedOrder)
            .filter(
                ContestedOrder.store_id == refund.store_id,
                ContestedOrder.refund_covered_by_merchant == refund.amount,
            )
            .all()
        ]

        result.append({
            "refund_id": refund.id,
            "store_name": refund.store_name,
            "refund_amount": refund.amount,
            "refund_date": refund.refund_date,
            "linked_order_id": refund.linked_order_id,
            "contested_candidates": contested_candidates,
        })

    return {"total": len(result), "suggestions": result}


# ── Suggest orders for a single refund ────────────────────────────────────────

@router.get("/{refund_id}/suggest-orders")
def suggest_orders_for_refund(
    refund_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return contested order candidates whose refund_covered_by_merchant matches
    this refund's amount for the same store.
    """
    allowed = _user_store_ids(current_user, db)

    refund = db.query(StoreRefund).filter(StoreRefund.id == refund_id).first()
    if not refund:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refund not found")

    if allowed is not None and refund.store_id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    if not refund.store_id or not refund.amount:
        return {"refund_id": refund_id, "candidates": []}

    candidates = [
        {
            "order_id": o.order_id,
            "order_uuid": o.order_uuid,
            "refund_covered_by_merchant": o.refund_covered_by_merchant,
            "date": o.time_customer_ordered,
        }
        for o in db.query(ContestedOrder)
        .filter(
            ContestedOrder.store_id == refund.store_id,
            ContestedOrder.refund_covered_by_merchant == refund.amount,
        )
        .all()
    ]

    return {"refund_id": refund_id, "candidates": candidates}


# ── Link order to refund ────────────────────────────────────────────────────────

@router.patch("/{refund_id}/link-order")
def link_order(
    refund_id: int,
    payload: LinkOrderRequest,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — manually link a refund to a specific order ID."""
    refund = db.query(StoreRefund).filter(StoreRefund.id == refund_id).first()
    if not refund:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refund not found")

    refund.linked_order_id = payload.linked_order_id
    db.commit()

    order_id = payload.linked_order_id
    for reported in db.query(ReportedOrder).filter(ReportedOrder.order_id == order_id).all():
        reported.remboursement_status = "remboursé"
    for contested in db.query(ContestedOrder).filter(ContestedOrder.order_id == order_id).all():
        contested.remboursement_status = "remboursé"
    db.commit()

    db.refresh(refund)
    return _fmt(refund)


# ── Unlink order from refund ───────────────────────────────────────────────────

@router.patch("/{refund_id}/unlink-order")
def unlink_order(
    refund_id: int,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — remove the linked order from a refund and revert its remboursement status."""
    refund = db.query(StoreRefund).filter(StoreRefund.id == refund_id).first()
    if not refund:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Refund not found")
    if not refund.linked_order_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Refund is not linked to any order")

    order_id = refund.linked_order_id
    refund.linked_order_id = None

    for reported in db.query(ReportedOrder).filter(ReportedOrder.order_id == order_id).all():
        reported.remboursement_status = "en attente"
    for contested in db.query(ContestedOrder).filter(ContestedOrder.order_id == order_id).all():
        contested.remboursement_status = "en attente"

    db.commit()
    db.refresh(refund)
    return _fmt(refund)
