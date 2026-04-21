import csv
import io
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.users_auth import _require_admin
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.user import User
from app.models.user_store import STATUS_PENDING, STATUS_VERIFIED, UserStore
from app.services.uber_service import get_store_status, get_stores, get_uber_token

router = APIRouter(prefix="/smartkitchen-stores", tags=["smartkitchen-stores"])


# ── Schemas ────────────────────────────────────────────────────────────────────

class StoreUUIDsRequest(BaseModel):
    store_ids: List[str]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _upsert_store(db: Session, store: dict) -> SmartKitchenStore:
    """Insert or update a SmartKitchenStore row from an Uber store dict."""
    store_id = store.get("store_id") or store.get("id") or store.get("uuid")
    if not store_id:
        return None

    location = store.get("location", {})
    pos = store.get("pos_data", {})

    record = db.query(SmartKitchenStore).filter(SmartKitchenStore.store_id == store_id).first()
    if not record:
        record = SmartKitchenStore(store_id=store_id)
        db.add(record)

    record.store_name = store.get("name") or store.get("store_name")
    record.address = location.get("address")
    record.city = location.get("city")
    record.postal_code = location.get("postal_code")
    record.country = location.get("country")
    record.state = location.get("state")
    record.latitude = location.get("latitude")
    record.longitude = location.get("longitude")
    record.timezone = store.get("timezone")
    record.avg_prep_time = store.get("avg_prep_time")
    record.status = store.get("status")
    record.web_url = store.get("web_url")
    record.pos_integration_enabled = pos.get("integration_enabled", False)
    record.synced_at = datetime.now(timezone.utc)
    return record


def _auto_verify_user_stores(db: Session, store_ids: List[str]) -> int:
    """Promote pending user stores to verified and sync store_name from smartkitchen_stores."""
    sk_name_map = {
        s.store_id: s.store_name
        for s in db.query(SmartKitchenStore).filter(SmartKitchenStore.store_id.in_(store_ids)).all()
    }
    updated = (
        db.query(UserStore)
        .filter(UserStore.store_id.in_(store_ids), UserStore.status == STATUS_PENDING)
        .all()
    )
    for us in updated:
        us.status = STATUS_VERIFIED
        us.store_name = sk_name_map.get(us.store_id)
    return len(updated)


# ── Admin: sync from Uber ──────────────────────────────────────────────────────

@router.post("/admin/sync", status_code=status.HTTP_200_OK)
def sync_from_uber(
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """
    Admin — pull all stores from the Uber API where SmartKitchen is manager,
    upsert them into smartkitchen_stores, and auto-verify matching pending user stores.
    """
    token_data = get_uber_token()
    token = token_data.get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not obtain Uber token: {token_data}",
        )

    stores_response = get_stores(token)
    stores = stores_response.get("stores", [])
    if not stores and "error" in stores_response:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Uber API error: {stores_response}",
        )

    synced_ids = []
    for store in stores:
        record = _upsert_store(db, store)
        if not record:
            continue

        # Check live status via dedicated endpoint
        status_data = get_store_status(token, record.store_id)
        uber_status = str(status_data.get("status") or "").upper()
        offline_reason = status_data.get("offlineReason") or None

        record.status = uber_status or "UNKNOWN"
        record.offline_reason = offline_reason

        if uber_status == "ONLINE" or uber_status == "UNKNOWN":
            record.is_active = 1
        elif uber_status == "OFFLINE" and offline_reason != "INVISIBLE":
            record.is_active = 1
        else:
            record.is_active = 0

        synced_ids.append(record.store_id)

    db.flush()
    verified_count = _auto_verify_user_stores(db, synced_ids)
    db.commit()

    return {
        "synced": len(synced_ids),
        "user_stores_verified": verified_count,
        "store_ids": synced_ids,
    }



@router.get("/admin/list")
def list_sk_stores(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    _admin: User = Depends(_require_admin),
):
    """Admin — list all SmartKitchen stores, optionally filtered by status."""
    q = db.query(SmartKitchenStore)
    if status_filter:
        q = q.filter(SmartKitchenStore.status == status_filter)
    stores = q.all()
    return [
        {
            "store_id": s.store_id,
            "store_name": s.store_name,
        }
        for s in stores
    ]


# ── User: declare store UUIDs ──────────────────────────────────────────────────

