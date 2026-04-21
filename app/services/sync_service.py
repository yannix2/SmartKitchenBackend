import logging
import time
from datetime import datetime, timedelta, timezone

from app.db.session import SessionLocal
from app.models.order_report_job import OrderReportJob
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.user_store import UserStore
from app.services.contested_report_engine import process_contested_csv
from app.services.order_report_engine import process_order_history_csv
from app.services.uber_service import create_report, get_store_status, get_stores, get_uber_token

logger = logging.getLogger(__name__)

# Uber report lag constraints
_CANCELLED_LAG = 2    # ORDER_HISTORY_REPORT: end_date = today - 2
_CONTESTED_LAG = 4    # ORDER_ERRORS_TRANSACTION_REPORT: end_date = today - 4
_PAYMENT_LAG = 0      # PAYMENT_DETAILS_REPORT: no lookback lag, end_date = today
_PERIOD_DAYS = 30     # sync window for cancelled + contested
_PAYMENT_PERIOD_DAYS = 29  # Uber counts range inclusively: 29-day diff = 30 days inclusive

# Polling config
_POLL_INTERVAL_SECONDS = 30    # check every 30 s
_POLL_MAX_ATTEMPTS = 40        # 40 × 30 s = 20 min max per report

# Delay between stores to avoid hammering Uber
_STORE_REST_SECONDS = 5


def _date_range(lag_days: int):
    today = datetime.now(timezone.utc).date()
    end = today - timedelta(days=lag_days)
    start = end - timedelta(days=_PERIOD_DAYS)
    return start.isoformat(), end.isoformat()


def _poll_until_ready(workflow_id: str) -> dict | None:
    """
    Poll Uber every _POLL_INTERVAL_SECONDS until the report is done.
    Also short-circuits if the webhook already processed this job.
    Returns the final status response, or None on timeout.
    """
    print(f"[Poll] waiting for webhook to complete workflow={workflow_id} ...")
    for attempt in range(1, _POLL_MAX_ATTEMPTS + 1):
        # Use a fresh session each check — avoids transaction isolation hiding webhook commits
        with SessionLocal() as check_session:
            db_job = check_session.query(OrderReportJob).filter(OrderReportJob.job_id == workflow_id).first()
            job_status = db_job.status if db_job else "not_found"

        print(f"[Poll] attempt {attempt}/{_POLL_MAX_ATTEMPTS}  workflow={workflow_id}  db_status={job_status}")

        if job_status == "completed":
            print(f"[Poll] ✓ webhook completed workflow={workflow_id}")
            return {"_completed_by_webhook": True}
        if job_status == "failed":
            print(f"[Poll] ✗ job marked failed for workflow={workflow_id}")
            return {"_failed": True}

        print(f"[Poll]   still pending — sleeping {_POLL_INTERVAL_SECONDS}s ...")
        time.sleep(_POLL_INTERVAL_SECONDS)

    print(f"[Poll] ✗ timed out after {_POLL_MAX_ATTEMPTS} attempts for workflow={workflow_id}")
    return None


