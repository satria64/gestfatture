"""Bandi di finanziamento: scraping AI da portali pubblici + matching AI con
profilo utente. MVP: 2-3 sources note, sync giornaliero, niente API a pagamento.
"""
import json
import logging
import re
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


# Sources iniziali — espandibile con un AppSettings dedicato in futuro
DEFAULT_SOURCES = [
    {
        "name":   "mimit",
        "label":  "MIMIT — Ministero delle Imprese",
        "url":    "https://www.mimit.gov.it/it/incentivi",
        "region": "Italia",
    },
    {
        "name":   "invitalia",
        "label":  "Invitalia",
        "url":    "https://www.invitalia.it/cosa-facciamo/incentivi-e-strumenti",
        "region": "Italia",
    },
]


SCRAPING_PROMPT = """Sei un assistente che aiuta una piccola azienda italiana
a trovare bandi di finanziamento attivi (contributi, agevolazioni, crediti
d'imposta).

Ti viene dato il TESTO ESTRATTO da una pagina web di un ente che pubblica bandi.
Estrai TUTTI i bandi distinti che riesci a identificare in questa pagina.
Restituisci SOLO un array JSON valido, senza markdown, senza testo extra.

Schema per ciascun bando:
{{
  "external_id":  "URL completo o ID univoco del bando",
  "title":        "titolo breve (max 200 char)",
  "ente":         "ente erogatore (es. 'MIMIT', 'Camera di Commercio Milano', 'Invitalia', 'Regione Lombardia')",
  "region":       "Italia | <nome regione> (default 'Italia' se nazionale)",
  "category":     "innovazione | digitalizzazione | internazionalizzazione | turismo | export | green | startup | ricerca | formazione | altro",
  "deadline":     "YYYY-MM-DD oppure null se non specificata",
  "amount_max":   numero in euro oppure null,
  "description":  "2-4 frasi sintetiche su cosa finanzia",
  "requirements": "requisiti chiave (1-3 frasi)",
  "target_size":  "micro | pmi | all (default 'all')",
  "ateco_hints":  ["lista", "settori", "o", "codici", "ATECO", "se", "noti"],
  "url":          "link al bando ufficiale (lascia null se non sicuro)"
}}

REGOLE:
- IGNORA: news generiche, articoli di commento, eventi, comunicati senza bando.
- PRENDI: bandi/avvisi/incentivi attivi o in scadenza con riferimento a contributi.
- Se trovi 0 bandi rilevanti, restituisci array vuoto: [].
- Non inventare URL: se l'unica info è il titolo, lascia url=null.
- Risposta in italiano, JSON puro: nessun ```, nessuna spiegazione.

PAGINA WEB:
URL:    {url}
SOURCE: {source_name}

CONTENUTO TESTUALE (estratto da HTML):
\"\"\"
{text}
\"\"\"
"""


MATCH_PROMPT = """Stai aiutando un'azienda italiana a capire se questo bando
di finanziamento è rilevante per lei.

PROFILO DELL'AZIENDA:
- Nome: {company}
- Codice ATECO: {ateco}
- Regione: {region}
- Dimensione: {size}
- Descrizione attività: {description}

BANDO:
- Titolo: {bando_title}
- Ente: {bando_ente}
- Regione: {bando_region}
- Categoria: {bando_category}
- Target dimensione: {bando_target_size}
- Settori (hint): {bando_ateco}
- Descrizione: {bando_description}
- Requisiti: {bando_requirements}

Restituisci SOLO JSON, senza markdown:
{{
  "score":  numero 0-100 (rilevanza per QUESTA azienda),
  "reason": "una frase max 25 parole che spiega perché"
}}

Linee guida punteggio:
- 80-100: perfettamente in target (settore + regione + dimensione coerenti)
- 50-79:  parzialmente in target (es. settore giusto ma altra regione, o requisiti elastici)
- 20-49:  marginale (settore vicino, dimensione compatibile)
- 0-19:   non pertinente

Se l'azienda ha profilo vuoto o insufficiente, score=30 e reason="profilo incompleto, valuta tu".
"""


