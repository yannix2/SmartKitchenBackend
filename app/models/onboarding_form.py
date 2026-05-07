import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, JSON, Date
from app.db.base import Base


class OnboardingForm(Base):
    __tablename__ = "onboarding_forms"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=False, unique=True)

    # ── KYB — Business identity ──
    legal_entity_name = Column(String, nullable=True)
    business_type = Column(String, nullable=True)          # personne_physique | sarl | suarl | sa | auto_entrepreneur
    tax_id = Column(String, nullable=True)                 # Matricule fiscal
    rne_number = Column(String, nullable=True)             # Trade register number
    years_in_business = Column(Integer, nullable=True)
    business_address_rue = Column(String, nullable=True)
    business_address_city = Column(String, nullable=True)
    business_address_gouvernorat = Column(String, nullable=True)
    business_address_zip_code = Column(String, nullable=True)
    business_address_same_as_personal = Column(String, nullable=True)  # "yes" | "no"

    # ── KYB — Operations ──
    store_count = Column(Integer, nullable=True)
    other_platforms = Column(JSON, default=list, nullable=True)        # ["glovo","jahez","bolt"]
    monthly_uber_revenue = Column(String, nullable=True)               # bracket
    monthly_loss_estimate = Column(String, nullable=True)
    refund_handling_today = Column(String, nullable=True)              # yes | no | outsourced

    # ── KYC — Personal identity of signer ──
    signer_role = Column(String, nullable=True)                        # owner | manager | accountant | other
    cin_or_passport = Column(String, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    nationality = Column(String, nullable=True)
    id_document_url = Column(String, nullable=True)                    # Supabase URL — required if owner
    business_proof_url = Column(String, nullable=True)                 # Supabase URL — required if owner

    # ── KYC — Banking ──
    bank_name = Column(String, nullable=True)
    rib_iban = Column(String, nullable=True)
    bank_account_holder = Column(String, nullable=True)
    bank_statement_url = Column(String, nullable=True)                 # Supabase URL — optional

    # ── Operational preferences ──
    preferred_call_time = Column(String, nullable=True)
    preferred_contact_method = Column(String, nullable=True)           # phone | whatsapp | email
    referral_source = Column(String, nullable=True)
    notes = Column(Text, nullable=True)

    # ── Legacy / kept for compatibility ──
    uber_experience = Column(String, nullable=True)
    work_frequency = Column(String, nullable=True)

    submitted_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
