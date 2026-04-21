from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean
from app.db.base import Base
from datetime import datetime, timezone


class SmartKitchenStore(Base):
    __tablename__ = "smartkitchen_stores"

    store_id = Column(String, primary_key=True)
    store_name = Column(String, nullable=True)
    address = Column(String, nullable=True)
    city = Column(String, nullable=True)
    postal_code = Column(String, nullable=True)
    country = Column(String, nullable=True)
    state = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    timezone = Column(String, nullable=True)
    avg_prep_time = Column(Integer, nullable=True)
    status = Column(String, nullable=True)           # ONLINE | OFFLINE | etc. (raw from Uber)
    offline_reason = Column(String, nullable=True)   # e.g. OUT_OF_MENU_HOURS
    is_active = Column(Integer, default=1)           # 1 = include in syncs, 0 = skip
    web_url = Column(String, nullable=True)
    pos_integration_enabled = Column(Boolean, default=False)
    synced_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
