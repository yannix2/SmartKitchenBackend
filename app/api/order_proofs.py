import logging

from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from sqlalchemy.orm import Session

from app.api.users_auth import _require_admin
from app.core.security import get_current_user
from app.db.session import SessionLocal, get_db
from app.models.contested_order import ContestedOrder
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.user import User
from app.models.user_store import STATUS_VERIFIED, UserStore
from app.services.email_service import send_contested_refund_email
from app.services.supabase_storage import find_and_download_proof, upload_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/order-proofs", tags=["order-proofs"])

_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_SIZE = 10 * 1024 * 1024  # 10 MB


# ── Shared helper ──────────────────────────────────────────────────────────────

def _send_for_stores(store_ids: list[str], db: Session) -> dict:
    """
    Find pending contested orders for the given store_ids, check proofs,
    send emails, update statuses. Returns a summary dict.
    """
    stores = (
        db.query(SmartKitchenStore)
        .filter(SmartKitchenStore.store_id.in_(store_ids), SmartKitchenStore.is_active != 0)
        .all()
    )
    store_map = {s.store_id: s for s in stores}
    active_store_ids = list(store_map.keys())

    sent = 0
    skipped_no_proof = 0
    errors = 0

    if not active_store_ids:
        return {"sent": sent, "skipped_no_proof": skipped_no_proof, "errors": errors}

    pending = (
        db.query(ContestedOrder)
        .filter(
            ContestedOrder.store_id.in_(active_store_ids),
            ContestedOrder.remboursement_status == "en attente",
        )
        .all()
    )

    for order in pending:
        order_id = order.order_id or order.order_uuid
        if not order_id:
            continue

        proof = find_and_download_proof(order_id)
        if proof is None:
            logger.info("No proof found for order %s — skipping", order_id)
            skipped_no_proof += 1
            continue

        attachment_bytes, attachment_name, content_type = proof

        store = store_map.get(order.store_id)
        restaurant_name = store.store_name if store else (order.store_name or order.store_id)
        restaurant_uuid = order.store_id

        try:
            ok = send_contested_refund_email(
                restaurant_name=restaurant_name,
                restaurant_uuid=restaurant_uuid,
                order_number=order_id,
                attachment_bytes=attachment_bytes,
                attachment_name=attachment_name,
                content_type=content_type,
            )
            if ok:
                order.remboursement_status = "email envoyé"
                sent += 1
            else:
                errors += 1
        except Exception as exc:
            logger.error("Error sending email for order %s: %s", order_id, exc)
            errors += 1

    db.commit()
    return {"sent": sent, "skipped_no_proof": skipped_no_proof, "errors": errors}


# ── Daily scheduled job ────────────────────────────────────────────────────────

def run_daily_refund_emails() -> None:
    """
    Scheduled daily job — send contested-order refund emails for all active
    users' verified stores. Opens its own DB session so it is safe to run
    from APScheduler outside of a request context.
    """
    db = SessionLocal()
    try:
        active_users = db.query(User).filter(User.is_active == True, User.role != "admin").all()

        store_ids = set()
        for user in active_users:
            rows = (
                db.query(UserStore)
                .filter(UserStore.user_id == user.id, UserStore.status == STATUS_VERIFIED)
                .all()
            )
            for r in rows:
                store_ids.add(r.store_id)

        if not store_ids:
            logger.info("[daily_refund_emails] No active users with verified stores — nothing to do.")
            return

        result = _send_for_stores(list(store_ids), db)
        logger.info("[daily_refund_emails] %s", result)
    except Exception as exc:
        logger.error("[daily_refund_emails] Unexpected error: %s", exc)
    finally:
        db.close()


# ── Upload proof ───────────────────────────────────────────────────────────────

@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_proof(
    files: List[UploadFile] = File(description="One or more proof images (jpeg/png/webp, max 10 MB each)"),
    _admin: User = Depends(_require_admin),
):
    """
    Admin — upload one or more proof images using their original filenames
    (e.g. OrderID.jpg). Each file is stored as-is at order-proofs/{filename}
    in Supabase.
    """
    uploaded = []
    failed = []

    for file in files:
        if file.content_type not in _ALLOWED_TYPES:
            failed.append({"filename": file.filename, "error": f"Unsupported type '{file.content_type}'"})
            continue

        data = await file.read()
        if len(data) > _MAX_SIZE:
            failed.append({"filename": file.filename, "error": "File too large (max 10 MB)"})
            continue

        filename = file.filename
        if not filename:
            failed.append({"filename": None, "error": "Missing filename"})
            continue

        try:
            public_url = upload_file(
                bucket="order-proofs",
                path=filename,
                data=data,
                content_type=file.content_type,
            )
            uploaded.append({"filename": filename, "url": public_url})
        except Exception as exc:
            logger.error("Failed to upload proof %s: %s", filename, exc)
            failed.append({"filename": filename, "error": str(exc)})

    return {"uploaded": uploaded, "failed": failed}


# ── Send refund emails — user ──────────────────────────────────────────────────

@router.post("/send-refund-emails")
def send_refund_emails_user(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    User — send refund emails for contested orders belonging to their own
    verified stores. Only orders with remboursement_status == 'en attente'
    and an existing proof in Supabase are processed.
    """
    store_ids = [
        r.store_id
        for r in db.query(UserStore)
        .filter(UserStore.user_id == current_user.id, UserStore.status == STATUS_VERIFIED)
        .all()
    ]

    if not store_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No verified stores linked to your account.",
        )

    return _send_for_stores(store_ids, db)


# ── Send refund emails — admin ─────────────────────────────────────────────────

@router.post("/admin/send-refund-emails")
def send_refund_emails_admin(
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """
    Admin — send refund emails for contested orders belonging to active users'
    verified stores only. Skips orders with no proof in Supabase.
    """
    active_users = (
        db.query(User)
        .filter(User.is_active == True, User.role != "admin")
        .all()
    )

    store_ids: set[str] = set()
    for user in active_users:
        rows = (
            db.query(UserStore)
            .filter(UserStore.user_id == user.id, UserStore.status == STATUS_VERIFIED)
            .all()
        )
        for r in rows:
            store_ids.add(r.store_id)

    if not store_ids:
        return {"sent": 0, "skipped_no_proof": 0, "errors": 0}

    return _send_for_stores(list(store_ids), db)
