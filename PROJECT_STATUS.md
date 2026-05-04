# 📊 GestFatture — Stato del progetto

> **Per chi riprende il progetto**: leggi questo file prima di tutto.
> Riassume cosa è stato fatto e cosa rimane.

---

## 🎯 Cos'è

**GestFatture** è un SaaS multi-tenant italiano per gestione fatture, solleciti automatici e contabilità clienti.

- **Live**: https://gestfatture.onrender.com
- **Codice**: https://github.com/satria64/gestfatture
- **Owner**: Marco Fioretti (admin: `admin` con password personalizzata)
- **Stack**: Python 3.12 + Flask + SQLAlchemy + SQLite + Bootstrap 5

---

## ✅ Funzionalità implementate

### Core (gestione fatture)
- [x] Multi-tenant SaaS: ogni utente vede SOLO i suoi clienti/fatture
- [x] CRUD clienti + fatture
- [x] Tipi documento: TD01 (fattura), TD04 (NC con compensazione automatica), TD05 (ND), TD06 (parcella)
- [x] Stati: pending, overdue, paid, cancelled, compensated
- [x] Credit scoring per cliente
- [x] Allegato PDF per fattura

### Solleciti automatici
- [x] Job giornaliero alle 08:00 (APScheduler)
- [x] Pre-scadenza: 7/3/1 giorni prima
- [x] Post-scadenza: 1/7/15/30 giorni dopo
- [x] Tipi: pre_scadenza, sollecito_1/2/3, diffida formale
- [x] PDF allegato automaticamente all'email
- [x] NC escluse dai solleciti

