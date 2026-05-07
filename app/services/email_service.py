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


def send_cancelled_refund_email(
    restaurant_name: str,
    restaurant_uuid: str,
    order_number: str,
    amount_eur: float,
) -> bool:
    """
    Send a cancelled-order refund request to Uber support via Mailjet.
    Unlike contested, no proof attachment — admin enters the amount manually
    (the Uber Eats Manager dashboard is the source of truth for cancelled
    orders since the CSV reports ticket_size=0).
    Returns True if sent.
    """
    mailjet = _get_mailjet()
    if mailjet is None:
        logger.warning("Mailjet credentials not configured — skipping cancelled refund email for order %s", order_number)
        return False

    recipient = settings.UBER_SUPPORT_EMAIL
    if not recipient:
        logger.warning("UBER_SUPPORT_EMAIL not set — skipping cancelled refund email for order %s", order_number)
        return False

    amount_str = f"{amount_eur:,.2f} €".replace(",", " ").replace(".", ",")
    subject = f"Demande de remboursement — Commande annulée {order_number}"
    body = (
        f"Bonjour,\n\n"
        f"Nous sollicitons le remboursement de la commande annulée suivante :\n\n"
        f"- Restaurant : {restaurant_name}\n"
        f"- UUID restaurant : {restaurant_uuid}\n"
        f"- Numéro de commande : {order_number}\n"
        f"- Montant : {amount_str}\n\n"
        f"Cette commande a été annulée et n'a pas été facturée au client. "
        f"Merci de bien vouloir traiter cette demande de remboursement.\n\n"
        f"Cordialement,"
    )

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
            }
        ]
    }

    try:
        result = mailjet.send.create(data=data)
        if result.status_code == 200:
            logger.info("Cancelled refund email sent for order %s", order_number)
            return True
        logger.error(
            "Mailjet error for cancelled order %s: status=%s body=%s",
            order_number, result.status_code, result.json(),
        )
        return False
    except Exception as exc:
        logger.error("Failed to send cancelled refund email for order %s: %s", order_number, exc)
        return False


# ── CRM onboarding emails ─────────────────────────────────────────────────────

def send_onboarding_received_email(to_email: str, name: str) -> None:
    """Notify user their onboarding form was received and a call is coming."""
    _send(
        to_email, name,
        "Welcome to SmartKitchen — Your application is under review",
        f"Hello {name},\n\n"
        f"Thank you for completing your SmartKitchen onboarding form!\n\n"
        f"Our team has received your application and one of our agents will call you "
        f"at your preferred time to walk you through the platform, explain our process, "
        f"and answer any questions you may have.\n\n"
        f"You will be fully set up and ready to recover your refunds as soon as your "
        f"account is approved.\n\n"
        f"If you have any urgent questions in the meantime, feel free to reply to this email.\n\n"
        f"Best regards,\n"
        f"SmartKitchen Team",
    )


def send_approval_email(to_email: str, name: str) -> None:
    """Notify user their account has been approved by a manager."""
    _send(
        to_email, name,
        "Your SmartKitchen account has been approved!",
        f"Hello {name},\n\n"
        f"Great news — your SmartKitchen account has been approved!\n\n"
        f"You now have full access to the platform. Log in to subscribe and start "
        f"recovering your Uber Eats refunds automatically.\n\n"
        f"If you have any questions, do not hesitate to contact our team.\n\n"
        f"Best regards,\n"
        f"SmartKitchen Team",
    )


def send_rejection_email(to_email: str, name: str, reason: str) -> None:
    """Notify user their application was rejected with a reason."""
    _send(
        to_email, name,
        "Update on your SmartKitchen application",
        f"Hello {name},\n\n"
        f"Thank you for your interest in SmartKitchen.\n\n"
        f"After reviewing your application, we are unfortunately unable to approve "
        f"your account at this time.\n\n"
        f"Reason: {reason}\n\n"
        f"If you believe this is an error or your situation has changed, please do not "
        f"hesitate to contact us and we will be happy to review your case again.\n\n"
        f"Best regards,\n"
        f"SmartKitchen Team",
    )


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
