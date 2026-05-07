-- ============================================================
-- 2026-04-25 — Manual amount + refund-email-sent timestamp
-- on cancelled orders (Uber CSV ticket_size is always 0 for
-- cancelled rows, so admin enters the real amount manually).
-- ============================================================

ALTER TABLE reported_orders ADD COLUMN manual_amount        DOUBLE PRECISION;
ALTER TABLE reported_orders ADD COLUMN refund_email_sent_at TIMESTAMPTZ;
