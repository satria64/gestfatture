"""Migrazione database: aggiunge le colonne mancanti al DB esistente."""
import sqlite3, os

db_path = os.path.join(os.path.dirname(__file__), "invoice_manager.db")
conn = sqlite3.connect(db_path)
cur  = conn.cursor()

def add_column(table, col, col_type):
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        cur.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_type}')
        print(f"  + {table}.{col}")
    else:
        print(f"  = {table}.{col} (già presente)")

print("Migrazione in corso...")
add_column("invoices", "payment_ref", "TEXT DEFAULT ''")
add_column("users",    "id",          "INTEGER PRIMARY KEY")  # tabella creata da SQLAlchemy, skip se esiste

conn.commit()
conn.close()
print("Migrazione completata.")