def _sync_store(token: str, store_id: str, user_id: str, db) -> dict:
    """
    Request + wait + process both report types for one store.
    Returns {"cancelled": int, "contested": int, "errors": int}.
    """
    cancelled_start, cancelled_end = _date_range(_CANCELLED_LAG)
    contested_start, contested_end = _date_range(_CONTESTED_LAG)

    results = {"cancelled": 0, "contested": 0, "errors": 0}

    for report_type, job_type, start, end in [
        ("ORDER_HISTORY_REPORT",             "cancelled", cancelled_start, cancelled_end),
        ("ORDER_ERRORS_TRANSACTION_REPORT",  "contested", contested_start, contested_end),
    ]:
        # 1 — Request report from Uber
        result = create_report(token, [store_id], start, end, report_type)
        workflow_id = result.get("workflow_id") or result.get("job_id")

        if not workflow_id:
            logger.warning("No workflow_id for %s store=%s: %s", report_type, store_id, result)
            results["errors"] += 1
            continue

        # 2 — Save job as pending
        db.add(OrderReportJob(
            job_id=workflow_id,
            user_id=user_id,
            store_id=store_id,
            job_type=job_type,
            status="pending",
        ))
        db.commit()

        logger.info("Waiting for %s  workflow=%s  store=%s ...", report_type, workflow_id, store_id)

        # 3 — Poll until Uber finishes (or webhook beats us to it)
        final = _poll_until_ready(workflow_id)

        if final is None:
            # Timed out
            db.query(OrderReportJob).filter(OrderReportJob.job_id == workflow_id).update({"status": "failed"})
            db.commit()
            results["errors"] += 1
            continue

        # If the webhook already processed it, orders are already saved — nothing more to do
        if final.get("_completed_by_webhook"):
            results[job_type] += 1
            continue

        # 4 — Check final status
        final_status = str(final.get("status") or final.get("state") or "").lower()
        if final_status not in ("success", "completed"):
            db.query(OrderReportJob).filter(OrderReportJob.job_id == workflow_id).update({"status": "failed"})
            db.commit()
            results["errors"] += 1
            continue

        # 5 — Extract download URL from poll response
        sections = final.get("report_metadata", {}).get("sections", [])
        download_url = sections[0].get("download_url") if sections else None

        if not download_url:
            logger.warning("No download_url in completed report %s", workflow_id)
            db.query(OrderReportJob).filter(OrderReportJob.job_id == workflow_id).update({"status": "failed"})
            db.commit()
            results["errors"] += 1
            continue

        # 6 — Process CSV (engines update job.status to completed/failed internally)
        if job_type == "cancelled":
            process_order_history_csv(workflow_id, download_url, db)
        else:
            process_contested_csv(workflow_id, download_url, db)

        results[job_type] += 1
        logger.info("Done %s  workflow=%s  store=%s", report_type, workflow_id, store_id)

    return results


def run_bulk_sync(user_id: str = "system"):
    """
    V2 sync — fires only 2 Uber reports (one per type) covering ALL active SK
    store UUIDs at once. Much more efficient than one-report-per-store.

    After download, each CSV row is linked to its store by matching the
    'Store' column against smartkitchen_stores.store_name (unique per store).

    user_id="system"       — APScheduler background run
    user_id=<admin uuid>   — manual trigger via /admin/sync-nowV2
    """
    db = SessionLocal()
    try:
        print(f"[BulkSync] ▶ started  user={user_id}")

        # Step 1 — Uber token
        print("[BulkSync] Step 1: obtaining Uber token ...")
        token_data = get_uber_token()
        token = token_data.get("access_token")
        if not token:
            print(f"[BulkSync] ✗ could not obtain token: {token_data}")
            logger.error("BulkSync: could not obtain Uber token — %s", token_data)
            return
        print("[BulkSync] ✓ token obtained")

        # Step 2 — Load active stores
        print("[BulkSync] Step 2: loading active stores from DB ...")
        stores = db.query(SmartKitchenStore).filter(
            SmartKitchenStore.is_active != 0
        ).all()

        if not stores:
            print("[BulkSync] ✗ no active stores found — aborting")
            logger.warning("BulkSync: no active SmartKitchen stores found")
            return

        store_uuids = [s.store_id for s in stores]
        name_to_id = {s.store_name: s.store_id for s in stores if s.store_name}
        print(f"[BulkSync] ✓ {len(stores)} active stores  |  {len(name_to_id)} with names mapped")

        totals = {"cancelled": 0, "contested": 0, "payment": 0, "errors": 0}
        cancelled_start, cancelled_end = _date_range(_CANCELLED_LAG)
        contested_start, contested_end = _date_range(_CONTESTED_LAG)
        today = datetime.now(timezone.utc).date()
        payment_end = today.isoformat()
        payment_start = (today - timedelta(days=_PAYMENT_PERIOD_DAYS)).isoformat()

        for report_type, job_type, start, end in [
            ("ORDER_HISTORY_REPORT",             "cancelled", cancelled_start, cancelled_end),
            ("ORDER_ERRORS_TRANSACTION_REPORT",  "contested", contested_start, contested_end),
            ("PAYMENT_DETAILS_REPORT",           "payment",   payment_start,   payment_end),
        ]:
            print(f"[BulkSync] Step 3 [{report_type}]: requesting report  period={start}→{end} ...")
            result = create_report(token, store_uuids, start, end, report_type)
            workflow_id = result.get("workflow_id") or result.get("job_id")
            print(f"[BulkSync]   Uber response: {result}")

            if not workflow_id:
                print(f"[BulkSync] ✗ no workflow_id returned for {report_type} — skipping")
                logger.warning("BulkSync: no workflow_id for %s: %s", report_type, result)
                totals["errors"] += 1
                continue

            print(f"[BulkSync] ✓ workflow_id={workflow_id} — saving job as pending ...")
            db.add(OrderReportJob(
                job_id=workflow_id,
                user_id=user_id,
                store_id="bulk",
                job_type=job_type,
                status="pending",
            ))
            db.commit()
            print(f"[BulkSync]   job saved. Now polling Uber until report is ready ...")

            final = _poll_until_ready(workflow_id)

            if final is None:
                print(f"[BulkSync] ✗ polling timed out for workflow={workflow_id}")
                db.query(OrderReportJob).filter(OrderReportJob.job_id == workflow_id).update({"status": "failed"})
                db.commit()
                totals["errors"] += 1
                continue

            if final.get("_completed_by_webhook"):
                print(f"[BulkSync] ✓ webhook already processed workflow={workflow_id} — skipping CSV download")
                totals[job_type] += 1
                continue

            if final.get("_failed"):
                print(f"[BulkSync] ✗ job failed for workflow={workflow_id}")
                totals["errors"] += 1
                continue

            # Timed out without webhook — count as error
            print(f"[BulkSync] ✗ unexpected poll result for workflow={workflow_id}: {final}")
            totals["errors"] += 1

        print(
            f"[BulkSync] ■ complete  user={user_id}  "
            f"cancelled={totals['cancelled']}  contested={totals['contested']}  "
            f"payment={totals['payment']}  errors={totals['errors']}"
        )

    except Exception as exc:
        print(f"[BulkSync] ✗ CRASHED: {exc}")
        logger.error("BulkSync crashed: %s", exc)
        db.rollback()
    finally:
        db.close()


