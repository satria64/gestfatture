from datetime import date, datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


# Password "comuni" / facilmente indovinabili — vietate
COMMON_PASSWORDS = {
    "password", "password1", "password123", "passw0rd",
    "admin", "admin123", "admin1234", "administrator",
    "123456", "12345678", "123456789", "1234567890",
    "qwerty", "qwerty123", "abc123", "abc12345",
    "iloveyou", "letmein", "welcome", "welcome1",
    "monkey", "master", "dragon", "sunshine",
    "guest", "user", "root", "test", "demo",
    "gestfatture", "gestfatture123",
}


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin      = db.Column(db.Boolean, default=False, nullable=False)
    email         = db.Column(db.String(200), default="")     # per notifiche al titolare
    phone         = db.Column(db.String(50),  default="")     # numero WhatsApp (con prefisso +39…)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    # 2FA TOTP (opzionale, opt-in)
    totp_secret        = db.Column(db.String(64),  default="")  # base32, vuoto se non configurato
    totp_enabled       = db.Column(db.Boolean,    default=False, nullable=False)
    totp_backup_codes  = db.Column(db.Text,       default="")   # JSON list di hash SHA-256 single-use

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @staticmethod
    def validate_password(pw: str) -> tuple[bool, str]:
        """Verifica regole di complessità. Restituisce (ok, messaggio)."""
        if not pw or len(pw) < 8:
            return False, "Password troppo corta: minimo 8 caratteri."
        if pw.lower() in COMMON_PASSWORDS:
            return False, "Password troppo comune. Sceglierne una più sicura."
        if not any(c.isalpha() for c in pw):
            return False, "La password deve contenere almeno una lettera."
        if not any(c.isdigit() for c in pw):
            return False, "La password deve contenere almeno un numero."
        # Niente spazi all'inizio/fine
        if pw != pw.strip():
            return False, "La password non può iniziare o finire con spazi."
        return True, ""

    @property
    def role_label(self):
        return ("Amministratore", "primary") if self.is_admin else ("Utente", "secondary")


class Client(db.Model):
    __tablename__ = "clients"

    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name         = db.Column(db.String(200), nullable=False)
    email        = db.Column(db.String(200), default="")
    pec          = db.Column(db.String(200), default="")
    phone        = db.Column(db.String(50),  default="")
    address      = db.Column(db.String(500), default="")
    vat_number   = db.Column(db.String(50),  default="")
    credit_score = db.Column(db.Float, default=100.0)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    invoices = db.relationship("Invoice", back_populates="client", cascade="all, delete-orphan")

    @property
    def contact_email(self):
        return self.pec or self.email

    @property
    def total_invoices(self):
        return len(self.invoices)

    @property
    def paid_invoices(self):
        return sum(1 for i in self.invoices if i.status == "paid")

    @property
    def overdue_invoices(self):
        return sum(1 for i in self.invoices if i.status == "overdue")

    @property
    def total_overdue_amount(self):
        return sum(i.amount for i in self.invoices if i.status == "overdue")

    @property
    def risk_label(self):
        if self.credit_score >= 80:
            return ("Basso", "success")
        elif self.credit_score >= 50:
            return ("Medio", "warning")
        else:
            return ("Alto", "danger")


