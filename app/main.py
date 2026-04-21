from contextlib import asynccontextmanager
from fastapi import FastAPI
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from fastapi.middleware.cors import CORSMiddleware
from app.api.uber_webhook import router as uber_router
from app.api.uber_test import router as uber_test_router
from app.api.users_auth import router as auth_router
from app.api.refund import router as refund_router
from app.api.smartkitchen_stores import router as sk_stores_router
from app.api.order_reports import router as order_reports_router
from app.api.profile import router as profile_router
from app.api.store_refunds import router as store_refunds_router
from app.api.order_proofs import router as order_proofs_router, run_daily_refund_emails
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

__all__ = [User, UberReport, UserStore, RefundJob, RefundRequest, SmartKitchenStore, OrderReportJob, ReportedOrder, ContestedOrder, StoreRefund]

Base.metadata.create_all(bind=engine)


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


@app.get("/")
def root():
    return {"message": "SmartKitchen running 🚀"}


