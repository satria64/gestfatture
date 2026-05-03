import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "invoice-manager-secret-2024")
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///invoice_manager.db")

    # Email / PEC
    SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT     = int(os.environ.get("SMTP_PORT", 587))
    SMTP_USER     = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_USE_TLS  = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

    COMPANY_NAME  = os.environ.get("COMPANY_NAME", "La Tua Azienda Srl")
    COMPANY_EMAIL = os.environ.get("COMPANY_EMAIL", "")

    # Link pagamento base (es. Stripe, PayPal, SumUp)
    PAYMENT_BASE_URL = os.environ.get("PAYMENT_BASE_URL", "")

    # Giorni PRIMA della scadenza per inviare promemoria
    DAYS_BEFORE_DUE: list = [7, 3, 1]
    # Giorni DOPO la scadenza per inviare solleciti progressivi
    DAYS_AFTER_DUE: list  = [1, 7, 15, 30]

config = Config()