class Invoice(db.Model):
    __tablename__ = "invoices"

    id                 = db.Column(db.Integer, primary_key=True)
    user_id            = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    client_id          = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    number             = db.Column(db.String(50), nullable=False)
    amount             = db.Column(db.Float, nullable=False)
    due_date           = db.Column(db.Date, nullable=False)
    issue_date         = db.Column(db.Date, nullable=False)
    # TD01=Fattura, TD04=Nota di Credito, TD05=Nota di Debito, TD06=Parcella
    document_type      = db.Column(db.String(10), default="TD01", index=True)
    # FK a un'altra Invoice: se questa è TD04 collega la fattura stornata
    linked_invoice_id  = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=True)
    # status: pending | overdue | paid | cancelled | compensated
    status             = db.Column(db.String(20), default="pending")
    payment_date       = db.Column(db.Date, nullable=True)
    reminder_count     = db.Column(db.Integer, default=0)
    last_reminder_date = db.Column(db.DateTime, nullable=True)
    payment_link       = db.Column(db.String(500), default="")
    # Stripe payment_intent_id o PayPal order_id per matching automatico
    payment_ref        = db.Column(db.String(200), default="")
    pdf_filename       = db.Column(db.String(255), default="")
    user_notified_at   = db.Column(db.DateTime, nullable=True)  # quando è stato notificato il titolare di scadenza
    notes              = db.Column(db.Text, default="")
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)

    client    = db.relationship("Client", back_populates="invoices")
    reminders = db.relationship("Reminder", back_populates="invoice", cascade="all, delete-orphan")

    # Auto-relazione: questa NC riferisce a quale fattura?
    linked_invoice  = db.relationship(
        "Invoice", remote_side="Invoice.id", foreign_keys=[linked_invoice_id],
        backref=db.backref("credit_notes", lazy="dynamic"),
    )

    @property
    def is_credit_note(self) -> bool:
        return self.document_type == "TD04"

    @property
    def is_debit_note(self) -> bool:
        return self.document_type == "TD05"

    @property
    def document_type_label(self) -> tuple[str, str, str]:
        """Restituisce (etichetta, colore_bootstrap, icona_bi)."""
        return {
            "TD01": ("Fattura",         "primary",  "bi-file-earmark-text"),
            "TD04": ("Nota di Credito", "warning",  "bi-arrow-counterclockwise"),
            "TD05": ("Nota di Debito",  "info",     "bi-arrow-up-circle"),
            "TD06": ("Parcella",        "primary",  "bi-file-earmark-text"),
        }.get(self.document_type or "TD01", ("Documento", "secondary", "bi-file-earmark"))

    @property
    def days_overdue(self):
        if self.status == "paid":
            return 0
        return max(0, (date.today() - self.due_date).days)

    @property
    def days_until_due(self):
        return (self.due_date - date.today()).days

    @property
    def status_label(self):
        labels = {
            "pending":     ("In attesa",         "primary"),
            "overdue":     ("Scaduta",           "danger"),
            "paid":        ("Pagata",            "success"),
            "cancelled":   ("Annullata",         "secondary"),
            "compensated": ("Compensata da NC",  "info"),
        }
        return labels.get(self.status, ("Sconosciuto", "secondary"))

    def update_status(self):
        # Le note di credito sono "fittiziamente pagate" — non vanno sollecitate
        if self.is_credit_note:
            if self.status not in ("paid", "cancelled"):
                self.status = "paid"
                if not self.payment_date:
                    self.payment_date = self.issue_date
            return
        if self.status in ("paid", "cancelled", "compensated"):
            return
        if date.today() > self.due_date:
            self.status = "overdue"


class Reminder(db.Model):
    __tablename__ = "reminders"

    id             = db.Column(db.Integer, primary_key=True)
    invoice_id     = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    sent_at        = db.Column(db.DateTime, default=datetime.utcnow)
    reminder_type  = db.Column(db.String(50))
    subject        = db.Column(db.String(500))
    recipient      = db.Column(db.String(200))
    success        = db.Column(db.Boolean, default=True)
    error_message  = db.Column(db.Text, default="")

    invoice = db.relationship("Invoice", back_populates="reminders")


# Chiavi UserSetting/AppSettings i cui valori vengono cifrati at-rest se la
# env var SECRETS_ENCRYPTION_KEY è configurata. Idempotente in lettura.
SENSITIVE_USER_KEYS = {
    "whatsapp_apikey",
    "integration_pec_password",
    "integration_fic_access_token",
    "integration_fic_refresh_token",
    "integration_fic_client_secret",
}
SENSITIVE_APP_KEYS = {
    "smtp_password", "anthropic_api_key", "stripe_webhook_secret",
    "paypal_webhook_id", "resend_api_key",
    "backup_s3_secret_access_key",
    "gocardless_secret_id", "gocardless_secret_key",  # legacy, dormiente
    "tink_client_id", "tink_client_secret",
}


