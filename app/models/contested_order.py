from sqlalchemy import Column, Integer, String, DateTime, Numeric
from app.db.base import Base
from datetime import datetime, timezone


class ContestedOrder(Base):
    __tablename__ = "contested_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # CSV fields
    store_name = Column(String, nullable=True)           # Store
    external_store_id = Column(String, nullable=True)    # External Store ID
    country = Column(String, nullable=True)
    country_code = Column(String, nullable=True)
    city = Column(String, nullable=True)
    workflow_uuid = Column(String, nullable=True)         # Workflow UUID
    order_id = Column(String, nullable=True)             # Order ID (short)
    order_uuid = Column(String, nullable=True)           # Order UUID
    time_customer_ordered = Column(String, nullable=True)
    time_merchant_accepted = Column(String, nullable=True)
    time_customer_refunded = Column(String, nullable=True)
    order_issue = Column(String, nullable=True)
    inaccurate_items = Column(String, nullable=True)
    currency_code = Column(String, nullable=True)
    ticket_size = Column(String, nullable=True)
    customer_refunded = Column(String, nullable=True)
    refund_covered_by_merchant = Column(String, nullable=True)
    refund_not_covered_by_merchant = Column(String, nullable=True)
    fulfillment_type = Column(String, nullable=True)
    order_channel = Column(String, nullable=True)
    eats_brand = Column(String, nullable=True)

    # Context
    store_id = Column(String, nullable=False)
    user_id = Column(String, nullable=True)   # NULL for system-triggered jobs
    report_job_id = Column(String, nullable=False)

    remboursement_status = Column(String, nullable=False, default="en attente")

    fetched_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
