import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey
from app.db.base import Base


class CallLog(Base):
    __tablename__ = "call_logs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))

    # Participants
    prospect_id = Column(String, ForeignKey("users.id"), nullable=False)   # the user being onboarded
    agent_id = Column(String, ForeignKey("users.id"), nullable=True)        # agent who made/took call

    # Twilio identifiers
    twilio_call_sid = Column(String, nullable=True, unique=True)
    twilio_recording_sid = Column(String, nullable=True)
    twilio_transcription_sid = Column(String, nullable=True)

    # Call metadata
    direction = Column(String, default="outbound")   # inbound | outbound
    status = Column(String, default="initiated")     # initiated | ringing | in-progress | completed | failed | no-answer | busy
    duration_seconds = Column(Integer, nullable=True)
    phone_number = Column(String, nullable=True)     # phone number called / that called

    # Recording & transcription
    recording_url = Column(String, nullable=True)
    transcription_text = Column(Text, nullable=True)

    # Agent outcome after the call
    # pending | approved | rejected | callback | no_answer
    outcome = Column(String, default="pending")
    agent_notes = Column(Text, nullable=True)

    # Timestamps
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