class UserSetting(db.Model):
    """Impostazioni per-utente (integrazioni, branding, ecc.)."""
    __tablename__ = "user_settings"
    __table_args__ = (db.UniqueConstraint("user_id", "key", name="uq_user_key"),)

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    key     = db.Column(db.String(100), nullable=False)
    value   = db.Column(db.Text, default="")

    @staticmethod
    def get(user_id: int, key: str, default: str = "") -> str:
        row = UserSetting.query.filter_by(user_id=user_id, key=key).first()
        v = row.value if row else default
        if key in SENSITIVE_USER_KEYS and v:
            from crypto_service import decrypt
            v = decrypt(v)
        return v

    @staticmethod
    def set(user_id: int, key: str, value):
        v = str(value or "")
        if key in SENSITIVE_USER_KEYS and v:
            from crypto_service import encrypt
            v = encrypt(v)
        row = UserSetting.query.filter_by(user_id=user_id, key=key).first()
        if row:
            row.value = v
        else:
            db.session.add(UserSetting(user_id=user_id, key=key, value=v))
        db.session.commit()


class AuditLog(db.Model):
    """Registro azioni sensibili eseguite dagli utenti (security audit)."""
    __tablename__ = "audit_logs"

    id          = db.Column(db.Integer, primary_key=True)
    timestamp   = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    username    = db.Column(db.String(80))                 # snapshot
    action      = db.Column(db.String(60), index=True)     # login, logout, password_change…
    target      = db.Column(db.String(200))                # es. "invoice:42"
    details     = db.Column(db.Text)                       # JSON / testo libero
    ip_address  = db.Column(db.String(50))
    user_agent  = db.Column(db.String(500))

    @property
    def action_label(self):
        labels = {
            "login_success":     ("Login OK",            "success"),
            "login_failed":      ("Login fallito",       "danger"),
            "logout":            ("Logout",              "secondary"),
            "guest_login":       ("Login ospite",        "info"),
            "guest_deleted":     ("Ospite eliminato",    "secondary"),
            "password_change":   ("Cambio password",     "warning"),
            "password_reset":    ("Reset password",      "warning"),
            "user_created":      ("Utente creato",       "primary"),
            "user_deleted":      ("Utente eliminato",    "danger"),
            "user_admin_toggle": ("Cambio ruolo admin",  "warning"),
            "settings_change":   ("Impostazioni admin",  "primary"),
            "profile_update":    ("Aggiornamento profilo","info"),
            "fic_connect":       ("FiC connesso",        "primary"),
            "fic_disconnect":    ("FiC disconnesso",     "secondary"),
            "fic_app_credentials":("FiC OAuth credentials","warning"),
            "ticket_created":    ("Ticket aperto",       "info"),
            "ticket_status":     ("Stato ticket",        "info"),
            "clients_merged":    ("Clienti unificati",   "warning"),
            "test_claude":       ("Test Claude API",     "info"),
            "data_export":       ("Export dati GDPR",    "primary"),
            "account_deleted":   ("Account cancellato",  "danger"),
            "account_delete_failed": ("Cancellazione fallita", "warning"),
            "login_step1_ok":    ("Login step 1 OK",     "info"),
            "login_2fa_failed":  ("2FA fallita",         "danger"),
            "2fa_enabled":       ("2FA attivata",        "success"),
            "2fa_disabled":      ("2FA disattivata",     "warning"),
            "2fa_setup_failed":  ("2FA setup fallita",   "warning"),
            "2fa_disable_failed": ("2FA disable fallita", "warning"),
            "2fa_codes_regenerated": ("2FA codici rigenerati", "info"),
            "pec_notify_ok":     ("PEC notifica inviata", "success"),
            "pec_notify_failed": ("PEC notifica fallita", "danger"),
            "pec_notify_test":   ("PEC notifica test",    "info"),
            "pec_reanalyzed":    ("PEC ri-analizzata",    "info"),
            "bandi_notify_digest": ("Bandi: digest inviato", "primary"),
            "survey_sent":       ("Survey inviato",       "info"),
            "secrets_migrated":  ("Secret cifrati",       "primary"),
            "backup_run":        ("Backup S3",            "info"),
            "bank_connect_start": ("Banca: avvio connect", "info"),
            "bank_connect_ok":    ("Banca: collegata",     "success"),
            "bank_connect_failed":("Banca: connect fallito","danger"),
            "bank_sync":          ("Banca: sync",          "info"),
            "bank_disconnect":    ("Banca: scollegata",    "warning"),
            "bank_match_manual":  ("Banca: match manuale", "primary"),
            "bank_ignore":        ("Banca: tx ignorata",   "secondary"),
            "bank_recon_notify":  ("Banca: notifica digest","primary"),
        }
        return labels.get(self.action, (self.action, "secondary"))


