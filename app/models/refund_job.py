from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from app.db.base import Base
from datetime import datetime, timezone


class RefundJob(Base):
    __tablename__ = "refund_jobs"

    job_id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    store_id = Column(String, nullable=False)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
