"""Backup settimanale del DB SQLite + uploads/ su S3 (o S3-compatibile).

Configurato via AppSettings (admin). Compatibile con AWS S3, Backblaze B2,
DigitalOcean Spaces, Cloudflare R2, Minio (qualsiasi servizio S3-API).

Job scheduler: ogni lunedì alle 03:00 (Europe/Rome).
Retention: cancella backup più vecchi di N giorni (configurabile, default 30).
"""
import io
import logging
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def _get_config():
    from models import AppSettings
    return {
        "enabled":     AppSettings.get("backup_s3_enabled", "false") == "true",
        "endpoint":    AppSettings.get("backup_s3_endpoint_url", "").strip(),
        "bucket":      AppSettings.get("backup_s3_bucket", "").strip(),
        "key_id":      AppSettings.get("backup_s3_access_key_id", "").strip(),
        "secret":      AppSettings.get("backup_s3_secret_access_key", "").strip(),
        "region":      AppSettings.get("backup_s3_region", "eu-west-1").strip() or "eu-west-1",
        "retention":   int(AppSettings.get("backup_s3_retention_days", "30") or "30"),
        "prefix":      AppSettings.get("backup_s3_prefix", "gestfatture-backups").strip() or "gestfatture-backups",
    }


def _db_path() -> str:
    """Estrae il path del DB SQLite dalla DATABASE_URL."""
    from config import config
    url = config.DATABASE_URL
    if url.startswith("sqlite:///"):
        # sqlite:///relative/path or sqlite:////absolute/path
        return url.replace("sqlite:///", "", 1) if not url.startswith("sqlite:////") else url.replace("sqlite:////", "/", 1)
    raise RuntimeError(f"Backup S3 supporta solo SQLite per ora. URL: {url[:50]}")


def _safe_copy_sqlite(src: str, dst: str):
    """Copia il DB SQLite in modo consistente usando l'API backup nativa."""
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(dst)
    with dst_conn:
        src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.close()


def build_backup_zip(upload_folder: str) -> tuple[bytes, str]:
    """Costruisce un ZIP in memoria contenente DB + uploads. Restituisce (bytes, filename)."""
    db_path = _db_path()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"backup_{timestamp}.zip"

    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Copia consistente del DB
        db_copy = os.path.join(tmp, "invoice_manager.db")
        _safe_copy_sqlite(db_path, db_copy)

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_copy, "invoice_manager.db")

            # 2. Tutti i PDF in uploads/
            if upload_folder and os.path.isdir(upload_folder):
                for fn in os.listdir(upload_folder):
                    p = os.path.join(upload_folder, fn)
                    if os.path.isfile(p):
                        try:
                            zf.write(p, f"uploads/{fn}")
                        except OSError as e:
                            log.warning("Backup: skip %s (%s)", p, e)

            # 3. README
            zf.writestr("README.txt",
                f"GestFatture backup\nGenerato: {datetime.now(timezone.utc).isoformat()}\n"
                f"DB SQLite + cartella uploads/ con i PDF allegati alle fatture.\n")
    buf.seek(0)
    return buf.getvalue(), fname


def _s3_client(cfg):
    import boto3
    from botocore.config import Config as BotoConfig
    kwargs = {
        "aws_access_key_id":     cfg["key_id"],
        "aws_secret_access_key": cfg["secret"],
        "region_name":           cfg["region"],
        "config":                BotoConfig(signature_version="s3v4"),
    }
    if cfg["endpoint"]:
        kwargs["endpoint_url"] = cfg["endpoint"]
    return boto3.client("s3", **kwargs)


def upload_to_s3(data: bytes, filename: str, cfg: dict = None) -> str:
    """Upload del backup su S3. Restituisce la chiave S3 finale."""
    cfg = cfg or _get_config()
    if not cfg["bucket"]:
        raise RuntimeError("Backup S3: bucket non configurato")
    s3 = _s3_client(cfg)
    key = f"{cfg['prefix']}/{filename}"
    s3.put_object(
        Bucket=cfg["bucket"], Key=key, Body=data,
        ContentType="application/zip",
        Metadata={"app": "gestfatture", "type": "weekly_backup"},
    )
    return key


def cleanup_old_backups(cfg: dict = None) -> int:
    """Cancella backup più vecchi di retention_days. Restituisce numero cancellati."""
    cfg = cfg or _get_config()
    if not cfg["bucket"] or cfg["retention"] <= 0:
        return 0
    s3 = _s3_client(cfg)
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["retention"])
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg["bucket"], Prefix=cfg["prefix"] + "/"):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                s3.delete_object(Bucket=cfg["bucket"], Key=obj["Key"])
                deleted += 1
    return deleted


def run_backup(app) -> dict:
    """Esegue backup completo: build ZIP → upload S3 → cleanup vecchi.
    Restituisce dict con esito."""
    with app.app_context():
        cfg = _get_config()
        if not cfg["enabled"]:
            return {"skipped": True, "reason": "backup non abilitato"}
        if not (cfg["bucket"] and cfg["key_id"] and cfg["secret"]):
            return {"error": "configurazione S3 incompleta"}
        from app import get_upload_folder
        try:
            data, fname = build_backup_zip(get_upload_folder())
            key = upload_to_s3(data, fname, cfg)
            n_deleted = cleanup_old_backups(cfg)
            log.info("Backup S3 OK: %s (%d KB) — cleanup=%d", key, len(data) // 1024, n_deleted)
            return {
                "ok": True, "key": key, "size_bytes": len(data),
                "cleaned_up": n_deleted,
            }
        except Exception as e:
            log.exception("Backup S3 fallito: %s", e)
            return {"error": str(e)}


def list_backups(cfg: dict = None) -> list[dict]:
    """Lista backup recenti (ultimi 30) presenti su S3."""
    cfg = cfg or _get_config()
    if not cfg["bucket"]:
        return []
    try:
        s3 = _s3_client(cfg)
        resp = s3.list_objects_v2(Bucket=cfg["bucket"], Prefix=cfg["prefix"] + "/")
        items = resp.get("Contents", [])
        items.sort(key=lambda x: x["LastModified"], reverse=True)
        return [
            {"key": i["Key"], "size": i["Size"], "modified": i["LastModified"]}
            for i in items[:30]
        ]
    except Exception as e:
        log.error("list_backups fallita: %s", e)
        return []
