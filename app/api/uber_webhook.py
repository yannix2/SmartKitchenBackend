import hashlib
import hmac

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from app.core.config import settings
from app.db.session import get_db
from app.models.order_report_job import OrderReportJob
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.uber_report import UberReport
from app.services.contested_report_engine import process_contested_csv, process_contested_csv_bulk
from app.services.order_report_engine import process_order_history_csv, process_order_history_csv_bulk
from app.services.payment_details_engine import process_payment_details_csv_bulk
from app.services.refund_engine import process_report_csv


router = APIRouter()


def _verify_signature(body: bytes, signature: str) -> bool:
    expected = hmac.new(
        settings.UBER_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhooks/uber")
async def uber_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.body()
    except ClientDisconnect:
        # Uber sometimes retries and the first delivery disconnects mid-read — ignore silently
        return {"status": "ok"}

    signature = request.headers.get("X-Uber-Signature", "")

    if not _verify_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    event_type = payload.get("event_type")

    print(f"📩 Uber webhook received: {event_type}", payload)

    if event_type == "eats.report.success":
        job_id = payload.get("job_id")
        report_type = payload.get("report_type")
        sections = payload.get("report_metadata", {}).get("sections", [])

        first_download_url: str | None = None

        for section in sections:
            section_id = section.get("section_id")
            download_url = section.get("download_url")
            content_type = section.get("content_type")

            print(f"📊 Report ready — type={report_type} job_id={job_id}")
            print(f"   ↳ {section_id} ({content_type}): {download_url}")

            if first_download_url is None:
                first_download_url = download_url

            record = db.query(UberReport).filter(UberReport.job_id == job_id).first()
            if record:
                record.download_url = download_url
                record.section_id = section_id
                record.content_type = content_type
            else:
                db.add(UberReport(
                    job_id=job_id,
                    report_type=report_type,
                    section_id=section_id,
                    download_url=download_url,
                    content_type=content_type,
                ))

        db.commit()

        if job_id and first_download_url:
            # Look up the job — may not exist yet if webhook fires before sync thread commits
            job = db.query(OrderReportJob).filter(OrderReportJob.job_id == job_id).first()

            # Skip re-processing if already completed (Uber retries the webhook 3-4 times)
            if job and job.status == "completed":
                print(f"[Webhook] job_id={job_id} already completed — skipping duplicate delivery")
                return {"status": "ok"}

            is_bulk = job.store_id == "bulk" if job else False

            if not job:
                print(f"[Webhook] job_id={job_id} not in DB yet — defaulting to bulk engine")
                is_bulk = True

            if is_bulk:
                # Build store_name → store_id map — active stores only
                active_stores = db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
                name_to_id = {s.store_name: s.store_id for s in active_stores if s.store_name}
                triggered_user_id = job.user_id if job else "system"

                # Determine job_type from report_type when job not found
                if job:
                    inferred_job_type = job.job_type
                else:
                    if report_type == "ORDER_HISTORY_REPORT":
                        inferred_job_type = "cancelled"
                    elif report_type == "PAYMENT_DETAILS_REPORT":
                        inferred_job_type = "payment"
                    else:
                        inferred_job_type = "contested"

                if inferred_job_type == "cancelled":
                    process_order_history_csv_bulk(job_id, first_download_url, name_to_id, triggered_user_id)
                elif inferred_job_type == "payment":
                    process_payment_details_csv_bulk(job_id, first_download_url, name_to_id, triggered_user_id)
                else:
                    process_contested_csv_bulk(job_id, first_download_url, name_to_id, triggered_user_id)
            else:
                # Per-store job — use the regular engines
                process_report_csv(job_id, first_download_url, db)
                process_order_history_csv(job_id, first_download_url, db)
                process_contested_csv(job_id, first_download_url, db)

        return {"status": "ok"}

    return {"status": "ok"}


@router.get("/uber/reports")
def list_reports(db: Session = Depends(get_db)):
    """List all received reports with their download URLs."""
    reports = db.query(UberReport).order_by(UberReport.received_at.desc()).all()
    return [
        {
            "job_id": r.job_id,
            "report_type": r.report_type,
            "section_id": r.section_id,
            "content_type": r.content_type,
            "received_at": r.received_at,
        }
        for r in reports
    ]


@router.get("/uber/reports/{job_id}/download")
def download_report(job_id: str, db: Session = Depends(get_db)):
    """Stream the CSV file for a completed report."""
    record = db.query(UberReport).filter(UberReport.job_id == job_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Report not found")
    if not record.download_url:
        raise HTTPException(status_code=404, detail="Download URL not yet available")

    response = requests.get(record.download_url, stream=True)
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch report from Uber")

    filename = f"{record.report_type}_{record.job_id}.csv"
    return StreamingResponse(
        response.iter_content(chunk_size=8192),
        media_type=record.content_type or "text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

