import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.users_auth import _require_admin
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.contested_order import ContestedOrder
from app.models.order_report_job import OrderReportJob
from app.models.reported_order import ReportedOrder
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.user import User
from app.models.user_store import UserStore
from app.services.sync_service import run_bulk_sync, run_payment_sync
from app.services.uber_service import create_report, get_uber_token

router = APIRouter(prefix="/order-reports", tags=["order-reports"])

# Uber lag: ORDER_HISTORY_REPORT = 2 days, ORDER_ERRORS_TRANSACTION_REPORT = 4 days
UBER_CANCELLED_LAG_DAYS = 2
UBER_CONTESTED_LAG_DAYS = 4


def _max_end_date(lag_days: int) -> date:
    return (datetime.now(timezone.utc) - timedelta(days=lag_days)).date()


# ── Schema ─────────────────────────────────────────────────────────────────────

class ReportRequestPayload(BaseModel):
    store_id: str
    start_date: str   # YYYY-MM-DD
    end_date: str     # YYYY-MM-DD

    @field_validator("end_date")
    @classmethod
    def cap_end_date(cls, v: str) -> str:
        max_end = _max_end_date(UBER_CANCELLED_LAG_DAYS)
        try:
            requested = date.fromisoformat(v)
        except ValueError:
            raise ValueError("end_date must be YYYY-MM-DD")
        if requested > max_end:
            return max_end.isoformat()
        return v


# ── Helpers ────────────────────────────────────────────────────────────────────

def _request_report_for_store(
    store_id: str,
    user_id: str,
    start_date: str,
    end_date: str,
    token: str,
    db: Session,
    report_type: str = "ORDER_HISTORY_REPORT",
    job_type: str = "cancelled",
) -> tuple[str | None, dict]:
    result = create_report(token, [store_id], start_date, end_date, report_type)
    print(f"📋 create_report [{report_type}] store={store_id} → {result}")
    workflow_id = result.get("workflow_id") or result.get("job_id")
    if not workflow_id:
        return None, result
    db.add(OrderReportJob(
        job_id=workflow_id,
        user_id=user_id,
        store_id=store_id,
        job_type=job_type,
        status="pending",
    ))
    return workflow_id, result


# ── Reusable helper: kick off both reports for one user's verified stores ────

def trigger_user_sync(
    user_id: str,
    db: Session,
    days_back: int = 30,
    max_stores: int | None = None,
) -> dict:
    """
    Fire both ORDER_HISTORY_REPORT (cancelled) and ORDER_ERRORS_TRANSACTION_REPORT
    (contested) for the user's verified+integrated stores. Returns the workflow IDs
    so callers can monitor. Safe to call from non-request contexts (e.g. on approve).
    """
    from app.models.user_store import STATUS_VERIFIED  # late import to avoid cycles

    active_sk_ids = {
        s.store_id for s in
        db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
    }
    user_stores = (
        db.query(UserStore)
        .filter(UserStore.user_id == user_id, UserStore.status == STATUS_VERIFIED)
        .all()
    )
    stores = [s for s in user_stores if s.store_id in active_sk_ids]
    if max_stores:
        stores = stores[:max_stores]

    if not stores:
        return {"triggered": [], "errors": [{"reason": "no_verified_integrated_stores"}]}

    token_data = get_uber_token()
    token = token_data.get("access_token")
    if not token:
        return {"triggered": [], "errors": [{"reason": "no_uber_token", "raw": token_data}]}

    end_cancelled = _max_end_date(UBER_CANCELLED_LAG_DAYS)
    end_contested = _max_end_date(UBER_CONTESTED_LAG_DAYS)
    start_cancelled = (end_cancelled - timedelta(days=days_back)).isoformat()
    start_contested = (end_contested - timedelta(days=days_back)).isoformat()

    triggered, errors = [], []
    for s in stores:
        wf_c, raw_c = _request_report_for_store(
            s.store_id, user_id, start_cancelled, end_cancelled.isoformat(),
            token, db, report_type="ORDER_HISTORY_REPORT", job_type="cancelled",
        )
        (triggered if wf_c else errors).append(
            {"store_id": s.store_id, "type": "cancelled", "workflow_id": wf_c, "raw": None if wf_c else raw_c}
        )

        wf_x, raw_x = _request_report_for_store(
            s.store_id, user_id, start_contested, end_contested.isoformat(),
            token, db, report_type="ORDER_ERRORS_TRANSACTION_REPORT", job_type="contested",
        )
        (triggered if wf_x else errors).append(
            {"store_id": s.store_id, "type": "contested", "workflow_id": wf_x, "raw": None if wf_x else raw_x}
        )

    db.commit()
    return {"triggered": triggered, "errors": errors}


