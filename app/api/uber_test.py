from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.uber_service import (
    create_report,
    get_store_by_id,
    get_uber_token,
    get_order
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
def get_order_details(order_id: str):
    """Fetch details for a single order by its ID."""
    return get_order(_get_token(), order_id)