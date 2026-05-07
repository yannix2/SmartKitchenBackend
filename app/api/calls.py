from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import get_current_user
from app.db.session import get_db
from app.models.call_log import CallLog
from app.models.user import User

router = APIRouter(prefix="/calls", tags=["calls"])

ALLOWED_ROLES = ("admin", "agent")

GDPR_ANNOUNCEMENT_FR = (
    "Bonjour, cet appel est enregistré à des fins de formation et de contrôle qualité. "
    "SmartKitchen vous contacte au sujet de votre inscription. "
    "Merci de votre patience."
)


# ── Auth guard ─────────────────────────────────────────────────────────────────

def _require_agent(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="Agent access required")
    return current_user


def _get_twilio():
    """Lazy-import Twilio client, raise 503 if not configured."""
    try:
        from twilio.rest import Client
    except ImportError:
        raise HTTPException(status_code=503, detail="Twilio library not installed")
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Twilio not configured")
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


# ── Schemas ────────────────────────────────────────────────────────────────────

class OutboundCallRequest(BaseModel):
    prospect_id: str
    phone_number: str   # E.164 format, e.g. "+21698765432"


# ── Browser capability token ───────────────────────────────────────────────────

@router.get("/token")
def get_twilio_token(agent: User = Depends(_require_agent)):
    """Generate a Twilio Access Token so the browser can make/receive calls."""
    try:
        from twilio.jwt.access_token import AccessToken
        from twilio.jwt.access_token.grants import VoiceGrant
    except ImportError:
        raise HTTPException(status_code=503, detail="Twilio library not installed")

    if not all([settings.TWILIO_ACCOUNT_SID, settings.TWILIO_API_KEY,
                settings.TWILIO_API_SECRET, settings.TWILIO_TWIML_APP_SID]):
        raise HTTPException(status_code=503, detail="Twilio not fully configured")

    token = AccessToken(
        settings.TWILIO_ACCOUNT_SID,
        settings.TWILIO_API_KEY,
        settings.TWILIO_API_SECRET,
        identity=agent.id,
        ttl=3600,
    )
    grant = VoiceGrant(
        outgoing_application_sid=settings.TWILIO_TWIML_APP_SID,
        incoming_allow=True,
    )
    token.add_grant(grant)
    return {"token": token.to_jwt(), "identity": agent.id}


# ── TwiML: called by Twilio when browser initiates an outbound call ───────────