# ── Main endpoint: trigger + return saved orders ───────────────────────────────

@router.get("/get-cancelled-orders")
def get_cancelled_orders(
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD. Defaults to 7 days ago."),
    end_date: Optional[str] = Query(None, description=f"YYYY-MM-DD. Capped at today-{UBER_CANCELLED_LAG_DAYS} days."),
    store_id: Optional[str] = Query(None, description="Filter to a single store."),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Trigger a fresh ORDER_HISTORY_REPORT on Uber for all linked stores
    and return all cancelled orders already stored in the database.
    Cancelled orders = Completed? == 0 AND Ticket Size == 0.
    """
    max_end = _max_end_date(UBER_CANCELLED_LAG_DAYS)

    # Resolve dates
    resolved_end = min(
        date.fromisoformat(end_date) if end_date else max_end,
        max_end,
    )
    resolved_start = (
        date.fromisoformat(start_date)
        if start_date
        else resolved_end - timedelta(days=7)
    )

    if resolved_start > resolved_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"start_date must be before end_date (max end_date is {max_end})",
        )

    # Fetch user's linked active stores only
    active_sk_ids = {
        s.store_id for s in
        db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
    }
    stores_query = db.query(UserStore).filter(UserStore.user_id == current_user.id)
    if store_id:
        stores_query = stores_query.filter(UserStore.store_id == store_id)
    stores = [s for s in stores_query.all() if s.store_id in active_sk_ids]

    if not stores:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No linked active stores found for your account.",
        )

    # Request fresh reports
    token_data = get_uber_token()
    token = token_data.get("access_token")
    triggered_jobs = []
    trigger_errors = []
    if token:
        for s in stores:
            wf_id, raw = _request_report_for_store(
                s.store_id,
                current_user.id,
                resolved_start.isoformat(),
                resolved_end.isoformat(),
                token,
                db,
            )
            if wf_id:
                triggered_jobs.append(wf_id)
            else:
                trigger_errors.append({"store_id": s.store_id, "uber_response": raw})
        db.commit()
    else:
        trigger_errors.append({"error": "could_not_obtain_token", "uber_response": token_data})

    # Return already-stored cancelled orders
    q = db.query(ReportedOrder).filter(ReportedOrder.user_id == current_user.id)
    if store_id:
        q = q.filter(ReportedOrder.store_id == store_id)
    total = q.count()
    orders = q.order_by(ReportedOrder.fetched_at.desc()).offset(skip).limit(limit).all()

    return {
        "report_period": {
            "start_date": resolved_start.isoformat(),
            "end_date": resolved_end.isoformat(),
        },
        "triggered_jobs": triggered_jobs,
        "trigger_errors": trigger_errors,
        "total_stored": total,
        "skip": skip,
        "limit": limit,
        "cancelled_orders": [
            {
                
                "store_name": r.store_name,
                "country_code": r.country_code,
                "order_id": r.order_id,
                "order_uuid": r.order_uuid,
                "order_status": r.order_status,
                "menu_item_count": r.menu_item_count,
                "date_ordered": r.date_ordered,
                "workflow_uuid": r.workflow_uuid,
                "store_id": r.store_id,
                "remboursement_status": r.remboursement_status,
                "report_job_id": r.report_job_id,
                "fetched_at": r.fetched_at,
            }
            for r in orders
        ],
    }


# ── Contested orders ───────────────────────────────────────────────────────────

@router.get("/get-contested-orders")
def get_contested_orders(
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD. Defaults to 7 days ago."),
    end_date: Optional[str] = Query(None, description=f"YYYY-MM-DD. Capped at today-{UBER_CONTESTED_LAG_DAYS} days."),
    store_id: Optional[str] = Query(None, description="Filter to a single store."),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Trigger a fresh ORDER_ERRORS_TRANSACTION_REPORT on Uber for all linked stores
    and return all contested orders (non remboursé only) stored in the database.
    Refund Covered by Merchant == 0 rows are skipped (already remboursé).
    """
    max_end = _max_end_date(UBER_CONTESTED_LAG_DAYS)

    resolved_end = min(
        date.fromisoformat(end_date) if end_date else max_end,
        max_end,
    )
    resolved_start = (
        date.fromisoformat(start_date)
        if start_date
        else resolved_end - timedelta(days=7)
    )

    if resolved_start > resolved_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"start_date must be before end_date (max end_date is {max_end})",
        )

    active_sk_ids = {
        s.store_id for s in
        db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
    }
    stores_query = db.query(UserStore).filter(UserStore.user_id == current_user.id)
    if store_id:
        stores_query = stores_query.filter(UserStore.store_id == store_id)
    stores = [s for s in stores_query.all() if s.store_id in active_sk_ids]

    if not stores:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No linked active stores found for your account.",
        )

    # Trigger fresh reports
    token_data = get_uber_token()
    token = token_data.get("access_token")
    triggered_jobs = []
    trigger_errors = []
    if token:
        for s in stores:
            wf_id, raw = _request_report_for_store(
                s.store_id,
                current_user.id,
                resolved_start.isoformat(),
                resolved_end.isoformat(),
                token,
                db,
                report_type="ORDER_ERRORS_TRANSACTION_REPORT",
                job_type="contested",
            )
            if wf_id:
                triggered_jobs.append(wf_id)
            else:
                trigger_errors.append({"store_id": s.store_id, "uber_response": raw})
        db.commit()
    else:
        trigger_errors.append({"error": "could_not_obtain_token", "uber_response": token_data})

    # Return already-stored contested orders
    q = db.query(ContestedOrder).filter(ContestedOrder.user_id == current_user.id)
    if store_id:
        q = q.filter(ContestedOrder.store_id == store_id)
    total = q.count()
    orders = q.order_by(ContestedOrder.fetched_at.desc()).offset(skip).limit(limit).all()

    return {
        "report_period": {
            "start_date": resolved_start.isoformat(),
            "end_date": resolved_end.isoformat(),
        },
        "triggered_jobs": triggered_jobs,
        "trigger_errors": trigger_errors,
        "total_stored": total,
        "skip": skip,
        "limit": limit,
        "contested_orders": [
            {
                "id": r.id,
                "order_id": r.order_id,
                "order_uuid": r.order_uuid,
                "workflow_uuid": r.workflow_uuid,
                "store_id": r.store_id,
                "store_name": r.store_name,
                "city": r.city,
                "order_issue": r.order_issue,
                "inaccurate_items": r.inaccurate_items,
                "ticket_size": r.ticket_size,
                "customer_refunded": r.customer_refunded,
                "refund_covered_by_merchant": r.refund_covered_by_merchant,
                "refund_not_covered_by_merchant": r.refund_not_covered_by_merchant,
                "currency_code": r.currency_code,
                "time_customer_ordered": r.time_customer_ordered,
                "time_merchant_accepted": r.time_merchant_accepted,
                "time_customer_refunded": r.time_customer_refunded,
                "fulfillment_type": r.fulfillment_type,
                "order_channel": r.order_channel,
                "remboursement_status": r.remboursement_status,
                "report_job_id": r.report_job_id,
                "fetched_at": r.fetched_at,
            }
            for r in orders
        ],
    }


