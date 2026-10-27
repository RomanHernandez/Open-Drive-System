#!/usr/bin/env python3
"""Inventario automático de Google Drive (1-3 cuentas) -> SQLite.

Uso:
  drivedata.py add-account NOMBRE     # autoriza una cuenta (abre el navegador)
  drivedata.py sync [--account NOMBRE]  # indexa metadatos de Drive
  drivedata.py stats                  # resumen por cuenta/tipo
  drivedata.py search TEXTO           # busca por nombre
  drivedata.py duplicates             # archivos duplicados (mismo md5)
  drivedata.py install-schedule [--hour 3]  # programa sync diario (launchd)
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

BASE = Path(__file__).resolve().parent
TOKENS = BASE / "tokens"
CREDS = BASE / "credentials.json"
DB = BASE / "drive.db"
WRITE_SCOPE = "https://www.googleapis.com/auth/drive"
# Se pide permiso completo para poder mandar archivos a la papelera desde el panel.
SCOPES = [WRITE_SCOPE]
FIELDS = ("nextPageToken, files(id,name,mimeType,size,md5Checksum,createdTime,"
          "modifiedTime,trashed,starred,shared,parents,owners(emailAddress),"
          "webViewLink)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  account TEXT, id TEXT, name TEXT, mime TEXT, size INTEGER, md5 TEXT,
  created TEXT, modified TEXT, trashed INTEGER, starred INTEGER, shared INTEGER,
  parent TEXT, owner TEXT, link TEXT, seen_run INTEGER, deleted INTEGER DEFAULT 0,
  PRIMARY KEY(account, id));
CREATE INDEX IF NOT EXISTS idx_md5 ON files(md5);
CREATE INDEX IF NOT EXISTS idx_name ON files(name);
CREATE TABLE IF NOT EXISTS runs(
  run INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, started TEXT,
  finished TEXT, files INTEGER);
"""


def db():
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def get_service(account):
    tok = TOKENS / f"{account}.json"
    if not tok.exists():
        sys.exit(f"Cuenta '{account}' no autorizada. Ejecuta: add-account {account}")
    creds = Credentials.from_authorized_user_file(str(tok))  # usa los scopes guardados
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
            tok.write_text(creds.to_json())
        else:
            sys.exit(f"Token expirado para '{account}'. Repite add-account.")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def can_write(account):
    """True si el token de la cuenta incluye permiso para modificar archivos."""
    import json
    tok = TOKENS / f"{account}.json"
    try:
        return WRITE_SCOPE in json.loads(tok.read_text()).get("scopes", [])
    except Exception:
        return False


def cmd_add(a):
    if not CREDS.exists():
        sys.exit("Falta credentials.json (ver README.md, paso 1).")
    TOKENS.mkdir(exist_ok=True)
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
    creds = flow.run_local_server(port=0)
    (TOKENS / f"{a.name}.json").write_text(creds.to_json())
    TOKENS.chmod(0o700)
    print(f"Cuenta '{a.name}' autorizada.")


