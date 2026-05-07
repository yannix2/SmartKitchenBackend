import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, Boolean
from app.db.base import Base


class Feedback(Base):
    __tablename__ = "feedbacks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)

    rating = Column(Integer, nullable=False)         # 1..5
    comment = Column(Text, nullable=True)

    # Admin-controlled — published feedbacks are shown on the public landing page
    is_published = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
