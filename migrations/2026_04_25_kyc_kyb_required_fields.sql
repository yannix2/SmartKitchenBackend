-- ============================================================
-- 2026-04-25 — KYC/KYB onboarding overhaul (PL/SQL ALTER style)
-- ============================================================

-- ── 1. Backfill NULLs in users so NOT NULL can be applied ──
UPDATE users SET name                = '' WHERE name IS NULL;
UPDATE users SET family_name         = '' WHERE family_name IS NULL;
UPDATE users SET phone_number        = '' WHERE phone_number IS NULL;
UPDATE users SET phone_code          = '+216' WHERE phone_code IS NULL;
UPDATE users SET address_rue         = '' WHERE address_rue IS NULL;
UPDATE users SET address_city        = '' WHERE address_city IS NULL;
UPDATE users SET address_gouvernorat = '' WHERE address_gouvernorat IS NULL;
UPDATE users SET address_zip_code    = '' WHERE address_zip_code IS NULL;

-- ── 2. ALTER users — make personal + address fields NOT NULL ──
ALTER TABLE users ALTER COLUMN name                SET NOT NULL;
ALTER TABLE users ALTER COLUMN family_name         SET NOT NULL;
ALTER TABLE users ALTER COLUMN phone_number        SET NOT NULL;
ALTER TABLE users ALTER COLUMN phone_code          SET NOT NULL;
ALTER TABLE users ALTER COLUMN address_rue         SET NOT NULL;
ALTER TABLE users ALTER COLUMN address_city        SET NOT NULL;
ALTER TABLE users ALTER COLUMN address_gouvernorat SET NOT NULL;
ALTER TABLE users ALTER COLUMN address_zip_code    SET NOT NULL;

-- ── 3. ALTER onboarding_forms — KYB Identity (signer) ──
ALTER TABLE onboarding_forms ADD COLUMN signer_role        VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN cin_or_passport    VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN date_of_birth      DATE;
ALTER TABLE onboarding_forms ADD COLUMN nationality        VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN id_document_url    VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN business_proof_url VARCHAR;

-- ── 4. ALTER onboarding_forms — KYB Business identity ──
ALTER TABLE onboarding_forms ADD COLUMN legal_entity_name                  VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN business_type                      VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN tax_id                             VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN rne_number                         VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN years_in_business                  INTEGER;
ALTER TABLE onboarding_forms ADD COLUMN business_address_rue               VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN business_address_city              VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN business_address_gouvernorat       VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN business_address_zip_code          VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN business_address_same_as_personal  VARCHAR;

-- ── 5. ALTER onboarding_forms — KYB Operations ──
ALTER TABLE onboarding_forms ADD COLUMN other_platforms       JSONB DEFAULT '[]'::jsonb;
ALTER TABLE onboarding_forms ADD COLUMN monthly_uber_revenue  VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN refund_handling_today VARCHAR;

-- ── 6. ALTER onboarding_forms — KYC Banking ──
ALTER TABLE onboarding_forms ADD COLUMN bank_name           VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN rib_iban            VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN bank_account_holder VARCHAR;
ALTER TABLE onboarding_forms ADD COLUMN bank_statement_url  VARCHAR;

-- ── 7. ALTER onboarding_forms — Operational preferences ──
ALTER TABLE onboarding_forms ADD COLUMN preferred_contact_method VARCHAR;
