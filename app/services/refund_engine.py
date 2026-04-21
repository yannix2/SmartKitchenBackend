import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from sqlalchemy.orm import Session

from app.models.refund_job import RefundJob
from app.models.refund_request import RefundRequest
from app.models.user_store import UserStore
from app.services.email_service import send_refund_email
from app.services.uber_service import create_report, get_uber_token

logger = logging.getLogger(__name__)

PROOFS_DIR = Path("uploads/proofs")

# CSV column name candidates
_ORDER_ID_KEYS = ["order_id", "Order ID", "Order UUID", "id"]
_STATUS_KEYS = ["status", "Status", "Order Status"]


def _get_csv_value(row: dict, candidates: list[str]) -> str | None:
    for key in candidates:
        if key in row:
            return row[key]
    return None


def run_refund_for_user(user_id: int, db: Session) -> list[str]:
    """Trigger ORDER_HISTORY_REPORT jobs for every store linked to user_id.

    Returns a list of Uber workflow IDs (job_ids).
    """
    stores = db.query(UserStore).filter(UserStore.user_id == user_id).all()
    if not stores:
        logger.info("No linked stores for user %s", user_id)
        return []

    token_data = get_uber_token()
    token = token_data.get("access_token")
    if not token:
        logger.error("Could not obtain Uber token for user %s: %s", user_id, token_data)
        return []

    today = datetime.now(timezone.utc).date()
    start_date = (today - timedelta(days=9)).isoformat()
    end_date = (today - timedelta(days=2)).isoformat()

    job_ids: list[str] = []

    for store in stores:
        result = create_report(
            token,
            [store.store_id],
            start_date,
            end_date,
            "ORDER_HISTORY_REPORT",
        )
        workflow_id = result.get("workflow_id") or result.get("job_id")
        if not workflow_id:
            logger.warning(
                "No workflow_id returned for store %s, user %s: %s",
                store.store_id,
                user_id,
                result,
            )
            continue

        job = RefundJob(
            job_id=workflow_id,
            user_id=user_id,
            store_id=store.store_id,
            status="pending",
        )
        db.add(job)
        job_ids.append(workflow_id)

    db.commit()
    return job_ids


def process_report_csv(job_id: str, download_url: str, db: Session) -> None:
    """Download and process a completed Uber report CSV.

    Detects cancelled and contested orders, sends refund emails, and records
    RefundRequest rows. Updates RefundJob.status to "completed" or "failed".
    """
    job: RefundJob | None = db.query(RefundJob).filter(RefundJob.job_id == job_id).first()
    if not job:
        # Not a refund job — nothing to do
        return

    try:
        response = requests.get(download_url, timeout=60)
        response.raise_for_status()

        content = response.content.decode("utf-8-sig")  # strip BOM if present
        reader = csv.DictReader(io.StringIO(content))

        now = datetime.now(timezone.utc)

        for row in reader:
            raw_status = _get_csv_value(row, _STATUS_KEYS) or ""
            status_upper = raw_status.upper()

            if "CANCELLED" in status_upper:
                order_type = "cancelled"
            elif "DISPUTED" in status_upper or "CONTESTED" in status_upper:
                order_type = "contested"
            else:
                continue

            order_id = _get_csv_value(row, _ORDER_ID_KEYS)
            if not order_id:
                logger.warning("Row has no recognisable order_id column — skipping: %s", row)
                continue

            proof_path = PROOFS_DIR / f"{order_id}.jpg"
            attachment = str(proof_path) if proof_path.exists() else None

            send_refund_email(order_id, order_type, job.store_id, attachment)

            refund_req = RefundRequest(
                job_id=job_id,
                user_id=job.user_id,
                store_id=job.store_id,
                order_id=order_id,
                order_type=order_type,
                status="sent",
                sent_at=now,
            )
            db.add(refund_req)

        job.status = "completed"
        db.commit()

    except Exception as exc:
        logger.error("Failed to process report CSV for job %s: %s", job_id, exc)
        job.status = "failed"
        db.commit()
