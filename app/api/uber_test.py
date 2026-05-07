from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.uber_service import (
    create_report,
    get_store_by_id,
    get_uber_token,
    get_order,
    get_order_details,
)

router = APIRouter(prefix="/uber", tags=["uber"])


def _get_token() -> str:
    token_data = get_uber_token()
    token = token_data.get("access_token")
    if not token:
        raise HTTPException(status_code=400, detail={"message": "Failed to get Uber token", "uber": token_data})
    return token


# ── Token ────────────────────────────────────────────────────────────────────

@router.get("/token")
def test_token():
    """Verify credentials and inspect the token + granted scopes."""
    return get_uber_token()


# ── Stores ───────────────────────────────────────────────────────────────────


@router.get("/stores/{store_id}")
def store_detail(store_id: str):
    """Get full details for a single store by its UUID."""
    return get_store_by_id(_get_token(), store_id)


# ── Reports ──────────────────────────────────────────────────────────────────

class ReportRequest(BaseModel):
    store_uuids: List[str]
    start_date: date
    end_date: date
    report_type: str = "ORDER_HISTORY_REPORT"


@router.post("/reports")
def request_report(body: ReportRequest):
    """
    Request a batch report from Uber Eats.

    Order History Report constraints:
      - start_date >= today - 188 days
      - end_date   <= today - 2 days
      - end_date   >= start_date
    """
    today = date.today()
    earliest = today - timedelta(days=188)
    latest = today - timedelta(days=2)

    if body.report_type == "ORDER_HISTORY_REPORT":
        if body.start_date < earliest:
            raise HTTPException(
                status_code=400,
                detail=f"start_date too old. Earliest allowed: {earliest}",
            )
        if body.end_date > latest:
            raise HTTPException(
                status_code=400,
                detail=f"end_date too recent. Latest allowed: {latest}",
            )

    if body.end_date < body.start_date:
        raise HTTPException(status_code=400, detail="end_date must be >= start_date")

    return create_report(
        token=_get_token(),
        store_uuids=body.store_uuids,
        start_date=str(body.start_date),
        end_date=str(body.end_date),
        report_type=body.report_type,
    )
@router.get("/orders/{order_id}")
def get_order_v2(order_id: str):
    """Legacy v2 endpoint. Kept for compatibility — prefer /uber/order-details/{order_id}."""
    return get_order(_get_token(), order_id)


@router.get("/order-details/{order_id}")
def fetch_order_details(
    order_id: str,
    expand: Optional[str] = "carts,deliveries,payment",
):
    """
    Get full MerchantOrder details from Uber:
      GET /v1/delivery/order/{order_id}?expand=carts,deliveries,payment

    NOTE: Uber only returns COMPLETED orders here. Cancelled / failed orders
    return 404 "cannot find completed order by uuid" — that's the API design,
    not a bug. Use /uber/order-amount-from-db/{order_id} instead for our
    contested/cancelled flows.
    """
    return get_order_details(_get_token(), order_id, expand or None)




# ── Pull order amount from our local data (what we actually need) ────────────

from fastapi import Depends
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.contested_order import ContestedOrder
from app.models.reported_order import ReportedOrder
from app.models.store_refund import StoreRefund


@router.get("/order-amount-from-db/{order_id}")
def order_amount_from_db(order_id: str, db: Session = Depends(get_db)):
    """
    Returns the merchant-loss amount for an order_id by looking at the data
    Uber already gives us in the CSV reports — no extra API call needed.

    - For CONTESTED orders: amount = refund_covered_by_merchant (column from the
      ORDER_ERRORS_TRANSACTION_REPORT). This is exactly what the merchant lost.
    - For CANCELLED orders: the ORDER_HISTORY_REPORT zeroes out the ticket size,
      but if a matching StoreRefund exists for the same store and date, we
      surface its amount as the best-available estimate.
    """
    contested = db.query(ContestedOrder).filter(
        (ContestedOrder.order_id == order_id) | (ContestedOrder.order_uuid == order_id)
    ).first()
    if contested:
        return {
            "order_id": contested.order_id,
            "order_uuid": contested.order_uuid,
            "kind": "contested",
            "store_name": contested.store_name,
            "amount_eur": contested.refund_covered_by_merchant,
            "issue": contested.order_issue,
            "remboursement_status": contested.remboursement_status,
            "source": "ORDER_ERRORS_TRANSACTION_REPORT",
        }

    cancelled = db.query(ReportedOrder).filter(
        (ReportedOrder.order_id == order_id) | (ReportedOrder.order_uuid == order_id)
    ).first()
    if cancelled:
        # Best-effort: any StoreRefund for the same store on the same date
        matching_refund = (
            db.query(StoreRefund)
            .filter(
                StoreRefund.store_id == cancelled.store_id,
                StoreRefund.refund_date == cancelled.date_ordered,
            )
            .first()
        )
        return {
            "order_id": cancelled.order_id,
            "order_uuid": cancelled.order_uuid,
            "kind": "cancelled",
            "store_name": cancelled.store_name,
            "amount_eur": matching_refund.amount if matching_refund else None,
            "amount_source": "matched_store_refund" if matching_refund else "unknown_ticket_size_zero_in_csv",
            "remboursement_status": cancelled.remboursement_status,
            "source": "ORDER_HISTORY_REPORT",
        }

    raise HTTPException(status_code=404, detail=f"No record for order_id={order_id} in contested or cancelled tables")