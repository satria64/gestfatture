"""
Folder Watcher: monitora una cartella locale e importa automaticamente
i nuovi file XML/p7m/PDF/ZIP che vi vengono depositati.

I file processati vengono spostati in <cartella>/processed/ per evitare
import duplicati.
"""

import os
import shutil
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

ACCEPTED_EXT = (".xml", ".p7m", ".pdf", ".zip")


def _get_upload_folder() -> str:
    """Cartella scrivibile per gli allegati delle fatture (uguale a app.py)."""
    folder = os.path.join(os.getcwd(), "uploads")
    os.makedirs(folder, exist_ok=True)
    return folder


def test_folder(path: str) -> tuple[bool, str]:
    """Verifica che la cartella esista e sia scrivibile. (ok, messaggio)."""
    if not path:
        return False, "Percorso non impostato."
    p = Path(path)
    if not p.exists():
        return False, f"La cartella '{path}' non esiste."
    if not p.is_dir():
        return False, f"'{path}' non è una cartella."
    try:
        # prova a creare la subfolder processed/
        (p / "processed").mkdir(exist_ok=True)
        # prova a scrivere un file temp
        test = p / ".gestfatture_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink()
    except Exception as e:
        return False, f"Cartella non scrivibile: {e}"
    return True, "Cartella OK e scrivibile."


def _import_file(file_path: Path, db, user_id: int) -> tuple[int, int, list[str]]:
    """Importa un singolo file assegnandolo all'utente specificato."""
    from import_service import (
        process_pdf_import, process_xml_import,
        process_p7m_import, process_zip_import,
    )
    upload_folder = _get_upload_folder()
    ext  = file_path.suffix.lower().lstrip(".")
    name = file_path.name
    data = file_path.read_bytes()

    if name.lower().endswith(".xml.p7m") or ext == "p7m":
        return process_p7m_import(data, name, db, upload_folder, user_id=user_id)
    if ext == "xml":
        return process_xml_import(data, name, db, upload_folder, user_id=user_id)
    if ext == "pdf":
        return process_pdf_import(data, name, db, upload_folder, user_id=user_id)
    if ext == "zip":
        return process_zip_import(data, name, db, upload_folder, user_id=user_id)
    return 0, 1, [f"{name}: estensione non supportata."]


def sync_for_user(app, user_id: int):
    """Scansiona la cartella di un singolo utente."""
    from models import UserSetting, db
    with app.app_context():
        if UserSetting.get(user_id, "integration_folder_enabled") != "true":
            return
        path = UserSetting.get(user_id, "integration_folder_path", "").strip()
        if not path:
            return

        p = Path(path)
        if not p.exists() or not p.is_dir():
            log.warning("Folder watcher [u=%d]: cartella inesistente '%s'", user_id, path)
            return

        processed_dir = p / "processed"
        processed_dir.mkdir(exist_ok=True)

        total_ok = total_skip = 0
        for f in p.iterdir():
            if not f.is_file():
                continue
            if not f.name.lower().endswith(ACCEPTED_EXT) and not f.name.lower().endswith(".xml.p7m"):
                continue

            log.info("Folder watcher [u=%d]: import '%s'", user_id, f.name)
            try:
                n_ok, n_skip, _ = _import_file(f, db, user_id)
                total_ok  += n_ok
                total_skip += n_skip
            except Exception as e:
                log.error("Errore import '%s': %s", f.name, e)
                continue

            dst = processed_dir / f.name
            if dst.exists():
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                dst = processed_dir / f"{f.stem}_{stamp}{f.suffix}"
            try:
                shutil.move(str(f), str(dst))
            except Exception as e:
                log.error("Spostamento di '%s' fallito: %s", f.name, e)

        if total_ok or total_skip:
            log.info("Folder watcher [u=%d]: %d importate, %d saltate.", user_id, total_ok, total_skip)
        UserSetting.set(user_id, "integration_folder_last_sync", datetime.utcnow().isoformat())


def sync(app):
    """Job periodico: esegue la sync per TUTTI gli utenti che hanno la folder watcher abilitata."""
    from models import User
    with app.app_context():
        users = User.query.all()
    for u in users:
        try:
            sync_for_user(app, u.id)
        except Exception as e:
            log.error("Folder watcher u=%d: %s", u.id, e)
