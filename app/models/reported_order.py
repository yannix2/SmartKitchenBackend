from sqlalchemy import Column, Float, Integer, String, DateTime
from app.db.base import Base
from datetime import datetime, timezone


class ReportedOrder(Base):
    __tablename__ = "reported_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # CSV fields
    store_name = Column(String, nullable=True)       # Store
    country_code = Column(String, nullable=True)     # Country Code
    order_id = Column(String, nullable=True)         # Order ID
    order_uuid = Column(String, nullable=True)       # Order UUID
    order_status = Column(String, nullable=True)     # Order Status
    menu_item_count = Column(String, nullable=True)  # Menu Item Count
    date_ordered = Column(String, nullable=True)     # Date Ordered
    workflow_uuid = Column(String, nullable=True)    # Workflow UUID

    # Context
    store_id = Column(String, nullable=False)
    user_id = Column(String, nullable=True)   # NULL for system-triggered jobs
    report_job_id = Column(String, nullable=False)

    # Remboursement
    remboursement_status = Column(String, nullable=False, default="en attente")
    fetched_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Admin-entered amount (Uber CSV always reports ticket_size=0 for cancelled
    # orders; admin pulls the real amount from the Uber Eats Manager dashboard
    # and stores it here so a refund email can be sent).
    manual_amount = Column(Float, nullable=True)
    refund_email_sent_at = Column(DateTime(timezone=True), nullable=True)
