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
        return row.value if row else default

    @staticmethod
    def set(user_id: int, key: str, value):
        row = UserSetting.query.filter_by(user_id=user_id, key=key).first()
        if row:
            row.value = str(value)
        else:
            db.session.add(UserSetting(user_id=user_id, key=key, value=str(value)))
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


class AppSettings(db.Model):
    __tablename__ = "app_settings"

    id    = db.Column(db.Integer, primary_key=True)
    key   = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, default="")

    @staticmethod
    def get(key, default=""):
        row = AppSettings.query.filter_by(key=key).first()
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = AppSettings.query.filter_by(key=key).first()
        if row:
            row.value = str(value)
        else:
            db.session.add(AppSettings(key=key, value=str(value)))
        db.session.commit()
