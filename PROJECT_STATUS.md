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
- [x] **Filtro non-fatture**: PDF che non sono fatture (lettere, scontrini, contratti, ecc.) vengono saltati con messaggio chiaro invece di creare record spurious
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

### Sottoscrizione SaaS — signup pubblico + abbonamenti Stripe
- [x] **Registrazione self-service** `/register` (form: username, email, password, ragione sociale, P.IVA, accept terms)
- [x] Validation server-side + rate limit 5/min + CSRF
- [x] **Email di benvenuto** automatica via Resend/SMTP al nuovo utente
- [x] **Stripe Checkout** in modalità subscription con `trial_period_days=30` e carta richiesta upfront
- [x] **Customer Portal** Stripe per gestire metodo di pagamento e disdire (`/billing/portal`)
- [x] **Pagina /billing** con stato sottoscrizione, giorni di prova rimasti, prossimo rinnovo, FAQ
- [x] **Webhook Stripe esteso**: gestisce `checkout.session.completed (mode=subscription)`,
      `customer.subscription.created/updated/deleted`, `invoice.paid`, `invoice.payment_failed`
- [x] **Middleware enforcer**: utenti con trial scaduto / sub disdetta vengono reindirizzati a `/billing`
- [x] **Colonne User**: `plan`, `stripe_customer_id`, `stripe_subscription_id`,
      `subscription_status`, `trial_ends_at`, `current_period_end` (migration automatica)
- [x] **Settings admin**: sezione Stripe SaaS per inserire `stripe_secret_key`, `stripe_publishable_key`,
      `stripe_price_id`, toggle `signup_enabled`
- [x] **Voce sidebar "Sottoscrizione"** per utenti non-admin
- [x] **Link "Registrati"** in pagina login

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

### Bandi di finanziamento
- [x] **Bandi MVP** — scraping AI da MIMIT/Invitalia (estensibile), matching AI personalizzato sul profilo utente (ATECO/regione/dimensione/descrizione), lista filtrabile per score, save/dismiss, sync giornaliero alle 06:00. Sezione "Mio profilo" estesa con campi per matching.

### Quadro generale "salute aziendale" (`/health`)
- [x] Vista executive con tutto integrato: alert critici, saldo netto previsto, KPI banche/incassi/pagamenti, prossimi pagamenti 7gg, prossimi incassi 7gg, scadenze fiscali 30gg, bandi rilevanti, ticket aperti, transazioni da riconciliare.
- [x] Alert dinamici (saldo negativo, pagamenti in ritardo, scadenze fiscali in ritardo, ecc.) con bottoni di azione contestuale.

### Cash flow forecast (`/cash-flow`)
- [x] Proiezione settimanale per 12 settimane (~3 mesi) con saldo cumulativo per ogni settimana
- [x] Saldo iniziale = somma `last_balance` delle banche collegate (sync automatico da Tink)
- [x] Entrate previste: fatture attive aperte raggruppate per `due_date`
- [x] Uscite previste: fatture passive aperte raggruppate per `due_date`
- [x] Scaduti sommati al saldo iniziale (assumendo regolazione immediata)
- [x] Alert "saldo previsto negativo" se min cumulativo < 0
- [x] Mini grafico barre orizzontali (no librerie esterne, pure HTML/CSS)

### Calendario scadenze fiscali (`/fiscal`)
- [x] Modello `FiscalDeadline` con categorie italiane (IVA mensile/trimestrale, F24, INPS, INAIL, CCIAA, dichiarazione, 770, LIPE, intrastat, altro)
- [x] CRUD: nuova scadenza, complete, delete; con flag `is_recurring` (monthly/quarterly/yearly) che rigenera la prossima istanza al completamento
- [x] **Seed automatico scadenze IT standard**: bottone "Carica scadenze IT" che popola IVA mensile/trimestrale, INPS artigiani, INAIL, CCIAA, LIPE, acconti IRPEF, dichiarazione redditi, mod. 770 per l'anno corrente
- [x] **Notifica automatica** giornaliera alle 08:30 (email + WhatsApp) per scadenze nei prossimi 7gg non ancora notificate
- [x] KPI: aperte, in ritardo, entro 7gg, entro 30gg

