from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from app.db.base import Base
from datetime import datetime, timezone


class RefundRequest(Base):
    __tablename__ = "refund_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, ForeignKey("refund_jobs.job_id"), nullable=False)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    store_id = Column(String, nullable=False)
    order_id = Column(String, nullable=False)
    order_type = Column(String, nullable=False)  # "cancelled" or "contested"
    status = Column(String, default="pending")
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
