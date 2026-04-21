import csv
import io
import logging
from datetime import datetime, timezone

import requests

from app.db.session import SessionLocal
from app.models.order_report_job import OrderReportJob
from app.models.store_refund import StoreRefund
from app.services.order_report_engine import _norm

logger = logging.getLogger(__name__)

_REFUND_DESCRIPTION = "restaurant refunds"

_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"]


def _cell(row: dict, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None:
            return str(v).strip()
    return ""


def _normalize_date(raw: str) -> str:
    """Convert any common date string to YYYY-MM-DD; fall back to raw value."""
    value = raw.strip()[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw


def process_payment_details_csv_bulk(
    job_id: str,
    download_url: str,
    name_to_store_id: dict,
    user_id: str,
) -> int:
    """
    Process a PAYMENT_DETAILS_REPORT CSV.
    Saves only rows where 'Other payments description' == 'Restaurant refunds'.
    Deduplicates by payout_reference_id.
    Returns the count of newly saved rows.
    """
    norm_map = {_norm(k): v for k, v in name_to_store_id.items()}

    db = SessionLocal()
    try:
        job: OrderReportJob | None = (
            db.query(OrderReportJob).filter(OrderReportJob.job_id == job_id).first()
        )
        response = requests.get(download_url, timeout=60)
        response.raise_for_status()

        content = response.content.decode("utf-8-sig")
        # PAYMENT_DETAILS_REPORT has 2 header rows:
        #   row 1 → long descriptions (skip)
        #   row 2 → short column names (Order ID, Store Name, Total payout, ...)
        lines = content.splitlines()
        usable = "\n".join(lines[1:])
        reader = csv.DictReader(io.StringIO(usable))
        # Strip trailing/leading spaces from all column names
        if reader.fieldnames:
            reader.fieldnames = [f.strip() for f in reader.fieldnames]

        print(f"[PaymentDetails] CSV columns: {reader.fieldnames}")

        now = datetime.now(timezone.utc)
        saved = 0
        total_rows = 0
        description_values: set = set()

        for row in reader:
            total_rows += 1
            description = _cell(row, "Other payments description").lower().strip()
            description_values.add(description or "(empty)")

            if description != _REFUND_DESCRIPTION:
                continue

            payout_ref = _cell(row, "Payout reference ID") or None

            # Deduplicate by payout_reference_id
            if payout_ref and db.query(StoreRefund).filter(
                StoreRefund.payout_reference_id == payout_ref
            ).first():
                continue

            store_name = _cell(row, "Store Name") or None
            store_id = norm_map.get(_norm(store_name)) if store_name else None

            if not store_id:
                logger.warning(
                    "payment_details bulk job %s: unknown store '%s' — saving without store_id",
                    job_id, store_name,
                )

            amount = _cell(row, "Total payout") or None
            raw_date = _cell(row, "Order Date")
            refund_date = _normalize_date(raw_date) if raw_date else None

            db.add(StoreRefund(
                store_name=store_name,
                store_id=store_id,
                refund_date=refund_date,
                amount=amount,
                payout_reference_id=payout_ref,
                linked_order_id=None,
                report_job_id=job_id,
                user_id=user_id,
                fetched_at=now,
            ))
            saved += 1

        non_empty = {v for v in description_values if v != "(empty)"}
        print(f"[PaymentDetails] total_rows={total_rows}  matched={saved}")
        print(f"[PaymentDetails] non-empty description values: {non_empty}")

        if job:
            job.status = "completed"
        db.commit()
        print(f"[PaymentDetails] bulk job {job_id}: {saved} refunds saved")
        return saved

    except Exception as exc:
        logger.error("Failed to process payment details CSV for job %s: %s", job_id, exc)
        try:
            db.rollback()
            job = db.query(OrderReportJob).filter(OrderReportJob.job_id == job_id).first()
            if job:
                job.status = "failed"
            db.commit()
        except Exception:
            pass
        return 0
    finally:
        db.close()