### Lato passivo (fornitori + pagamenti da fare)
- [x] **Anagrafica fornitori** in `/suppliers` (riusa modello Client con flag `is_supplier=True`, aggiunto campo `iban` per bonifici)
- [x] **Fatture passive** in `/payables` (Invoice con flag `is_passive=True`, campo `payment_method` per metodo di pagamento)
- [x] CRUD fornitori (`/suppliers/new`, `/suppliers/<id>`, edit, delete con check fatture collegate)
- [x] CRUD fatture passive (`/payables/new` con creazione fornitore al volo, mark-paid con data + metodo + ref)
- [x] Lista filtrabile (Aperte / Pagate / Tutte) con KPI: da pagare, in ritardo, importo aperto, pagato 30gg
- [x] **Dashboard split** in 3 sezioni:
  - In alto: **Saldo netto previsto** (entrate aperte − uscite aperte) con colore success/danger
  - **Lato attivo** (entrate, sezione esistente)
  - **Lato passivo** (uscite, KPI count + lista prossimi pagamenti)
- [x] Sidebar: nuove voci "Fornitori" e "Pagamenti"

### Riconciliazione bancaria (Tink, PSD2)
- [x] Provider: **Tink** (Visa) — copre tutte le banche italiane. GoCardless temporaneamente disabilitato per nuove signup.
- [x] Connessione via Tink Link (redirect → scegli banca → SCA → callback con code → exchange per access_token + refresh_token)
- [x] Sync giornaliero transazioni alle 07:00 (incrementale, ultimi 14gg)
- [x] Auto-refresh dell'access_token quando scaduto (refresh_token valido 90gg)
- [x] Auto-match transazione → fattura (importo + numero fattura/P.IVA/nome cliente nella causale, score 0-100)
- [x] Riconciliazione automatica se score≥80 e candidato unico → fattura marcata pagata + payment_ref `bank:<tx_id>`
- [x] Coda manuale `/bank/reconciliation` con UI per match dubbi (top 5 candidati per ogni tx + opzione "forza match")
- [x] Notifica email + WhatsApp digest dopo sync (auto-matched + pending count)
- [x] Re-auth ogni 90gg (limite PSD2, alert 14gg prima della scadenza)
- [x] tink_client_id + tink_client_secret in admin settings cifrati at-rest

### Nice to have
- [x] **Dashboard admin con metriche di sistema** (utenti, fatture, attività, errori) → `/admin/metrics`
- [x] **Resend** come alternativa SMTP (provider configurabile in Impostazioni admin, fallback automatico)
- [x] **Customer portal pubblico** (`/portal/<token>`, link firmato 1 anno, generabile dalla pagina cliente)
- [x] **Knowledge base** /help — 11 sezioni, 30+ FAQ, ricerca live, indice navigabile
- [x] **Custom domain** gestfatture.com (Cloudflare DNS + Render Let's Encrypt)
- [x] **Export tickets** in CSV (nativo) + PDF (reportlab, layout tabellare landscape)
- [x] **Survey post-risoluzione ticket** — email automatica al cambio status → resolved, link firmato 90gg, stelle 1-5 + commento, dashboard /admin/surveys
- [x] **Crittografia at-rest dei secret** (Fernet AES-128) — opt-in via env `SECRETS_ENCRYPTION_KEY`, retrocompatibile, bottone "Cifra secret esistenti" in /admin/metrics
- [x] **Backup S3 settimanali** — lunedì 03:00 in automatico, supporta AWS/Backblaze/R2/Spaces/Minio (qualsiasi S3-compatibile), retention configurabile, dashboard /admin/backups

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

*Ultimo aggiornamento: 2026-05-05 (signup pubblico self-service + Stripe Subscriptions + Customer Portal + middleware enforcer trial)*

---

## 🚦 Setup Stripe (post-deploy)

Per abilitare il signup pubblico con abbonamenti €9,99/mese:

1. **Crea il prodotto su Stripe**
   Dashboard → Products → "GestFatture Pro", prezzo ricorrente €9,99/mese, copia il `price_id`

2. **Configura il Customer Portal**
   Dashboard → Settings → Billing → Customer portal → abilita
   "Cancellazione subscription" e "Aggiornamento metodi di pagamento"

3. **Crea il webhook**
   Dashboard → Developers → Webhooks → endpoint:
   `https://app.gestfatture.com/webhook/stripe`
   Eventi: `checkout.session.completed`, `payment_intent.succeeded`,
   `customer.subscription.created`, `customer.subscription.updated`,
   `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`

4. **In app → Impostazioni admin**: incolla
   - `stripe_secret_key` (sk_live_...)
   - `stripe_publishable_key` (pk_live_...)
   - `stripe_price_id` (price_...)
   - `stripe_webhook_secret` (whsec_...)
