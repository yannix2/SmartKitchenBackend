# Migrations

Plain SQL files. No Alembic — just run them against your database in **chronological order**.

## How to run

```bash
psql $DATABASE_URL -f migrations/<file>.sql
```

Each file is idempotent where possible (uses `ADD COLUMN IF NOT EXISTS`-style guards or only operates on safe `ALTER`s), so re-running an already-applied file should not corrupt data — but you should still keep track of which ones you've run in production.

## Current migrations (run top-to-bottom for a fresh DB)

| Order | File | What it does |
|------:|------|--------------|
| 1 | [`2026_04_25_kyc_kyb_required_fields.sql`](2026_04_25_kyc_kyb_required_fields.sql) | Backfills nulls + applies NOT NULL on `users` (name, family_name, phone, address). Adds 24 KYB/KYC/banking columns to `onboarding_forms`. |
| 2 | [`2026_04_25_cancelled_manual_amount.sql`](2026_04_25_cancelled_manual_amount.sql) | Adds `manual_amount` and `refund_email_sent_at` to `reported_orders` (admin enters the amount manually for cancelled orders since the Uber CSV reports `ticket_size = 0`). |

## After running

Restart the backend so SQLAlchemy reloads the schema in its session pool:

```bash
# whatever your start command is, e.g.:
uvicorn app.main:app --reload
```

## Adding a new migration

1. Create a new file: `migrations/YYYY_MM_DD_short_description.sql`
2. Lead with a comment block explaining what + why
3. Use `ALTER TABLE ... ADD COLUMN <name> <type>` for new columns (avoid `IF NOT EXISTS` only if you're sure it's a brand-new column)
4. Use plain `UPDATE` to backfill before applying any `SET NOT NULL` constraints
5. Update the table above
