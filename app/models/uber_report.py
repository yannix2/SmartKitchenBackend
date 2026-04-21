from sqlalchemy import Column, String, DateTime
from app.db.base import Base
from datetime import datetime, timezone


class UberReport(Base):
    __tablename__ = "uber_reports"

    job_id = Column(String, primary_key=True)
    report_type = Column(String, nullable=False)
    section_id = Column(String, nullable=True)
    download_url = Column(String, nullable=True)
    content_type = Column(String, nullable=True)
    received_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
