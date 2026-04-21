import csv
import io
import logging
from datetime import datetime, timezone

import requests
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.contested_order import ContestedOrder
from app.models.order_report_job import OrderReportJob
from app.services.order_report_engine import _norm

logger = logging.getLogger(__name__)


def _cell(row: dict, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v is not None:
            return str(v).strip()
    return ""


def _is_zero(value: str) -> bool:
    v = value.strip().lower()
    if v in ("0", "", "false", "no", "n", "0.0", "0.00"):
        return True
    try:
        return float(v) == 0.0
    except (ValueError, TypeError):
        return False


def process_contested_csv_bulk(
    job_id: str,
    download_url: str,
    name_to_store_id: dict,
    user_id: str,
) -> int:
    """
    Bulk variant: one CSV covers multiple stores.
    Opens its own DB session so a long-running CSV parse never breaks
    the webhook request's connection.
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
        reader = csv.DictReader(io.StringIO(content))

        now = datetime.now(timezone.utc)
        saved = 0

        for row in reader:
            order_uuid = _cell(row, "Order UUID")
            refund_covered = _cell(row, "Refund Covered by Merchant")

            if _is_zero(refund_covered):
                continue

            store_name = _cell(row, "Store") or None
            store_id = norm_map.get(_norm(store_name)) if store_name else None
            if not store_id:
                logger.warning("contested bulk job %s: unknown store '%s' — skipping row", job_id, store_name)
                continue

            if order_uuid and db.query(ContestedOrder).filter(
                ContestedOrder.order_uuid == order_uuid
            ).first():
                continue

            db.add(ContestedOrder(
                store_name=store_name,
                external_store_id=_cell(row, "External Store ID") or None,
                country=_cell(row, "Country") or None,
                country_code=_cell(row, "Country Code") or None,
                city=_cell(row, "City") or None,
                workflow_uuid=_cell(row, "Workflow UUID") or None,
                order_id=_cell(row, "Order ID") or None,
                order_uuid=order_uuid or None,
                time_customer_ordered=_cell(row, "Time Customer Ordered") or None,
                time_merchant_accepted=_cell(row, "Time Merchant Accepted") or None,
                time_customer_refunded=_cell(row, "Time Customer Was Refunded") or None,
                order_issue=_cell(row, "Order Issue") or None,
                inaccurate_items=_cell(row, "Inaccurate Items") or None,
                currency_code=_cell(row, "Currency Code") or None,
                ticket_size=_cell(row, "Ticket Size") or None,
                customer_refunded=_cell(row, "Customer Refunded") or None,
                refund_covered_by_merchant=refund_covered or None,
                refund_not_covered_by_merchant=_cell(row, "Refund Not Covered by Merchant") or None,
                fulfillment_type=_cell(row, "Fulfillment Type") or None,
                order_channel=_cell(row, "Order Channel") or None,
                eats_brand=_cell(row, "Eats Brand") or None,
                store_id=store_id,
                user_id=user_id,
                report_job_id=job_id,
                remboursement_status="en attente",
                fetched_at=now,
            ))
            saved += 1

        if job:
            job.status = "completed"
        db.commit()
        logger.info("contested bulk job %s: %d orders saved", job_id, saved)
        return saved

    except Exception as exc:
        logger.error("Failed to process bulk contested CSV for job %s: %s", job_id, exc)
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


def process_contested_csv(job_id: str, download_url: str, db: Session) -> None:
    """Per-store variant — uses the caller's session (short-lived per-store jobs)."""
    job: OrderReportJob | None = (
        db.query(OrderReportJob).filter(OrderReportJob.job_id == job_id).first()
    )
    if not job or job.job_type != "contested":
        return

    try:
        response = requests.get(download_url, timeout=60)
        response.raise_for_status()

        content = response.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))

        now = datetime.now(timezone.utc)
        saved = 0

        for row in reader:
            order_uuid = _cell(row, "Order UUID")
            refund_covered = _cell(row, "Refund Covered by Merchant")

            if _is_zero(refund_covered):
                continue

            if order_uuid and db.query(ContestedOrder).filter(
                ContestedOrder.order_uuid == order_uuid
            ).first():
                continue

            db.add(ContestedOrder(
                store_name=_cell(row, "Store"),
                external_store_id=_cell(row, "External Store ID") or None,
                country=_cell(row, "Country") or None,
                country_code=_cell(row, "Country Code") or None,
                city=_cell(row, "City") or None,
                workflow_uuid=_cell(row, "Workflow UUID") or None,
                order_id=_cell(row, "Order ID") or None,
                order_uuid=order_uuid or None,
                time_customer_ordered=_cell(row, "Time Customer Ordered") or None,
                time_merchant_accepted=_cell(row, "Time Merchant Accepted") or None,
                time_customer_refunded=_cell(row, "Time Customer Was Refunded") or None,
                order_issue=_cell(row, "Order Issue") or None,
                inaccurate_items=_cell(row, "Inaccurate Items") or None,
                currency_code=_cell(row, "Currency Code") or None,
                ticket_size=_cell(row, "Ticket Size") or None,
                customer_refunded=_cell(row, "Customer Refunded") or None,
                refund_covered_by_merchant=refund_covered or None,
                refund_not_covered_by_merchant=_cell(row, "Refund Not Covered by Merchant") or None,
                fulfillment_type=_cell(row, "Fulfillment Type") or None,
                order_channel=_cell(row, "Order Channel") or None,
                eats_brand=_cell(row, "Eats Brand") or None,
                store_id=job.store_id,
                user_id=job.user_id,
                report_job_id=job_id,
                remboursement_status="en attente",
                fetched_at=now,
            ))
            saved += 1

        job.status = "completed"
        db.commit()
        logger.info("contested job %s: %d orders saved", job_id, saved)

    except Exception as exc:
        logger.error("Failed to process contested CSV for job %s: %s", job_id, exc)
        job.status = "failed"
        db.commit()
