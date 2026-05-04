import os
import sys
import logging
from datetime import date, datetime, timedelta

from flask import (Flask, render_template, request, redirect, session,
                   url_for, flash, jsonify, Response, abort, send_from_directory)
from werkzeug.utils import secure_filename
from functools import wraps
from flask_login import login_required, login_user, logout_user, current_user
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from models import (db, Client, Invoice, Reminder, AppSettings, UserSetting,
                    User, PecMessage, SupportTicket, TicketMessage, AuditLog,
                    Bando, BandoMatch, BankAccount, BankTransaction,
                    FiscalDeadline)
from auth import login_manager
from config import config

# Istanze globali — verranno collegate all'app in create_app()
csrf    = CSRFProtect()
limiter = Limiter(get_remote_address, default_limits=["1000 per hour"])


# ─── Helpers multi-tenant: filtri per current_user ───────────────────────────
def my_clients():
    """Anagrafica clienti (chi compra da me). Esclude i fornitori puri."""
    return Client.query.filter_by(user_id=current_user.id).filter(
        db.or_(Client.is_supplier.is_(False), Client.is_supplier.is_(None))
    )


def my_suppliers():
    """Anagrafica fornitori (chi fattura a me)."""
    return Client.query.filter_by(user_id=current_user.id, is_supplier=True)


def my_invoices():
    """Fatture attive (emesse a clienti). Esclude le passive."""
    return Invoice.query.filter_by(user_id=current_user.id).filter(
        db.or_(Invoice.is_passive.is_(False), Invoice.is_passive.is_(None))
    )


def my_payables():
    """Fatture passive (ricevute da fornitori)."""
    return Invoice.query.filter_by(user_id=current_user.id, is_passive=True)


def get_my_client(cid):
    """Cliente del current_user, 404 se non gli appartiene."""
    c = Client.query.filter_by(id=cid, user_id=current_user.id).first()
    if not c:
        from flask import abort
        abort(404)
    return c


def get_my_invoice(iid):
    """Fattura del current_user, 404 se non gli appartiene."""
    i = Invoice.query.filter_by(id=iid, user_id=current_user.id).first()
    if not i:
        from flask import abort
        abort(404)
    return i


def admin_required(f):
    """Richiede utente autenticato CON flag is_admin=True."""
    @wraps(f)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            flash("Accesso riservato all'amministratore.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapped


def audit(action: str, target: str = "", details: str = "", user=None):
    """Registra un evento di sicurezza nel log."""
    try:
        u = user or (current_user._get_current_object()
                     if current_user.is_authenticated else None)
        log = AuditLog(
            user_id    = u.id if u else None,
            username   = u.username if u else "anonymous",
            action     = action[:60],
            target     = target[:200],
            details    = (details or "")[:2000],
            ip_address = (request.remote_addr or "")[:50],
            user_agent = (request.user_agent.string if request.user_agent else "")[:500],
        )
        db.session.add(log)
        db.session.commit()
    except Exception as e:
        logging.warning("Audit log fallito: %s", e)
        try: db.session.rollback()
        except Exception: pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.abspath(os.path.dirname(__file__)))
    return os.path.join(base, rel)


def get_upload_folder():
    """Cartella scrivibile per i PDF.

    Priorità:
      1. variabile env UPLOAD_FOLDER (se settata in produzione)
      2. /var/data/uploads se esiste (Render con disco persistente)
      3. ./uploads (sviluppo locale e PyInstaller)
    """
    # Override esplicito da env
    if os.environ.get("UPLOAD_FOLDER"):
        folder = os.environ["UPLOAD_FOLDER"]
    # Render persistent disk
    elif os.path.isdir("/var/data"):
        folder = "/var/data/uploads"
    # Default locale
    else:
        folder = os.path.join(os.getcwd(), "uploads")
    os.makedirs(folder, exist_ok=True)
    return folder


def save_invoice_pdf(file_storage, invoice_id):
    """Salva il PDF caricato come uploads/invoice_<id>.pdf, restituisce il filename."""
    if not file_storage or not file_storage.filename:
        return None
    if not file_storage.filename.lower().endswith(".pdf"):
        return None
    filename = f"invoice_{invoice_id}.pdf"
    file_storage.save(os.path.join(get_upload_folder(), filename))
    return filename


def delete_invoice_pdf(filename):
    if not filename:
        return
    try:
        os.remove(os.path.join(get_upload_folder(), filename))
    except OSError:
        pass