@router.post("/my/add", status_code=status.HTTP_201_CREATED)
def add_my_stores(
    payload: StoreUUIDsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    User — declare store UUIDs they own.
    Status is set to 'en attente d'integration' unless the store is already
    in smartkitchen_stores (manager confirmed), in which case it's 'verified'.
    """
    sk_map = {
        s.store_id: s
        for s in db.query(SmartKitchenStore)
        .filter(SmartKitchenStore.store_id.in_(payload.store_ids))
        .all()
    }

    results = []
    for store_id in payload.store_ids:
        existing = (
            db.query(UserStore)
            .filter(UserStore.user_id == current_user.id, UserStore.store_id == store_id)
            .first()
        )
        if existing:
            results.append({"store_id": store_id, "action": "already_exists", "status": existing.status})
            continue

        sk = sk_map.get(store_id)
        new_status = STATUS_VERIFIED if sk else STATUS_PENDING
        record = UserStore(
            user_id=current_user.id,
            store_id=store_id,
            store_name=sk.store_name if sk else None,
            status=new_status,
        )
        db.add(record)
        results.append({"store_id": store_id, "action": "added", "status": new_status})

    db.commit()
    return {"results": results}


@router.get("/my")
def my_stores(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    User — list their stores. On every call, re-validates each store against
    smartkitchen_stores: pending stores become verified if SK is now manager,
    and store_name is always kept in sync.
    """
    user_stores = (
        db.query(UserStore).filter(UserStore.user_id == current_user.id).all()
    )

    store_ids = [us.store_id for us in user_stores]
    sk_map = {
        s.store_id: s
        for s in db.query(SmartKitchenStore).filter(SmartKitchenStore.store_id.in_(store_ids)).all()
    }

    changed = False
    for us in user_stores:
        sk = sk_map.get(us.store_id)
        if sk:
            # Promote to verified if still pending
            if us.status == STATUS_PENDING:
                us.status = STATUS_VERIFIED
                changed = True
            # Always sync store_name from master table
            if us.store_name != sk.store_name:
                us.store_name = sk.store_name
                changed = True
        else:
            # SK is no longer manager — revert to pending if it was verified
            if us.status == STATUS_VERIFIED:
                us.status = STATUS_PENDING
                us.store_name = None
                changed = True

    if changed:
        db.commit()

    return [
        {
            "id": us.id,
            "store_id": us.store_id,
            "store_name": us.store_name,
            "status": us.status,
        }
        for us in user_stores
    ]


@router.post("/my/import-csv", status_code=status.HTTP_201_CREATED)
async def import_stores_from_csv(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    User — upload an Uber Eats stores CSV (columns: Shop UUID, Name, Address,
    Town/city, Postal code, External ID) to bulk-import their stores.
    """
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="CSV is empty or has no data rows.")

    store_ids_from_csv = [
        r.get("Shop UUID", "").strip().strip('"')
        for r in rows
        if r.get("Shop UUID", "").strip().strip('"')
    ]

    if not store_ids_from_csv:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid Shop UUID values found in CSV.")

    sk_map = {
        s.store_id: s
        for s in db.query(SmartKitchenStore)
        .filter(SmartKitchenStore.store_id.in_(store_ids_from_csv))
        .all()
    }

    results = []
    for row in rows:
        store_id = row.get("Shop UUID", "").strip().strip('"')
        name     = row.get("Name", "").strip().strip('"') or None

        if not store_id:
            continue

        existing = (
            db.query(UserStore)
            .filter(UserStore.user_id == current_user.id, UserStore.store_id == store_id)
            .first()
        )
        if existing:
            results.append({
                "store_id": store_id,
                "store_name": existing.store_name or name,
                "action": "already_exists",
                "status": existing.status,
            })
            continue

        sk = sk_map.get(store_id)
        new_status = STATUS_VERIFIED if sk else STATUS_PENDING
        db.add(UserStore(
            user_id=current_user.id,
            store_id=store_id,
            store_name=sk.store_name if sk else name,
            status=new_status,
        ))
        results.append({
            "store_id": store_id,
            "store_name": sk.store_name if sk else name,
            "action": "added",
            "status": new_status,
        })

    db.commit()
    return {"results": results}


@router.delete("/my/{store_id}", status_code=status.HTTP_200_OK)
def remove_my_store(
    store_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """User — remove a store from their list."""
    record = (
        db.query(UserStore)
        .filter(UserStore.user_id == current_user.id, UserStore.store_id == store_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Store not found")
    db.delete(record)
    db.commit()
    return {"message": f"Store {store_id} removed"}
