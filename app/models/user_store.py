from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from app.db.base import Base
from datetime import datetime, timezone

STATUS_PENDING = "en attente d'integration"
STATUS_VERIFIED = "verified"


class UserStore(Base):
    __tablename__ = "user_stores"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    store_id = Column(String, nullable=False)
    store_name = Column(String, nullable=True)
    status = Column(String, default=STATUS_PENDING, nullable=False)
    linked_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
