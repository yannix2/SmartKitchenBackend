import logging
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


def _get_mailjet():
    """Return an authenticated Mailjet client, or None if credentials are missing."""
    from mailjet_rest import Client  # pip install mailjet-rest

    if not settings.MAILJET_API_KEY or not settings.MAILJET_API_SECRET:
        return None
    return Client(auth=(settings.MAILJET_API_KEY, settings.MAILJET_API_SECRET), version="v3.1")


def _send(to_email: str, to_name: str, subject: str, text_body: str) -> None:
    """Send a plain-text email via Mailjet."""
    mailjet = _get_mailjet()
    if mailjet is None:
        logger.warning(
            "Mailjet credentials not configured — skipping email to %s. Subject: %s",
            to_email,
            subject,
        )
        return

    data = {
        "Messages": [
            {
                "From": {
                    "Email": settings.MAILJET_FROM_EMAIL,
                    "Name": settings.MAILJET_FROM_NAME,
                },
                "To": [{"Email": to_email, "Name": to_name}],
                "Subject": subject,
                "TextPart": text_body,
            }
        ]
    }

    try:
        result = mailjet.send.create(data=data)
        if result.status_code == 200:
            logger.info("Email sent to %s via Mailjet. Subject: %s", to_email, subject)
        else:
            logger.error(
                "Mailjet error sending to %s: status=%s body=%s",
                to_email,
                result.status_code,
                result.json(),
            )
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)


# ── Auth emails ────────────────────────────────────────────────────────────────

def send_verification_email(to_email: str, name: str, token: str) -> None:
    """Send account verification email with a clickable link."""
    verify_url = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    subject = "Verify your SmartKitchen account"
    body = (
        f"Hello {name},\n\n"
        f"Thank you for registering on SmartKitchen.\n\n"
        f"Please click the link below to verify your email address and activate your account:\n\n"
        f"{verify_url}\n\n"
        f"This link will expire in 24 hours.\n\n"
        f"If you did not create an account, please ignore this email.\n\n"
        f"Best regards,\n"
        f"SmartKitchen Team"
    )
    _send(to_email, name, subject, body)


def send_reset_password_email(to_email: str, name: str, token: str) -> None:
    """Send password reset email with a clickable link."""
    reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    subject = "Reset your SmartKitchen password"
    body = (
        f"Hello {name},\n\n"
        f"We received a request to reset your SmartKitchen password.\n\n"
        f"Click the link below to choose a new password:\n\n"
        f"{reset_url}\n\n"
        f"This link will expire in 1 hour.\n\n"
        f"If you did not request a password reset, please ignore this email — your password will not change.\n\n"
        f"Best regards,\n"
        f"SmartKitchen Team"
    )
    _send(to_email, name, subject, body)


# ── Contested order refund emails ─────────────────────────────────────────────

def send_contested_refund_email(
    restaurant_name: str,
    restaurant_uuid: str,
    order_number: str,
    attachment_bytes: bytes,
    attachment_name: str,
    content_type: str,
) -> bool:
    """
    Send a contested-order refund request to Uber support via Mailjet,
    with the proof image attached as a base64 inline attachment.
    Returns True if the email was sent successfully.
    """
    import base64

    mailjet = _get_mailjet()
    if mailjet is None:
        logger.warning("Mailjet credentials not configured — skipping contested refund email for order %s", order_number)
        return False

    recipient = settings.UBER_SUPPORT_EMAIL
    if not recipient:
        logger.warning("UBER_SUPPORT_EMAIL not set — skipping contested refund email for order %s", order_number)
        return False

    subject = f"Demande de remboursement — Commande contestée {order_number}"
    body = (
        f"Bonjour,\n\n"
        f"Veuillez trouver ci-joint le justificatif relatif à la commande contestée suivante :\n\n"
        f"- Restaurant : {restaurant_name}\n"
        f"- UUID restaurant : {restaurant_uuid}\n"
        f"- Numéro de commande : {order_number}\n"
        f"- Pièce jointe : {attachment_name}\n\n"
        f"Merci de bien vouloir traiter cette demande.\n\n"
        f"Cordialement,"
    )

    encoded = base64.b64encode(attachment_bytes).decode("utf-8")

    data = {
        "Messages": [
            {
                "From": {
                    "Email": settings.MAILJET_FROM_EMAIL,
                    "Name": settings.MAILJET_FROM_NAME,
                },
                "To": [{"Email": recipient, "Name": "Uber Eats Support"}],
                "Subject": subject,
                "TextPart": body,
                "Attachments": [
                    {
                        "ContentType": content_type,
                        "Filename": attachment_name,
                        "Base64Content": encoded,
                    }
                ],
            }
        ]
    }

    try:
        result = mailjet.send.create(data=data)
        if result.status_code == 200:
            logger.info("Contested refund email sent for order %s", order_number)
            return True
        else:
            logger.error(
                "Mailjet error for order %s: status=%s body=%s",
                order_number,
                result.status_code,
                result.json(),
            )
            return False
    except Exception as exc:
        logger.error("Failed to send contested refund email for order %s: %s", order_number, exc)
        return False


# ── Refund emails (existing) ───────────────────────────────────────────────────

def send_refund_email(
    order_id: str,
    order_type: str,
    store_id: str,
    attachment_path: str = None,
) -> None:
    """Send a refund request email to Uber support."""
    recipient = settings.UBER_SUPPORT_EMAIL
    if not recipient:
        logger.warning("UBER_SUPPORT_EMAIL not set — skipping refund email for order %s", order_id)
        return

    if order_type == "cancelled":
        subject = f"Refund Request — Cancelled Order {order_id}"
        body = (
            f"Dear Uber Eats Support,\n\n"
            f"We are writing to request a refund for order {order_id} associated with store {store_id}.\n\n"
            f"This order was cancelled and we have not received the corresponding refund. "
            f"Please review this case and process the refund at your earliest convenience.\n\n"
            f"Order ID: {order_id}\n"
            f"Store ID: {store_id}\n"
            f"Issue Type: Cancelled Order\n\n"
            f"Thank you for your assistance.\n\n"
            f"Best regards,\n"
            f"SmartKitchen Operations Team"
        )
    else:
        subject = f"Refund Request — Contested Order {order_id}"
        body = (
            f"Dear Uber Eats Support,\n\n"
            f"We are writing to request a refund for order {order_id} associated with store {store_id}.\n\n"
            f"This order has been contested due to a dispute. We are requesting a full review and refund "
            f"for this transaction.\n\n"
            f"Order ID: {order_id}\n"
            f"Store ID: {store_id}\n"
            f"Issue Type: Contested / Disputed Order\n\n"
            f"Thank you for your assistance.\n\n"
            f"Best regards,\n"
            f"SmartKitchen Operations Team"
        )

    # Note: Mailjet v3.1 send API does not support binary attachments in the same
    # simple wrapper — attachment handling would require base64 encoding via the
    # Attachments field. For now, log a warning if an attachment was requested.
    if attachment_path and Path(attachment_path).exists():
        logger.warning(
            "Attachment %s not sent — Mailjet attachment support not yet wired up for order %s",
            attachment_path,
            order_id,
        )

    _send(recipient, "Uber Eats Support", subject, body)
