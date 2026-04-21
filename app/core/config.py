import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    DATABASE_URL = os.getenv("DATABASE_URL")
    SECRET_KEY = os.getenv("SECRET_KEY", "changeme-secret-key")
    # Separate key for refresh tokens; falls back to SECRET_KEY + suffix if not set
    REFRESH_SECRET_KEY = os.getenv(
        "REFRESH_SECRET_KEY",
        os.getenv("SECRET_KEY", "changeme-secret-key") + "_refresh",
    )
    UBER_CLIENT_ID = os.getenv("UBER_CLIENT_ID")
    UBER_CLIENT_SECRET = os.getenv("UBER_CLIENT_SECRET")
    UBER_ENV = os.getenv("UBER_ENV")
    UBER_REDIRECT_URI = os.getenv("UBER_REDIRECT_URI")
    UBER_WEBHOOK_SECRET = os.getenv("UBER_WEBHOOK_SECRET", "")
    MAILJET_API_KEY = os.getenv("MAILJET_API_KEY", "")
    MAILJET_API_SECRET = os.getenv("MAILJET_API_SECRET", "")
    MAILJET_FROM_EMAIL = os.getenv("MAILJET_FROM_EMAIL", "")
    MAILJET_FROM_NAME = os.getenv("MAILJET_FROM_NAME", "SmartKitchen")
    UBER_SUPPORT_EMAIL = os.getenv("UBER_SUPPORT_EMAIL", "")
    # Base URL of the frontend — used to build verification / reset-password links
    FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

    # Supabase Storage
    SUPABASE_URL = os.getenv("SUPABASE_URL", "")
    SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


settings = Settings()
