import uuid
from sqlalchemy import Column, String, Boolean, DateTime, JSON
from app.db.base import Base
from datetime import datetime, timezone


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # Personal info — required at registration
    name = Column(String, nullable=False)
    family_name = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    phone_number = Column(String, nullable=False)
    phone_code = Column(String, nullable=False)  # e.g. "+216"

    # Address — required at registration
    address_rue = Column(String, nullable=False)
    address_city = Column(String, nullable=False)
    address_gouvernorat = Column(String, nullable=False)
    address_zip_code = Column(String, nullable=False)

    # Profile photo
    avatar_url = Column(String, nullable=True)

    # Uber stores linked to this user (list of store IDs)
    uber_stores = Column(JSON, default=list, nullable=True)

    # Subscription
    abonnement_id = Column(String, nullable=True)

    # Onboarding / CRM
    is_verified_bymanager = Column(Boolean, default=False)
    # not_started | pending_call | pending_approval | approved | rejected
    onboarding_status = Column(String, default="not_started")
    rejection_reason = Column(String, nullable=True)

    # Role & status
    role = Column(String, default="user")  # user | admin | agent
    is_active = Column(Boolean, default=False)

    # Email verification
    is_verified = Column(Boolean, default=False)
    verification_token = Column(String, nullable=True)
    verification_token_expires = Column(DateTime(timezone=True), nullable=True)

    # Password reset
    reset_password_token = Column(String, nullable=True)
    reset_password_token_expires = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
