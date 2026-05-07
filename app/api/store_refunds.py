from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, or_
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
    search: Optional[str] = Query(None, description="Search by store name, payout ref, or order ID"),
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
        # Default: show only linked refunds unless the caller explicitly requests unlinked
        if linked is None:
            q = q.filter(StoreRefund.linked_order_id.isnot(None))
    if store_id:
        q = q.filter(StoreRefund.store_id == store_id)
    if linked is True:
        q = q.filter(StoreRefund.linked_order_id.isnot(None))
    elif linked is False:
        q = q.filter(StoreRefund.linked_order_id.is_(None))
    if date_from:
        q = q.filter(func.date(StoreRefund.refund_date) >= date_from)
    if date_to:
        q = q.filter(func.date(StoreRefund.refund_date) <= date_to)
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(or_(
            StoreRefund.store_name.ilike(term),
            StoreRefund.payout_reference_id.ilike(term),
            StoreRefund.linked_order_id.ilike(term),
        ))

    total = q.count()
    rows = q.order_by(StoreRefund.fetched_at.desc()).offset(skip).limit(limit).all()
    return {"total": total, "skip": skip, "limit": limit, "refunds": [_fmt(r) for r in rows]}


# ── Wallet: income per store / per month ──────────────────────────────────────

@router.get("/wallet")
def get_wallet(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return total income broken down by store and by month (linked refunds only)."""
    allowed = _user_store_ids(current_user, db)

    q = db.query(StoreRefund).filter(StoreRefund.linked_order_id.isnot(None))
    if allowed is not None:
        q = q.filter(StoreRefund.store_id.in_(allowed))

    refunds = q.all()

    def _parse(s: str | None) -> float:
        try:
            return abs(float((s or "0").replace(",", ".")))
        except (ValueError, TypeError):
            return 0.0

    total_income = round(sum(_parse(r.amount) for r in refunds), 2)

    by_store: dict = {}
    for r in refunds:
        sid = r.store_id or "unknown"
        if sid not in by_store:
            by_store[sid] = {"store_id": sid, "store_name": r.store_name, "total": 0.0, "count": 0}
        by_store[sid]["total"] = round(by_store[sid]["total"] + _parse(r.amount), 2)
        by_store[sid]["count"] += 1

    by_month: dict = {}
    for r in refunds:
        if not r.refund_date:
            continue
        month = str(r.refund_date)[:7]
        if month not in by_month:
            by_month[month] = {"month": month, "total": 0.0, "count": 0}
        by_month[month]["total"] = round(by_month[month]["total"] + _parse(r.amount), 2)
        by_month[month]["count"] += 1

    return {
        "total_income": total_income,
        "refund_count": len(refunds),
        "by_store": sorted(by_store.values(), key=lambda x: -x["total"]),
        "by_month": sorted(by_month.values(), key=lambda x: x["month"]),
    }


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


# ── Admin revenue ─────────────────────────────────────────────────────────────

@router.get("/admin/revenue")
def admin_revenue(
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """
    SK revenue per user / per store.
    Contested (20%): StoreRefunds linked to a remboursé ContestedOrder.
    Cancelled (15%): StoreRefunds linked to a remboursé ReportedOrder.
    Uses 7 bulk queries regardless of user/store count.
    """
    def _parse(s: str | None) -> float:
        try:
            return abs(float((s or "0").replace(",", ".")))
        except (ValueError, TypeError):
            return 0.0

    # ── 1. All non-admin users ────────────────────────────────────────────────
    users = (
        db.query(User)
        .filter(User.role != "admin")
        .order_by(User.name)
        .all()
    )

    # ── 2. All verified UserStores, grouped by user ───────────────────────────
    all_user_stores = (
        db.query(UserStore)
        .filter(UserStore.status == STATUS_VERIFIED)
        .all()
    )
    user_to_stores: dict[str, list[UserStore]] = {}
    all_store_ids: set[str] = set()
    for us in all_user_stores:
        user_to_stores.setdefault(us.user_id, []).append(us)
        all_store_ids.add(us.store_id)

    if not all_store_ids:
        return {
            "total_revenue": 0.0,
            "contested_revenue": 0.0,
            "cancelled_revenue": 0.0,
            "commission_rates": {"contested": 0.20, "cancelled": 0.15},
            "users": [],
        }

    # ── 3. order_ids of remboursé ContestedOrders ─────────────────────────────
    rembourse_contested_ids: set[str] = {
        row.order_id
        for row in db.query(ContestedOrder.order_id)
        .filter(
            ContestedOrder.store_id.in_(all_store_ids),
            ContestedOrder.remboursement_status == "remboursé",
        )
        .all()
    }

    # ── 4. order_ids of remboursé ReportedOrders ──────────────────────────────
    rembourse_cancelled_ids: set[str] = {
        row.order_id
        for row in db.query(ReportedOrder.order_id)
        .filter(
            ReportedOrder.store_id.in_(all_store_ids),
            ReportedOrder.remboursement_status == "remboursé",
        )
        .all()
    }

    # ── 5. All linked StoreRefunds for relevant stores ────────────────────────
    linked_refs = (
        db.query(StoreRefund)
        .filter(
            StoreRefund.store_id.in_(all_store_ids),
            StoreRefund.linked_order_id.isnot(None),
        )
        .all()
    )

    store_contested_amt: dict[str, float] = {}
    store_contested_cnt: dict[str, int] = {}
    store_cancelled_amt: dict[str, float] = {}
    store_cancelled_cnt: dict[str, int] = {}
    for ref in linked_refs:
        sid = ref.store_id
        amt = _parse(ref.amount)
        if ref.linked_order_id in rembourse_contested_ids:
            store_contested_amt[sid] = store_contested_amt.get(sid, 0.0) + amt
            store_contested_cnt[sid] = store_contested_cnt.get(sid, 0) + 1
        if ref.linked_order_id in rembourse_cancelled_ids:
            store_cancelled_amt[sid] = store_cancelled_amt.get(sid, 0.0) + amt
            store_cancelled_cnt[sid] = store_cancelled_cnt.get(sid, 0) + 1

    # ── 6. ContestedOrder counts per store (total + remboursé) ────────────────
    contested_counts: dict[str, tuple[int, int]] = {
        row.store_id: (int(row.total), int(row.rembourse))
        for row in db.query(
            ContestedOrder.store_id,
            func.count(ContestedOrder.store_id).label("total"),
            func.sum(
                case((ContestedOrder.remboursement_status == "remboursé", 1), else_=0)
            ).label("rembourse"),
        )
        .filter(ContestedOrder.store_id.in_(all_store_ids))
        .group_by(ContestedOrder.store_id)
        .all()
    }

    # ── 7. ReportedOrder counts per store (total + remboursé) ─────────────────
    cancelled_counts: dict[str, tuple[int, int]] = {
        row.store_id: (int(row.total), int(row.rembourse))
        for row in db.query(
            ReportedOrder.store_id,
            func.count(ReportedOrder.store_id).label("total"),
            func.sum(
                case((ReportedOrder.remboursement_status == "remboursé", 1), else_=0)
            ).label("rembourse"),
        )
        .filter(ReportedOrder.store_id.in_(all_store_ids))
        .group_by(ReportedOrder.store_id)
        .all()
    }

    # ── Assemble result ───────────────────────────────────────────────────────
    result_users = []
    grand_total = grand_contested = grand_cancelled = 0.0

    for user in users:
        user_store_list = user_to_stores.get(user.id, [])
        stores_data = []
        user_contested_rev = user_cancelled_rev = 0.0

        for us in user_store_list:
            sid = us.store_id
            contested_amt = store_contested_amt.get(sid, 0.0)
            contested_rev = round(contested_amt * 0.20, 2)
            cancelled_amt = store_cancelled_amt.get(sid, 0.0)
            cancelled_rev = round(cancelled_amt * 0.15, 2)

            c_total, c_rembourse = contested_counts.get(sid, (0, 0))
            r_total, r_rembourse = cancelled_counts.get(sid, (0, 0))

            user_contested_rev += contested_rev
            user_cancelled_rev += cancelled_rev
            stores_data.append({
                "store_id": sid,
                "store_name": us.store_name or sid,
                "status": us.status,
                "contested_amount": round(contested_amt, 2),
                "contested_revenue": contested_rev,
                "contested_refunds": store_contested_cnt.get(sid, 0),
                "contested_orders_total": c_total,
                "contested_orders_rembourse": c_rembourse,
                "cancelled_amount": round(cancelled_amt, 2),
                "cancelled_revenue": cancelled_rev,
                "cancelled_refunds": store_cancelled_cnt.get(sid, 0),
                "cancelled_orders_total": r_total,
                "cancelled_orders_rembourse": r_rembourse,
                "total_revenue": round(contested_rev + cancelled_rev, 2),
            })

        user_total = round(user_contested_rev + user_cancelled_rev, 2)
        grand_total += user_total
        grand_contested += user_contested_rev
        grand_cancelled += user_cancelled_rev
        result_users.append({
            "user_id": user.id,
            "name": user.name,
            "family_name": user.family_name,
            "email": user.email,
            "is_active": user.is_active,
            "avatar_url": user.avatar_url,
            "contested_revenue": round(user_contested_rev, 2),
            "cancelled_revenue": round(user_cancelled_rev, 2),
            "total_revenue": user_total,
            "stores": sorted(stores_data, key=lambda x: -x["total_revenue"]),
        })

    result_users.sort(key=lambda x: -x["total_revenue"])

    return {
        "total_revenue": round(grand_total, 2),
        "contested_revenue": round(grand_contested, 2),
        "cancelled_revenue": round(grand_cancelled, 2),
        "commission_rates": {"contested": 0.20, "cancelled": 0.15},
        "users": result_users,
    }


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