def _init_sentry():
    """Inizializza Sentry se SENTRY_DSN è settata. PII spente per GDPR."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        environment = "production" if (os.environ.get("RENDER") or os.environ.get("PORT")) else "development"
        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,                # niente IP / cookie / corpi richiesta
            environment=environment,
            release=os.environ.get("RENDER_GIT_COMMIT", "")[:12] or None,
        )
        logging.info("Sentry inizializzato (env=%s)", environment)
    except Exception as e:
        logging.warning("Sentry non inizializzato: %s", e)


def create_app():
    _init_sentry()
    app = Flask(
        __name__,
        template_folder=resource_path("templates"),
        static_folder=resource_path("static"),
    )
    app.config["SECRET_KEY"]                     = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"]        = config.DATABASE_URL
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # In produzione dietro reverse proxy (Render/Heroku/nginx) i header X-Forwarded-*
    # devono essere onorati per generare URL HTTPS corretti (necessario per OAuth FiC).
    if os.environ.get("PORT") or os.environ.get("RENDER"):
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
        # In produzione: forza HTTPS in url_for(_external=True)
        app.config["PREFERRED_URL_SCHEME"] = "https"

    # ── Sicurezza ────────────────────────────────────────────────────────────
    app.config["MAX_CONTENT_LENGTH"]   = 10 * 1024 * 1024   # 10 MB upload max
    app.config["WTF_CSRF_TIME_LIMIT"]  = 3600 * 8           # 8h CSRF token
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=4)  # logout per inattività
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    if os.environ.get("PORT") or os.environ.get("RENDER"):
        app.config["SESSION_COOKIE_SECURE"] = True          # HTTPS-only in prod

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    # ── Header sicurezza HTTP ────────────────────────────────────────────────
    @app.after_request
    def add_security_headers(resp):
        resp.headers["X-Frame-Options"]        = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"]     = "geolocation=(), microphone=(), camera=()"
        # HSTS solo in produzione (HTTPS)
        if request.is_secure or app.config.get("SESSION_COOKIE_SECURE"):
            resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # CSP — permette CDN Bootstrap, inline (per chat widget) e immagini varie
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' https://cdn.jsdelivr.net data:; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "frame-ancestors 'none';"
        )
        return resp

    # Esponi csrf_token() in tutti i template Jinja
    @app.context_processor
    def inject_csrf():
        return {"csrf_token": generate_csrf}

    # ── Logout automatico per inattività (4h) ────────────────────────────────
    @app.before_request
    def check_session_inactivity():
        if not current_user.is_authenticated:
            return
        # Skip per gli endpoint statici e webhook
        if request.endpoint in (None, "static") or (request.endpoint or "").startswith("webhook"):
            return
        last_seen = session.get("_last_activity")
        now = datetime.utcnow().isoformat()
        if last_seen:
            try:
                last_dt = datetime.fromisoformat(last_seen)
                if datetime.utcnow() - last_dt > app.config["PERMANENT_SESSION_LIFETIME"]:
                    audit("logout", target="auto", details="timeout inattività")
                    logout_user()
                    session.clear()
                    flash("⏰ Sessione scaduta per inattività. Effettua di nuovo il login.", "info")
                    return redirect(url_for("login"))
            except Exception:
                pass
        session["_last_activity"] = now
        session.permanent = True

    with app.app_context():
        db.create_all()
        _migrate_db()
        _seed_settings()
        _ensure_admin()

    from scheduler_service import start_scheduler
    start_scheduler(app)

    # ── Filtri Jinja ──────────────────────────────────────────────────────────
    @app.template_filter("eur")
    def eur_filter(v):
        return f"€ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    @app.template_filter("it_date")
    def it_date(v):
        if not v:
            return ""
        if isinstance(v, str):
            v = datetime.strptime(v, "%Y-%m-%d").date()
        return v.strftime("%d/%m/%Y")

    @app.template_filter("from_json")
    def from_json(v):
        import json as _json
        try:
            return _json.loads(v or "[]")
        except Exception:
            return []

    # ═══════════════════════════════════════════════════════════════════════════
    # AUTH
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                if user.totp_enabled:
                    # 2FA: stato pendente in sessione, l'utente NON è ancora loggato
                    session["pending_2fa_uid"]      = user.id
                    session["pending_2fa_remember"] = request.form.get("remember") == "on"
                    session["pending_2fa_next"]    = request.args.get("next") or ""
                    audit("login_step1_ok", target=f"user:{username}", user=user)
                    return redirect(url_for("login_2fa"))
                login_user(user, remember=request.form.get("remember") == "on")
                audit("login_success", target=f"user:{username}", user=user)
                return redirect(request.args.get("next") or url_for("dashboard"))
            audit("login_failed", target=f"user:{username}", details="credenziali errate")
            flash("Username o password non corretti.", "danger")
        return render_template("login.html")

    @app.route("/login/2fa", methods=["GET", "POST"])
    @limiter.limit("10 per minute", methods=["POST"])
    def login_2fa():
        """Secondo step di login: verifica codice TOTP o codice di backup."""
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        uid = session.get("pending_2fa_uid")
        if not uid:
            return redirect(url_for("login"))
        user = User.query.get(uid)
        if not user or not user.totp_enabled:
            session.pop("pending_2fa_uid", None)
            return redirect(url_for("login"))

        if request.method == "POST":
            from totp_service import verify as totp_verify, consume_backup_code
            code = request.form.get("code", "").strip()
            ok = totp_verify(user.totp_secret, code)
            used_backup = False
            if not ok:
                ok, new_codes_json = consume_backup_code(user.totp_backup_codes, code)
                if ok:
                    used_backup = True
                    user.totp_backup_codes = new_codes_json
                    db.session.commit()
            if not ok:
                audit("login_2fa_failed", target=f"user:{user.username}", user=user)
                flash("Codice non valido. Riprova.", "danger")
                return render_template("login_2fa.html")
            remember = bool(session.pop("pending_2fa_remember", False))
            next_url = session.pop("pending_2fa_next", "") or url_for("dashboard")
            session.pop("pending_2fa_uid", None)
            login_user(user, remember=remember)
            audit("login_success", target=f"user:{user.username}", user=user,
                  details="2FA OK" + (" (backup code)" if used_backup else ""))
            if used_backup:
                import json as _json
                try:
                    remaining = len(_json.loads(user.totp_backup_codes or "[]"))
                except Exception:
                    remaining = 0
                flash(
                    f"Hai usato un codice di backup. Codici rimanenti: {remaining}. "
                    "Se ne hai pochi, rigenerali dalle Impostazioni.",
                    "warning",
                )
            return redirect(next_url)
        return render_template("login_2fa.html")

    def _delete_user_and_all_data(user):
        """Cancella un utente e TUTTI i suoi dati (clienti, fatture, settings, ticket, PEC)."""
        uid = user.id
        # PDF su disco: raccogli i filename prima di cancellare i record
        pdf_filenames = [
            inv.pdf_filename
            for inv in Invoice.query.filter_by(user_id=uid).all()
            if inv.pdf_filename
        ]
        for fname in pdf_filenames:
            delete_invoice_pdf(fname)
        # Ticket → cascade ai messaggi
        for t in SupportTicket.query.filter_by(user_id=uid).all():
            db.session.delete(t)
        for p in PecMessage.query.filter_by(user_id=uid).all():
            db.session.delete(p)
        # Clienti → cascade a fatture → reminders
        for c in Client.query.filter_by(user_id=uid).all():
            db.session.delete(c)
        # Eventuali fatture orfane
        for inv in Invoice.query.filter_by(user_id=uid).all():
            db.session.delete(inv)
        UserSetting.query.filter_by(user_id=uid).delete()
        db.session.delete(user)
        db.session.commit()

    @app.route("/logout")
    @login_required
    def logout():
        # Se è un ospite, dopo il logout cancella anche l'account + i dati
        is_guest = current_user.username.startswith("ospite_")
        guest_user = current_user._get_current_object() if is_guest else None
        username = current_user.username
        if not is_guest:
            audit("logout", target=f"user:{username}")
        logout_user()
        if guest_user:
            try:
                _delete_user_and_all_data(guest_user)
                audit("guest_deleted", target=f"user:{username}",
                      user=None, details="ospite auto-eliminato al logout")
                flash("👋 Account ospite eliminato. Grazie per aver provato GestFatture!", "info")
            except Exception as e:
                logging.error("Errore cleanup ospite: %s", e)
        return redirect(url_for("login"))

    @app.route("/login/guest")
    @limiter.limit("5 per minute")
    def login_as_guest():
        """Crea un account ospite temporaneo per chi vuole testare l'app.
        Ogni accesso crea un utente nuovo con dati separati."""
        import secrets
        suffix = secrets.token_hex(3)  # 6 caratteri esadecimali
        guest = User(username=f"ospite_{suffix}", is_admin=False,
                     email=f"ospite_{suffix}@guest.local")
        # Password impossibile: l'ospite NON può rientrare con login normale
        guest.set_password(secrets.token_urlsafe(32))
        db.session.add(guest)
        db.session.commit()
        login_user(guest, remember=False)
        audit("guest_login", target=f"user:{guest.username}", user=guest)
        flash(
            f"👋 Benvenuto, sei entrato come ospite ({guest.username}). "
            "I tuoi dati di test sono privati. Quando esci l'account scompare.",
            "info",
        )
        return redirect(url_for("dashboard"))

    @app.route("/settings/change-password", methods=["POST"])
    @login_required
    def change_password():
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        if not current_user.check_password(old):
            flash("Password attuale non corretta.", "danger")
            audit("password_change", details="password attuale errata")
        else:
            ok, err = User.validate_password(new)
            if not ok:
                flash(f"❌ {err}", "danger")
            else:
                current_user.set_password(new)
                db.session.commit()
                audit("password_change", target=f"user:{current_user.username}")
                flash("✅ Password aggiornata.", "success")
        return redirect(url_for("settings"))

    # ═══════════════════════════════════════════════════════════════════════════
    # 2FA TOTP (opzionale, opt-in dalle Impostazioni)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/settings/2fa/setup")
    @login_required
    def settings_2fa_setup():
        if current_user.totp_enabled:
            flash("2FA è già attiva. Disabilitala prima di re-iscriverti.", "warning")
            return redirect(url_for("settings"))
        from totp_service import (generate_secret, provisioning_uri, qr_data_uri)
        secret = generate_secret()
        current_user.totp_secret = secret  # candidato, non ancora attivato
        current_user.totp_enabled = False
        db.session.commit()
        uri = provisioning_uri(secret, current_user.username)
        return render_template("settings_2fa_setup.html",
                               secret=secret, qr=qr_data_uri(uri))

    @app.route("/settings/2fa/setup/verify", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def settings_2fa_setup_verify():
        from totp_service import (verify as totp_verify,
                                  generate_backup_codes, hash_codes_json)
        code = request.form.get("code", "").strip()
        if not totp_verify(current_user.totp_secret, code):
            audit("2fa_setup_failed", target=f"user:{current_user.username}",
                  details="codice di verifica iniziale errato")
            flash("Codice non valido. Verifica l'orologio del telefono e riprova.", "danger")
            return redirect(url_for("settings_2fa_setup"))
        current_user.totp_enabled = True
        plain = generate_backup_codes()
        current_user.totp_backup_codes = hash_codes_json(plain)
        db.session.commit()
        audit("2fa_enabled", target=f"user:{current_user.username}")
        return render_template("settings_2fa_codes.html", backup_codes=plain)

    @app.route("/settings/2fa/disable", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def settings_2fa_disable():
        from totp_service import (verify as totp_verify, consume_backup_code)
        password = request.form.get("password", "")
        code     = request.form.get("code", "").strip()
        if not current_user.check_password(password):
            audit("2fa_disable_failed", target=f"user:{current_user.username}",
                  details="password errata")
            flash("Password non corretta.", "danger")
            return redirect(url_for("settings"))
        ok = totp_verify(current_user.totp_secret, code)
        if not ok:
            ok, _ = consume_backup_code(current_user.totp_backup_codes, code)
        if not ok:
            audit("2fa_disable_failed", target=f"user:{current_user.username}",
                  details="codice errato")
            flash("Codice non valido.", "danger")
            return redirect(url_for("settings"))
        current_user.totp_secret = ""
        current_user.totp_enabled = False
        current_user.totp_backup_codes = ""
        db.session.commit()
        audit("2fa_disabled", target=f"user:{current_user.username}")
        flash("✅ 2FA disattivata.", "success")
        return redirect(url_for("settings"))

    @app.route("/settings/2fa/regenerate-codes", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def settings_2fa_regenerate():
        """Rigenera i codici di backup (richiede password e codice TOTP attuale)."""
        from totp_service import (verify as totp_verify,
                                  generate_backup_codes, hash_codes_json)
        if not current_user.totp_enabled:
            flash("2FA non attiva.", "warning")
            return redirect(url_for("settings"))
        password = request.form.get("password", "")
        code     = request.form.get("code", "").strip()
        if not current_user.check_password(password):
            flash("Password non corretta.", "danger")
            return redirect(url_for("settings"))
        if not totp_verify(current_user.totp_secret, code):
            flash("Codice TOTP non valido.", "danger")
            return redirect(url_for("settings"))
        plain = generate_backup_codes()
        current_user.totp_backup_codes = hash_codes_json(plain)
        db.session.commit()
        audit("2fa_codes_regenerated", target=f"user:{current_user.username}")
        return render_template("settings_2fa_codes.html",
                               backup_codes=plain, regenerated=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # GDPR — diritti dell'utente sui propri dati
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/account/export")
    @login_required
    @limiter.limit("3 per hour")
    def account_export():
        """GDPR Art. 20 — esporta tutti i dati personali in un archivio ZIP."""
        from gdpr_service import build_export_zip
        user = current_user._get_current_object()
        # Registriamo prima l'azione: così l'evento appare anche dentro l'archivio
        audit("data_export", target=f"user:{user.username}")
        buf = build_export_zip(user, upload_folder=get_upload_folder())
        fname = f"gestfatture_export_{user.username}_{date.today().isoformat()}.zip"
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # FAVICON (fallback per browser che cercano /favicon.ico in radice)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(
            app.static_folder, "favicon.png", mimetype="image/png"
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # RICONCILIAZIONE BANCARIA (GoCardless Bank Account Data, PSD2)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/my-integrations/bank")
    @login_required
    def bank_overview():
        """Lista conti collegati dell'utente + bottone per collegarne uno nuovo."""
        accounts = (BankAccount.query.filter_by(user_id=current_user.id)
                    .order_by(BankAccount.created_at.desc()).all())
        # Conta transazioni pending per UI
        pending_count = BankTransaction.query.filter_by(
            user_id=current_user.id, status="pending"
        ).filter(BankTransaction.amount > 0).count()
        return render_template("bank_overview.html",
                               accounts=accounts, pending_count=pending_count)

    @app.route("/my-integrations/bank/connect", methods=["GET", "POST"])
    @login_required
    @limiter.limit("10 per hour")
    def bank_connect():
        """Avvia il flow Tink Link: redirect a Tink per scegliere la banca."""
        from bank_service import build_link_url
        import secrets as _secrets
        try:
            base_url = AppSettings.get("app_external_url", "").rstrip("/") \
                       or request.host_url.rstrip("/")
            redirect_url = f"{base_url}/my-integrations/bank/callback"
            state = _secrets.token_urlsafe(24)
            session["bank_link_state"] = state
            url = build_link_url(redirect_url, state, market="IT", locale="it_IT")
            audit("bank_connect_start", target="tink", details=f"state:{state[:10]}")
            return redirect(url)
        except Exception as e:
            logging.exception("Errore avvio Tink Link")
            flash(f"❌ Errore: {e}", "danger")
            return redirect(url_for("bank_overview"))

    @app.route("/my-integrations/bank/callback")
    @login_required
    def bank_callback():
        """Callback Tink Link: ?code=...&state=... → exchange + fetch accounts."""
        from bank_service import (exchange_code, list_user_accounts)
        code = request.args.get("code", "").strip()
        state = request.args.get("state", "").strip()
        expected_state = session.pop("bank_link_state", None)
        error = request.args.get("error", "")

        if error:
            flash(f"❌ Connessione annullata: {error}", "warning")
            return redirect(url_for("bank_overview"))
        if not code:
            flash("Sessione scaduta o code mancante.", "warning")
            return redirect(url_for("bank_overview"))
        if not expected_state or state != expected_state:
            flash("State CSRF non valido. Riprova.", "danger")
            audit("bank_connect_failed", details="state mismatch")
            return redirect(url_for("bank_overview"))

        try:
            base_url = AppSettings.get("app_external_url", "").rstrip("/") \
                       or request.host_url.rstrip("/")
            redirect_url = f"{base_url}/my-integrations/bank/callback"
            tok_data = exchange_code(code, redirect_url)
            access_token  = tok_data["access_token"]
            refresh_token = tok_data.get("refresh_token", "")
            expires_in    = int(tok_data.get("expires_in", 3600))
            token_exp     = datetime.utcnow() + timedelta(seconds=expires_in)
            access_exp    = datetime.utcnow() + timedelta(days=90)

            accounts = list_user_accounts(access_token)
            saved = 0
            for acc in accounts:
                ext_id = acc.get("id", "")
                if not ext_id:
                    continue
                if BankAccount.query.filter_by(
                    user_id=current_user.id, external_account_id=ext_id
                ).first():
                    continue
                identifiers = acc.get("identifiers", {}) or {}
                iban = ((identifiers.get("iban", {}) or {}).get("iban") or "")
                fin = acc.get("financialInstitutionId", "") or ""
                name = acc.get("name", "") or acc.get("type", "")
                ba = BankAccount(
                    user_id=current_user.id,
                    requisition_id="",  # non usato con Tink
                    external_account_id=ext_id[:80],
                    iban=iban[:40],
                    institution_id=fin[:80],
                    institution_name=(fin or "Banca")[:120],
                    name=name[:200],
                    currency=(acc.get("currencyCode") or "EUR"),
                    status="linked",
                    expires_at=access_exp,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    token_expires_at=token_exp,
                )
                db.session.add(ba)
                saved += 1
            db.session.commit()
            audit("bank_connect_ok", target="tink", details=f"{saved} conti")
            flash(f"✅ {saved} conto/i collegato/i. Click 'Sincronizza' per scaricare le transazioni.",
                  "success")
        except Exception as e:
            logging.exception("Errore Tink callback")
            audit("bank_connect_failed", details=str(e)[:200])
            flash(f"❌ Errore: {e}", "danger")
        return redirect(url_for("bank_overview"))

    @app.route("/my-integrations/bank/<int:bid>/sync-now", methods=["POST"])
    @login_required
    @limiter.limit("10 per hour")
    def bank_sync_now(bid):
        from bank_service import sync_account, auto_reconcile_user
        ba = BankAccount.query.filter_by(id=bid, user_id=current_user.id).first_or_404()
        try:
            stats = sync_account(db, ba, days_back=60)
            recon = auto_reconcile_user(db, current_user.id)
            audit("bank_sync", target=f"account:{ba.id}",
                  details=f"new={stats['new']} auto={recon['auto_matched']}")
            flash(f"✅ Sync OK: {stats['new']} nuove transazioni · "
                  f"{recon['auto_matched']} riconciliate automaticamente · "
                  f"{recon['left_pending']} in coda manuale.", "success")
        except Exception as e:
            logging.exception("Errore sync bank manuale")
            flash(f"❌ Errore sync: {e}", "danger")
        return redirect(url_for("bank_overview"))

    @app.route("/my-integrations/bank/<int:bid>/disconnect", methods=["POST"])
    @login_required
    def bank_disconnect(bid):
        from bank_service import disconnect_account
        ba = BankAccount.query.filter_by(id=bid, user_id=current_user.id).first_or_404()
        disconnect_account(db, ba)
        audit("bank_disconnect", target=f"account:{ba.id}")
        flash("Conto bancario scollegato. Le transazioni storiche restano per la riconciliazione.", "info")
        return redirect(url_for("bank_overview"))

    @app.route("/bank/reconciliation")
    @login_required
    def bank_reconciliation():
        """Coda di transazioni pending da matchare manualmente con fatture."""
        from bank_service import find_matches_for_transaction
        # Solo entrate pending
        pending = (BankTransaction.query
                   .filter_by(user_id=current_user.id, status="pending")
                   .filter(BankTransaction.amount > 0)
                   .order_by(BankTransaction.booking_date.desc()).all())
        # Carica fatture aperte una volta sola
        open_invoices = Invoice.query.filter(
            Invoice.user_id == current_user.id,
            Invoice.status.in_(["pending", "overdue"]),
            db.or_(Invoice.document_type != "TD04", Invoice.document_type.is_(None)),
        ).all()
        # Per ogni tx, calcola top 5 candidati
        rows = []
        for tx in pending:
            candidates = find_matches_for_transaction(tx, open_invoices)[:5]
            rows.append((tx, candidates))
        return render_template("bank_reconciliation.html",
                               rows=rows, open_invoices=open_invoices)

    @app.route("/bank/reconciliation/<int:tx_id>/match/<int:inv_id>", methods=["POST"])
    @login_required
    def bank_match_manual(tx_id, inv_id):
        tx = BankTransaction.query.filter_by(id=tx_id, user_id=current_user.id).first_or_404()
        inv = Invoice.query.filter_by(id=inv_id, user_id=current_user.id).first_or_404()
        if tx.amount <= 0:
            flash("Non si può matchare un'uscita.", "warning")
            return redirect(url_for("bank_reconciliation"))
        tx.matched_invoice_id = inv.id
        tx.status = "manual_matched"
        tx.match_confidence = 100
        tx.match_reason = "Match confermato manualmente"
        tx.matched_at = datetime.utcnow()
        tx.matched_by_user_id = current_user.id
        inv.status = "paid"
        inv.payment_date = tx.booking_date or date.today()
        inv.payment_ref = f"bank:{tx.external_id[:60]}"
        db.session.commit()
        audit("bank_match_manual", target=f"tx:{tx.id}",
              details=f"invoice:{inv.id} num:{inv.number}")
        flash(f"✅ Transazione del {tx.booking_date} matchata con fattura {inv.number}. Marcata come pagata.",
              "success")
        return redirect(url_for("bank_reconciliation"))

    @app.route("/bank/reconciliation/<int:tx_id>/ignore", methods=["POST"])
    @login_required
    def bank_ignore_tx(tx_id):
        tx = BankTransaction.query.filter_by(id=tx_id, user_id=current_user.id).first_or_404()
        tx.status = "ignored"
        tx.matched_at = datetime.utcnow()
        tx.matched_by_user_id = current_user.id
        db.session.commit()
        audit("bank_ignore", target=f"tx:{tx.id}")
        flash("Transazione marcata come ignorata.", "info")
        return redirect(url_for("bank_reconciliation"))

    # ═══════════════════════════════════════════════════════════════════════════
    # KNOWLEDGE BASE / HELP
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/help")
    @login_required
    def help_page():
        return render_template("help.html")

    # ═══════════════════════════════════════════════════════════════════════════
    # BANDI di finanziamento
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/bandi")
    @login_required
    def bandi_list():
        """Lista bandi con score di rilevanza personalizzato per current_user."""
        # Filtri
        f_min_score = int(request.args.get("min_score", 30))
        f_show_dismissed = request.args.get("show_dismissed") == "1"
        f_only_saved = request.args.get("only_saved") == "1"

        q = (db.session.query(Bando, BandoMatch)
             .outerjoin(BandoMatch,
                        (BandoMatch.bando_id == Bando.id) &
                        (BandoMatch.user_id == current_user.id))
             .filter(Bando.is_active.is_(True)))
        if f_only_saved:
            q = q.filter(BandoMatch.is_saved.is_(True))
        if not f_show_dismissed:
            q = q.filter((BandoMatch.is_dismissed.is_(None)) |
                         (BandoMatch.is_dismissed.is_(False)))
        rows = q.all()
        # Filtro per score (in Python perché match potrebbe essere None)
        rows = [(b, m) for b, m in rows
                if (m.relevance_score if m else 0) >= f_min_score or f_only_saved]
        # Ordina per relevance score desc, poi deadline asc
        rows.sort(key=lambda x: (-(x[1].relevance_score if x[1] else 0),
                                 x[0].deadline or date(2099, 12, 31)))

        # Profilo completato?
        profile_complete = bool(
            UserSetting.get(current_user.id, "user_ateco_code") or
            UserSetting.get(current_user.id, "user_business_description")
        )
        return render_template("bandi_list.html",
                               rows=rows,
                               min_score=f_min_score,
                               show_dismissed=f_show_dismissed,
                               only_saved=f_only_saved,
                               profile_complete=profile_complete)

    @app.route("/bandi/<int:bid>")
    @login_required
    def bando_detail(bid):
        b = Bando.query.get_or_404(bid)
        m = BandoMatch.query.filter_by(user_id=current_user.id, bando_id=bid).first()
        return render_template("bando_detail.html", bando=b, match=m)

    @app.route("/bandi/<int:bid>/save", methods=["POST"])
    @login_required
    def bando_save(bid):
        b = Bando.query.get_or_404(bid)
        m = BandoMatch.query.filter_by(user_id=current_user.id, bando_id=bid).first()
        if not m:
            m = BandoMatch(user_id=current_user.id, bando_id=bid)
            db.session.add(m)
        m.is_saved = not m.is_saved
        if m.is_saved:
            m.is_dismissed = False
        db.session.commit()
        flash("✅ Salvato nei preferiti." if m.is_saved else "Rimosso dai preferiti.", "success")
        return redirect(request.referrer or url_for("bando_detail", bid=bid))

    @app.route("/bandi/<int:bid>/dismiss", methods=["POST"])
    @login_required
    def bando_dismiss(bid):
        b = Bando.query.get_or_404(bid)
        m = BandoMatch.query.filter_by(user_id=current_user.id, bando_id=bid).first()
        if not m:
            m = BandoMatch(user_id=current_user.id, bando_id=bid)
            db.session.add(m)
        m.is_dismissed = True
        m.is_saved = False
        db.session.commit()
        flash("Bando nascosto.", "info")
        return redirect(url_for("bandi_list"))

    @app.route("/bandi/sync-now", methods=["POST"])
    @admin_required
    def bandi_sync_now():
        """Forza sync bandi (solo admin). Esegue scraping + matching per current_user."""
        api_key = AppSettings.get("anthropic_api_key", "")
        if not api_key:
            flash("Configura prima la API key Anthropic nelle Impostazioni.", "danger")
            return redirect(url_for("bandi_list"))
        from bandi_service import sync_all_sources, compute_matches_for_user
        from claude_service import DEFAULT_MODEL
        scrape_model = AppSettings.get("anthropic_model") or DEFAULT_MODEL
        try:
            stats = sync_all_sources(db, api_key, scrape_model)
            user = current_user._get_current_object()
            n_matches = compute_matches_for_user(
                db, user, api_key, model="claude-haiku-4-5-20251001"
            )
            flash(
                f"✅ Sync completato: {stats['new']} nuovi bandi, "
                f"{stats['updated']} aggiornati, {stats['errors']} errori. "
                f"Match aggiornati per te: {n_matches}.",
                "success",
            )
        except Exception as e:
            logging.exception("Sync bandi fallito")
            flash(f"❌ Errore sync bandi: {e}", "danger")
        return redirect(url_for("bandi_list"))

    # ── Customer portal pubblico (link firmato, no login) ────────────────────
    @app.route("/portal/<token>")
    @limiter.limit("60 per hour")
    def client_portal(token):
        from tokens import verify_portal_token
        payload = verify_portal_token(token)
        if not payload:
            return render_template("portal_error.html",
                                   reason="Link non valido o scaduto."), 404
        client = Client.query.filter_by(id=payload["c"], user_id=payload["u"]).first()
        if not client:
            return render_template("portal_error.html",
                                   reason="Cliente non trovato."), 404
        owner = User.query.get(payload["u"])
        company_name = (UserSetting.get(owner.id, "company_name") if owner else "") \
                       or AppSettings.get("company_name", "GestFatture")
        contact_email = AppSettings.get("legal_contact_email", "") or (owner.email if owner else "")
        # Aggiorna stato fatture (per evitare di mostrare "pending" su fatture in realtà scadute)
        for inv in client.invoices:
            inv.update_status()
        db.session.commit()
        # Solo fatture vere (no NC)
        invs = [i for i in client.invoices if i.document_type != "TD04"]
        open_invs = sorted(
            [i for i in invs if i.status in ("pending", "overdue")],
            key=lambda i: i.due_date,
        )
        paid_invs = sorted(
            [i for i in invs if i.status == "paid"],
            key=lambda i: i.payment_date or i.due_date, reverse=True,
        )
        total_due = sum(i.amount for i in open_invs)
        return render_template("portal_client.html",
                               client=client, company_name=company_name,
                               contact_email=contact_email,
                               open_invs=open_invs, paid_invs=paid_invs,
                               total_due=total_due, token=token)

    @app.route("/portal/<token>/invoice/<int:iid>/pdf")
    @limiter.limit("60 per hour")
    def client_portal_pdf(token, iid):
        """Scarica un PDF di fattura tramite token portale (no login)."""
        from tokens import verify_portal_token
        payload = verify_portal_token(token)
        if not payload:
            abort(404)
        inv = Invoice.query.filter_by(
            id=iid, client_id=payload["c"], user_id=payload["u"]
        ).first()
        if not inv or not inv.pdf_filename:
            abort(404)
        return send_from_directory(get_upload_folder(), inv.pdf_filename,
                                   as_attachment=False)

    # ── Pagine legali pubbliche (privacy + termini) ──────────────────────────
    def _legal_context():
        keys = ("legal_company", "legal_vat", "legal_address", "legal_contact_email")
        ctx = {k: AppSettings.get(k, "") for k in keys}
        # Fallback al company_name globale se l'admin non ha specificato un'entità legale separata
        if not ctx["legal_company"]:
            ctx["legal_company"] = AppSettings.get("company_name", "")
        ctx["legal_complete"] = all(ctx[k] for k in keys)
        ctx["updated_at"] = "4 maggio 2026"
        return ctx

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html", **_legal_context())

    @app.route("/terms")
    def terms():
        return render_template("terms.html", **_legal_context())

    @app.route("/account/delete", methods=["POST"])
    @login_required
    @limiter.limit("3 per hour")
    def account_delete():
        """GDPR Art. 17 — cancella permanentemente l'account e tutti i dati."""
        password     = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "").strip()

        if confirmation != "ELIMINA":
            flash("Per confermare devi digitare ELIMINA in maiuscolo.", "danger")
            return redirect(url_for("settings"))

        if not current_user.check_password(password):
            audit("account_delete_failed",
                  target=f"user:{current_user.username}",
                  details="password errata")
            flash("Password non corretta. Cancellazione annullata.", "danger")
            return redirect(url_for("settings"))

        # Protezione: l'unico amministratore non può eliminarsi (lock-out)
        if current_user.is_admin:
            other_admins = (
                User.query.filter(User.id != current_user.id, User.is_admin.is_(True)).count()
            )
            if other_admins == 0:
                flash(
                    "Sei l'unico amministratore: non puoi cancellare l'account. "
                    "Promuovi prima un altro utente come admin.",
                    "danger",
                )
                return redirect(url_for("settings"))

        user = current_user._get_current_object()
        username = user.username

        # Audit PRIMA della cancellazione (la riga rimane nel log anche dopo)
        audit("account_deleted", target=f"user:{username}",
              details="cancellazione su richiesta utente (GDPR Art. 17)")

        logout_user()
        try:
            _delete_user_and_all_data(user)
            flash(
                "✅ Account e tutti i tuoi dati sono stati cancellati definitivamente. "
                "Grazie per aver usato GestFatture.",
                "success",
            )
        except Exception as e:
            logging.exception("Errore cancellazione account: %s", e)
            flash(
                "⚠️ Errore durante la cancellazione. Contatta l'assistenza.",
                "danger",
            )
        return redirect(url_for("login"))

    # ═══════════════════════════════════════════════════════════════════════════
    # DASHBOARD
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/")
    @login_required
    def dashboard():
        today = date.today()
        uid = current_user.id

        for inv in my_invoices().filter(Invoice.status == "pending").all():
            inv.update_status()
        db.session.commit()

        # Helper: conta/somma SOLO fatture vere (TD01/TD05/None per legacy), no TD04
        invoices_only = my_invoices().filter(
            db.or_(Invoice.document_type != "TD04", Invoice.document_type.is_(None))
        )

        total_invoices   = invoices_only.count()
        paid_invoices    = invoices_only.filter_by(status="paid").count()
        overdue_invoices = invoices_only.filter_by(status="overdue").count()
        pending_invoices = invoices_only.filter_by(status="pending").count()

        sum_q = lambda **kw: (
            db.session.query(db.func.sum(Invoice.amount))
            .filter_by(user_id=uid, **kw)
            .filter(db.or_(Invoice.document_type != "TD04", Invoice.document_type.is_(None)))
            .scalar() or 0
        )
        total_amount_issued  = sum_q()
        total_amount_paid    = sum_q(status="paid")
        total_amount_pending = sum_q(status="pending")
        total_overdue_amount = sum_q(status="overdue")

        # Statistiche separate per Note di Credito (TD04) e Note di Debito (TD05)
        credit_notes_count   = my_invoices().filter_by(document_type="TD04").count()
        credit_notes_amount  = (
            db.session.query(db.func.sum(Invoice.amount))
            .filter_by(user_id=uid, document_type="TD04").scalar() or 0
        )
        debit_notes_count    = my_invoices().filter_by(document_type="TD05").count()
        debit_notes_amount   = (
            db.session.query(db.func.sum(Invoice.amount))
            .filter_by(user_id=uid, document_type="TD05").scalar() or 0
        )

        from sqlalchemy import and_
        upcoming = [
            i for i in my_invoices().filter(
                and_(Invoice.status == "pending", Invoice.due_date >= today)
            ).order_by(Invoice.due_date).limit(10).all()
            if i.days_until_due <= 7
        ]

        top_overdue = my_invoices().filter_by(status="overdue").order_by(Invoice.due_date).limit(5).all()
        top_debtors = (
            my_clients().join(Invoice, Invoice.client_id == Client.id)
            .filter(Invoice.status == "overdue", Invoice.user_id == uid)
            .order_by(Client.credit_score)
            .limit(5).all()
        )

        # ── LATO PASSIVO: pagamenti che devo fare ─────────────────────────────
        for inv in my_payables().filter(Invoice.status.in_(["pending", "overdue"])).all():
            inv.update_status()
        db.session.commit()

        payable_pending_count = my_payables().filter_by(status="pending").count()
        payable_overdue_count = my_payables().filter_by(status="overdue").count()
        payable_paid_count    = my_payables().filter_by(status="paid").count()

        sum_pq = lambda **kw: (
            db.session.query(db.func.sum(Invoice.amount))
            .filter_by(user_id=uid, is_passive=True, **kw)
            .scalar() or 0
        )
        payable_open_amount    = sum_pq(status="pending") + sum_pq(status="overdue")
        payable_overdue_amount = sum_pq(status="overdue")
        payable_paid_30d = (
            db.session.query(db.func.sum(Invoice.amount))
            .filter(Invoice.user_id == uid, Invoice.is_passive.is_(True),
                    Invoice.status == "paid",
                    Invoice.payment_date >= (today - timedelta(days=30)))
            .scalar() or 0
        )

        upcoming_payables = sorted(
            [i for i in my_payables().filter(
                Invoice.status.in_(["pending", "overdue"])
            ).all()],
            key=lambda i: i.due_date
        )[:5]

        # Saldo netto previsto = entrate aperte - uscite aperte
        net_position = total_amount_pending + total_overdue_amount - payable_open_amount

        return render_template("dashboard.html",
            total_invoices=total_invoices, paid_invoices=paid_invoices,
            overdue_invoices=overdue_invoices, pending_invoices=pending_invoices,
            total_amount_issued=total_amount_issued,
            total_amount_paid=total_amount_paid,
            total_amount_pending=total_amount_pending,
            total_overdue_amount=total_overdue_amount,
            credit_notes_count=credit_notes_count, credit_notes_amount=credit_notes_amount,
            debit_notes_count=debit_notes_count,   debit_notes_amount=debit_notes_amount,
            upcoming=upcoming, top_overdue=top_overdue, top_debtors=top_debtors,
            payable_pending_count=payable_pending_count,
            payable_overdue_count=payable_overdue_count,
            payable_paid_count=payable_paid_count,
            payable_open_amount=payable_open_amount,
            payable_overdue_amount=payable_overdue_amount,
            payable_paid_30d=payable_paid_30d,
            upcoming_payables=upcoming_payables,
            net_position=net_position,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # SALUTE AZIENDALE (vista executive: tutto a colpo d'occhio)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/health")
    @login_required
    def health_overview():
        """Quadro generale: liquidità, fatture, pagamenti, fiscale, bandi, ticket."""
        today = date.today()
        uid = current_user.id

        # ── Banche (saldo attuale) ──────────────────────────────────────────
        bank_accounts = BankAccount.query.filter_by(user_id=uid, status="linked").all()
        starting_balance = sum(a.last_balance or 0 for a in bank_accounts
                               if a.last_balance is not None)
        has_balance = any(a.last_balance is not None for a in bank_accounts)

        # ── Fatture aperte (attive) ─────────────────────────────────────────
        for inv in my_invoices().filter(Invoice.status.in_(["pending", "overdue"])).all():
            inv.update_status()
        for inv in my_payables().filter(Invoice.status.in_(["pending", "overdue"])).all():
            inv.update_status()
        db.session.commit()

        active_open_amount = (db.session.query(db.func.sum(Invoice.amount))
                              .filter_by(user_id=uid, is_passive=False)
                              .filter(Invoice.status.in_(["pending", "overdue"]))
                              .filter(db.or_(Invoice.document_type != "TD04",
                                             Invoice.document_type.is_(None)))
                              .scalar() or 0)
        active_overdue_amount = (db.session.query(db.func.sum(Invoice.amount))
                                 .filter_by(user_id=uid, is_passive=False, status="overdue")
                                 .scalar() or 0)
        active_overdue_count = my_invoices().filter_by(status="overdue").count()

        # ── Pagamenti (passive) ─────────────────────────────────────────────
        passive_open_amount = (db.session.query(db.func.sum(Invoice.amount))
                               .filter_by(user_id=uid, is_passive=True)
                               .filter(Invoice.status.in_(["pending", "overdue"]))
                               .scalar() or 0)
        passive_overdue_amount = (db.session.query(db.func.sum(Invoice.amount))
                                  .filter_by(user_id=uid, is_passive=True, status="overdue")
                                  .scalar() or 0)
        passive_overdue_count = my_payables().filter_by(status="overdue").count()

        # ── Saldo netto previsto ────────────────────────────────────────────
        net_position = starting_balance + active_open_amount - passive_open_amount

        # ── Prossimi 7gg: pagamenti + scadenze fiscali ──────────────────────
        next_7d = today + timedelta(days=7)
        upcoming_payables = (my_payables()
                             .filter(Invoice.status.in_(["pending", "overdue"]))
                             .filter(Invoice.due_date <= next_7d)
                             .order_by(Invoice.due_date).limit(10).all())
        upcoming_active = (my_invoices()
                           .filter(Invoice.status.in_(["pending", "overdue"]))
                           .filter(Invoice.due_date <= next_7d)
                           .order_by(Invoice.due_date).limit(10).all())

        # ── Scadenze fiscali (prossimi 30gg) ────────────────────────────────
        upcoming_fiscal = (FiscalDeadline.query
                           .filter_by(user_id=uid, completed=False)
                           .filter(FiscalDeadline.deadline >= today)
                           .filter(FiscalDeadline.deadline <= today + timedelta(days=30))
                           .order_by(FiscalDeadline.deadline).limit(10).all())
        fiscal_overdue = (FiscalDeadline.query
                          .filter_by(user_id=uid, completed=False)
                          .filter(FiscalDeadline.deadline < today).count())

        # ── Riepilogo mensile (ultimi 12 mesi): incassato + pagato ─────────
        from dateutil.relativedelta import relativedelta
        monthly_summary = []
        first_day_curr = today.replace(day=1)
        for i in range(11, -1, -1):
            m_start = first_day_curr - relativedelta(months=i)
            m_end   = (m_start + relativedelta(months=1)) - timedelta(days=1)
            incassato = (db.session.query(db.func.sum(Invoice.amount))
                         .filter_by(user_id=uid, is_passive=False, status="paid")
                         .filter(Invoice.payment_date >= m_start,
                                 Invoice.payment_date <= m_end)
                         .filter(db.or_(Invoice.document_type != "TD04",
                                        Invoice.document_type.is_(None)))
                         .scalar() or 0)
            pagato = (db.session.query(db.func.sum(Invoice.amount))
                      .filter_by(user_id=uid, is_passive=True, status="paid")
                      .filter(Invoice.payment_date >= m_start,
                              Invoice.payment_date <= m_end)
                      .scalar() or 0)
            monthly_summary.append({
                "label": m_start.strftime("%b %y").capitalize(),
                "year_month": m_start.strftime("%Y-%m"),
                "incassato": incassato,
                "pagato": pagato,
                "netto": incassato - pagato,
            })
        max_monthly = max([abs(m["incassato"]) for m in monthly_summary] +
                          [abs(m["pagato"]) for m in monthly_summary] + [1])

        # ── Bandi rilevanti ─────────────────────────────────────────────────
        top_bandi = (db.session.query(Bando, BandoMatch)
                     .join(BandoMatch, BandoMatch.bando_id == Bando.id)
                     .filter(BandoMatch.user_id == uid,
                             BandoMatch.is_dismissed.is_not(True),
                             Bando.is_active.is_(True),
                             BandoMatch.relevance_score >= 60)
                     .order_by(BandoMatch.relevance_score.desc())
                     .limit(5).all())

        # ── Ticket aperti ───────────────────────────────────────────────────
        open_tickets = (SupportTicket.query.filter_by(user_id=uid)
                        .filter(SupportTicket.status.in_(["open", "in_progress", "waiting_user"]))
                        .count())

        # ── Riconciliazione bancaria pending ────────────────────────────────
        bank_pending = BankTransaction.query.filter_by(
            user_id=uid, status="pending"
        ).filter(BankTransaction.amount > 0).count()

        # ── Alert critici ───────────────────────────────────────────────────
        alerts = []
        if net_position < 0:
            alerts.append({
                "level": "danger", "icon": "exclamation-octagon",
                "msg": f"Saldo netto previsto negativo: {net_position:+,.2f} €. Considera di sollecitare incassi o rimandare uscite.".replace(",", "X").replace(".", ",").replace("X", "."),
                "action_url": url_for("cash_flow"), "action_label": "Vedi cash flow",
            })
        if active_overdue_count >= 3:
            alerts.append({
                "level": "warning", "icon": "exclamation-triangle",
                "msg": f"{active_overdue_count} fatture attive sono scadute. Solleciti automatici attivi, ma controlla.",
                "action_url": url_for("invoices") + "?status=overdue", "action_label": "Vedi scadute",
            })
        if passive_overdue_count >= 1:
            alerts.append({
                "level": "danger", "icon": "credit-card",
                "msg": f"{passive_overdue_count} pagamenti in ritardo verso fornitori — €{passive_overdue_amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."),
                "action_url": url_for("payables") + "?status=open", "action_label": "Vedi pagamenti",
            })
        if fiscal_overdue:
            alerts.append({
                "level": "danger", "icon": "calendar-x",
                "msg": f"{fiscal_overdue} scadenze fiscali in ritardo!",
                "action_url": url_for("fiscal_deadlines"), "action_label": "Vedi scadenze",
            })
        if not has_balance and bank_accounts:
            alerts.append({
                "level": "info", "icon": "info-circle",
                "msg": "Saldi banche non aggiornati. Sincronizza per avere un saldo netto reale.",
                "action_url": url_for("bank_overview"), "action_label": "Vai alle banche",
            })

        return render_template("health.html",
            starting_balance=starting_balance,
            has_balance=has_balance,
            bank_accounts=bank_accounts,
            active_open_amount=active_open_amount,
            active_overdue_amount=active_overdue_amount,
            active_overdue_count=active_overdue_count,
            passive_open_amount=passive_open_amount,
            passive_overdue_amount=passive_overdue_amount,
            passive_overdue_count=passive_overdue_count,
            net_position=net_position,
            upcoming_payables=upcoming_payables,
            upcoming_active=upcoming_active,
            upcoming_fiscal=upcoming_fiscal,
            fiscal_overdue=fiscal_overdue,
            top_bandi=top_bandi,
            open_tickets=open_tickets,
            bank_pending=bank_pending,
            alerts=alerts,
            today=today,
            monthly_summary=monthly_summary,
            max_monthly=max_monthly,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # CALENDARIO SCADENZE FISCALI
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/fiscal")
    @login_required
    def fiscal_deadlines():
        status = request.args.get("status", "open")  # open / completed / all
        q = FiscalDeadline.query.filter_by(user_id=current_user.id)
        if status == "open":
            q = q.filter_by(completed=False)
        elif status == "completed":
            q = q.filter_by(completed=True)
        items = q.order_by(FiscalDeadline.deadline.asc()).all()
        # KPI
        all_open = FiscalDeadline.query.filter_by(user_id=current_user.id, completed=False).all()
        kpi = {
            "open":     len(all_open),
            "overdue":  sum(1 for x in all_open if x.days_until < 0),
            "this_week": sum(1 for x in all_open if 0 <= x.days_until <= 7),
            "this_month": sum(1 for x in all_open if 0 <= x.days_until <= 30),
        }
        return render_template("fiscal_deadlines.html", items=items, status=status, kpi=kpi)

    @app.route("/fiscal/new", methods=["GET", "POST"])
    @login_required
    def new_fiscal_deadline():
        if request.method == "POST":
            try:
                amt = request.form.get("amount", "").strip().replace(",", ".")
                amount = float(amt) if amt else None
            except ValueError:
                amount = None
            d = FiscalDeadline(
                user_id=current_user.id,
                title=request.form["title"][:200],
                deadline=datetime.strptime(request.form["deadline"], "%Y-%m-%d").date(),
                category=request.form.get("category", "altro")[:40],
                amount=amount,
                notes=request.form.get("notes", ""),
                is_recurring=request.form.get("is_recurring") == "on",
                recurrence=request.form.get("recurrence", "")[:20],
            )
            db.session.add(d); db.session.commit()
            flash("Scadenza aggiunta.", "success")
            return redirect(url_for("fiscal_deadlines"))
        return render_template("new_fiscal_deadline.html",
                               today_iso=date.today().isoformat())

    @app.route("/fiscal/<int:fid>/complete", methods=["POST"])
    @login_required
    def fiscal_complete(fid):
        d = FiscalDeadline.query.filter_by(id=fid, user_id=current_user.id).first_or_404()
        d.completed = True
        d.completed_at = datetime.utcnow()
        # Se ricorrente, genera la prossima istanza
        if d.is_recurring and d.recurrence:
            from dateutil.relativedelta import relativedelta
            try:
                if d.recurrence == "monthly":
                    next_d = d.deadline + relativedelta(months=1)
                elif d.recurrence == "quarterly":
                    next_d = d.deadline + relativedelta(months=3)
                elif d.recurrence == "yearly":
                    next_d = d.deadline + relativedelta(years=1)
                else:
                    next_d = None
                if next_d:
                    db.session.add(FiscalDeadline(
                        user_id=current_user.id, title=d.title,
                        deadline=next_d, category=d.category,
                        amount=d.amount, notes=d.notes,
                        is_recurring=True, recurrence=d.recurrence,
                    ))
            except ImportError:
                # dateutil non disponibile (improbabile, è dep di anthropic), fallback manuale
                pass
        db.session.commit()
        flash(f"✅ Scadenza '{d.title}' marcata come completata.", "success")
        return redirect(url_for("fiscal_deadlines"))

    @app.route("/fiscal/<int:fid>/delete", methods=["POST"])
    @login_required
    def fiscal_delete(fid):
        d = FiscalDeadline.query.filter_by(id=fid, user_id=current_user.id).first_or_404()
        db.session.delete(d); db.session.commit()
        flash("Scadenza eliminata.", "info")
        return redirect(url_for("fiscal_deadlines"))

    @app.route("/fiscal/seed-it", methods=["POST"])
    @login_required
    def fiscal_seed_it():
        """Popola le scadenze italiane standard per l'anno in corso (idempotente)."""
        from datetime import date as _d
        year = _d.today().year
        # Lista scadenze fisse italiane (semplificate, regime ordinario)
        templates = [
            # IVA mensile (versamento il 16 di ogni mese)
            *[(_d(year, m, 16), f"IVA mensile {_d(year, m, 1).strftime('%B').capitalize()} {year}",
               "iva_mensile", "monthly") for m in range(1, 13)],
            # IVA trimestrale (16 maggio/agosto/novembre/febbraio successivo)
            (_d(year, 5, 16),  f"IVA 1° trimestre {year}", "iva_trimestrale", "quarterly"),
            (_d(year, 8, 16),  f"IVA 2° trimestre {year}", "iva_trimestrale", "quarterly"),
            (_d(year, 11, 16), f"IVA 3° trimestre {year}", "iva_trimestrale", "quarterly"),
            # F24 ritenute lavoratori (16 di ogni mese — già coperto da IVA mensile)
            # INPS commercianti/artigiani (16 maggio/agosto/novembre/febbraio)
            (_d(year, 5, 16),  f"INPS artigiani 1ª rata {year}", "inps", "yearly"),
            (_d(year, 8, 20),  f"INPS artigiani 2ª rata {year}", "inps", "yearly"),
            (_d(year, 11, 16), f"INPS artigiani 3ª rata {year}", "inps", "yearly"),
            # INAIL autoliquidazione
            (_d(year, 2, 16),  f"INAIL autoliquidazione {year}", "inail", "yearly"),
            # CCIAA diritto annuale
            (_d(year, 6, 30),  f"Diritto annuale CCIAA {year}", "cciaa", "yearly"),
            # LIPE Liquidazione IVA Periodica trimestrale
            (_d(year, 5, 31),  f"LIPE 1° trim {year}",  "lipe", "yearly"),
            (_d(year, 9, 16),  f"LIPE 2° trim {year}",  "lipe", "yearly"),
            (_d(year, 11, 30), f"LIPE 3° trim {year}",  "lipe", "yearly"),
            # Acconti IRPEF
            (_d(year, 6, 30),  f"1° acconto IRPEF {year}", "f24", "yearly"),
            (_d(year, 11, 30), f"2° acconto IRPEF {year}", "f24", "yearly"),
            # Dichiarazione redditi
            (_d(year, 11, 30), f"Dichiarazione redditi {year}", "dichiarazione", "yearly"),
            # 770 sostituti d'imposta
            (_d(year, 10, 31), f"Mod. 770 - sostituti d'imposta {year}", "770", "yearly"),
        ]
        added = 0
        skipped = 0
        for dl_date, title, cat, recur in templates:
            # Skip se già presente (idempotente per title+deadline)
            if FiscalDeadline.query.filter_by(
                user_id=current_user.id, title=title, deadline=dl_date
            ).first():
                skipped += 1
                continue
            db.session.add(FiscalDeadline(
                user_id=current_user.id, title=title, deadline=dl_date,
                category=cat, is_recurring=True, recurrence=recur,
            ))
            added += 1
        db.session.commit()
        audit("fiscal_seed_it", details=f"added={added}, skipped={skipped}")
        flash(f"✅ Caricate {added} scadenze italiane standard ({skipped} già presenti).",
              "success")
        return redirect(url_for("fiscal_deadlines"))

    # ═══════════════════════════════════════════════════════════════════════════
    # CASH FLOW FORECAST (proiezione liquidità 90gg)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/cash-flow")
    @login_required
    def cash_flow():
        """Pagina previsione liquidità: settimanale per 12 settimane (~3 mesi).
        Combina saldo banche + fatture aperte attive (entrate previste) +
        fatture passive aperte (uscite previste)."""
        today = date.today()
        weeks = 12

        # Saldo iniziale: somma saldi banche linked
        accounts = (BankAccount.query.filter_by(user_id=current_user.id, status="linked")
                    .filter(BankAccount.last_balance.isnot(None)).all())
        starting_balance = sum(a.last_balance or 0 for a in accounts)
        has_balance_data = bool(accounts)

        # Aggiorna stato fatture aperte
        for inv in my_invoices().filter(Invoice.status.in_(["pending", "overdue"])).all():
            inv.update_status()
        for inv in my_payables().filter(Invoice.status.in_(["pending", "overdue"])).all():
            inv.update_status()
        db.session.commit()

        # Buckets settimanali: lunedi → domenica
        from collections import defaultdict
        # Trova il prossimo lunedi (o oggi se è lunedi)
        week_start = today - timedelta(days=today.weekday())  # lunedi corrente
        buckets_in  = defaultdict(float)
        buckets_out = defaultdict(float)
        overdue_in_total  = 0.0
        overdue_out_total = 0.0

        # Fatture attive aperte: entrata prevista alla due_date
        for inv in my_invoices().filter(Invoice.status.in_(["pending", "overdue"])).all():
            if inv.is_credit_note:
                continue
            if inv.due_date < week_start:
                overdue_in_total += inv.amount
                continue
            week_idx = (inv.due_date - week_start).days // 7
            if 0 <= week_idx < weeks:
                buckets_in[week_idx] += inv.amount

        # Fatture passive aperte: uscita prevista alla due_date
        for inv in my_payables().filter(Invoice.status.in_(["pending", "overdue"])).all():
            if inv.due_date < week_start:
                overdue_out_total += inv.amount
                continue
            week_idx = (inv.due_date - week_start).days // 7
            if 0 <= week_idx < weeks:
                buckets_out[week_idx] += inv.amount

        # Costruisci righe: saldo cumulativo settimana per settimana
        # Saldo iniziale = saldo banche - già scaduti uscite + già scaduti entrate
        # (assumendo che gli scaduti vengano "regolati subito" all'inizio)
        cumulative = starting_balance + overdue_in_total - overdue_out_total
        rows = []
        for w in range(weeks):
            wk_start = week_start + timedelta(weeks=w)
            wk_end   = wk_start + timedelta(days=6)
            inflow  = buckets_in.get(w, 0)
            outflow = buckets_out.get(w, 0)
            net     = inflow - outflow
            cumulative += net
            rows.append({
                "week_start": wk_start,
                "week_end":   wk_end,
                "label":      f"{wk_start.strftime('%d/%m')} – {wk_end.strftime('%d/%m')}",
                "inflow":     inflow,
                "outflow":    outflow,
                "net":        net,
                "cumulative": cumulative,
                "is_negative": cumulative < 0,
            })

        # Min cumulativo (peggiore proiezione) per allerta visiva
        min_cumulative = min((r["cumulative"] for r in rows), default=cumulative)
        # Max abs per scaling barre
        max_abs = max(
            [abs(r["inflow"]) for r in rows] + [abs(r["outflow"]) for r in rows] + [1]
        )

        return render_template("cash_flow.html",
            rows=rows,
            starting_balance=starting_balance,
            has_balance_data=has_balance_data,
            accounts=accounts,
            overdue_in=overdue_in_total,
            overdue_out=overdue_out_total,
            min_cumulative=min_cumulative,
            max_abs=max_abs,
            today=today,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # CLIENTI
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/clients")
    @login_required
    def clients():
        q    = request.args.get("q", "")
        sort = request.args.get("sort", "name")
        q_obj = my_clients()
        if q:
            q_obj = q_obj.filter(Client.name.ilike(f"%{q}%"))
        q_obj = q_obj.order_by(Client.credit_score if sort == "score" else Client.name)
        return render_template("clients.html", clients=q_obj.all(), q=q, sort=sort)

    @app.route("/clients/new", methods=["GET", "POST"])
    @login_required
    def new_client():
        if request.method == "POST":
            c = Client(
                user_id=current_user.id,
                name=request.form["name"], email=request.form.get("email",""),
                pec=request.form.get("pec",""), phone=request.form.get("phone",""),
                address=request.form.get("address",""), vat_number=request.form.get("vat_number",""),
            )
            db.session.add(c); db.session.commit()
            flash("Cliente aggiunto.", "success")
            return redirect(url_for("client_detail", cid=c.id))
        return render_template("new_client.html")

    @app.route("/clients/<int:cid>")
    @login_required
    def client_detail(cid):
        from tokens import make_portal_url
        c = get_my_client(cid)
        return render_template("client_detail.html",
                               client=c,
                               portal_url=make_portal_url(c))

    @app.route("/clients/<int:cid>/edit", methods=["GET", "POST"])
    @login_required
    def edit_client(cid):
        c = get_my_client(cid)
        if request.method == "POST":
            c.name=request.form["name"]; c.email=request.form.get("email","")
            c.pec=request.form.get("pec",""); c.phone=request.form.get("phone","")
            c.address=request.form.get("address",""); c.vat_number=request.form.get("vat_number","")
            db.session.commit(); flash("Cliente aggiornato.", "success")
            return redirect(url_for("client_detail", cid=c.id))
        return render_template("new_client.html", client=c)

    @app.route("/clients/<int:cid>/delete", methods=["POST"])
    @login_required
    def delete_client(cid):
        c = get_my_client(cid)
        db.session.delete(c); db.session.commit()
        flash("Cliente eliminato.", "info")
        return redirect(url_for("clients"))

    @app.route("/clients/merge-duplicates", methods=["POST"])
    @login_required
    def merge_duplicate_clients():
        """Unifica i clienti del current_user che hanno la stessa P.IVA."""
        from collections import defaultdict
        all_clients = my_clients().filter(Client.vat_number != "").all()

        # Raggruppa per P.IVA
        groups = defaultdict(list)
        for c in all_clients:
            groups[c.vat_number].append(c)

        merged_count = 0
        for vat, clients_with_vat in groups.items():
            if len(clients_with_vat) <= 1:
                continue
            # Tieni il primo (più vecchio) e sposta tutto il resto su quello
            keeper = clients_with_vat[0]
            duplicates = clients_with_vat[1:]
            for dup in duplicates:
                # Sposta tutte le fatture del duplicato sul keeper
                for inv in dup.invoices:
                    inv.client_id = keeper.id
                    inv.client    = keeper
                # Aggiorna campi mancanti del keeper con quelli del duplicato
                if dup.email   and not keeper.email:   keeper.email   = dup.email
                if dup.pec     and not keeper.pec:     keeper.pec     = dup.pec
                if dup.phone   and not keeper.phone:   keeper.phone   = dup.phone
                if dup.address and not keeper.address: keeper.address = dup.address
                # Elimina il duplicato
                db.session.delete(dup)
                merged_count += 1

        db.session.commit()
        if merged_count:
            audit("clients_merged", details=f"{merged_count} duplicati rimossi")
            flash(f"✅ Unificati {merged_count} clienti duplicati (raggruppati per P.IVA).", "success")
        else:
            flash("Nessun duplicato trovato.", "info")
        return redirect(url_for("clients"))

    # ═══════════════════════════════════════════════════════════════════════════
    # FORNITORI (lato passivo) — Client.is_supplier=True
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/suppliers")
    @login_required
    def suppliers():
        q = request.args.get("q", "")
        sort = request.args.get("sort", "name")
        q_obj = my_suppliers()
        if q:
            q_obj = q_obj.filter(Client.name.ilike(f"%{q}%"))
        q_obj = q_obj.order_by(Client.name)
        return render_template("suppliers.html", suppliers=q_obj.all(), q=q, sort=sort)

    @app.route("/suppliers/new", methods=["GET", "POST"])
    @login_required
    def new_supplier():
        if request.method == "POST":
            s = Client(
                user_id=current_user.id, is_supplier=True,
                name=request.form["name"], email=request.form.get("email", ""),
                pec=request.form.get("pec", ""), phone=request.form.get("phone", ""),
                address=request.form.get("address", ""),
                vat_number=request.form.get("vat_number", ""),
                iban=request.form.get("iban", ""),
            )
            db.session.add(s); db.session.commit()
            flash("Fornitore aggiunto.", "success")
            return redirect(url_for("supplier_detail", sid=s.id))
        return render_template("new_supplier.html")

    @app.route("/suppliers/<int:sid>")
    @login_required
    def supplier_detail(sid):
        s = Client.query.filter_by(id=sid, user_id=current_user.id, is_supplier=True).first_or_404()
        # Fatture passive di questo fornitore
        payables = (Invoice.query.filter_by(user_id=current_user.id,
                                            client_id=s.id, is_passive=True)
                    .order_by(Invoice.due_date.desc()).all())
        total_pending = sum(i.amount for i in payables if i.status in ("pending", "overdue"))
        total_paid = sum(i.amount for i in payables if i.status == "paid")
        return render_template("supplier_detail.html", supplier=s, payables=payables,
                               total_pending=total_pending, total_paid=total_paid)

    @app.route("/suppliers/<int:sid>/edit", methods=["GET", "POST"])
    @login_required
    def edit_supplier(sid):
        s = Client.query.filter_by(id=sid, user_id=current_user.id, is_supplier=True).first_or_404()
        if request.method == "POST":
            s.name = request.form["name"]
            s.email = request.form.get("email", "")
            s.pec = request.form.get("pec", "")
            s.phone = request.form.get("phone", "")
            s.address = request.form.get("address", "")
            s.vat_number = request.form.get("vat_number", "")
            s.iban = request.form.get("iban", "")
            db.session.commit()
            flash("Fornitore aggiornato.", "success")
            return redirect(url_for("supplier_detail", sid=s.id))
        return render_template("new_supplier.html", supplier=s)

    @app.route("/suppliers/<int:sid>/delete", methods=["POST"])
    @login_required
    def delete_supplier(sid):
        s = Client.query.filter_by(id=sid, user_id=current_user.id, is_supplier=True).first_or_404()
        # Non cancello se ha fatture passive collegate
        n = Invoice.query.filter_by(client_id=s.id, is_passive=True).count()
        if n:
            flash(f"Impossibile cancellare: ha {n} fatture passive collegate.", "danger")
            return redirect(url_for("supplier_detail", sid=s.id))
        db.session.delete(s); db.session.commit()
        flash("Fornitore eliminato.", "info")
        return redirect(url_for("suppliers"))

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGAMENTI DA FARE — Invoice.is_passive=True
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/payables")
    @login_required
    def payables():
        from sqlalchemy import func as _f
        status = request.args.get("status", "open")  # open / paid / all
        q = my_payables()
        if status == "open":
            q = q.filter(Invoice.status.in_(["pending", "overdue"]))
        elif status == "paid":
            q = q.filter_by(status="paid")
        invs = q.order_by(Invoice.due_date.asc()).all()

        # Aggiorna stato (pending → overdue se scadute)
        for inv in invs:
            inv.update_status()
        db.session.commit()

        # KPI
        all_open = my_payables().filter(Invoice.status.in_(["pending", "overdue"])).all()
        kpi = {
            "open_count":   len(all_open),
            "open_amount":  sum(i.amount for i in all_open),
            "overdue_count": sum(1 for i in all_open if i.status == "overdue"),
            "overdue_amount": sum(i.amount for i in all_open if i.status == "overdue"),
            "paid_30d_amount": (
                db.session.query(_f.sum(Invoice.amount))
                .filter(Invoice.user_id == current_user.id,
                        Invoice.is_passive.is_(True),
                        Invoice.status == "paid",
                        Invoice.payment_date >= (date.today() - timedelta(days=30)))
                .scalar() or 0
            ),
        }
        return render_template("payables.html", invs=invs, status=status, kpi=kpi)

    @app.route("/payables/new", methods=["GET", "POST"])
    @login_required
    def new_payable():
        if request.method == "POST":
            supplier_id = request.form.get("supplier_id", "").strip()
            if not supplier_id:
                # Nuovo fornitore al volo (solo se nome compilato)
                sup_name = request.form.get("new_supplier_name", "").strip()
                if not sup_name:
                    flash("Scegli un fornitore o inserisci un nuovo nome.", "warning")
                    return redirect(url_for("new_payable"))
                s = Client(
                    user_id=current_user.id, is_supplier=True, name=sup_name,
                    vat_number=request.form.get("new_supplier_vat", ""),
                )
                db.session.add(s); db.session.flush()
                supplier_id = s.id
            try:
                amount = float(request.form.get("amount", "0").replace(",", "."))
            except ValueError:
                flash("Importo non valido.", "danger")
                return redirect(url_for("new_payable"))
            inv = Invoice(
                user_id=current_user.id,
                client_id=int(supplier_id),
                is_passive=True,
                document_type="TD01",
                number=request.form.get("number", "").strip() or f"PASS-{int(datetime.utcnow().timestamp())}",
                amount=amount,
                issue_date=datetime.strptime(request.form.get("issue_date") or date.today().isoformat(), "%Y-%m-%d").date(),
                due_date=datetime.strptime(request.form.get("due_date") or (date.today() + timedelta(days=30)).isoformat(), "%Y-%m-%d").date(),
                notes=request.form.get("notes", ""),
            )
            inv.update_status()
            db.session.add(inv); db.session.commit()
            flash(f"Fattura passiva n. {inv.number} registrata.", "success")
            return redirect(url_for("payable_detail", iid=inv.id))
        # GET: form
        suppliers_list = my_suppliers().order_by(Client.name).all()
        return render_template("new_payable.html", suppliers=suppliers_list,
                               today_iso=date.today().isoformat(),
                               due_iso=(date.today() + timedelta(days=30)).isoformat())

    @app.route("/payables/<int:iid>")
    @login_required
    def payable_detail(iid):
        inv = Invoice.query.filter_by(id=iid, user_id=current_user.id, is_passive=True).first_or_404()
        return render_template("payable_detail.html", invoice=inv,
                               today_iso=date.today().isoformat())

    @app.route("/payables/<int:iid>/mark-paid", methods=["POST"])
    @login_required
    def payable_mark_paid(iid):
        inv = Invoice.query.filter_by(id=iid, user_id=current_user.id, is_passive=True).first_or_404()
        try:
            pd = request.form.get("payment_date") or date.today().isoformat()
            inv.payment_date = datetime.strptime(pd, "%Y-%m-%d").date()
        except Exception:
            inv.payment_date = date.today()
        inv.payment_method = request.form.get("payment_method", "")[:40]
        inv.payment_ref    = request.form.get("payment_ref", "")[:200]
        inv.status = "paid"
        db.session.commit()
        flash(f"✅ Pagamento registrato per la fattura {inv.number}.", "success")
        return redirect(url_for("payable_detail", iid=iid))

    @app.route("/payables/<int:iid>/delete", methods=["POST"])
    @login_required
    def payable_delete(iid):
        inv = Invoice.query.filter_by(id=iid, user_id=current_user.id, is_passive=True).first_or_404()
        db.session.delete(inv); db.session.commit()
        flash("Fattura passiva eliminata.", "info")
        return redirect(url_for("payables"))

    # ═══════════════════════════════════════════════════════════════════════════
    # FATTURE
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/invoices")
    @login_required
    def invoices():
        status = request.args.get("status", "all")
        q      = request.args.get("q", "")
        query  = my_invoices().join(Client, Invoice.client_id == Client.id)
        if status != "all":
            query = query.filter(Invoice.status == status)
        if q:
            query = query.filter(Client.name.ilike(f"%{q}%"))
        return render_template("invoices.html", invoices=query.order_by(Invoice.due_date).all(),
                               status=status, q=q)

    @app.route("/invoices/new", methods=["GET", "POST"])
    @login_required
    def new_invoice():
        clients_list = my_clients().order_by(Client.name).all()
        # Fatture stornabili (TD01/TD05 attive del current_user) per dropdown NC
        invoiceable = (my_invoices()
                       .filter(db.or_(Invoice.document_type == "TD01",
                                      Invoice.document_type == "TD05",
                                      Invoice.document_type.is_(None)))
                       .filter(Invoice.status != "compensated")
                       .order_by(Invoice.due_date.desc()).all())
        if request.method == "POST":
            client_id = int(request.form["client_id"])
            if not my_clients().filter_by(id=client_id).first():
                flash("Cliente non valido.", "danger")
                return redirect(url_for("new_invoice"))

            doc_type = request.form.get("document_type", "TD01")
            amount = float(request.form["amount"].replace(",", "."))
            # NC con importo positivo → forza negativo (è un nostro debito verso cliente)
            if doc_type == "TD04" and amount > 0:
                amount = -amount

            # Risolvi link a fattura per le NC
            linked_id = None
            linked_inv = None
            if doc_type == "TD04":
                lid = request.form.get("linked_invoice_id", "").strip()
                if lid:
                    linked_inv = my_invoices().filter_by(id=int(lid)).first()
                    if linked_inv:
                        linked_id = linked_inv.id

            link = request.form.get("payment_link", "").strip() or config.PAYMENT_BASE_URL
            inv = Invoice(
                user_id=current_user.id,
                client_id=client_id,
                number=request.form["number"],
                amount=amount,
                due_date=datetime.strptime(request.form["due_date"],   "%Y-%m-%d").date(),
                issue_date=datetime.strptime(request.form["issue_date"], "%Y-%m-%d").date(),
                document_type=doc_type, linked_invoice_id=linked_id,
                payment_link=link if doc_type != "TD04" else "",
                notes=request.form.get("notes", ""),
            )
            inv.update_status()
            db.session.add(inv); db.session.flush()
            pdf_name = save_invoice_pdf(request.files.get("pdf_file"), inv.id)
            if pdf_name:
                inv.pdf_filename = pdf_name

            # Marca la fattura collegata come compensata
            if doc_type == "TD04" and linked_inv and linked_inv.status not in ("paid", "compensated"):
                linked_inv.status = "compensated"
                flash(f"Fattura {linked_inv.number} marcata come COMPENSATA dalla NC.", "info")

            db.session.commit()
            flash("Documento aggiunto.", "success")
            return redirect(url_for("invoice_detail", iid=inv.id))
        return render_template("new_invoice.html",
            clients=clients_list, today=date.today(), invoiceable=invoiceable)

    @app.route("/invoices/<int:iid>")
    @login_required
    def invoice_detail(iid):
        return render_template("invoice_detail.html", inv=get_my_invoice(iid))

    @app.route("/invoices/<int:iid>/edit", methods=["GET", "POST"])
    @login_required
    def edit_invoice(iid):
        inv = get_my_invoice(iid)
        clients_list = my_clients().order_by(Client.name).all()
        invoiceable = (my_invoices()
                       .filter(db.or_(Invoice.document_type == "TD01",
                                      Invoice.document_type == "TD05",
                                      Invoice.document_type.is_(None)))
                       .filter(Invoice.id != inv.id)
                       .order_by(Invoice.due_date.desc()).all())
        if request.method == "POST":
            inv.client_id=int(request.form["client_id"]); inv.number=request.form["number"]
            inv.amount=float(request.form["amount"].replace(",", "."))
            inv.due_date=datetime.strptime(request.form["due_date"],   "%Y-%m-%d").date()
            inv.issue_date=datetime.strptime(request.form["issue_date"], "%Y-%m-%d").date()
            inv.payment_link=request.form.get("payment_link",""); inv.notes=request.form.get("notes","")
            # Sostituisci il PDF se ne è stato caricato uno nuovo
            new_pdf = save_invoice_pdf(request.files.get("pdf_file"), inv.id)
            if new_pdf:
                if inv.pdf_filename and inv.pdf_filename != new_pdf:
                    delete_invoice_pdf(inv.pdf_filename)
                inv.pdf_filename = new_pdf
            elif request.form.get("remove_pdf") == "1":
                delete_invoice_pdf(inv.pdf_filename)
                inv.pdf_filename = ""
            inv.update_status(); db.session.commit()
            flash("Fattura aggiornata.", "success")
            return redirect(url_for("invoice_detail", iid=inv.id))
        return render_template("new_invoice.html",
            inv=inv, clients=clients_list, today=date.today(), invoiceable=invoiceable)

    @app.route("/invoices/<int:iid>/delete", methods=["POST"])
    @login_required
    def delete_invoice(iid):
        inv = get_my_invoice(iid)
        # Se è una NC che compensava una fattura → ripristina la fattura
        if inv.is_credit_note and inv.linked_invoice_id:
            linked = Invoice.query.get(inv.linked_invoice_id)
            if linked and linked.status == "compensated":
                linked.status = "pending"
                linked.update_status()  # ricalcola overdue se serve
                flash(f"Fattura {linked.number} ripristinata (non più compensata).", "info")
        delete_invoice_pdf(inv.pdf_filename)
        db.session.delete(inv); db.session.commit()
        flash("Documento eliminato.", "info")
        return redirect(url_for("invoices"))

    @app.route("/invoices/<int:iid>/pdf")
    @login_required
    def invoice_pdf(iid):
        inv = get_my_invoice(iid)
        if not inv.pdf_filename:
            abort(404)
        return send_from_directory(
            get_upload_folder(),
            inv.pdf_filename,
            as_attachment=False,
            download_name=f"{inv.number}.pdf"
        )

    # ── Segna pagata ──────────────────────────────────────────────────────────
    @app.route("/api/invoices/<int:iid>/mark-paid", methods=["POST"])
    @login_required
    def mark_paid(iid):
        inv = get_my_invoice(iid)
        inv.status = "paid"; inv.payment_date = date.today()
        db.session.commit()
        from credit_scoring import compute_score
        inv.client.credit_score = compute_score(inv.client)
        db.session.commit()
        flash(f"Fattura {inv.number} segnata come pagata.", "success")
        return redirect(request.referrer or url_for("invoices"))

    # ── Invia sollecito manuale ───────────────────────────────────────────────
    @app.route("/api/invoices/<int:iid>/send-reminder", methods=["POST"])
    @login_required
    def send_manual_reminder(iid):
        from email_service import send_reminder
        inv    = get_my_invoice(iid)
        r_type = request.form.get("reminder_type", "sollecito_1")
        ok     = send_reminder(inv, r_type)
        if ok:
            inv.reminder_count += 1; inv.last_reminder_date = datetime.utcnow()
            db.session.add(Reminder(invoice_id=inv.id, reminder_type=r_type,
                subject=f"Sollecito manuale – {inv.number}",
                recipient=inv.client.contact_email, success=True))
            db.session.commit()
            flash("Sollecito inviato con successo.", "success")
        else:
            flash("Errore nell'invio del sollecito. Controlla la configurazione SMTP.", "danger")
        return redirect(request.referrer or url_for("invoice_detail", iid=iid))

    # ── Stampa / PDF sollecito ────────────────────────────────────────────────
    @app.route("/invoices/<int:iid>/print-reminder")
    @login_required
    def print_reminder(iid):
        inv          = get_my_invoice(iid)
        reminder_type = request.args.get("type", "sollecito_1")
        # Nome azienda: prima cerca quello personale del current_user, poi quello globale
        company_name  = (UserSetting.get(current_user.id, "company_name")
                         or AppSettings.get("company_name", config.COMPANY_NAME))
        return render_template("print_reminder.html",
                               inv=inv, reminder_type=reminder_type,
                               company_name=company_name, today=date.today())

    # ── Import CSV / Excel ────────────────────────────────────────────────────
    @app.route("/invoices/import", methods=["GET", "POST"])
    @login_required
    def import_invoices():
        if request.method == "POST":
            files = request.files.getlist("file")
            files = [f for f in files if f and f.filename]
            if not files:
                flash("Seleziona almeno un file.", "warning")
                return redirect(request.url)

            from import_service import (
                process_import, process_pdf_import,
                process_xml_import, process_p7m_import, process_zip_import,
            )
            tot_ok = tot_skip = 0
            all_errs = []
            uid = current_user.id

            for f in files:
                fname = f.filename
                lower = fname.lower()
                ext   = fname.rsplit(".", 1)[-1].lower()
                data  = f.read()

                if lower.endswith(".xml.p7m") or ext == "p7m":
                    n_ok, n_skip, errs = process_p7m_import(data, fname, db, get_upload_folder(), user_id=uid)
                elif ext == "xml":
                    n_ok, n_skip, errs = process_xml_import(data, fname, db, get_upload_folder(), user_id=uid)
                elif ext == "zip":
                    n_ok, n_skip, errs = process_zip_import(data, fname, db, get_upload_folder(), user_id=uid)
                elif ext == "pdf":
                    n_ok, n_skip, errs = process_pdf_import(data, fname, db, get_upload_folder(), user_id=uid)
                else:
                    n_ok, n_skip, errs = process_import(data, fname, db, user_id=uid)

                tot_ok += n_ok; tot_skip += n_skip; all_errs.extend(errs)

            for e in all_errs[:15]:
                flash(e, "warning")
            flash(f"Import completato: {tot_ok} fatture importate, {tot_skip} saltate.", "success")
            return redirect(url_for("invoices"))
        return render_template("import_invoices.html")

    @app.route("/invoices/import/template")
    @login_required
    def import_template():
        from import_service import CSV_TEMPLATE
        return Response(CSV_TEMPLATE, mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=template_fatture.csv"})

    # ── Anteprima diagnostica estrazione PDF ─────────────────────────────────
    @app.route("/invoices/pdf-preview", methods=["GET", "POST"])
    @login_required
    def pdf_preview():
        result = None
        if request.method == "POST":
            f = request.files.get("file")
            if f and f.filename:
                from import_service import (
                    _extract_pdf_text, _find_client_block, extract_invoice_data
                )
                data_bytes = f.read()

                # ── Tentativo via Claude API ─────────────────────────────────
                claude_data  = None
                claude_error = None
                api_key = AppSettings.get("anthropic_api_key", "")
                if api_key:
                    try:
                        from claude_service import extract_with_claude, DEFAULT_MODEL
                        model = AppSettings.get("anthropic_model", "") or DEFAULT_MODEL
                        claude_data = extract_with_claude(data_bytes, api_key, model=model)
                    except Exception as e:
                        claude_error = f"{type(e).__name__}: {e}"

                # ── Estrazione regex (sempre, per confronto) ────────────────
                text  = _extract_pdf_text(data_bytes)
                block, c_start, c_end, side = _find_client_block(text)
                regex_data = extract_invoice_data(text)
                all_pivas  = [
                    {"piva": m.group(1), "pos": m.start()}
                    for m in __import__("re").finditer(r"\b(\d{11})\b", text)
                ]

                result = {
                    "filename":   f.filename,
                    "text":       text,
                    "regex_data":  regex_data,
                    "claude_data": claude_data,
                    "claude_error": claude_error,
                    "claude_enabled": bool(api_key),
                    "client_block": block,
                    "client_block_start": c_start,
                    "client_side": side,
                    "all_pivas":  all_pivas,
                }
        return render_template("pdf_preview.html", result=result)

    # ── Job manuale ───────────────────────────────────────────────────────────
    @app.route("/api/run-job", methods=["POST"])
    @login_required
    def run_job_now():
        from scheduler_service import run_daily_job
        run_daily_job(app)
        flash("Job giornaliero eseguito manualmente.", "success")
        return redirect(url_for("dashboard"))

    # ═══════════════════════════════════════════════════════════════════════════
    # QUICK ACTIONS via token firmato (link da email / WhatsApp)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/quick/<token>", methods=["GET", "POST"])
    def quick_action(token):
        """
        Esegue un'azione rapida tramite link firmato.
        GET  → mostra pagina di conferma con riepilogo (anti link-preview).
        POST → esegue l'azione e mostra esito + suggerimento prossima azione.
        """
        from tokens import verify_token, ACTIONS, make_action_url
        payload = verify_token(token)
        if not payload:
            return render_template("quick_action.html",
                error="Link non valido o scaduto. Genera una nuova notifica."), 400

        u = User.query.get(payload["u"])
        inv = Invoice.query.filter_by(id=payload["i"], user_id=payload["u"]).first()
        if not u or not inv:
            return render_template("quick_action.html",
                error="Fattura o utente non trovato."), 404

        action_code = payload["a"]
        action_label, reminder_type = ACTIONS.get(action_code, ("?", None))

        # GET = conferma (no azione eseguita per evitare scan automatici e link-preview)
        if request.method == "GET":
            return render_template("quick_action.html",
                stage="confirm", token=token, user=u, inv=inv,
                action_code=action_code, action_label=action_label)

        # POST = esegui l'azione
        result = {"ok": False, "msg": ""}

        if action_code == "paid":
            inv.status = "paid"; inv.payment_date = date.today()
            db.session.commit()
            from credit_scoring import compute_score
            inv.client.credit_score = compute_score(inv.client)
            db.session.commit()
            result = {"ok": True, "msg": f"Fattura {inv.number} segnata come PAGATA."}

        elif action_code == "stop":
            from datetime import datetime as _dt
            inv.user_notified_at = _dt(9999, 12, 31)
            db.session.commit()
            result = {"ok": True, "msg": "Notifiche disabilitate per questa fattura."}

        elif reminder_type:
            from email_service import send_reminder
            ok = send_reminder(inv, reminder_type)
            if ok:
                inv.reminder_count   += 1
                inv.last_reminder_date = datetime.utcnow()
                db.session.add(Reminder(invoice_id=inv.id, reminder_type=reminder_type,
                    subject=f"{action_label} – {inv.number}",
                    recipient=inv.client.contact_email, success=True))
                db.session.commit()
                result = {"ok": True,
                    "msg": f"{action_label} inviato a {inv.client.contact_email}."}
            else:
                result = {"ok": False,
                    "msg": "Errore SMTP — controlla la configurazione email."}
        else:
            result = {"ok": False, "msg": "Azione sconosciuta."}

        # Calcola la prossima azione suggerita
        next_act = None
        if inv.status != "paid":
            n = inv.reminder_count or 0
            next_act = "s1" if n == 0 else ("s2" if n == 1 else
                                            ("s3" if n == 2 else "diffida"))

        return render_template("quick_action.html",
            stage="done", result=result, user=u, inv=inv,
            action_label=action_label,
            next_url=make_action_url(inv, next_act) if next_act else None,
            next_label={"s1":"1° Sollecito","s2":"2° Sollecito",
                        "s3":"3° Sollecito","diffida":"Diffida formale"
                       }.get(next_act, ""),
            paid_url=make_action_url(inv, "paid") if inv.status != "paid" else None,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # AI CHAT ASSISTENTE (widget fluttuante)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/api/chat", methods=["POST"])
    @login_required
    @limiter.limit("30 per minute")
    def chat_api():
        from claude_service import chat_response
        data = request.json or {}
        history = data.get("history", [])
        user_msg = (data.get("message") or "").strip()

        if not user_msg:
            return jsonify({"error": "Messaggio vuoto"}), 400

        api_key = AppSettings.get("anthropic_api_key", "")
        if not api_key:
            return jsonify({"reply":
                "L'AI Chat non è disponibile: l'amministratore non ha configurato la API key Claude. "
                "Puoi aprire un ticket di assistenza qui: /tickets/new"})

        # Limita storia a ultimi 10 scambi (20 messaggi)
        history = history[-20:]
        history.append({"role": "user", "content": user_msg})

        ctx = {
            "username":  current_user.username,
            "is_admin":  current_user.is_admin,
            "current_page": request.headers.get("Referer", "/"),
            "company":   UserSetting.get(current_user.id, "company_name") or "?",
        }
        try:
            reply = chat_response(history, ctx, api_key)
            return jsonify({"reply": reply})
        except Exception as e:
            logging.error("Chat AI error: %s", e)
            return jsonify({"error": str(e)}), 500

    # ═══════════════════════════════════════════════════════════════════════════
    # TICKET DI ASSISTENZA
    # ═══════════════════════════════════════════════════════════════════════════
    def _send_ticket_notification(ticket, recipient_email, kind):
        """Invia email per eventi ticket (open/reply). kind: 'opened'|'replied'."""
        if not recipient_email:
            return
        cfg = {
            "host":     AppSettings.get("smtp_host", ""),
            "port":     int(AppSettings.get("smtp_port", "587")),
            "user":     AppSettings.get("smtp_user", ""),
            "password": AppSettings.get("smtp_password", ""),
            "use_tls":  AppSettings.get("smtp_use_tls", "true") == "true",
        }
        if not cfg["host"] or not cfg["user"]:
            return

        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        base_url = AppSettings.get("app_external_url", "http://127.0.0.1:5000").rstrip("/")
        link = f"{base_url}/tickets/{ticket.id}"

        if kind == "opened":
            subj = f"[GestFatture] Nuovo ticket #{ticket.id}: {ticket.subject[:80]}"
            body_text = (f"Nuovo ticket aperto da {ticket.user.username}.\n\n"
                         f"Oggetto: {ticket.subject}\nCategoria: {ticket.category}\n"
                         f"Priorità: {ticket.priority}\n\nApri: {link}")
        else:
            subj = f"[GestFatture] Risposta ticket #{ticket.id}: {ticket.subject[:80]}"
            last = ticket.messages[-1] if ticket.messages else None
            body_text = (f"Nuovo messaggio nel ticket #{ticket.id}.\n\n"
                         f"Da: {last.author.username if last else '?'}\n\n"
                         f"{last.body if last else ''}\n\nApri: {link}")

        try:
            msg = MIMEMultipart()
            msg["Subject"] = subj
            msg["From"]    = cfg["user"]
            msg["To"]      = recipient_email
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
            if cfg["use_tls"]:
                s = smtplib.SMTP(cfg["host"], cfg["port"], timeout=10); s.starttls()
            else:
                s = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=10)
            if cfg["password"]:
                s.login(cfg["user"], cfg["password"])
            s.sendmail(cfg["user"], recipient_email, msg.as_string())
            s.quit()
        except Exception as e:
            logging.warning("Notifica ticket fallita: %s", e)

    @app.route("/tickets")
    @login_required
    def tickets():
        # Admin con flag ?all=1 vede tutti, altrimenti solo i suoi
        if current_user.is_admin and request.args.get("all") == "1":
            ts = SupportTicket.query.order_by(SupportTicket.updated_at.desc()).all()
            scope = "all"
        else:
            ts = SupportTicket.query.filter_by(user_id=current_user.id) \
                .order_by(SupportTicket.updated_at.desc()).all()
            scope = "mine"
        # Conteggi per badge
        admin_open_count = 0
        if current_user.is_admin:
            admin_open_count = SupportTicket.query.filter(
                SupportTicket.status.in_(["open", "in_progress"])
            ).count()
        return render_template("tickets.html", tickets=ts, scope=scope,
                               admin_open_count=admin_open_count)

    @app.route("/tickets/export")
    @login_required
    def tickets_export():
        """Export ticket dell'utente (o tutti se admin con ?all=1) in CSV/PDF."""
        fmt = (request.args.get("format", "csv") or "csv").lower()
        if current_user.is_admin and request.args.get("all") == "1":
            ts = SupportTicket.query.order_by(SupportTicket.created_at.desc()).all()
            scope_label = "all"
        else:
            ts = (SupportTicket.query.filter_by(user_id=current_user.id)
                  .order_by(SupportTicket.created_at.desc()).all())
            scope_label = current_user.username
        from ticket_export import tickets_to_csv, tickets_to_pdf
        ts_str = date.today().isoformat()
        if fmt == "pdf":
            data = tickets_to_pdf(ts, title=f"Ticket assistenza — {scope_label}")
            return Response(
                data, mimetype="application/pdf",
                headers={"Content-Disposition":
                         f'attachment; filename="tickets_{scope_label}_{ts_str}.pdf"'},
            )
        # default CSV
        body = tickets_to_csv(ts)
        return Response(
            body, mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="tickets_{scope_label}_{ts_str}.csv"'},
        )

    @app.route("/tickets/new", methods=["GET", "POST"])
    @login_required
    def new_ticket():
        if request.method == "POST":
            subject = request.form.get("subject", "").strip()
            body    = request.form.get("body", "").strip()
            if not subject or not body:
                flash("Oggetto e descrizione sono obbligatori.", "danger")
                return redirect(url_for("new_ticket"))

            t = SupportTicket(
                user_id=current_user.id, subject=subject,
                category=request.form.get("category", "question"),
                priority=request.form.get("priority", "normal"),
            )
            db.session.add(t); db.session.flush()
            db.session.add(TicketMessage(
                ticket_id=t.id, author_id=current_user.id, body=body,
            ))
            db.session.commit()

            # Notifica all'admin
            admin = User.query.filter_by(is_admin=True).first()
            if admin and admin.email:
                _send_ticket_notification(t, admin.email, "opened")

            flash(f"Ticket #{t.id} aperto. Riceverai una notifica via email quando risponderemo.", "success")
            return redirect(url_for("ticket_detail", tid=t.id))
        return render_template("new_ticket.html",
                               prefill_subject=request.args.get("subject", ""),
                               prefill_body=request.args.get("body", ""))

    @app.route("/tickets/<int:tid>")
    @login_required
    def ticket_detail(tid):
        t = SupportTicket.query.get_or_404(tid)
        if t.user_id != current_user.id and not current_user.is_admin:
            abort(403)
        return render_template("ticket_detail.html", ticket=t)

    @app.route("/tickets/<int:tid>/reply", methods=["POST"])
    @login_required
    def reply_ticket(tid):
        t = SupportTicket.query.get_or_404(tid)
        if t.user_id != current_user.id and not current_user.is_admin:
            abort(403)
        body = request.form.get("body", "").strip()
        if not body:
            flash("Messaggio vuoto.", "warning")
            return redirect(url_for("ticket_detail", tid=tid))

        is_internal = current_user.is_admin and request.form.get("internal") == "on"
        db.session.add(TicketMessage(
            ticket_id=t.id, author_id=current_user.id, body=body, is_internal=is_internal
        ))
        t.updated_at = datetime.utcnow()
        # Aggiorna stato in base a chi risponde
        if not is_internal:
            if current_user.is_admin:
                t.status = "waiting_user"
            else:
                if t.status in ("waiting_user", "resolved"):
                    t.status = "open"
        db.session.commit()

        # Notifica controparte
        if not is_internal:
            if current_user.is_admin:
                # Admin ha risposto → notifica utente
                if t.user.email:
                    _send_ticket_notification(t, t.user.email, "replied")
            else:
                # Utente ha risposto → notifica admin
                admin = User.query.filter_by(is_admin=True).first()
                if admin and admin.email:
                    _send_ticket_notification(t, admin.email, "replied")

        flash("Risposta inviata.", "success")
        return redirect(url_for("ticket_detail", tid=tid))

    @app.route("/tickets/<int:tid>/status", methods=["POST"])
    @admin_required
    def change_ticket_status(tid):
        from models import TicketSurvey
        t = SupportTicket.query.get_or_404(tid)
        new_status = request.form.get("status")
        if new_status in ("open", "in_progress", "waiting_user", "resolved", "closed"):
            old_status = t.status
            t.status = new_status
            db.session.commit()
            flash(f"Stato aggiornato a '{t.status_label[0]}'.", "info")
            # Genera survey + email quando passa a resolved (prima volta)
            if (new_status == "resolved" and old_status != "resolved"
                and not TicketSurvey.query.filter_by(ticket_id=t.id).first()):
                try:
                    survey = TicketSurvey(ticket_id=t.id)
                    db.session.add(survey)
                    db.session.commit()
                    if t.user and t.user.email:
                        from notification_service import send_ticket_survey_email
                        ok, msg = send_ticket_survey_email(t.user, t, survey)
                        audit("survey_sent", target=f"ticket:{t.id}",
                              details=f"{'OK' if ok else 'FAIL'}: {msg[:120]}")
                except Exception as e:
                    logging.exception("Errore invio survey ticket #%d", t.id)
        return redirect(url_for("ticket_detail", tid=tid))

    # ── Survey post-risoluzione ticket (pubblica via token) ──────────────────
    @app.route("/survey/<token>", methods=["GET", "POST"])
    @limiter.limit("30 per hour")
    def ticket_survey(token):
        from models import TicketSurvey
        from tokens import verify_survey_token
        payload = verify_survey_token(token)
        if not payload:
            return render_template("portal_error.html",
                                   reason="Link survey non valido o scaduto."), 404
        survey = TicketSurvey.query.get(payload["s"])
        if not survey or survey.ticket_id != payload["t"]:
            return render_template("portal_error.html",
                                   reason="Survey non trovato."), 404
        if request.method == "POST":
            if survey.submitted_at:
                flash("Hai già risposto a questo survey, grazie!", "info")
                return redirect(url_for("ticket_survey", token=token))
            try:
                rating = int(request.form.get("rating", 0))
            except ValueError:
                rating = 0
            if rating < 1 or rating > 5:
                flash("Scegli una valutazione da 1 a 5 stelle.", "danger")
                return render_template("survey.html", survey=survey, ticket=survey.ticket)
            survey.rating = rating
            survey.comment = (request.form.get("comment", "") or "")[:2000].strip()
            survey.submitted_at = datetime.utcnow()
            db.session.commit()
            return render_template("survey.html", survey=survey, ticket=survey.ticket,
                                   thank_you=True)
        return render_template("survey.html", survey=survey, ticket=survey.ticket)

    @app.route("/admin/surveys")
    @admin_required
    def admin_surveys():
        from models import TicketSurvey
        from sqlalchemy import func as _f
        surveys = (TicketSurvey.query
                   .order_by(TicketSurvey.submitted_at.desc().nullslast(),
                             TicketSurvey.sent_at.desc())
                   .limit(200).all())
        # Statistiche
        completed = [s for s in surveys if s.rating]
        avg = (sum(s.rating for s in completed) / len(completed)) if completed else 0
        n_pending = sum(1 for s in surveys if not s.submitted_at)
        return render_template("admin_surveys.html",
                               surveys=surveys, avg=avg,
                               n_completed=len(completed), n_pending=n_pending)

    # ── Test Claude API key (verifica che funzioni con 1 chiamata) ───────────
    @app.route("/settings/test-claude", methods=["POST"])
    @admin_required
    def test_claude_api():
        api_key = AppSettings.get("anthropic_api_key", "")
        if not api_key:
            flash("⚠️ API key non configurata.", "warning")
            return redirect(url_for("settings"))
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=20,
                messages=[{"role": "user", "content": "Rispondi solo: OK"}],
            )
            answer = resp.content[0].text[:80]
            audit("test_claude", details="ok")
            flash(f"✅ Claude API funziona. Risposta: «{answer}»", "success")
        except Exception as e:
            audit("test_claude", details=f"failed: {type(e).__name__}")
            flash(f"❌ Claude API errore: {type(e).__name__}: {e}", "danger")
        return redirect(url_for("settings"))

    # ═══════════════════════════════════════════════════════════════════════════
    # WEBHOOK STRIPE (esentato da CSRF — Stripe non manda token CSRF)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/webhook/stripe", methods=["POST"])
    @csrf.exempt
    @limiter.limit("60 per minute")
    def webhook_stripe():
        import stripe
        payload    = request.get_data()
        sig_header = request.headers.get("Stripe-Signature", "")
        secret     = AppSettings.get("stripe_webhook_secret", "")

        if secret:
            try:
                event = stripe.Webhook.construct_event(payload, sig_header, secret)
            except Exception:
                abort(400)
        else:
            import json
            event = json.loads(payload)

        et = event.get("type", "")
        obj = event.get("data", {}).get("object", {})

        invoice_id = None
        if et in ("checkout.session.completed", "payment_intent.succeeded"):
            meta = obj.get("metadata", {})
            invoice_id = meta.get("invoice_id") or obj.get("client_reference_id")

        if invoice_id:
            inv = Invoice.query.filter(
                (Invoice.id == invoice_id) | (Invoice.number == str(invoice_id))
            ).first()
            if inv and inv.status != "paid":
                inv.status       = "paid"
                inv.payment_date = date.today()
                inv.payment_ref  = obj.get("id", "")
                db.session.commit()
                from credit_scoring import compute_score
                inv.client.credit_score = compute_score(inv.client)
                db.session.commit()
                logging.info("Stripe: fattura %s segnata pagata.", inv.number)

        return jsonify({"status": "ok"})

    # ═══════════════════════════════════════════════════════════════════════════
    # WEBHOOK PAYPAL (esentato da CSRF)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/webhook/paypal", methods=["POST"])
    @csrf.exempt
    @limiter.limit("60 per minute")
    def webhook_paypal():
        data       = request.json or {}
        event_type = data.get("event_type", "")

        if event_type == "PAYMENT.CAPTURE.COMPLETED":
            resource   = data.get("resource", {})
            custom_id  = resource.get("custom_id") or resource.get("invoice_id", "")
            if custom_id:
                inv = Invoice.query.filter(
                    (Invoice.id == custom_id) | (Invoice.number == str(custom_id))
                ).first()
                if inv and inv.status != "paid":
                    inv.status       = "paid"
                    inv.payment_date = date.today()
                    inv.payment_ref  = resource.get("id", "")
                    db.session.commit()
                    from credit_scoring import compute_score
                    inv.client.credit_score = compute_score(inv.client)
                    db.session.commit()
                    logging.info("PayPal: fattura %s segnata pagata.", inv.number)

        return jsonify({"status": "ok"})

    # ═══════════════════════════════════════════════════════════════════════════
    # IMPOSTAZIONI
    # ═══════════════════════════════════════════════════════════════════════════
    # ── Aggiorna profilo (email, phone) — campi del modello User ─────────────
    @app.route("/settings/profile", methods=["POST"])
    @login_required
    def update_profile():
        current_user.email = request.form.get("email", "").strip()
        current_user.phone = request.form.get("phone", "").strip()
        db.session.commit()
        my_vat = "".join(c for c in request.form.get("my_vat_number", "") if c.isdigit())
        UserSetting.set(current_user.id, "my_vat_number", my_vat)
        # Profilo per matching bandi
        UserSetting.set(current_user.id, "user_ateco_code",
                        request.form.get("user_ateco_code", "").strip()[:20])
        UserSetting.set(current_user.id, "user_region",
                        request.form.get("user_region", "").strip()[:80])
        UserSetting.set(current_user.id, "user_company_size",
                        request.form.get("user_company_size", "").strip()[:40])
        UserSetting.set(current_user.id, "user_business_description",
                        request.form.get("user_business_description", "").strip()[:1000])
        audit("profile_update", target=f"user:{current_user.username}")
        flash("Profilo aggiornato.", "success")
        return redirect(url_for("settings"))

    # ── Salva preferenze notifiche ───────────────────────────────────────────
    @app.route("/settings/notifications", methods=["POST"])
    @login_required
    def save_notifications():
        uid = current_user.id
        UserSetting.set(uid, "notify_email_enabled",
                        "true" if request.form.get("notify_email") == "on" else "false")
        UserSetting.set(uid, "notify_whatsapp_enabled",
                        "true" if request.form.get("notify_whatsapp") == "on" else "false")
        new_key = request.form.get("whatsapp_apikey", "")
        if new_key:
            UserSetting.set(uid, "whatsapp_apikey", new_key)
        flash("Preferenze notifiche salvate.", "success")
        return redirect(url_for("settings"))

    # ── Test notifica al titolare ────────────────────────────────────────────
    @app.route("/settings/notifications/test", methods=["POST"])
    @login_required
    def test_notification():
        from notification_service import send_email_to_owner, send_whatsapp_to_owner
        # Trova una fattura del current_user (qualsiasi) per fare il test
        inv = my_invoices().first()
        if not inv:
            flash("Crea almeno una fattura per testare le notifiche.", "warning")
            return redirect(url_for("settings"))

        channel = request.form.get("channel", "email")
        if channel == "email":
            ok, msg = send_email_to_owner(current_user, inv)
        else:
            ok, msg = send_whatsapp_to_owner(current_user, inv)
        flash(("✅ " if ok else "❌ ") + msg, "success" if ok else "danger")
        return redirect(url_for("settings"))

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        # Chiavi PERSONALI dell'utente (UserSetting)
        user_keys = ["company_name", "payment_base_url"]
        # Chiavi GLOBALI riservate all'amministratore (AppSettings)
        admin_keys = ["smtp_host", "smtp_port", "smtp_user", "smtp_password",
                      "smtp_use_tls", "stripe_webhook_secret", "paypal_webhook_id",
                      "anthropic_api_key", "anthropic_model", "app_external_url",
                      "legal_company", "legal_vat", "legal_address", "legal_contact_email",
                      "email_provider", "resend_api_key", "resend_from_email",
                      "backup_s3_enabled", "backup_s3_endpoint_url", "backup_s3_bucket",
                      "backup_s3_access_key_id", "backup_s3_secret_access_key",
                      "backup_s3_region", "backup_s3_retention_days", "backup_s3_prefix",
                      "gocardless_secret_id", "gocardless_secret_key",
                      "tink_client_id", "tink_client_secret"]

        if request.method == "POST":
            # Salva sempre le chiavi personali del current_user
            for k in user_keys:
                UserSetting.set(current_user.id, k, request.form.get(k, ""))
            # Chiavi admin solo se admin
            if current_user.is_admin:
                changed_admin_keys = []
                for k in admin_keys:
                    val = request.form.get(k, "")
                    val = val.strip().strip("\r\n\t ")
                    if k in ("smtp_password", "resend_api_key",
                             "backup_s3_secret_access_key",
                             "gocardless_secret_key",
                             "tink_client_secret") and not val:
                        continue
                    old_val = AppSettings.get(k, "")
                    if old_val != val:
                        changed_admin_keys.append(k)
                    AppSettings.set(k, val)
                if changed_admin_keys:
                    audit("settings_change", details=f"keys: {', '.join(changed_admin_keys)}")
            flash("Impostazioni salvate.", "success")
            return redirect(url_for("settings"))

        # Carica valori: personali da UserSetting, admin da AppSettings (solo se admin)
        current = {k: UserSetting.get(current_user.id, k) for k in user_keys}
        # Default fallback al valore globale se l'utente non ha personalizzato
        if not current["company_name"]:
            current["company_name"] = AppSettings.get("company_name", config.COMPANY_NAME)
        if not current["payment_base_url"]:
            current["payment_base_url"] = AppSettings.get("payment_base_url", "")
        if current_user.is_admin:
            for k in admin_keys:
                current[k] = AppSettings.get(k)

        # Preferenze notifiche + P.IVA personale + profilo bandi
        notify_email    = UserSetting.get(current_user.id, "notify_email_enabled")
        notify_whatsapp = UserSetting.get(current_user.id, "notify_whatsapp_enabled")
        whatsapp_apikey = UserSetting.get(current_user.id, "whatsapp_apikey")
        my_vat_number   = UserSetting.get(current_user.id, "my_vat_number")
        ateco_code      = UserSetting.get(current_user.id, "user_ateco_code")
        user_region     = UserSetting.get(current_user.id, "user_region")
        company_size    = UserSetting.get(current_user.id, "user_company_size")
        business_desc   = UserSetting.get(current_user.id, "user_business_description")

        return render_template("settings.html",
            settings=current,
            user_notify_email=notify_email,
            user_notify_whatsapp=notify_whatsapp,
            user_whatsapp_apikey=whatsapp_apikey,
            user_my_vat_number=my_vat_number,
            user_ateco_code=ateco_code,
            user_region=user_region,
            user_company_size=company_size,
            user_business_description=business_desc,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # MIE INTEGRAZIONI (per ogni utente — dati separati)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/my-integrations")
    @login_required
    def my_integrations():
        uid = current_user.id
        keys = [
            "integration_folder_enabled", "integration_folder_path", "integration_folder_last_sync",
            "integration_pec_enabled", "integration_pec_host", "integration_pec_port",
            "integration_pec_user", "integration_pec_password",
            "integration_pec_use_ssl", "integration_pec_folder",
            "integration_pec_last_sync", "integration_pec_last_error", "integration_pec_last_count",
            "pec_analysis_enabled",
            "integration_fic_enabled", "integration_fic_company_id", "integration_fic_access_token",
            "integration_fic_last_sync", "integration_fic_last_error", "integration_fic_last_count",
        ]
        s = {k: UserSetting.get(uid, k) for k in keys}
        s["_pec_pwd_set"]   = bool(s["integration_pec_password"])
        s["_fic_connected"] = bool(s["integration_fic_access_token"])
        # Client ID/Secret restano globali (admin), per non costringere ogni utente a creare
        # un'app OAuth. Mostro solo se è configurato dall'admin.
        s["_fic_client_id_admin"] = AppSettings.get("integration_fic_client_id", "")
        return render_template("my_integrations.html", s=s)

    # ── Folder ────────────────────────────────────────────────────────────────
    @app.route("/my-integrations/folder/save", methods=["POST"])
    @login_required
    def my_save_folder():
        uid = current_user.id
        UserSetting.set(uid, "integration_folder_enabled",
                        "true" if request.form.get("enabled") == "on" else "false")
        UserSetting.set(uid, "integration_folder_path", request.form.get("path", "").strip())
        flash("Impostazioni cartella salvate.", "success")
        return redirect(url_for("my_integrations"))

    @app.route("/my-integrations/folder/test", methods=["POST"])
    @login_required
    def my_test_folder():
        from integration_folder import test_folder
        ok, msg = test_folder(request.form.get("path", "").strip())
        flash(("✅ " if ok else "❌ ") + msg, "success" if ok else "danger")
        return redirect(url_for("my_integrations"))

    @app.route("/my-integrations/folder/sync-now", methods=["POST"])
    @login_required
    def my_sync_folder():
        from integration_folder import sync_for_user
        sync_for_user(app, current_user.id)
        flash("Scansione cartella eseguita.", "success")
        return redirect(url_for("my_integrations"))

    # ── PEC ───────────────────────────────────────────────────────────────────
    @app.route("/my-integrations/pec/save", methods=["POST"])
    @login_required
    def my_save_pec():
        uid = current_user.id
        UserSetting.set(uid, "integration_pec_enabled",
                        "true" if request.form.get("enabled") == "on" else "false")
        UserSetting.set(uid, "integration_pec_host",   request.form.get("host", "").strip())
        UserSetting.set(uid, "integration_pec_port",   request.form.get("port", "993").strip() or "993")
        UserSetting.set(uid, "integration_pec_user",   request.form.get("user", "").strip())
        new_pwd = request.form.get("password", "")
        if new_pwd:
            UserSetting.set(uid, "integration_pec_password", new_pwd)
        UserSetting.set(uid, "integration_pec_use_ssl",
                        "true" if request.form.get("use_ssl") == "on" else "false")
        UserSetting.set(uid, "integration_pec_folder", request.form.get("folder", "INBOX").strip() or "INBOX")
        # Analisi PEC istituzionali (AdE/INPS/INAIL) — opzionale
        UserSetting.set(uid, "pec_analysis_enabled",
                        "true" if request.form.get("pec_analysis") == "on" else "false")
        flash("Impostazioni PEC salvate.", "success")
        return redirect(url_for("my_integrations"))

    # ── PEC INBOX (messaggi istituzionali analizzati) ────────────────────────
    @app.route("/pec-inbox")
    @login_required
    def pec_inbox():
        sender_filter = request.args.get("sender", "")
        urgency_filter = request.args.get("urgency", "")
        show_archived = request.args.get("archived") == "1"

        q = PecMessage.query.filter_by(user_id=current_user.id)
        if not show_archived:
            q = q.filter_by(is_archived=False)
        if sender_filter:
            q = q.filter_by(sender_label=sender_filter)
        if urgency_filter:
            q = q.filter_by(urgency=urgency_filter)
        msgs = q.order_by(PecMessage.received_at.desc()).limit(200).all()

        # Conteggi per filtri
        unread_count = PecMessage.query.filter_by(
            user_id=current_user.id, is_read=False, is_archived=False
        ).count()
        return render_template("pec_inbox.html",
            messages=msgs, unread_count=unread_count,
            sender_filter=sender_filter, urgency_filter=urgency_filter,
            show_archived=show_archived,
        )

    @app.route("/pec/<int:pid>")
    @login_required
    def pec_detail(pid):
        m = PecMessage.query.filter_by(id=pid, user_id=current_user.id).first_or_404()
        if not m.is_read:
            m.is_read = True
            db.session.commit()
        return render_template("pec_detail.html", m=m)

    @app.route("/pec/<int:pid>/archive", methods=["POST"])
    @login_required
    def pec_archive(pid):
        m = PecMessage.query.filter_by(id=pid, user_id=current_user.id).first_or_404()
        m.is_archived = not m.is_archived
        db.session.commit()
        flash(("PEC archiviata." if m.is_archived else "PEC ripristinata."), "info")
        return redirect(url_for("pec_inbox"))

    @app.route("/pec/<int:pid>/delete", methods=["POST"])
    @login_required
    def pec_delete(pid):
        m = PecMessage.query.filter_by(id=pid, user_id=current_user.id).first_or_404()
        db.session.delete(m); db.session.commit()
        flash("PEC eliminata.", "info")
        return redirect(url_for("pec_inbox"))

    @app.route("/pec/<int:pid>/test-whatsapp", methods=["POST"])
    @login_required
    @limiter.limit("10 per minute")
    def pec_test_whatsapp(pid):
        """Manda un WhatsApp di test usando questa PEC, per diagnosticare le notifiche PEC."""
        m = PecMessage.query.filter_by(id=pid, user_id=current_user.id).first_or_404()
        from notification_service import _send_pec_whatsapp_to_owner
        ok, msg = _send_pec_whatsapp_to_owner(current_user, m)
        audit("pec_notify_test", target=f"pec:{m.id}",
              details=f"{'OK' if ok else 'FAIL'}: {msg[:120]}")
        flash(("✅ WhatsApp inviato: " if ok else "❌ Errore: ") + msg,
              "success" if ok else "danger")
        return redirect(url_for("pec_detail", pid=pid))

    @app.route("/pec/<int:pid>/reanalyze", methods=["POST"])
    @login_required
    @limiter.limit("5 per minute")
    def pec_reanalyze(pid):
        """Rilancia l'analisi AI su una PEC già salvata (usa i dati già in DB)."""
        m = PecMessage.query.filter_by(id=pid, user_id=current_user.id).first_or_404()
        api_key = AppSettings.get("anthropic_api_key", "")
        if not api_key:
            flash("API key Anthropic non configurata.", "danger")
            return redirect(url_for("pec_detail", pid=pid))
        try:
            import json as _json
            from datetime import datetime as _dt
            from claude_service import analyze_pec_email, DEFAULT_MODEL
            model = AppSettings.get("anthropic_model", "") or DEFAULT_MODEL
            analysis = analyze_pec_email(
                m.subject or "", m.body_excerpt or "", m.sender or "",
                m.attachments_list, api_key, model,
            )
            m.category = (analysis.get("category", "altro") or "altro")[:50]
            m.urgency  = (analysis.get("urgency", "media") or "media")[:20]
            m.summary  = analysis.get("summary", "") or ""
            m.suggested_action = analysis.get("suggested_action", "") or ""
            m.key_facts = _json.dumps(analysis.get("key_facts", []))
            if analysis.get("deadline"):
                try:
                    m.deadline = _dt.strptime(analysis["deadline"], "%Y-%m-%d").date()
                except Exception:
                    pass
            db.session.commit()
            audit("pec_reanalyzed", target=f"pec:{m.id}", details="OK")
            flash("✅ Analisi AI rigenerata.", "success")
        except Exception as e:
            err_type = type(e).__name__
            err_str  = str(e)[:300]
            m.summary = f"[Analisi AI fallita: {err_type}: {err_str}] {m.subject or ''}"
            db.session.commit()
            audit("pec_reanalyzed", target=f"pec:{m.id}",
                  details=f"FAIL {err_type}: {err_str[:120]}")
            flash(f"❌ Analisi fallita: {err_type}: {err_str}", "danger")
        return redirect(url_for("pec_detail", pid=pid))

    @app.route("/my-integrations/pec/test", methods=["POST"])
    @login_required
    def my_test_pec():
        from integration_pec import test_connection
        uid = current_user.id
        host = UserSetting.get(uid, "integration_pec_host", "")
        port = int(UserSetting.get(uid, "integration_pec_port", "993") or "993")
        user = UserSetting.get(uid, "integration_pec_user", "")
        pwd  = UserSetting.get(uid, "integration_pec_password", "")
        ssl  = UserSetting.get(uid, "integration_pec_use_ssl", "true") == "true"
        folder = UserSetting.get(uid, "integration_pec_folder", "INBOX") or "INBOX"
        ok, msg = test_connection(host, port, user, pwd, ssl, folder)
        flash(("✅ " if ok else "❌ ") + msg, "success" if ok else "danger")
        return redirect(url_for("my_integrations"))

    @app.route("/my-integrations/pec/sync-now", methods=["POST"])
    @login_required
    def my_sync_pec():
        from integration_pec import sync_for_user
        sync_for_user(app, current_user.id)
        flash("Sincronizzazione PEC eseguita.", "success")
        return redirect(url_for("my_integrations"))

    # ── Fatture in Cloud ──────────────────────────────────────────────────────
    @app.route("/my-integrations/fic/save", methods=["POST"])
    @login_required
    def my_save_fic():
        UserSetting.set(current_user.id, "integration_fic_enabled",
                        "true" if request.form.get("enabled") == "on" else "false")
        flash("Impostazioni Fatture in Cloud salvate.", "success")
        return redirect(url_for("my_integrations"))

    @app.route("/my-integrations/fic/connect")
    @login_required
    def my_fic_connect():
        import secrets as _secrets
        from integration_fic import get_authorize_url
        cid = AppSettings.get("integration_fic_client_id", "")
        if not cid:
            flash("L'amministratore non ha ancora configurato l'app Fatture in Cloud.", "warning")
            return redirect(url_for("my_integrations"))
        # Genera state casuale + salva in sessione (anti-CSRF su OAuth)
        state = _secrets.token_urlsafe(32)
        session["fic_oauth_state"] = state
        session["fic_oauth_user_id"] = current_user.id
        redirect_uri = url_for("my_fic_callback", _external=True)
        return redirect(get_authorize_url(cid, redirect_uri, state=state))

    @app.route("/my-integrations/fic/callback")
    @login_required
    def my_fic_callback():
        from integration_fic import exchange_code, get_companies
        # Verifica state OAuth (anti-CSRF)
        received_state = request.args.get("state", "")
        expected_state = session.pop("fic_oauth_state", None)
        expected_uid   = session.pop("fic_oauth_user_id", None)
        if not expected_state or received_state != expected_state:
            flash("⚠️ Errore di sicurezza: state OAuth non valido. Riprova.", "danger")
            return redirect(url_for("my_integrations"))
        if expected_uid != current_user.id:
            flash("⚠️ Errore di sicurezza: utente diverso da quello che ha avviato l'autorizzazione.", "danger")
            return redirect(url_for("my_integrations"))

        code = request.args.get("code")
        if not code:
            flash("Autorizzazione annullata o non valida.", "danger")
            return redirect(url_for("my_integrations"))
        cid  = AppSettings.get("integration_fic_client_id", "")
        csec = AppSettings.get("integration_fic_client_secret", "")
        redirect_uri = url_for("my_fic_callback", _external=True)
        try:
            tokens = exchange_code(code, redirect_uri, cid, csec)
        except Exception as e:
            flash(f"Errore scambio token: {e}", "danger")
            return redirect(url_for("my_integrations"))

        uid = current_user.id
        UserSetting.set(uid, "integration_fic_access_token",  tokens.get("access_token", ""))
        UserSetting.set(uid, "integration_fic_refresh_token", tokens.get("refresh_token", ""))
        if tokens.get("expires_in"):
            from datetime import timedelta
            exp = datetime.utcnow() + timedelta(seconds=int(tokens["expires_in"]))
            UserSetting.set(uid, "integration_fic_token_expires_at", exp.isoformat())
        try:
            companies = get_companies(tokens["access_token"])
            if companies:
                UserSetting.set(uid, "integration_fic_company_id", str(companies[0].get("id")))
                UserSetting.set(uid, "integration_fic_company_name", companies[0].get("name", ""))
        except Exception as e:
            logging.warning("FiC u=%d: get_companies fallito: %s", uid, e)
        audit("fic_connect", target=f"user:{current_user.username}")
        flash("✅ Account Fatture in Cloud collegato.", "success")
        return redirect(url_for("my_integrations"))

    @app.route("/my-integrations/fic/disconnect", methods=["POST"])
    @login_required
    def my_fic_disconnect():
        uid = current_user.id
        for k in ("access_token", "refresh_token", "token_expires_at",
                  "company_id", "company_name", "last_sync"):
            UserSetting.set(uid, f"integration_fic_{k}", "")
        audit("fic_disconnect", target=f"user:{current_user.username}")
        flash("Account Fatture in Cloud disconnesso.", "info")
        return redirect(url_for("my_integrations"))

    @app.route("/my-integrations/fic/sync-now", methods=["POST"])
    @login_required
    def my_sync_fic():
        from integration_fic import sync_for_user
        sync_for_user(app, current_user.id)
        flash("Sincronizzazione Fatture in Cloud eseguita.", "success")
        return redirect(url_for("my_integrations"))

    # ═══════════════════════════════════════════════════════════════════════════
    # CONFIGURAZIONE FiC GLOBALE (solo admin: Client ID/Secret per OAuth app)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/integrations")
    @admin_required
    def integrations():
        s = {
            "integration_fic_client_id":     AppSettings.get("integration_fic_client_id"),
            "integration_fic_client_secret": AppSettings.get("integration_fic_client_secret"),
        }
        return render_template("integrations.html", s=s)

    @app.route("/integrations/fic/save-app", methods=["POST"])
    @admin_required
    def save_fic_app():
        AppSettings.set("integration_fic_client_id", request.form.get("client_id", "").strip())
        new_secret = request.form.get("client_secret", "")
        secret_changed = bool(new_secret)
        if new_secret:
            AppSettings.set("integration_fic_client_secret", new_secret)
        audit("fic_app_credentials",
              details=f"client_id_changed=true, secret_changed={secret_changed}")
        flash("Configurazione app FiC salvata.", "success")
        return redirect(url_for("integrations"))

    # ═══════════════════════════════════════════════════════════════════════════
    # GESTIONE UTENTI (solo admin)
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/users")
    @admin_required
    def users():
        users_list = User.query.order_by(User.id).all()
        return render_template("users.html", users=users_list)

    @app.route("/users/new", methods=["POST"])
    @admin_required
    def new_user():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        is_admin = request.form.get("is_admin") == "on"

        if not username or len(username) < 3:
            flash("Username troppo corto (min 3 caratteri).", "danger")
        elif User.query.filter_by(username=username).first():
            flash(f"Username '{username}' già esistente.", "warning")
        else:
            ok, err = User.validate_password(password)
            if not ok:
                flash(f"❌ {err}", "danger")
            else:
                u = User(username=username, is_admin=is_admin)
                u.set_password(password)
                db.session.add(u); db.session.commit()
                audit("user_created", target=f"user:{username}",
                      details=f"admin={is_admin}")
                flash(f"Utente '{username}' creato.", "success")
        return redirect(url_for("users"))

    @app.route("/users/<int:uid>/delete", methods=["POST"])
    @admin_required
    def delete_user(uid):
        u = User.query.get_or_404(uid)
        if u.id == current_user.id:
            flash("Non puoi eliminare te stesso.", "danger")
        elif u.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
            flash("Non puoi eliminare l'unico amministratore.", "danger")
        else:
            uname = u.username
            db.session.delete(u); db.session.commit()
            audit("user_deleted", target=f"user:{uname}")
            flash(f"Utente '{uname}' eliminato.", "info")
        return redirect(url_for("users"))

    @app.route("/users/<int:uid>/toggle-admin", methods=["POST"])
    @admin_required
    def toggle_admin(uid):
        u = User.query.get_or_404(uid)
        if u.id == current_user.id:
            flash("Non puoi rimuovere i diritti di admin a te stesso.", "danger")
        elif u.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
            flash("Deve esistere almeno un amministratore.", "danger")
        else:
            u.is_admin = not u.is_admin
            db.session.commit()
            audit("user_admin_toggle", target=f"user:{u.username}",
                  details=f"is_admin={u.is_admin}")
            flash(f"Utente '{u.username}' è ora {'amministratore' if u.is_admin else 'utente normale'}.", "success")
        return redirect(url_for("users"))

    @app.route("/users/<int:uid>/reset-password", methods=["POST"])
    @admin_required
    def reset_user_password(uid):
        u = User.query.get_or_404(uid)
        new_pwd = request.form.get("new_password", "")
        ok, err = User.validate_password(new_pwd)
        if not ok:
            flash(f"❌ {err}", "danger")
        else:
            u.set_password(new_pwd); db.session.commit()
            audit("password_reset", target=f"user:{u.username}")
            flash(f"Password di '{u.username}' aggiornata.", "success")
        return redirect(url_for("users"))

    @app.route("/admin/metrics")
    @admin_required
    def admin_metrics():
        """Dashboard admin: metriche di sistema (utenti, fatture, attività, errori)."""
        from sqlalchemy import func as _f
        now = datetime.utcnow()
        d7  = now - timedelta(days=7)
        d30 = now - timedelta(days=30)

        # ── Utenti ──────────────────────────────────────────────────────────
        users_all     = User.query.count()
        users_admin   = User.query.filter_by(is_admin=True).count()
        users_guest   = User.query.filter(User.username.like("ospite_%")).count()
        users_real    = users_all - users_guest
        users_new_7d  = User.query.filter(User.created_at >= d7).count()
        users_new_30d = User.query.filter(User.created_at >= d30).count()
        users_active_30d = (db.session.query(_f.count(_f.distinct(AuditLog.user_id)))
                            .filter(AuditLog.action == "login_success",
                                    AuditLog.timestamp >= d30,
                                    AuditLog.user_id.isnot(None))
                            .scalar() or 0)

        # ── Clienti / fatture (aggregati globali, tutti gli utenti) ─────────
        clients_total  = Client.query.count()
        invoices_total = Invoice.query.filter(
            db.or_(Invoice.document_type != "TD04", Invoice.document_type.is_(None))
        ).count()
        by_status = dict(
            db.session.query(Invoice.status, _f.count(Invoice.id))
            .filter(db.or_(Invoice.document_type != "TD04", Invoice.document_type.is_(None)))
            .group_by(Invoice.status).all()
        )
        amount_issued = (db.session.query(_f.sum(Invoice.amount))
                         .filter(db.or_(Invoice.document_type != "TD04",
                                        Invoice.document_type.is_(None)))
                         .scalar() or 0)
        amount_paid    = (db.session.query(_f.sum(Invoice.amount))
                          .filter(Invoice.status == "paid").scalar() or 0)
        amount_overdue = (db.session.query(_f.sum(Invoice.amount))
                          .filter(Invoice.status == "overdue").scalar() or 0)

        # ── Solleciti (Reminder) ────────────────────────────────────────────
        reminders_30d = Reminder.query.filter(Reminder.sent_at >= d30).count()
        reminders_failed_30d = Reminder.query.filter(
            Reminder.sent_at >= d30, Reminder.success.is_(False)
        ).count()

        # ── PEC istituzionali ───────────────────────────────────────────────
        pec_total = PecMessage.query.count()
        pec_7d    = PecMessage.query.filter(PecMessage.received_at >= d7).count()

        # ── Audit log: top azioni ultimi 7 giorni ───────────────────────────
        top_actions = (db.session.query(AuditLog.action, _f.count(AuditLog.id))
                       .filter(AuditLog.timestamp >= d7)
                       .group_by(AuditLog.action)
                       .order_by(_f.count(AuditLog.id).desc())
                       .limit(10).all())

        # ── Errori integrazioni recenti ─────────────────────────────────────
        # Recupera per ogni utente l'ultimo error string salvato in UserSetting
        integration_errors = []
        for key in ("integration_pec_last_error",
                    "integration_folder_last_error",
                    "integration_fic_last_error"):
            rows = (UserSetting.query
                    .filter(UserSetting.key == key, UserSetting.value != "")
                    .all())
            for r in rows:
                u = User.query.get(r.user_id)
                integration_errors.append({
                    "user": u.username if u else f"#{r.user_id}",
                    "kind": key.replace("integration_", "").replace("_last_error", ""),
                    "msg":  r.value[:200],
                })

        # ── Tickets ─────────────────────────────────────────────────────────
        tickets_open = SupportTicket.query.filter(
            SupportTicket.status.in_(["open", "in_progress", "waiting_user"])
        ).count()

        # ── Bandi ───────────────────────────────────────────────────────────
        bandi_total  = Bando.query.filter_by(is_active=True).count()
        bandi_new_7d = Bando.query.filter(Bando.created_at >= d7).count()
        last_bando   = (Bando.query.filter_by(is_active=True)
                        .order_by(Bando.last_seen_at.desc()).first())
        bandi_last_sync = last_bando.last_seen_at if last_bando else None

        return render_template("admin_metrics.html",
            users_all=users_all, users_admin=users_admin, users_guest=users_guest,
            users_real=users_real, users_new_7d=users_new_7d,
            users_new_30d=users_new_30d, users_active_30d=users_active_30d,
            clients_total=clients_total, invoices_total=invoices_total,
            by_status=by_status,
            amount_issued=amount_issued, amount_paid=amount_paid,
            amount_overdue=amount_overdue,
            reminders_30d=reminders_30d, reminders_failed_30d=reminders_failed_30d,
            pec_total=pec_total, pec_7d=pec_7d,
            top_actions=top_actions,
            integration_errors=integration_errors,
            tickets_open=tickets_open,
            bandi_total=bandi_total, bandi_new_7d=bandi_new_7d,
            bandi_last_sync=bandi_last_sync,
        )

    @app.route("/admin/backups")
    @admin_required
    def admin_backups():
        """Lista backup S3 + bottone esegui ora."""
        from backup_service import list_backups, _get_config
        cfg = _get_config()
        items = list_backups(cfg) if cfg["enabled"] and cfg["bucket"] else []
        return render_template("admin_backups.html", backups=items, cfg=cfg)

    @app.route("/admin/backups/run-now", methods=["POST"])
    @admin_required
    def admin_backup_run_now():
        from backup_service import run_backup
        result = run_backup(app)
        if result.get("ok"):
            kb = result["size_bytes"] // 1024
            flash(f"✅ Backup eseguito: {result['key']} ({kb} KB). "
                  f"Vecchi cancellati: {result['cleaned_up']}.", "success")
            audit("backup_run", target="manual", details=str(result)[:200])
        elif result.get("skipped"):
            flash(f"⚠️ {result['reason']}", "warning")
        else:
            flash(f"❌ Errore backup: {result.get('error', '?')}", "danger")
            audit("backup_run", target="manual", details=f"FAIL: {result.get('error', '?')[:200]}")
        return redirect(url_for("admin_backups"))

    @app.route("/admin/secrets/migrate", methods=["POST"])
    @admin_required
    def admin_secrets_migrate():
        """Cifra tutti i secret esistenti in DB. Idempotente."""
        from crypto_service import migrate_existing_secrets, is_encryption_enabled
        if not is_encryption_enabled():
            flash("⚠️ Imposta prima la env var SECRETS_ENCRYPTION_KEY su Render, poi riavvia.", "warning")
            return redirect(url_for("admin_metrics"))
        try:
            stats = migrate_existing_secrets(db)
            flash(
                f"✅ Migrazione completata: {stats.get('app_encrypted', 0)} AppSettings + "
                f"{stats.get('user_encrypted', 0)} UserSetting cifrati. "
                f"{stats.get('skipped_already_enc', 0)} erano già cifrati.",
                "success",
            )
            audit("secrets_migrated", details=str(stats))
        except Exception as e:
            flash(f"❌ Errore migrazione: {e}", "danger")
        return redirect(url_for("admin_metrics"))

    @app.route("/admin/audit-log")
    @admin_required
    def admin_audit_log():
        # Filtri opzionali
        action = request.args.get("action", "")
        username = request.args.get("user", "")
        q = AuditLog.query
        if action:
            q = q.filter_by(action=action)
        if username:
            q = q.filter(AuditLog.username.ilike(f"%{username}%"))
        logs = q.order_by(AuditLog.timestamp.desc()).limit(500).all()
        # Lista azioni distinte per il filtro dropdown
        all_actions = [a[0] for a in db.session.query(AuditLog.action)
                       .distinct().order_by(AuditLog.action).all()]
        return render_template("audit_log.html",
                               logs=logs, all_actions=all_actions,
                               filter_action=action, filter_user=username)

    return app


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _migrate_db():
    """Aggiunge colonne mancanti al DB senza perdere dati (idempotente)."""
    engine = db.engine
    with engine.connect() as conn:
        from sqlalchemy import text, inspect
        inspector = inspect(engine)

        # ── Tabella invoices ─────────────────────────────────────────────────
        existing = {c["name"] for c in inspector.get_columns("invoices")}
        if "payment_ref" not in existing:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN payment_ref TEXT DEFAULT ''"))
            conn.commit()
            logging.info("Migrazione: aggiunta colonna invoices.payment_ref")
        if "pdf_filename" not in existing:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN pdf_filename TEXT DEFAULT ''"))
            conn.commit()
            logging.info("Migrazione: aggiunta colonna invoices.pdf_filename")
        if "user_notified_at" not in existing:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN user_notified_at DATETIME"))
            conn.commit()
            logging.info("Migrazione: aggiunta colonna invoices.user_notified_at")
        if "document_type" not in existing:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN document_type TEXT DEFAULT 'TD01'"))
            conn.execute(text("UPDATE invoices SET document_type = 'TD01' WHERE document_type IS NULL"))
            conn.commit()
            logging.info("Migrazione: aggiunta colonna invoices.document_type")
        if "linked_invoice_id" not in existing:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN linked_invoice_id INTEGER REFERENCES invoices(id)"))
            conn.commit()
            logging.info("Migrazione: aggiunta colonna invoices.linked_invoice_id")
        if "is_passive" not in existing:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN is_passive INTEGER DEFAULT 0"))
            conn.commit()
            logging.info("Migrazione: aggiunta colonna invoices.is_passive")
        if "payment_method" not in existing:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN payment_method TEXT DEFAULT ''"))
            conn.commit()
            logging.info("Migrazione: aggiunta colonna invoices.payment_method")

        # ── Tabella clients: flag fornitore + IBAN ──────────────────────────
        if "clients" in inspector.get_table_names():
            existing_cli = {c["name"] for c in inspector.get_columns("clients")}
            if "is_supplier" not in existing_cli:
                conn.execute(text("ALTER TABLE clients ADD COLUMN is_supplier INTEGER DEFAULT 0"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna clients.is_supplier")
            if "iban" not in existing_cli:
                conn.execute(text("ALTER TABLE clients ADD COLUMN iban TEXT DEFAULT ''"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna clients.iban")

        # ── Tabella users ────────────────────────────────────────────────────
        if "users" in inspector.get_table_names():
            existing_u = {c["name"] for c in inspector.get_columns("users")}
            if "is_admin" not in existing_u:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0"))
                conn.execute(text("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna users.is_admin")
            if "created_at" not in existing_u:
                conn.execute(text("ALTER TABLE users ADD COLUMN created_at DATETIME"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna users.created_at")
            if "email" not in existing_u:
                conn.execute(text("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna users.email")
            if "phone" not in existing_u:
                conn.execute(text("ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna users.phone")
            if "totp_secret" not in existing_u:
                conn.execute(text("ALTER TABLE users ADD COLUMN totp_secret TEXT DEFAULT ''"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna users.totp_secret")
            if "totp_enabled" not in existing_u:
                conn.execute(text("ALTER TABLE users ADD COLUMN totp_enabled INTEGER DEFAULT 0"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna users.totp_enabled")
            if "totp_backup_codes" not in existing_u:
                conn.execute(text("ALTER TABLE users ADD COLUMN totp_backup_codes TEXT DEFAULT ''"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna users.totp_backup_codes")

        # ── Tabella bank_accounts (per refactoring GoCardless → Tink) ────────
        if "bank_accounts" in inspector.get_table_names():
            existing_ba = {c["name"] for c in inspector.get_columns("bank_accounts")}
            if "access_token" not in existing_ba:
                conn.execute(text("ALTER TABLE bank_accounts ADD COLUMN access_token TEXT DEFAULT ''"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna bank_accounts.access_token")
            if "refresh_token" not in existing_ba:
                conn.execute(text("ALTER TABLE bank_accounts ADD COLUMN refresh_token TEXT DEFAULT ''"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna bank_accounts.refresh_token")
            if "token_expires_at" not in existing_ba:
                conn.execute(text("ALTER TABLE bank_accounts ADD COLUMN token_expires_at DATETIME"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna bank_accounts.token_expires_at")
            if "last_balance" not in existing_ba:
                conn.execute(text("ALTER TABLE bank_accounts ADD COLUMN last_balance REAL"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna bank_accounts.last_balance")
            if "last_balance_at" not in existing_ba:
                conn.execute(text("ALTER TABLE bank_accounts ADD COLUMN last_balance_at DATETIME"))
                conn.commit()
                logging.info("Migrazione: aggiunta colonna bank_accounts.last_balance_at")

        # ── Multi-tenant: user_id su clients e invoices ──────────────────────
        admin_id_row = conn.execute(text("SELECT MIN(id) FROM users WHERE is_admin = 1")).fetchone()
        admin_id = admin_id_row[0] if admin_id_row and admin_id_row[0] else 1

        existing_c = {c["name"] for c in inspector.get_columns("clients")}
        if "user_id" not in existing_c:
            conn.execute(text("ALTER TABLE clients ADD COLUMN user_id INTEGER"))
            conn.execute(text(f"UPDATE clients SET user_id = {admin_id} WHERE user_id IS NULL"))
            conn.commit()
            logging.info(f"Migrazione: clients.user_id (record esistenti assegnati all'admin id={admin_id})")

        existing_i = {c["name"] for c in inspector.get_columns("invoices")}
        if "user_id" not in existing_i:
            conn.execute(text("ALTER TABLE invoices ADD COLUMN user_id INTEGER"))
            conn.execute(text(f"UPDATE invoices SET user_id = {admin_id} WHERE user_id IS NULL"))
            conn.commit()
            logging.info(f"Migrazione: invoices.user_id (record esistenti assegnati all'admin id={admin_id})")


def _seed_settings():
    defaults = {
        "company_name": config.COMPANY_NAME, "smtp_host": config.SMTP_HOST,
        "smtp_port": str(config.SMTP_PORT), "smtp_user": config.SMTP_USER,
        "smtp_password": config.SMTP_PASSWORD,
        "smtp_use_tls": "true" if config.SMTP_USE_TLS else "false",
        "payment_base_url": config.PAYMENT_BASE_URL,
        "stripe_webhook_secret": "", "paypal_webhook_id": "",
    }
    for k, v in defaults.items():
        if not AppSettings.get(k):
            AppSettings.set(k, v)


def _ensure_admin():
    """Crea l'utente creatore (admin) con password 'admin' al primo avvio."""
    if not User.query.first():
        u = User(username="admin", is_admin=True)
        u.set_password("admin")
        db.session.add(u)
        db.session.commit()
        logging.warning("Utente admin creato con password 'admin'. CAMBIALA subito nelle Impostazioni!")
    else:
        # Garantisce che almeno un admin esista (sicurezza)
        if not User.query.filter_by(is_admin=True).first():
            first = User.query.order_by(User.id).first()
            first.is_admin = True
            db.session.commit()
            logging.warning("Nessun admin trovato — promosso utente '%s'.", first.username)


# ─── Avvio ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import webbrowser, threading

    application = create_app()

    def open_browser():
        import time; time.sleep(1)
        webbrowser.open("http://127.0.0.1:5000")

    threading.Thread(target=open_browser, daemon=True).start()
    application.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