# ── My orders (read-only) ──────────────────────────────────────────────────────

@router.get("/my-cancelled")
def my_cancelled_orders(
    store_id: Optional[str] = Query(None),
    remboursement_status: Optional[str] = Query(None, description="en attente | remboursé"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    search: Optional[str] = Query(None, description="Search by order ID or store name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return all cancelled orders for stores linked to the authenticated user.
    Filters by the user's linked store_ids — no user_id stored on the order.
    """
    active_sk_ids = {
        s.store_id for s in
        db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
    }
    linked_store_ids = [
        us.store_id for us in
        db.query(UserStore).filter(UserStore.user_id == current_user.id).all()
        if us.store_id in active_sk_ids
    ]
    if not linked_store_ids:
        return {"total": 0, "skip": skip, "limit": limit, "cancelled_orders": []}

    q = db.query(ReportedOrder).filter(ReportedOrder.store_id.in_(linked_store_ids))
    if store_id:
        q = q.filter(ReportedOrder.store_id == store_id)
    if remboursement_status:
        q = q.filter(ReportedOrder.remboursement_status == remboursement_status)
    if start_date:
        q = q.filter(ReportedOrder.date_ordered >= start_date)
    if end_date:
        q = q.filter(ReportedOrder.date_ordered <= end_date + "T23:59:59")
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(or_(
            ReportedOrder.order_id.ilike(term),
            ReportedOrder.store_name.ilike(term),
        ))

    total = q.count()
    orders = q.order_by(ReportedOrder.fetched_at.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "cancelled_orders": [{
            "store_name": r.store_name,
            "country_code": r.country_code,
            "order_id": r.order_id,
            "order_uuid": r.order_uuid,
            "order_status": r.order_status,
            "menu_item_count": r.menu_item_count,
            "date_ordered": r.date_ordered,
            "workflow_uuid": r.workflow_uuid,
            "store_id": r.store_id,
            "remboursement_status": r.remboursement_status,
            "report_job_id": r.report_job_id,
            "fetched_at": r.fetched_at,
            "manual_amount": r.manual_amount,
            "refund_email_sent_at": r.refund_email_sent_at,
        }
            for r in orders
        ],
    }


@router.get("/my-contested")
def my_contested_orders(
    store_id: Optional[str] = Query(None),
    remboursement_status: Optional[str] = Query(None, description="en attente | remboursé"),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="YYYY-MM-DD"),
    search: Optional[str] = Query(None, description="Search by order ID or store name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return all contested orders for stores linked to the authenticated user.
    Filters by the user's linked store_ids — no user_id stored on the order.
    """
    active_sk_ids = {
        s.store_id for s in
        db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
    }
    linked_store_ids = [
        us.store_id for us in
        db.query(UserStore).filter(UserStore.user_id == current_user.id).all()
        if us.store_id in active_sk_ids
    ]
    if not linked_store_ids:
        return {"total": 0, "skip": skip, "limit": limit, "contested_orders": []}

    q = db.query(ContestedOrder).filter(ContestedOrder.store_id.in_(linked_store_ids))
    if store_id:
        q = q.filter(ContestedOrder.store_id == store_id)
    if remboursement_status:
        q = q.filter(ContestedOrder.remboursement_status == remboursement_status)
    if start_date:
        q = q.filter(ContestedOrder.time_customer_ordered >= start_date)
    if end_date:
        q = q.filter(ContestedOrder.time_customer_ordered <= end_date + "T23:59:59")
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(or_(
            ContestedOrder.order_id.ilike(term),
            ContestedOrder.store_name.ilike(term),
        ))

    total = q.count()
    orders = q.order_by(ContestedOrder.fetched_at.desc()).offset(skip).limit(limit).all()
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "contested_orders": [
            {
                "id": r.id,
                "store_id": r.store_id,
                "store_name": r.store_name,
                "city": r.city,
                "country_code": r.country_code,
                "order_id": r.order_id,
                "order_uuid": r.order_uuid,
                "workflow_uuid": r.workflow_uuid,
                "order_issue": r.order_issue,
                "inaccurate_items": r.inaccurate_items,
                "ticket_size": r.ticket_size,
                "currency_code": r.currency_code,
                "customer_refunded": r.customer_refunded,
                "refund_covered_by_merchant": r.refund_covered_by_merchant,
                "refund_not_covered_by_merchant": r.refund_not_covered_by_merchant,
                "time_customer_ordered": r.time_customer_ordered,
                "time_merchant_accepted": r.time_merchant_accepted,
                "time_customer_refunded": r.time_customer_refunded,
                "fulfillment_type": r.fulfillment_type,
                "order_channel": r.order_channel,
                "remboursement_status": r.remboursement_status,
                "fetched_at": r.fetched_at,
            }
            for r in orders
        ],
    }


@router.post("/admin/sync-nowV2")
def admin_sync_now_v2(admin: User = Depends(_require_admin)):
    """
    Admin — bulk sync: fires 2 Uber reports (one cancelled, one contested)
    covering ALL active SK store UUIDs at once, then links each CSV row back
    to its store via store_name. 100 stores = 2 API calls instead of 200.
    """
    admin_id = str(admin.id)
    thread = threading.Thread(
        target=run_bulk_sync,
        kwargs={"user_id": admin_id},
        daemon=True,
    )
    thread.start()
    return {
        "message": "Bulk sync started. 2 reports requested for all SK stores — rows linked by store name.",
        "triggered_by": admin_id,
    }


@router.post("/admin/payment-syncV2")
def admin_payment_sync_v2(admin: User = Depends(_require_admin)):
    """Admin — fire a single PAYMENT_DETAILS_REPORT for all active SK stores."""
    admin_id = str(admin.id)
    thread = threading.Thread(
        target=run_payment_sync,
        kwargs={"user_id": admin_id},
        daemon=True,
    )
    thread.start()
    return {
        "message": "Payment sync started. PAYMENT_DETAILS_REPORT requested for all SK stores.",
        "triggered_by": admin_id,
    }




# ── Jobs status ────────────────────────────────────────────────────────────────

@router.get("/jobs")
def list_report_jobs(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all report jobs requested by the current user."""
    jobs = (
        db.query(OrderReportJob)
        .filter(OrderReportJob.user_id == current_user.id)
        .order_by(OrderReportJob.created_at.desc())
        .all()
    )
    return [
        {
            "job_id": j.job_id,
            "store_id": j.store_id,
            "job_type": j.job_type,
            "status": j.status,
            "created_at": j.created_at,
        }
        for j in jobs
    ]


# ── Admin: manual amount on a cancelled order + send refund email ────────────

class CancelledAmountPayload(BaseModel):
    manual_amount: float


@router.patch("/admin/cancelled-orders/{order_id}/amount")
def set_cancelled_amount(
    order_id: str,
    payload: CancelledAmountPayload,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — set the manual amount on a cancelled order."""
    if payload.manual_amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be greater than 0")

    order = db.query(ReportedOrder).filter(
        (ReportedOrder.order_id == order_id) | (ReportedOrder.order_uuid == order_id)
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Cancelled order not found")

    order.manual_amount = payload.manual_amount
    db.commit()
    db.refresh(order)
    return {
        "order_id": order.order_id,
        "order_uuid": order.order_uuid,
        "manual_amount": order.manual_amount,
        "refund_email_sent_at": order.refund_email_sent_at,
    }


@router.post("/admin/cancelled-orders/{order_id}/send-refund")
def send_cancelled_refund(
    order_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """
    Admin — send a refund-request email to Uber support for a cancelled order.
    Requires the manual_amount to be set first via PATCH .../amount.
    """
    from app.services.email_service import send_cancelled_refund_email

    order = db.query(ReportedOrder).filter(
        (ReportedOrder.order_id == order_id) | (ReportedOrder.order_uuid == order_id)
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Cancelled order not found")

    if not order.manual_amount or order.manual_amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="Set the manual_amount on this order before sending the refund email.",
        )

    sent = send_cancelled_refund_email(
        restaurant_name=order.store_name or order.store_id,
        restaurant_uuid=order.store_id,
        order_number=order.order_id or order.order_uuid or "",
        amount_eur=float(order.manual_amount),
    )

    if not sent:
        raise HTTPException(
            status_code=502,
            detail="Mailjet did not accept the message. Check backend logs for the exact error.",
        )

    order.refund_email_sent_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    return {
        "order_id": order.order_id,
        "order_uuid": order.order_uuid,
        "manual_amount": order.manual_amount,
        "refund_email_sent_at": order.refund_email_sent_at,
        "message": "Refund email sent.",
    }
