"""
Migration script — run once to apply schema changes to the existing database.
Safe to run multiple times (checks before altering).

Usage:
    python migrate.py
"""

from sqlalchemy import text, inspect
from app.db.session import engine
from app.db.base import Base

# Import ALL models so SQLAlchemy registers them before create_all
from app.models.user import User
from app.models.uber_report import UberReport
from app.models.user_store import UserStore
from app.models.refund_job import RefundJob
from app.models.refund_request import RefundRequest
from app.models.smartkitchen_store import SmartKitchenStore
from app.models.order_report_job import OrderReportJob
from app.models.reported_order import ReportedOrder
from app.models.contested_order import ContestedOrder
from app.models.store_refund import StoreRefund
from app.models.abonnement import Abonnement
from app.models.onboarding_form import OnboardingForm
from app.models.call_log import CallLog


def column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return result.scalar() > 0


def table_exists(conn, table: str) -> bool:
    result = conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = :t"
    ), {"t": table})
    return result.scalar() > 0


def run():
    print("Starting migration…")

    with engine.begin() as conn:

        # ── 1. New columns on existing `users` table ───────────────────────────

        migrations = [
            (
                "users", "is_verified_bymanager",
                "ALTER TABLE users ADD COLUMN is_verified_bymanager BOOLEAN NOT NULL DEFAULT FALSE",
            ),
            (
                "users", "onboarding_status",
                "ALTER TABLE users ADD COLUMN onboarding_status VARCHAR NOT NULL DEFAULT 'not_started'",
            ),
            (
                "users", "rejection_reason",
                "ALTER TABLE users ADD COLUMN rejection_reason VARCHAR",
            ),
        ]

        for table, column, sql in migrations:
            if column_exists(conn, table, column):
                print(f"  ✓ {table}.{column} already exists — skipped")
            else:
                conn.execute(text(sql))
                print(f"  + {table}.{column} added")

        # ── 2. Create new tables that don't exist yet ──────────────────────────
        # create_all with checkfirst=True only creates missing tables, never touches existing ones.

    print("\nCreating any missing tables via SQLAlchemy…")
    Base.metadata.create_all(bind=engine, checkfirst=True)

    # Report which new tables now exist
    with engine.connect() as conn:
        for table_name in ["abonnements", "onboarding_forms", "call_logs"]:
            exists = table_exists(conn, table_name)
            print(f"  {'✓' if exists else '✗'} {table_name}")

    print("\nMigration complete.")


if __name__ == "__main__":
    run()
