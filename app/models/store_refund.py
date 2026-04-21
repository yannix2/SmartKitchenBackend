from sqlalchemy import Column, Integer, String, DateTime
from app.db.base import Base
from datetime import datetime, timezone


class StoreRefund(Base):
    __tablename__ = "store_refunds"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # From CSV
    store_name = Column(String, nullable=True)
    store_id = Column(String, nullable=True)       # matched via store_name normalization
    refund_date = Column(String, nullable=True)    # Order Date column
    amount = Column(String, nullable=True)         # Total payout (negative value from Uber)
    payout_reference_id = Column(String, nullable=True)  # dedup key

    # Manually linked by admin later
    linked_order_id = Column(String, nullable=True)

    # Context
    report_job_id = Column(String, nullable=False)
    user_id = Column(String, nullable=True)
    fetched_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