class SupportTicket(db.Model):
    """Ticket di assistenza: l'utente apre, l'admin risponde."""
    __tablename__ = "support_tickets"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    subject    = db.Column(db.String(500), nullable=False)
    # status: open | in_progress | waiting_user | resolved | closed
    status     = db.Column(db.String(30), default="open", index=True)
    # priority: low | normal | high | urgent
    priority   = db.Column(db.String(20), default="normal")
    # category: bug | feature | question | other
    category   = db.Column(db.String(50), default="question")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user     = db.relationship("User", foreign_keys=[user_id])
    messages = db.relationship("TicketMessage", back_populates="ticket",
                               cascade="all, delete-orphan",
                               order_by="TicketMessage.created_at")

    @property
    def status_label(self):
        return {
            "open":         ("Aperto",         "primary"),
            "in_progress":  ("In lavorazione", "warning"),
            "waiting_user": ("Attesa utente",  "info"),
            "resolved":     ("Risolto",        "success"),
            "closed":       ("Chiuso",         "secondary"),
        }.get(self.status, ("?", "secondary"))

    @property
    def priority_label(self):
        return {
            "low":    ("Bassa",   "secondary"),
            "normal": ("Normale", "primary"),
            "high":   ("Alta",    "warning"),
            "urgent": ("Urgente", "danger"),
        }.get(self.priority, ("Normale", "primary"))

    @property
    def category_label(self):
        return {
            "bug":      ("🐛 Bug",        "danger"),
            "feature":  ("✨ Feature",    "info"),
            "question": ("❓ Domanda",    "primary"),
            "other":    ("📝 Altro",      "secondary"),
        }.get(self.category, ("📝 Altro", "secondary"))

    @property
    def messages_count(self):
        return len(self.messages)


class TicketSurvey(db.Model):
    """Survey di soddisfazione inviato all'utente quando un ticket viene
    risolto/chiuso. Accesso pubblico via token firmato (1 per ticket)."""
    __tablename__ = "ticket_surveys"

    id           = db.Column(db.Integer, primary_key=True)
    ticket_id    = db.Column(db.Integer, db.ForeignKey("support_tickets.id"),
                             nullable=False, unique=True, index=True)
    rating       = db.Column(db.Integer, nullable=True)   # 1-5 (null = non ancora compilato)
    comment      = db.Column(db.Text, default="")
    sent_at      = db.Column(db.DateTime, default=datetime.utcnow)
    submitted_at = db.Column(db.DateTime, nullable=True)

    ticket = db.relationship("SupportTicket")

    @property
    def rating_label(self):
        return {
            1: ("😡 Pessimo",     "danger"),
            2: ("😞 Insoddisfatto","warning"),
            3: ("😐 Neutro",       "secondary"),
            4: ("🙂 Buono",        "primary"),
            5: ("😍 Eccellente",   "success"),
        }.get(self.rating or 0, ("In attesa", "light"))