@router.post("/twiml")
async def twiml_voice(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Twilio calls this URL (set as the TwiML App Voice URL) when a browser agent
    dials out. We return TwiML that plays a GDPR announcement then dials the prospect.
    This endpoint does NOT require JWT auth — Twilio calls it server-to-server.
    """
    form_data = await request.form()
    to_number = form_data.get("To", "")
    prospect_id = form_data.get("UserId", "")
    call_sid = form_data.get("CallSid", "")

    print("=" * 70)
    print(f"[TWIML] CallSid={call_sid}")
    print(f"[TWIML] To={to_number!r}  UserId={prospect_id!r}")
    print(f"[TWIML] CallerId(env)={settings.TWILIO_PHONE_NUMBER!r}")
    print(f"[TWIML] Full form: {dict(form_data)}")
    print("=" * 70)

    # Save initial call log
    if prospect_id and call_sid:
        try:
            log = db.query(CallLog).filter(CallLog.twilio_call_sid == call_sid).first()
            if not log:
                log = CallLog(
                    prospect_id=prospect_id,
                    twilio_call_sid=call_sid,
                    direction="outbound",
                    status="initiated",
                    phone_number=to_number,
                )
                db.add(log)
                db.commit()
        except Exception:
            pass

    webhook_base = settings.BACKEND_URL
    recording_cb = f"{webhook_base}/calls/recording-webhook"
    status_cb = f"{webhook_base}/calls/status-webhook"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Dial callerId="{settings.TWILIO_PHONE_NUMBER}">
    <Number>{to_number}</Number>
  </Dial>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


# ── TwiML: inbound calls ───────────────────────────────────────────────────────

@router.post("/twiml/inbound")
async def twiml_inbound(request: Request, db: Session = Depends(get_db)):
    """
    Handle inbound calls to the Twilio number.
    Rings all registered browser agents (Twilio Client).
    This URL is set as the Phone Number Voice webhook in the Twilio console.
    """
    form_data = await request.form()
    from_number = form_data.get("From", "unknown")
    call_sid = form_data.get("CallSid", "")

    # Try to match caller to a known prospect
    caller = db.query(User).filter(
        User.phone_number == from_number.lstrip("+"),
        User.role == "user"
    ).first()
    prospect_id = caller.id if caller else "unknown"

    if call_sid:
        try:
            log = db.query(CallLog).filter(CallLog.twilio_call_sid == call_sid).first()
            if not log:
                log = CallLog(
                    prospect_id=prospect_id if prospect_id != "unknown" else None,
                    twilio_call_sid=call_sid,
                    direction="inbound",
                    status="ringing",
                    phone_number=from_number,
                )
                db.add(log)
                db.commit()
        except Exception:
            pass

    webhook_base = settings.BACKEND_URL

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Lea" language="fr-FR">
    Bienvenue chez SmartKitchen. Veuillez patienter, un agent va prendre votre appel.
    Cet appel est enregistré à des fins de contrôle qualité.
  </Say>
  <Dial action="{webhook_base}/calls/status-webhook" method="POST">
    <Client>crm-agent</Client>
  </Dial>
</Response>"""

    return Response(content=twiml, media_type="application/xml")


# ── Webhook: call status updates ───────────────────────────────────────────────

@router.post("/status-webhook")
async def call_status_webhook(request: Request, db: Session = Depends(get_db)):
    """Twilio posts here on every call status change."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    call_status = form_data.get("CallStatus", "")
    duration = form_data.get("CallDuration")

    print("-" * 70)
    print(f"[STATUS] CallSid={call_sid}  Status={call_status}  Duration={duration}")
    print(f"[STATUS] Full form: {dict(form_data)}")
    print("-" * 70)

    if call_sid:
        log = db.query(CallLog).filter(CallLog.twilio_call_sid == call_sid).first()
        if log:
            log.status = call_status
            if duration:
                try:
                    log.duration_seconds = int(duration)
                except (ValueError, TypeError):
                    pass
            if call_status in ("completed", "failed", "busy", "no-answer"):
                log.ended_at = datetime.now(timezone.utc)
                if call_status == "no-answer":
                    log.outcome = "no_answer"
            db.commit()

    return Response(content="", status_code=204)


# ── Webhook: recording ready ───────────────────────────────────────────────────

@router.post("/recording-webhook")
async def recording_webhook(request: Request, db: Session = Depends(get_db)):
    """Twilio posts here when a call recording is ready."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    recording_sid = form_data.get("RecordingSid", "")
    recording_url = form_data.get("RecordingUrl", "")

    if call_sid and recording_url:
        log = db.query(CallLog).filter(CallLog.twilio_call_sid == call_sid).first()
        if log:
            log.twilio_recording_sid = recording_sid
            # Twilio recording URL: append .mp3 for browser playback
            log.recording_url = recording_url + ".mp3" if not recording_url.endswith(".mp3") else recording_url

            # Trigger transcription via Twilio
            try:
                client = _get_twilio()
                webhook_base = settings.BACKEND_URL
                client.recordings(recording_sid).transcriptions.create(
                    transcribe_callback=f"{webhook_base}/calls/transcription-webhook"
                )
            except Exception:
                pass

            db.commit()

    return Response(content="", status_code=204)


# ── Webhook: transcription ready ───────────────────────────────────────────────

@router.post("/transcription-webhook")
async def transcription_webhook(request: Request, db: Session = Depends(get_db)):
    """Twilio posts here when transcription of a recording is done."""
    form_data = await request.form()
    recording_sid = form_data.get("RecordingSid", "")
    transcription_text = form_data.get("TranscriptionText", "")
    transcription_sid = form_data.get("TranscriptionSid", "")

    if recording_sid and transcription_text:
        log = db.query(CallLog).filter(CallLog.twilio_recording_sid == recording_sid).first()
        if log:
            log.transcription_text = transcription_text
            log.twilio_transcription_sid = transcription_sid
            db.commit()

    return Response(content="", status_code=204)


# ── Manual outbound call log (if agent uses physical phone) ───────────────────

class ManualCallLog(BaseModel):
    prospect_id: str
    phone_number: str
    direction: str = "outbound"
    duration_seconds: Optional[int] = None
    outcome: str = "pending"
    agent_notes: Optional[str] = None


# ── SMS ───────────────────────────────────────────────────────────────────────

class SMSRequest(BaseModel):
    prospect_id: str
    phone_number: str   # E.164, e.g. "+21698765432"
    message: str


@router.post("/sms", status_code=status.HTTP_201_CREATED)
def send_sms(
    payload: SMSRequest,
    agent: User = Depends(_require_agent),
    db: Session = Depends(get_db),
):
    """Send an SMS to a prospect via Twilio."""
    if not settings.TWILIO_PHONE_NUMBER:
        raise HTTPException(status_code=503, detail="TWILIO_PHONE_NUMBER not configured")

    client = _get_twilio()
    try:
        msg = client.messages.create(
            body=payload.message,
            from_=settings.TWILIO_PHONE_NUMBER,
            to=payload.phone_number,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Twilio error: {e}")

    return {"sid": msg.sid, "status": msg.status, "to": payload.phone_number}


# ── Inbound SMS webhook ────────────────────────────────────────────────────────

@router.post("/sms-webhook")
async def sms_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Twilio posts here when an SMS is received on your Twilio number.
    Set this URL as the 'A message comes in' webhook on your Twilio phone number.
    """
    form_data = await request.form()
    from_number = form_data.get("From", "")
    body = form_data.get("Body", "")

    # Auto-reply (optional — remove if you don't want it)
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Message>Merci pour votre message. Un agent SmartKitchen vous répondra bientôt.</Message>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ── Manual outbound call log (if agent uses physical phone) ───────────────────

@router.post("/manual", status_code=status.HTTP_201_CREATED)
def log_manual_call(
    payload: ManualCallLog,
    agent: User = Depends(_require_agent),
    db: Session = Depends(get_db),
):
    """Log a call that happened outside the browser (physical phone, etc.)."""
    log = CallLog(
        prospect_id=payload.prospect_id,
        agent_id=agent.id,
        direction=payload.direction,
        status="completed",
        phone_number=payload.phone_number,
        duration_seconds=payload.duration_seconds,
        outcome=payload.outcome,
        agent_notes=payload.agent_notes,
        ended_at=datetime.now(timezone.utc),
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return {"id": log.id, "message": "Call logged"}
