import csv
import io
import logging
import re
import unicodedata
from datetime import datetime, timezone

import requests
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.order_report_job import OrderReportJob
from app.models.reported_order import ReportedOrder

logger = logging.getLogger(__name__)

_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F9FF"
    "\U0001F1E0-\U0001F1FF"
    "\u2600-\u27BF]+",
    flags=re.UNICODE,
)


def _norm(name: str) -> str:
    name = unicodedata.normalize("NFKC", name)
    name = name.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")
    name = _EMOJI_RE.sub("", name)
    return " ".join(name.split()).lower()


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


def process_order_history_csv_bulk(
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
            completed = _cell(row, "Completed?")
            ticket_size = _cell(row, "Ticket Size")
            order_status = _cell(row, "Order Status")

            if not (_is_zero(completed) and _is_zero(ticket_size)):
                continue
            if order_status.lower() != "canceled":
                continue

            store_name = _cell(row, "Store") or None
            store_id = norm_map.get(_norm(store_name)) if store_name else None
            if not store_id:
                logger.warning("order_history bulk job %s: unknown store '%s' — skipping", job_id, store_name)
                continue

            workflow_uuid = _cell(row, "Workflow UUID") or None

            if workflow_uuid and db.query(ReportedOrder).filter(
                ReportedOrder.workflow_uuid == workflow_uuid
            ).first():
                continue

            db.add(ReportedOrder(
                store_name=store_name,
                country_code=_cell(row, "Country Code") or None,
                order_id=_cell(row, "Order ID") or None,
                order_uuid=_cell(row, "Order UUID") or None,
                order_status=_cell(row, "Order Status") or None,
                menu_item_count=_cell(row, "Menu Item Count") or None,
                date_ordered=_cell(row, "Date Ordered") or None,
                workflow_uuid=workflow_uuid,
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
        logger.info("order_history bulk job %s: %d cancelled orders saved", job_id, saved)
        return saved

    except Exception as exc:
        logger.error("Failed to process bulk order history CSV for job %s: %s", job_id, exc)
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


def process_order_history_csv(job_id: str, download_url: str, db: Session) -> None:
    """Per-store variant. Saves rows where Completed? == 0 AND Ticket Size == 0."""
    job: OrderReportJob | None = (
        db.query(OrderReportJob).filter(OrderReportJob.job_id == job_id).first()
    )
    if not job:
        return

    try:
        response = requests.get(download_url, timeout=60)
        response.raise_for_status()

        content = response.content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))

        now = datetime.now(timezone.utc)
        saved = 0

        for row in reader:
            completed = _cell(row, "Completed?")
            ticket_size = _cell(row, "Ticket Size")
            order_status = _cell(row, "Order Status")

            if not (_is_zero(completed) and _is_zero(ticket_size)):
                continue
            if order_status.lower() != "canceled":
                continue

            workflow_uuid = _cell(row, "Workflow UUID") or None

            if workflow_uuid and db.query(ReportedOrder).filter(
                ReportedOrder.workflow_uuid == workflow_uuid
            ).first():
                continue

            db.add(ReportedOrder(
                store_name=_cell(row, "Store") or None,
                country_code=_cell(row, "Country Code") or None,
                order_id=_cell(row, "Order ID") or None,
                order_uuid=_cell(row, "Order UUID") or None,
                order_status=_cell(row, "Order Status") or None,
                menu_item_count=_cell(row, "Menu Item Count") or None,
                date_ordered=_cell(row, "Date Ordered") or None,
                workflow_uuid=workflow_uuid,
                store_id=job.store_id,
                user_id=job.user_id,
                report_job_id=job_id,
                remboursement_status="en attente",
                fetched_at=now,
            ))
            saved += 1

        job.status = "completed"
        db.commit()
        logger.info("order_history job %s: %d cancelled orders saved", job_id, saved)

    except Exception as exc:
        logger.error("Failed to process order history CSV for job %s: %s", job_id, exc)
        job.status = "failed"
        db.commit()
