import os
import sys
import logging
from datetime import date, datetime

from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify, Response, abort, send_from_directory)
from werkzeug.utils import secure_filename
from functools import wraps
from flask_login import login_required, login_user, logout_user, current_user

from models import (db, Client, Invoice, Reminder, AppSettings, UserSetting,
                    User, PecMessage, SupportTicket, TicketMessage)
from auth import login_manager
from config import config


# ─── Helpers multi-tenant: filtri per current_user ───────────────────────────
def my_clients():
    return Client.query.filter_by(user_id=current_user.id)


def my_invoices():
    return Invoice.query.filter_by(user_id=current_user.id)


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


def create_app():
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

    db.init_app(app)
    login_manager.init_app(app)

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

    # ═══════════════════════════════════════════════════════════════════════════
    # AUTH
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user, remember=request.form.get("remember") == "on")
                return redirect(request.args.get("next") or url_for("dashboard"))
            flash("Username o password non corretti.", "danger")
        return render_template("login.html")

    def _delete_user_and_all_data(user):
        """Cancella un utente e TUTTI i suoi dati (clienti, fatture, settings, ticket, PEC)."""
        uid = user.id
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
        logout_user()
        if guest_user:
            try:
                _delete_user_and_all_data(guest_user)
                flash("👋 Account ospite eliminato. Grazie per aver provato GestFatture!", "info")
            except Exception as e:
                logging.error("Errore cleanup ospite: %s", e)
        return redirect(url_for("login"))

    @app.route("/login/guest")
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
        elif len(new) < 6:
            flash("La nuova password deve avere almeno 6 caratteri.", "danger")
        else:
            current_user.set_password(new)
            db.session.commit()
            flash("Password aggiornata.", "success")
        return redirect(url_for("settings"))

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
        return render_template("client_detail.html", client=get_my_client(cid))

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
            flash(f"✅ Unificati {merged_count} clienti duplicati (raggruppati per P.IVA).", "success")
        else:
            flash("Nessun duplicato trovato.", "info")
        return redirect(url_for("clients"))

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
        t = SupportTicket.query.get_or_404(tid)
        new_status = request.form.get("status")
        if new_status in ("open", "in_progress", "waiting_user", "resolved", "closed"):
            t.status = new_status
            db.session.commit()
            flash(f"Stato aggiornato a '{t.status_label[0]}'.", "info")
        return redirect(url_for("ticket_detail", tid=tid))

    # ═══════════════════════════════════════════════════════════════════════════
    # WEBHOOK STRIPE
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/webhook/stripe", methods=["POST"])
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
    # WEBHOOK PAYPAL
    # ═══════════════════════════════════════════════════════════════════════════
    @app.route("/webhook/paypal", methods=["POST"])
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
        # P.IVA dell'utente in UserSetting (serve per riconoscere fatture passive)
        my_vat = "".join(c for c in request.form.get("my_vat_number", "") if c.isdigit())
        UserSetting.set(current_user.id, "my_vat_number", my_vat)
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
                      "anthropic_api_key", "anthropic_model", "app_external_url"]

        if request.method == "POST":
            # Salva sempre le chiavi personali del current_user
            for k in user_keys:
                UserSetting.set(current_user.id, k, request.form.get(k, ""))
            # Chiavi admin solo se admin
            if current_user.is_admin:
                for k in admin_keys:
                    val = request.form.get(k, "")
                    # Per le password lascia stare se vuoto
                    if k in ("smtp_password",) and not val:
                        continue
                    AppSettings.set(k, val)
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

        # Preferenze notifiche + P.IVA personale
        notify_email    = UserSetting.get(current_user.id, "notify_email_enabled")
        notify_whatsapp = UserSetting.get(current_user.id, "notify_whatsapp_enabled")
        whatsapp_apikey = UserSetting.get(current_user.id, "whatsapp_apikey")
        my_vat_number   = UserSetting.get(current_user.id, "my_vat_number")

        return render_template("settings.html",
            settings=current,
            user_notify_email=notify_email,
            user_notify_whatsapp=notify_whatsapp,
            user_whatsapp_apikey=whatsapp_apikey,
            user_my_vat_number=my_vat_number,
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
        from integration_fic import get_authorize_url
        cid = AppSettings.get("integration_fic_client_id", "")
        if not cid:
            flash("L'amministratore non ha ancora configurato l'app Fatture in Cloud.", "warning")
            return redirect(url_for("my_integrations"))
        redirect_uri = url_for("my_fic_callback", _external=True)
        return redirect(get_authorize_url(cid, redirect_uri, state=f"u{current_user.id}"))

    @app.route("/my-integrations/fic/callback")
    @login_required
    def my_fic_callback():
        from integration_fic import exchange_code, get_companies
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
        flash("✅ Account Fatture in Cloud collegato.", "success")
        return redirect(url_for("my_integrations"))

    @app.route("/my-integrations/fic/disconnect", methods=["POST"])
    @login_required
    def my_fic_disconnect():
        uid = current_user.id
        for k in ("access_token", "refresh_token", "token_expires_at",
                  "company_id", "company_name", "last_sync"):
            UserSetting.set(uid, f"integration_fic_{k}", "")
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
        if new_secret:
            AppSettings.set("integration_fic_client_secret", new_secret)
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
        elif len(password) < 6:
            flash("Password troppo corta (min 6 caratteri).", "danger")
        elif User.query.filter_by(username=username).first():
            flash(f"Username '{username}' già esistente.", "warning")
        else:
            u = User(username=username, is_admin=is_admin)
            u.set_password(password)
            db.session.add(u); db.session.commit()
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
            db.session.delete(u); db.session.commit()
            flash(f"Utente '{u.username}' eliminato.", "info")
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
            flash(f"Utente '{u.username}' è ora {'amministratore' if u.is_admin else 'utente normale'}.", "success")
        return redirect(url_for("users"))

    @app.route("/users/<int:uid>/reset-password", methods=["POST"])
    @admin_required
    def reset_user_password(uid):
        u = User.query.get_or_404(uid)
        new_pwd = request.form.get("new_password", "")
        if len(new_pwd) < 6:
            flash("Password troppo corta (min 6 caratteri).", "danger")
        else:
            u.set_password(new_pwd); db.session.commit()
            flash(f"Password di '{u.username}' aggiornata.", "success")
        return redirect(url_for("users"))

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
