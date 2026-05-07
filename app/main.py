from contextlib import asynccontextmanager
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.api.uber_webhook import router as uber_router
from app.api.uber_test import router as uber_test_router
from app.api.users_auth import router as auth_router
from app.api.refund import router as refund_router
from app.api.smartkitchen_stores import router as sk_stores_router
from app.api.order_reports import router as order_reports_router
from app.api.profile import router as profile_router
from app.api.store_refunds import router as store_refunds_router
from app.api.order_proofs import router as order_proofs_router, run_daily_refund_emails
from app.api.billing import router as billing_router
from app.api.onboarding import router as onboarding_router
from app.api.crm import router as crm_router
from app.api.calls import router as calls_router
from app.api.feedback import router as feedback_router
from app.db.base import Base
from app.db.session import engine
from app.services.sync_service import run_daily_sync

# Import all models so SQLAlchemy registers their tables before create_all
from app.models.user import User
from app.models.uber_report import UberReport
from app.models.user_store import UserStore
from app.models.refund_job import RefundJob
from app.models.refund_request import RefundRequest
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.order_report_job import OrderReportJob
from app.models.reported_order import ReportedOrder
from app.models.contested_order import ContestedOrder
from app.models.store_refund import StoreRefund
from app.models.abonnement import Abonnement
from app.models.onboarding_form import OnboardingForm
from app.models.call_log import CallLog
from app.models.feedback import Feedback

__all__ = [User, UberReport, UserStore, RefundJob, RefundRequest, SmartKitchenStore, OrderReportJob, ReportedOrder, ContestedOrder, StoreRefund, Abonnement, OnboardingForm, CallLog, Feedback]

Base.metadata.create_all(bind=engine)


# ── Inline schema migrations ─────────────────────────────────────────────────
# Run idempotent fixups for schema changes that `create_all` cannot apply
# (it never alters existing tables/indexes/constraints).

def _run_inline_migrations() -> None:
    with engine.begin() as conn:
        dialect = engine.dialect.name  # "postgresql" | "sqlite" | ...

        # Consolidate roles: legacy "manager" → "agent".
        # The role system is now strictly: user | admin | agent.
        result = conn.execute(text(
            "UPDATE users SET role = 'agent' WHERE role = 'manager'"
        ))
        if getattr(result, "rowcount", 0):
            print(f"[migrate] migrated {result.rowcount} 'manager' user(s) → 'agent'")

        # Drop the old UNIQUE INDEX on feedbacks.user_id and recreate it as
        # a non-unique index, so users can leave more than one feedback.
        if dialect == "postgresql":
            row = conn.execute(text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname = 'ix_feedbacks_user_id'"
            )).first()
            if row and "UNIQUE" in (row[0] or "").upper():
                conn.execute(text("DROP INDEX public.ix_feedbacks_user_id"))
                conn.execute(text("CREATE INDEX ix_feedbacks_user_id ON feedbacks (user_id)"))
                print("[migrate] feedbacks.user_id is no longer unique")

            # Defensive: drop a UNIQUE constraint variant if one exists
            cons = conn.execute(text(
                "SELECT c.conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'feedbacks' AND c.contype = 'u' "
                "AND pg_get_constraintdef(c.oid) LIKE '%(user_id)'"
            )).first()
            if cons:
                conn.execute(text(f'ALTER TABLE feedbacks DROP CONSTRAINT "{cons[0]}"'))
                print(f"[migrate] dropped unique constraint {cons[0]} on feedbacks.user_id")


try:
    _run_inline_migrations()
except Exception as e:
    print(f"[migrate] warning: inline migration failed: {e}")


# ── Scheduler ─────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()
scheduler.add_job(
    run_daily_sync,
    trigger=IntervalTrigger(weeks=2),
    id="biweekly_sync",
    replace_existing=True,
)
scheduler.add_job(
    run_daily_refund_emails,
    trigger=IntervalTrigger(days=1),
    id="daily_refund_emails",
    replace_existing=True,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="SmartKitchen API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# include routes
app.include_router(uber_router)
app.include_router(uber_test_router)
app.include_router(auth_router)
app.include_router(refund_router)
app.include_router(sk_stores_router)
app.include_router(order_reports_router)
app.include_router(profile_router)
app.include_router(store_refunds_router)
app.include_router(order_proofs_router)
app.include_router(billing_router)
app.include_router(onboarding_router)
app.include_router(crm_router)
app.include_router(calls_router)
app.include_router(feedback_router)


@app.get("/")
def root():
    return {"message": "SmartKitchen running 🚀"}