def run_payment_sync(user_id: str = "system"):
    """
    Fire a single PAYMENT_DETAILS_REPORT for all active stores and wait for
    the webhook to process it. Used by the admin payment-syncV2 endpoint.
    """
    db = SessionLocal()
    try:
        print(f"[PaymentSync] ▶ started  user={user_id}")

        token_data = get_uber_token()
        token = token_data.get("access_token")
        if not token:
            print(f"[PaymentSync] ✗ could not obtain token: {token_data}")
            return

        stores = db.query(SmartKitchenStore).filter(SmartKitchenStore.is_active != 0).all()
        if not stores:
            print("[PaymentSync] ✗ no active stores found")
            return

        store_uuids = [s.store_id for s in stores]
        print(f"[PaymentSync] ✓ {len(stores)} active stores")

        today = datetime.now(timezone.utc).date()
        end = today.isoformat()
        start = (today - timedelta(days=_PAYMENT_PERIOD_DAYS)).isoformat()

        print(f"[PaymentSync] requesting PAYMENT_DETAILS_REPORT  period={start}→{end} ...")
        result = create_report(token, store_uuids, start, end, "PAYMENT_DETAILS_REPORT")
        workflow_id = result.get("workflow_id") or result.get("job_id")
        print(f"[PaymentSync]   Uber response: {result}")

        if not workflow_id:
            print(f"[PaymentSync] ✗ no workflow_id returned — aborting")
            return

        db.add(OrderReportJob(
            job_id=workflow_id,
            user_id=user_id,
            store_id="bulk",
            job_type="payment",
            status="pending",
        ))
        db.commit()
        print(f"[PaymentSync] ✓ job saved. Polling ...")

        final = _poll_until_ready(workflow_id)

        if final is None:
            db.query(OrderReportJob).filter(OrderReportJob.job_id == workflow_id).update({"status": "failed"})
            db.commit()
            print(f"[PaymentSync] ✗ polling timed out")
            return

        if final.get("_completed_by_webhook"):
            print(f"[PaymentSync] ✓ webhook processed workflow={workflow_id}")
        elif final.get("_failed"):
            print(f"[PaymentSync] ✗ job failed for workflow={workflow_id}")
        else:
            print(f"[PaymentSync] ✗ unexpected result: {final}")

    except Exception as exc:
        print(f"[PaymentSync] ✗ CRASHED: {exc}")
        logger.error("PaymentSync crashed: %s", exc)
        db.rollback()
    finally:
        db.close()