### Import fatture
- [x] Manuale form
- [x] CSV / Excel (con auto-detect colonne)
- [x] PDF via Claude API (alta accuratezza) o regex (fallback)
- [x] XML FatturaPA (parsing nativo, 100% preciso)
- [x] .xml.p7m (CADES firmato)
- [x] ZIP (multipli mescolati)
- [x] **Riconoscimento fatture passive** (basato su P.IVA dell'utente)
- [x] Match clienti per P.IVA (no duplicati)
- [x] Anteprima estrazione PDF (debug)

### Integrazioni (per ogni utente)
- [x] **📁 Cartella locale**: polling 30s, file processed/
- [x] **📧 PEC IMAP**: polling 5min, scarica allegati XML/p7m/zip
- [x] **🏛 PEC istituzionali AdE/INPS/INAIL**: analisi AI, riassunti, notifiche
- [x] **☁️ Fatture in Cloud**: OAuth 2.0, polling 30min, OAuth state validato

### Notifiche al titolare
- [x] Email su scadenza fattura
- [x] WhatsApp via CallMeBot (gratis)
- [x] Quick action links (1-tap dal messaggio per inviare sollecito al cliente)
- [x] Conferma 2-step anti link-preview
- [x] Token firmati (itsdangerous, scadenza 30gg)

### Pagamenti
- [x] Webhook Stripe (verifica firma)
- [x] Webhook PayPal
- [x] Auto-mark paid quando webhook conferma

### Assistenza
- [x] AI Chat fluttuante (Claude Haiku, ~€0.001/msg)
- [x] Sistema ticket con stati, priorità, categoria
- [x] Note interne admin
- [x] Notifiche email su eventi ticket

### Sicurezza (Step 1 + Step 2 completati)
- [x] CSRF protection (Flask-WTF) su tutti i form
- [x] Rate limiting (Flask-Limiter): login 10/min, guest 5/min, chat 30/min
- [x] OAuth state validation
- [x] Security headers HTTP (HSTS, CSP, X-Frame-Options, ecc.)
- [x] MAX_CONTENT_LENGTH 10MB upload
- [x] Cookie SameSite=Lax + HTTPOnly + Secure
- [x] Password complexity (min 8 chars, lettera+numero, no comuni)
- [x] Session timeout 4h inattività
- [x] Audit log con UI admin (/admin/audit-log)

### GDPR & Compliance (Step 3 completato)
- [x] **Export dati GDPR Art. 20** — `/account/export` scarica ZIP con tutti i dati (JSON + PDF allegati). Credenziali di terzi redatte.
- [x] **Cancellazione account GDPR Art. 17** — `/account/delete` (POST con password + conferma "ELIMINA"). Cancella DB + file su disco. Audit preservato.
- [x] **Privacy Policy** — `/privacy` (pubblica), valori titolare configurabili in Impostazioni admin.
- [x] **Termini di servizio** — `/terms` (pubblica).
- [x] **Cookie banner di trasparenza** — solo cookie tecnici, niente consent module.
- [x] **2FA TOTP opzionale** — pyotp + QR code, 8 codici di backup single-use, login a 2 step. Toggle dalle Impostazioni.
- [x] **Sentry** — error tracking opzionale via env var `SENTRY_DSN`. PII spente per GDPR.

### UX
- [x] Dashboard con KPI count + KPI EUR
- [x] Modalità Ospite (`/login/guest`) con cleanup auto al logout
- [x] Multi-tenant separation completa
- [x] Notifiche flash colorate
- [x] Responsive mobile

---

## 🔧 Configurazione richiesta (post-deploy)

Login admin → Impostazioni:
1. **URL pubblico app**: `https://gestfatture.onrender.com`
2. **Mio profilo**: nome azienda + **P.IVA** (essenziale per fatture passive)
3. **SMTP** (Gmail con App Password o PEC)
4. **Anthropic API key** (sk-ant-..., serve credito Anthropic)
5. **Stripe webhook secret** (opzionale)
6. **App FiC OAuth** (Client ID/Secret se hai account FiC)

---

## 📁 Struttura del progetto

```
invoice_manager/
├── app.py                  # Flask app principale (3000+ righe)
├── models.py               # SQLAlchemy models (User, Client, Invoice, Reminder,
│                              UserSetting, AppSettings, PecMessage, SupportTicket,
│                              TicketMessage, AuditLog)
├── auth.py                 # Flask-Login setup
├── config.py               # Config da env vars
├── tokens.py               # Quick action tokens firmati
├── email_service.py        # Invio email solleciti
├── notification_service.py # Notifiche al titolare (email + WhatsApp)
├── claude_service.py       # Claude API (estrazione PDF, chat, analisi PEC)
├── credit_scoring.py       # Algoritmo credit score
├── import_service.py       # Import CSV/Excel/PDF (regex+Claude)
├── xml_parser.py           # Parser FatturaPA + extract p7m
├── integration_folder.py   # Folder watcher
├── integration_pec.py      # PEC IMAP scanner
├── integration_fic.py      # Fatture in Cloud OAuth
├── scheduler_service.py    # APScheduler jobs
├── make_logo.py            # Genera logo PNG (utility)
├── requirements.txt        # dipendenze Python
├── render.yaml             # config deploy Render (Infrastructure-as-Code)
├── Procfile                # comando di start
├── runtime.txt             # Python 3.12.7
├── .gitignore
├── .env.example
├── DEPLOY.md               # guida deploy completa
├── PROJECT_STATUS.md       # questo file
├── templates/              # 19 template Jinja2
└── static/style.css
```

---

## 🚀 Deploy

- **Hosting**: Render.com piano Starter ($7/mese)
- **Disco persistente**: 1GB su `/var/data` (DB SQLite + uploads PDF)
- **Snapshot automatici**: giornalieri, 7 giorni di retention
- **HTTPS**: automatico via Let's Encrypt
- **Auto-deploy**: ogni `git push` su main triggera redeploy

Per redeploy manuale:
```bash
git add . && git commit -m "..." && git push
```

---

## 💰 Costi mensili stimati

| Voce | Costo |
|---|---|
| Render Starter + persistent disk | $7 |
| Anthropic Claude API (uso medio) | $1-10 |
| Dominio custom (opzionale) | €1 (€10/anno) |
| **Totale** | **~$10/mese** |

---

## 📝 Cose ancora da fare (Roadmap)

### Step 3 — Compliance (✅ completato 2026-05-04)
Tutto implementato. Per attivare in produzione restano solo configurazioni:
- Compilare i 4 campi "Dati legali" in Impostazioni admin (ragione sociale, P.IVA, indirizzo, email contatto)
- Aggiungere `SENTRY_DSN` come env var su Render se si vuole l'error tracking
- Considerare revisione legale di `templates/privacy.html` e `templates/terms.html` prima della pubblicazione effettiva

### Nice to have
- [x] **Dashboard admin con metriche di sistema** (utenti, fatture, attività, errori) → `/admin/metrics`
- [x] **Resend** come alternativa SMTP (provider configurabile in Impostazioni admin, fallback automatico)
- [x] **Customer portal pubblico** (`/portal/<token>`, link firmato 1 anno, generabile dalla pagina cliente)
- [ ] Knowledge base navigabile in /help
- [ ] Custom domain setup (gestfatture.tuoazienda.it)
- [ ] Esportazione tickets in CSV/PDF
- [ ] Survey post-risoluzione ticket
- [ ] Crittografia at-rest dei secret (Fernet)
- [ ] Backup S3 settimanali (oltre agli snapshot Render)

### Bug noti / cose da rivedere
- Email vanno in spam (manca SPF/DKIM/DMARC sul dominio del mittente — risolvibile con Resend)
- Eventuali nomi clienti vecchi sbagliati: usare "Unifica duplicati" + ri-import
- Logo OAuth FiC: caricato su FiC tramite `make_logo.py`
- I PDF scansionati come immagine non sono leggibili (servirebbe OCR)

---

## 🔑 Credenziali / Account

Custodite altrove (NON in questo file):
- Account Anthropic (API key)
- Account Render
- Account GitHub (satria64)
- Account Fatture in Cloud
- Password admin app
- App password Gmail (per SMTP)
- CallMeBot API key personale

---

## 🤝 Per il prossimo Claude / sessione

Quando apri una nuova chat per continuare:

```
Sto sviluppando GestFatture, un SaaS multi-tenant Flask/Python deployato su
Render (gestfatture.onrender.com) con codice su github.com/satria64/gestfatture.

Leggi PROJECT_STATUS.md nella root del progetto per il contesto completo.

Voglio fare: [descrivi cosa vuoi fare]
```

Se usi **Claude Code** (CLI tool ufficiale Anthropic):
```bash
cd "C:\Users\MARCO\Desktop\cloude code\invoice_manager"
claude
```
Claude Code legge automaticamente tutti i file e ha il contesto completo.

---

*Ultimo aggiornamento: 2026-05-04 (Step 3 GDPR completato + Resend + Customer portal + Admin metriche + diagnostica notifiche PEC)*
