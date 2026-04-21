from sqlalchemy import Column, String, DateTime
from app.db.base import Base
from datetime import datetime, timezone


class OrderReportJob(Base):
    __tablename__ = "order_report_jobs"

    job_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=True)   # NULL for system-triggered jobs
    store_id = Column(String, nullable=False)
    job_type = Column(String, default="cancelled")  # cancelled | contested
    status = Column(String, default="pending")       # pending | completed | failed
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
