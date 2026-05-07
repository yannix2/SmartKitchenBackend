import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, DateTime, ForeignKey
from app.db.base import Base


class Abonnement(Base):
    __tablename__ = "abonnements"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, unique=True)

    # Stripe identifiers
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)

    # Plan
    plan_name = Column(String, default="Pro")
    status = Column(String, default="inactive")  # active | inactive | past_due | cancelled | trialing
    price = Column(Float, nullable=True)
    currency = Column(String, default="eur")

    # Lifecycle
    started_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
