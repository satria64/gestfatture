# 🚀 Guida deploy GestFatture su Render.com

Tempo totale: ~30 minuti. Costo: **gratis** per testare, **$7/mese** per produzione affidabile.

---

## Prerequisiti

- [ ] Account GitHub (gratis su [github.com](https://github.com))
- [ ] Account Render (gratis su [render.com](https://render.com))
- [ ] Git installato sul PC ([git-scm.com/download/win](https://git-scm.com/download/win))

---

## 1️⃣ Push del codice su GitHub

### A. Inizializza il repo locale

Apri PowerShell nella cartella del progetto:

```powershell
cd "C:\Users\MARCO\Desktop\cloude code\invoice_manager"
git init
git add .
git commit -m "Initial commit"
```

### B. Crea un repo su GitHub

1. Vai su [github.com/new](https://github.com/new)
2. Repository name: `gestfatture`
3. Lascia vuoti README/license
4. **Visibility**: scegli **Private** (consigliato — il codice contiene logica business)
5. Click **Create repository**

### C. Push

Copia i comandi che GitHub ti mostra (sotto "...or push an existing repository"):

```powershell
git remote add origin https://github.com/TUOUSER/gestfatture.git
git branch -M main
git push -u origin main
```

Al primo push GitHub chiede login — usa username + **Personal Access Token** (NON password): [github.com/settings/tokens/new](https://github.com/settings/tokens/new) → spunta `repo` → genera → usalo come password.

---

## 2️⃣ Crea il Web Service su Render

1. Vai su [dashboard.render.com](https://dashboard.render.com) → click **New +** → **Web Service**
2. **Connect a repository** → autorizza Render ad accedere a GitHub → scegli `gestfatture`
3. Render rileva automaticamente `render.yaml` → click **Apply**

Se invece vuoi configurare manualmente:

| Campo | Valore |
|---|---|
| **Name** | `gestfatture` |
| **Region** | `Frankfurt` (vicino all'Italia) |
| **Branch** | `main` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT "app:create_app()"` |
| **Plan** | `Starter ($7/month)` (consigliato) o `Free` (test) |

---

## 3️⃣ Aggiungi disco persistente ⚠️ IMPORTANTE

Senza disco persistente il database SQLite si **cancella ad ogni deploy**. Configurazione:

1. Nella pagina del servizio → tab **Disks** → **Add Disk**
2. Configura:
   - **Name**: `gestfatture-data`
   - **Mount Path**: `/var/data`
   - **Size (GB)**: `1`
3. Click **Save**

⚠️ Solo i piani **Starter+** supportano i dischi persistenti. Sul piano free il DB si cancella.

---

## 4️⃣ Configura le variabili d'ambiente

Tab **Environment** → **Add Environment Variable**:

| Key | Value | Note |
|---|---|---|
| `SECRET_KEY` | (Render ne genera una sicura) | click "Generate" |
| `DATABASE_URL` | `sqlite:////var/data/invoice_manager.db` | DB su disco persistente |
| `COMPANY_NAME` | il nome della tua azienda | default per nuovi utenti |
| `RENDER` | `true` | abilita ProxyFix automatico |

Click **Save Changes** → Render redeploya automaticamente.

---

## 5️⃣ Attendi il deploy

Tab **Logs** mostra il deploy in corso. Cerca queste righe:

```
==> Build successful 🎉
==> Starting service with 'gunicorn --workers 1 ...'
[INFO] Listening at: http://0.0.0.0:10000
INFO Scheduler avviato – solleciti 08:00 + integrazioni
```

Quando vedi **"Live"** in verde in alto, il deploy è completo.

---

## 6️⃣ Apri l'URL pubblico

In alto nella dashboard del servizio c'è l'URL, tipo:

```
https://gestfatture-xxxx.onrender.com
```

Apri → ti fa il login (`admin / admin` al primo avvio) → cambia subito la password!

---

## 7️⃣ Configura l'URL pubblico in GestFatture

Login admin → **Impostazioni** → **URL pubblico dell'app** → incolla l'URL Render:

```
https://gestfatture-xxxx.onrender.com
```

Salva. Ora:
- Le quick-action nelle email/WhatsApp punteranno a questo URL
- L'OAuth FiC userà questo URL come redirect

---

## 8️⃣ Custom domain (opzionale, ~€10/anno)

### Compra il dominio
- [Cloudflare Domains](https://www.cloudflare.com/products/registrar/) — il più economico (~$9/anno per .com, no markup)
- [Namecheap](https://www.namecheap.com/) — alternativo
- [Aruba](https://www.aruba.it/) — italiano, .it €11/anno

### Configura DNS
1. In Render → tab **Settings** → **Custom Domain** → **Add**
2. Inserisci `gestfatture.tuoazienda.it` (o quello che vuoi)
3. Render ti dà istruzioni: aggiungi un record **CNAME** sul tuo registrar:
   ```
   gestfatture  CNAME  gestfatture-xxxx.onrender.com
   ```
4. Aspetta 5-30 min che il DNS si propaghi
5. Render genera **automaticamente HTTPS** (Let's Encrypt)

### Aggiorna le impostazioni
Cambia l'URL pubblico in Impostazioni → `https://gestfatture.tuoazienda.it`.

---

## 9️⃣ Crea l'app OAuth Fatture in Cloud

Ora che hai un URL pubblico stabile (con HTTPS), puoi creare l'app FiC senza dover ricambiare il redirect URI.

Vai su [developers.fattureincloud.it](https://developers.fattureincloud.it) → segui [la guida che ti ho dato](#) → come **Redirect URI** usa:

```
https://gestfatture.tuoazienda.it/my-integrations/fic/callback
```

(o `https://gestfatture-xxxx.onrender.com/my-integrations/fic/callback` se non hai dominio custom)

---

## 🔄 Aggiornamenti futuri

Ogni volta che modifichi il codice in locale:

```powershell
git add .
git commit -m "descrizione modifica"
git push
```

Render rileva il push e **deploya automaticamente** in ~2 minuti.

---

## 🩺 Troubleshooting

| Problema | Soluzione |
|---|---|
| `Application failed to respond` | Manca `PORT` env var, controlla `0.0.0.0:$PORT` nel Start Command |
| `OperationalError: no such table` | DB non migrato: nei log cerca "Migrazione" — se manca, riavvia il servizio |
| `RuntimeError: Working outside of application context` | Bug del codice — controlla i log dettagliati |
| Quick-actions email non funzionano dal telefono | Imposta `app_external_url` con URL pubblico HTTPS |
| Solleciti email partono 2 volte | Workers > 1 in gunicorn — verifica `--workers 1` |
| Lento al primo accesso | Sei sul piano free, l'app si addormenta dopo 15min — passa a Starter |

---

## 💾 Backup automatici (Render)

Render fa **snapshot giornalieri automatici** del Persistent Disk sui piani **Starter+**.

Per verificare e gestire:

1. Dashboard Render → service `gestfatture` → tab **Disks**
2. Sotto al disk `gestfatture-data` vedi la sezione **Snapshots**
3. Sono disponibili gli ultimi **7 giorni** di backup (automatico, gratuito)
4. Per **ripristinare** uno snapshot:
   - Click sui 3 puntini accanto allo snapshot → **Restore**
   - Conferma → Render crea un nuovo disco da quello snapshot
   - Ti chiederà di riallineare il servizio sul nuovo disco

⚠️ **Importante**: il backup è del DISCO (database SQLite + uploads PDF). Le impostazioni
in environment variables sono separate (ma non cambiano spesso).

### Backup manuale (opzionale)

Per archivi più lunghi (>7 giorni) puoi:

1. SSH nel container: dashboard → tab **Shell**
2. Comprimi il DB:
   ```bash
   tar -czf /tmp/backup-$(date +%Y%m%d).tar.gz /var/data
   ```
3. Scarica via curl/scp / o usa un cron job che invia su S3

### Disaster recovery rapido

Se il DB è corrotto:
- Tab **Disks** → snapshot più recente → **Restore**
- Render rolla indietro tutto il disco in ~5 minuti
- Le ultime ore di dati possono essere perse (depende quando l'ultimo snapshot)

---

## 💰 Costi mensili totali (produzione)

| Voce | Costo |
|---|---|
| Render Starter (web service) | $7/mese |
| Render Persistent Disk 1GB | incluso |
| Anthropic Claude API | $0–10/mese (a consumo, dipende uso) |
| Custom domain | ~€10/anno = €0.83/mese |
| **TOTALE** | **~$8/mese (€7-8)** |

Per un SaaS B2B che fai pagare €10-30/mese ai tuoi clienti, è un margine ottimo.