class TicketMessage(db.Model):
    """Singolo messaggio dentro un ticket."""
    __tablename__ = "ticket_messages"

    id          = db.Column(db.Integer, primary_key=True)
    ticket_id   = db.Column(db.Integer, db.ForeignKey("support_tickets.id"), nullable=False)
    author_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    body        = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=False)  # nota privata admin
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    ticket = db.relationship("SupportTicket", back_populates="messages")
    author = db.relationship("User", foreign_keys=[author_id])


class PecMessage(db.Model):
    """Email PEC istituzionale (AdE / INPS / INAIL) ricevuta e analizzata."""
    __tablename__ = "pec_messages"
    __table_args__ = (db.UniqueConstraint("user_id", "message_id", name="uq_user_msgid"),)

    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    message_id      = db.Column(db.String(500), index=True)   # Header Message-ID
    received_at     = db.Column(db.DateTime, default=datetime.utcnow)

    sender          = db.Column(db.String(255))
    sender_label    = db.Column(db.String(100))                # "Agenzia delle Entrate" / "INPS" / "INAIL"
    subject         = db.Column(db.String(500))

    # Analisi (da Claude o euristica)
    category        = db.Column(db.String(50))                 # comunicazione / cartella / avviso / ...
    urgency         = db.Column(db.String(20))                 # alta / media / bassa
    summary         = db.Column(db.Text)
    suggested_action = db.Column(db.Text)
    deadline        = db.Column(db.Date, nullable=True)
    key_facts       = db.Column(db.Text)                       # JSON list

    body_excerpt    = db.Column(db.Text)
    attachments     = db.Column(db.Text)                       # JSON list di filename

    notified_at     = db.Column(db.DateTime, nullable=True)
    is_read         = db.Column(db.Boolean, default=False)
    is_archived     = db.Column(db.Boolean, default=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def urgency_label(self):
        return {
            "alta":  ("Alta",  "danger"),
            "media": ("Media", "warning"),
            "bassa": ("Bassa", "secondary"),
        }.get(self.urgency, ("?", "secondary"))

    @property
    def sender_icon(self):
        return {
            "Agenzia delle Entrate": "bi-bank2",
            "INPS":                  "bi-people-fill",
            "INAIL":                 "bi-shield-fill-check",
        }.get(self.sender_label, "bi-envelope")

    @property
    def attachments_list(self) -> list:
        import json
        try:
            return json.loads(self.attachments or "[]")
        except Exception:
            return []

    @property
    def key_facts_list(self) -> list:
        import json
        try:
            return json.loads(self.key_facts or "[]")
        except Exception:
            return []


class Bando(db.Model):
    """Bando di finanziamento (contributo, agevolazione, credito d'imposta)
    aggregato dagli scraper. Globale: un singolo Bando può essere visto da
    più utenti (matching personalizzato in BandoMatch).
    """
    __tablename__ = "bandi"
    __table_args__ = (db.UniqueConstraint("source", "external_id", name="uq_bando_source"),)

    id            = db.Column(db.Integer, primary_key=True)
    source        = db.Column(db.String(80), index=True)            # "retecamerale", "mimit", "regione_lombardia" …
    external_id   = db.Column(db.String(200))                        # ID/URL univoco nella fonte
    title         = db.Column(db.String(500), nullable=False)
    ente          = db.Column(db.String(255))                        # ente erogatore (es. "Camera di Commercio Milano", "MIMIT")
    region        = db.Column(db.String(80), index=True)             # "Italia", "Lombardia", "Lazio" …
    category      = db.Column(db.String(120))                        # "innovazione", "internazionalizzazione", "digitalizzazione" …
    deadline      = db.Column(db.Date, nullable=True, index=True)
    amount_max    = db.Column(db.Float, nullable=True)               # importo massimo del contributo (€)
    description   = db.Column(db.Text)                               # 2-4 frasi sintetiche
    requirements  = db.Column(db.Text)                               # requisiti riassunti
    target_size   = db.Column(db.String(40), default="all")          # micro / pmi / all
    ateco_hints   = db.Column(db.Text, default="[]")                 # JSON list di codici ATECO/keyword settori
    url           = db.Column(db.String(800))                        # link al bando ufficiale
    is_active     = db.Column(db.Boolean, default=True, nullable=False)
    last_seen_at  = db.Column(db.DateTime, default=datetime.utcnow)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    matches = db.relationship("BandoMatch", back_populates="bando",
                              cascade="all, delete-orphan")

    @property
    def days_until_deadline(self):
        if not self.deadline:
            return None
        return (self.deadline - date.today()).days

    @property
    def deadline_label(self):
        d = self.days_until_deadline
        if d is None:
            return ("aperto", "secondary")
        if d < 0:
            return ("scaduto", "secondary")
        if d <= 7:
            return (f"{d}gg", "danger")
        if d <= 30:
            return (f"{d}gg", "warning")
        return (f"{d}gg", "success")

    @property
    def ateco_hints_list(self) -> list:
        import json
        try:
            return json.loads(self.ateco_hints or "[]")
        except Exception:
            return []


class BandoMatch(db.Model):
    """Rilevanza personalizzata di un Bando per un singolo utente.
    Calcolato dall'AI sulla base del profilo utente (ATECO, regione, descrizione)."""
    __tablename__ = "bando_matches"
    __table_args__ = (db.UniqueConstraint("user_id", "bando_id", name="uq_user_bando"),)

    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    bando_id        = db.Column(db.Integer, db.ForeignKey("bandi.id"), nullable=False, index=True)
    relevance_score = db.Column(db.Integer, default=0)               # 0-100
    reason          = db.Column(db.Text)                             # spiegazione AI (1-2 frasi)
    is_saved        = db.Column(db.Boolean, default=False)           # utente ha messo "preferito"
    is_dismissed    = db.Column(db.Boolean, default=False)           # utente ha nascosto
    notified_at     = db.Column(db.DateTime, nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    bando = db.relationship("Bando", back_populates="matches")

    @property
    def relevance_label(self):
        s = self.relevance_score or 0
        if s >= 75:
            return ("Alta", "success")
        if s >= 40:
            return ("Media", "primary")
        return ("Bassa", "secondary")


class BankAccount(db.Model):
    """Conto bancario di un utente, collegato via GoCardless Bank Account Data (PSD2).
    Ogni utente può avere più conti (fino al limite PSD2 di 90 giorni di accesso prima
    della re-auth)."""
    __tablename__ = "bank_accounts"
    __table_args__ = (db.UniqueConstraint("user_id", "external_account_id",
                                          name="uq_user_extacc"),)

    id                   = db.Column(db.Integer, primary_key=True)
    user_id              = db.Column(db.Integer, db.ForeignKey("users.id"),
                                     nullable=False, index=True)
    requisition_id       = db.Column(db.String(80), index=True)   # ID GoCardless della requisition
    external_account_id  = db.Column(db.String(80), nullable=False)  # account_id GoCardless
    iban                 = db.Column(db.String(40), default="")
    institution_id       = db.Column(db.String(80))               # es. "INTESA_SANPAOLO_BCITITMM"
    institution_name     = db.Column(db.String(120))
    owner_name           = db.Column(db.String(200), default="")
    name                 = db.Column(db.String(200), default="")  # nome del conto se fornito
    currency             = db.Column(db.String(8), default="EUR")
    status               = db.Column(db.String(20), default="linked")  # linked / expired / error / disabled
    expires_at           = db.Column(db.DateTime, nullable=True)  # quando scade l'autorizzazione PSD2 (90 gg)
    last_sync_at         = db.Column(db.DateTime, nullable=True)
    last_error           = db.Column(db.Text, default="")
    # Tink user-level OAuth tokens (cifrati at-rest se SECRETS_ENCRYPTION_KEY)
    access_token         = db.Column(db.Text, default="")
    refresh_token        = db.Column(db.Text, default="")
    token_expires_at     = db.Column(db.DateTime, nullable=True)
    created_at           = db.Column(db.DateTime, default=datetime.utcnow)

    transactions = db.relationship("BankTransaction", back_populates="account",
                                   cascade="all, delete-orphan")

    @property
    def status_label(self):
        return {
            "linked":   ("Collegato",       "success"),
            "expired":  ("Scaduto (re-auth)","warning"),
            "error":    ("Errore",          "danger"),
            "disabled": ("Disabilitato",    "secondary"),
        }.get(self.status, ("?", "secondary"))

    @property
    def days_until_expiry(self):
        if not self.expires_at:
            return None
        return (self.expires_at - datetime.utcnow()).days


class BankTransaction(db.Model):
    """Singola transazione scaricata da una banca via GoCardless. Solo le entrate
    (amount > 0) sono usate per riconciliazione con le fatture."""
    __tablename__ = "bank_transactions"
    __table_args__ = (db.UniqueConstraint("bank_account_id", "external_id",
                                          name="uq_acc_extid"),)

    id                = db.Column(db.Integer, primary_key=True)
    bank_account_id   = db.Column(db.Integer, db.ForeignKey("bank_accounts.id"),
                                  nullable=False, index=True)
    user_id           = db.Column(db.Integer, db.ForeignKey("users.id"),
                                  nullable=False, index=True)  # denormalizzato per query veloci
    external_id       = db.Column(db.String(120), nullable=False)  # transactionId GoCardless
    booking_date      = db.Column(db.Date, nullable=True, index=True)
    value_date        = db.Column(db.Date, nullable=True)
    amount            = db.Column(db.Float, nullable=False)
    currency          = db.Column(db.String(8), default="EUR")
    debtor_name       = db.Column(db.String(200), default="")  # chi ha pagato
    debtor_iban       = db.Column(db.String(40), default="")
    description       = db.Column(db.Text, default="")          # remittanceInformation
    raw_data          = db.Column(db.Text, default="{}")        # JSON completo per audit/debug

    # Matching con Invoice
    matched_invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"),
                                   nullable=True, index=True)
    status            = db.Column(db.String(20), default="pending", index=True)
    # pending | auto_matched | manual_matched | ignored | non_invoice (uscita o non rilevante)
    match_confidence  = db.Column(db.Integer, default=0)        # 0-100
    match_reason      = db.Column(db.Text, default="")
    matched_at        = db.Column(db.DateTime, nullable=True)
    matched_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)

    account         = db.relationship("BankAccount", back_populates="transactions")
    matched_invoice = db.relationship("Invoice", foreign_keys=[matched_invoice_id])

    @property
    def status_label(self):
        return {
            "pending":        ("Da riconciliare",  "warning"),
            "auto_matched":   ("Match automatico", "success"),
            "manual_matched": ("Match manuale",    "primary"),
            "ignored":        ("Ignorata",         "secondary"),
            "non_invoice":    ("Non fattura",      "secondary"),
        }.get(self.status, ("?", "secondary"))


class AppSettings(db.Model):
    __tablename__ = "app_settings"

    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, default="")

    @staticmethod
    def get(key, default=""):
        row = AppSettings.query.filter_by(key=key).first()
        v = row.value if row else default
        if key in SENSITIVE_APP_KEYS and v:
            from crypto_service import decrypt
            v = decrypt(v)
        return v

    @staticmethod
    def set(key, value):
        v = str(value or "")
        if key in SENSITIVE_APP_KEYS and v:
            from crypto_service import encrypt
            v = encrypt(v)
        row = AppSettings.query.filter_by(key=key).first()
        if row:
            row.value = v
        else:
            db.session.add(AppSettings(key=key, value=v))
        db.session.commit()