def _strip_codefence(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*\n", "", s)
        s = re.sub(r"\n```\s*$", "", s)
    return s.strip()


def _fetch_url(url: str, timeout: int = 25) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GestFatture/1.0; +https://gestfatture.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def _html_to_text(html: str, max_chars: int = 28000) -> str:
    """Estrae solo il testo significativo da HTML, rimuovendo nav/footer/script."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                     "svg", "noscript", "iframe"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def scrape_source(source: dict, api_key: str, model: str) -> list[dict]:
    """Scarica una source e usa Claude per estrarre la lista di bandi.
    Solleva eccezione se la fetch o l'API falliscono."""
    log.info("Bandi scraping: %s (%s)", source["label"], source["url"])
    html = _fetch_url(source["url"])
    text = _html_to_text(html)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    prompt = SCRAPING_PROMPT.format(
        url=source["url"],
        source_name=source["name"],
        text=text,
    )
    msg = client.messages.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_codefence(msg.content[0].text)
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("JSON parse fallito per %s: %s | raw[:300]=%r",
                    source["name"], e, raw[:300])
        return []
    if not isinstance(result, list):
        return []
    return result


def upsert_bando(db, source: str, item: dict):
    """Inserisce o aggiorna un Bando. Restituisce (Bando, was_new)."""
    from models import Bando
    external_id = item.get("external_id") or item.get("url") or item.get("title", "")[:200]
    if not external_id or not item.get("title"):
        return None, False

    bando = Bando.query.filter_by(source=source, external_id=external_id).first()
    was_new = bando is None
    if was_new:
        bando = Bando(source=source, external_id=external_id[:200])
        db.session.add(bando)

    bando.title        = (item.get("title") or "")[:500]
    bando.ente         = (item.get("ente") or "")[:255]
    bando.region       = (item.get("region") or "Italia")[:80]
    bando.category     = (item.get("category") or "altro")[:120]
    bando.description  = item.get("description") or ""
    bando.requirements = item.get("requirements") or ""
    bando.target_size  = (item.get("target_size") or "all")[:40]
    bando.ateco_hints  = json.dumps(item.get("ateco_hints", []), ensure_ascii=False)
    bando.url          = (item.get("url") or "")[:800] or None
    bando.is_active    = True
    bando.last_seen_at = datetime.utcnow()

    if item.get("deadline"):
        try:
            bando.deadline = datetime.strptime(item["deadline"], "%Y-%m-%d").date()
        except Exception:
            pass

    if item.get("amount_max") is not None:
        try:
            bando.amount_max = float(item["amount_max"])
        except Exception:
            pass

    return bando, was_new


def sync_all_sources(db, api_key: str, model: str) -> dict:
    """Esegue scraping di tutte le sources e aggiorna il DB. Restituisce stats."""
    stats = {"new": 0, "updated": 0, "errors": 0, "by_source": {}}
    for source in DEFAULT_SOURCES:
        sname = source["name"]
        try:
            items = scrape_source(source, api_key, model)
            n_new = 0
            n_upd = 0
            for item in items:
                _, was_new = upsert_bando(db, sname, item)
                if was_new:
                    n_new += 1
                else:
                    n_upd += 1
            db.session.commit()
            stats["new"] += n_new
            stats["updated"] += n_upd
            stats["by_source"][sname] = {"new": n_new, "updated": n_upd, "total": len(items)}
        except Exception as e:
            log.error("Bandi scraping %s fallito: %s", sname, e)
            try:
                db.session.rollback()
            except Exception:
                pass
            stats["errors"] += 1
            stats["by_source"][sname] = {"error": str(e)[:200]}
    return stats


def match_user_to_bando(user, bando, api_key: str, model: str) -> tuple[int, str]:
    """Calcola la rilevanza (0-100, reason) di un bando per un utente."""
    from models import UserSetting
    uid = user.id
    profile = {
        "company":     UserSetting.get(uid, "company_name") or user.username,
        "ateco":       UserSetting.get(uid, "user_ateco_code") or "non specificato",
        "region":      UserSetting.get(uid, "user_region") or "non specificata",
        "size":        UserSetting.get(uid, "user_company_size") or "non specificata",
        "description": UserSetting.get(uid, "user_business_description") or "non specificata",
    }

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    prompt = MATCH_PROMPT.format(
        company=profile["company"],
        ateco=profile["ateco"],
        region=profile["region"],
        size=profile["size"],
        description=profile["description"],
        bando_title=bando.title,
        bando_ente=bando.ente or "",
        bando_region=bando.region or "Italia",
        bando_category=bando.category or "",
        bando_target_size=bando.target_size or "all",
        bando_ateco=", ".join(bando.ateco_hints_list),
        bando_description=bando.description or "",
        bando_requirements=bando.requirements or "",
    )
    msg = client.messages.create(
        model=model,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_codefence(msg.content[0].text)
    try:
        result = json.loads(raw)
        score = max(0, min(100, int(result.get("score", 0))))
        reason = (result.get("reason") or "")[:500]
        return score, reason
    except Exception as e:
        log.warning("JSON parse fallito su match: %s | raw=%r", e, raw[:200])
        return 0, "(errore parsing AI)"


def compute_matches_for_user(db, user, api_key: str, model: str) -> int:
    """Aggiorna i match per un utente su tutti i bandi attivi.
    Salta i bandi già scoreggiati di recente (cache: skip se match.created_at >= bando.last_seen_at)."""
    from models import Bando, BandoMatch
    bandi = Bando.query.filter_by(is_active=True).all()
    n = 0
    for b in bandi:
        match = BandoMatch.query.filter_by(user_id=user.id, bando_id=b.id).first()
        if match and match.created_at and b.last_seen_at and match.created_at >= b.last_seen_at:
            continue
        try:
            score, reason = match_user_to_bando(user, b, api_key, model)
        except Exception as e:
            log.error("match_user_to_bando u=%d b=%d: %s", user.id, b.id, e)
            continue
        if match is None:
            match = BandoMatch(user_id=user.id, bando_id=b.id)
            db.session.add(match)
        match.relevance_score = score
        match.reason = reason
        n += 1
    db.session.commit()
    return n