def run_stores_sync():
    """
    Pull all stores from the Uber API and upsert them into smartkitchen_stores.
    Also promotes any pending user_stores to verified where the store UUID now exists.
    Mirrors the logic in POST /smartkitchen-stores/admin/sync.
    """
    from datetime import datetime, timezone as tz
    db = SessionLocal()
    try:
        token_data = get_uber_token()
        token = token_data.get("access_token")
        if not token:
            logger.error("StoresSync: could not obtain Uber token — %s", token_data)
            return

        stores_response = get_stores(token)
        stores = stores_response.get("stores", [])
        if not stores:
            logger.warning("StoresSync: no stores returned from Uber — %s", stores_response)
            return

        synced_ids = []
        for store in stores:
            store_id = store.get("store_id") or store.get("id") or store.get("uuid")
            if not store_id:
                continue

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
            record.web_url = store.get("web_url")
            record.pos_integration_enabled = pos.get("integration_enabled", False)
            record.synced_at = datetime.now(tz.utc)

            # Check live status via dedicated endpoint
            status_data = get_store_status(token, store_id)
            uber_status = str(status_data.get("status") or "").upper()
            offline_reason = status_data.get("offlineReason") or None

            record.status = uber_status or "UNKNOWN"
            record.offline_reason = offline_reason

            # is_active=1 if ONLINE, or OFFLINE only because OUT_OF_MENU_HOURS
            # (store exists and will reopen — include in syncs)
            # is_active=0 for any other offline reason (truly inactive/paused)
            if uber_status == "ONLINE":
                record.is_active = 1
            elif uber_status == "OFFLINE" and offline_reason == "OUT_OF_MENU_HOURS":
                record.is_active = 1
            else:
                record.is_active = 0

            synced_ids.append(store_id)

        db.flush()

        # Auto-verify pending user_stores whose store_id is now in SK master table
        sk_name_map = {
            s.store_id: s.store_name
            for s in db.query(SmartKitchenStore).filter(SmartKitchenStore.store_id.in_(synced_ids)).all()
        }
        pending = (
            db.query(UserStore)
            .filter(UserStore.store_id.in_(synced_ids), UserStore.status == "en attente d'integration")
            .all()
        )
        for us in pending:
            us.status = "verified"
            us.store_name = sk_name_map.get(us.store_id)

        db.commit()
        logger.info("StoresSync complete — upserted=%d  verified=%d", len(synced_ids), len(pending))

    except Exception as exc:
        logger.error("StoresSync crashed: %s", exc)
        db.rollback()
    finally:
        db.close()


def run_daily_sync():
    """
    Daily job: sync SK stores from Uber first, then run the bulk order report sync.
    This ensures the store name map is always fresh before CSV row linking.
    """
    logger.info("Daily sync starting — step 1: sync stores")
    run_stores_sync()
    logger.info("Daily sync — step 2: bulk order reports")
    run_bulk_sync(user_id="system")


def run_biweekly_sync(user_id: str = "system"):
    """
    Run every 2 weeks (APScheduler) or on-demand (admin sync-now).

    user_id="system"       — background scheduler run
    user_id=<admin uuid>   — manual trigger via /admin/sync-now

    For each active SK store:
      • Requests ORDER_HISTORY_REPORT and ORDER_ERRORS_TRANSACTION_REPORT
      • Polls Uber until each report is ready (never saves orders with pending status)
      • Rests _STORE_REST_SECONDS between stores to avoid hammering Uber
    """
    db = SessionLocal()
    try:
        token_data = get_uber_token()
        token = token_data.get("access_token")
        if not token:
            logger.error("Sync: could not obtain Uber token — %s", token_data)
            return

        stores = db.query(SmartKitchenStore).filter(
            SmartKitchenStore.is_active != 0
        ).all()

        if not stores:
            logger.warning("Sync: no active SmartKitchen stores found")
            return

        logger.info("Sync started  user=%s  stores=%d", user_id, len(stores))
        totals = {"cancelled": 0, "contested": 0, "errors": 0}

        for i, store in enumerate(stores):
            if i > 0:
                logger.info("Resting %ds before next store ...", _STORE_REST_SECONDS)
                time.sleep(_STORE_REST_SECONDS)

            logger.info("Store %d/%d  id=%s", i + 1, len(stores), store.store_id)
            try:
                counts = _sync_store(token, store.store_id, user_id, db)
                for k in totals:
                    totals[k] += counts.get(k, 0)
            except Exception as store_exc:
                logger.error("Store %s failed — skipping: %s", store.store_id, store_exc)
                try:
                    db.rollback()
                except Exception:
                    pass
                totals["errors"] += 1

        logger.info(
            "Sync complete  user=%s  cancelled=%d  contested=%d  errors=%d",
            user_id, totals["cancelled"], totals["contested"], totals["errors"],
        )

    except Exception as exc:
        logger.error("Sync crashed: %s", exc)
        db.rollback()
    finally:
        db.close()