def sync_account(con, account):
    svc = get_service(account)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    run = con.execute("INSERT INTO runs(account,started) VALUES(?,?)",
                      (account, started)).lastrowid
    token, n = None, 0
    while True:
        for attempt in range(5):
            try:
                resp = svc.files().list(pageSize=1000, pageToken=token, fields=FIELDS,
                                        q="'me' in owners or sharedWithMe = true",
                                        includeItemsFromAllDrives=False).execute()
                break
            except Exception as e:  # reintento con backoff (cuota/red)
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        rows = []
        for f in resp.get("files", []):
            rows.append((account, f["id"], f.get("name"), f.get("mimeType"),
                         int(f["size"]) if f.get("size") else None,
                         f.get("md5Checksum"), f.get("createdTime"),
                         f.get("modifiedTime"), int(f.get("trashed", False)),
                         int(f.get("starred", False)), int(f.get("shared", False)),
                         (f.get("parents") or [None])[0],
                         (f.get("owners") or [{}])[0].get("emailAddress"),
                         f.get("webViewLink"), run))
        con.executemany(
            "INSERT OR REPLACE INTO files(account,id,name,mime,size,md5,created,"
            "modified,trashed,starred,shared,parent,owner,link,seen_run) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        n += len(rows)
        print(f"  [{account}] {n} archivos...", end="\r")
        token = resp.get("nextPageToken")
        if not token:
            break
    # los que no aparecieron en esta corrida fueron eliminados definitivamente
    con.execute("UPDATE files SET deleted=1 WHERE account=? AND seen_run<>?",
                (account, run))
    con.execute("UPDATE runs SET finished=?, files=? WHERE run=?",
                (time.strftime("%Y-%m-%d %H:%M:%S"), n, run))
    con.commit()
    print(f"  [{account}] {n} archivos indexados.      ")


def cmd_sync(a):
    con = db()
    accounts = [a.account] if a.account else sorted(p.stem for p in TOKENS.glob("*.json"))
    if not accounts:
        sys.exit("No hay cuentas. Ejecuta add-account NOMBRE.")
    for acc in accounts:
        sync_account(con, acc)


def fmt(b):
    b = b or 0
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"


def cmd_stats(_):
    con = db()
    print("== Por cuenta ==")
    for acc, n, s, tr, sh in con.execute(
            "SELECT account,COUNT(*),SUM(size),SUM(trashed),SUM(shared) FROM files "
            "WHERE deleted=0 GROUP BY account"):
        print(f"{acc}: {n} archivos, {fmt(s)}, {tr} en papelera, {sh} compartidos")
    print("\n== Top tipos por tamaño ==")
    for mime, n, s in con.execute(
            "SELECT mime,COUNT(*),SUM(size) s FROM files WHERE deleted=0 "
            "GROUP BY mime ORDER BY s DESC NULLS LAST LIMIT 10"):
        print(f"{mime}: {n} ({fmt(s)})")
    print("\n== Última sincronización ==")
    for acc, fin in con.execute(
            "SELECT account,MAX(finished) FROM runs GROUP BY account"):
        print(f"{acc}: {fin}")


def cmd_search(a):
    for acc, name, mime, mod, link in db().execute(
            "SELECT account,name,mime,modified,link FROM files "
            "WHERE deleted=0 AND name LIKE ? ORDER BY modified DESC LIMIT 50",
            (f"%{a.text}%",)):
        print(f"[{acc}] {name} ({mime}) {mod}\n    {link}")


def cmd_dups(_):
    con = db()
    for md5, n, s in con.execute(
            "SELECT md5,COUNT(*),MAX(size) FROM files WHERE deleted=0 AND trashed=0 "
            "AND md5 IS NOT NULL GROUP BY md5 HAVING COUNT(*)>1 "
            "ORDER BY MAX(size)*COUNT(*) DESC LIMIT 30"):
        print(f"\n{n} copias ({fmt(s)} c/u):")
        for acc, name, link in con.execute(
                "SELECT account,name,link FROM files WHERE md5=? AND deleted=0", (md5,)):
            print(f"  [{acc}] {name}  {link}")


def cmd_schedule(a):
    plist = Path.home() / "Library/LaunchAgents/com.drivedata.sync.plist"
    py = BASE / ".venv/bin/python"
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.drivedata.sync</string>
<key>ProgramArguments</key><array><string>{py}</string><string>{BASE / 'drivedata.py'}</string><string>sync</string></array>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{a.hour}</integer><key>Minute</key><integer>0</integer></dict>
<key>StandardOutPath</key><string>{BASE / 'sync.log'}</string>
<key>StandardErrorPath</key><string>{BASE / 'sync.log'}</string>
</dict></plist>
""")
    print(f"Creado {plist}\nActívalo con: launchctl load {plist}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    x = s.add_parser("add-account"); x.add_argument("name"); x.set_defaults(f=cmd_add)
    x = s.add_parser("sync"); x.add_argument("--account"); x.set_defaults(f=cmd_sync)
    s.add_parser("stats").set_defaults(f=cmd_stats)
    x = s.add_parser("search"); x.add_argument("text"); x.set_defaults(f=cmd_search)
    s.add_parser("duplicates").set_defaults(f=cmd_dups)
    x = s.add_parser("install-schedule"); x.add_argument("--hour", type=int, default=3)
    x.set_defaults(f=cmd_schedule)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
