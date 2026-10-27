#!/usr/bin/env python3
"""Panel web local estilo Synapse para drivedata. Abre http://127.0.0.1:8765

- Explorador visual con árbol de carpetas y tarjetas azules translúcidas
- Resumen, archivos grandes y duplicados -> mandar a la papelera de Drive (reversible)
- Archivos descargados en el Mac (Drive para escritorio) -> mostrar en Finder para quitar la descarga
"""
from collections import defaultdict
import glob
import os
import subprocess
import threading
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, Response

import drivedata as dd

app = Flask(__name__)
DATALESS = 0x40000000  # SF_DATALESS: archivo en la nube, sin copia local


def get_cloud_roots():
    return [Path(p).resolve() for p in
            glob.glob(str(Path.home() / "Library/CloudStorage/GoogleDrive-*"))]


@app.before_request
def guard():
    # Solo accesible desde este equipo y protección básica contra CSRF.
    if request.method == "POST":
        if request.headers.get("X-Panel") != "1":
            return jsonify(error="forbidden"), 403
        origin = request.headers.get("Origin", "")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            return jsonify(error="forbidden"), 403


def rows(sql, args=()):
    con = dd.db()
    con.row_factory = lambda c, r: {d[0]: r[i] for i, d in enumerate(c.description)}
    return con.execute(sql, args).fetchall()


def compute_folder_metrics():
    all_rows = rows("SELECT account, id, name, mime, size, parent FROM files WHERE deleted=0 AND trashed=0")
    by_id = {}
    children = defaultdict(list)
    for r in all_rows:
        key = (r["account"], r["id"])
        by_id[key] = r
        if r["parent"]:
            children[(r["account"], r["parent"])].append(key)

    memo = {}

    def calc(key, depth=0):
        if key in memo:
            return memo[key]
        if depth > 60:
            return (0, 0)
        f_count = 0
        b_sum = 0
        for ch in children.get(key, []):
            ch_row = by_id.get(ch)
            if not ch_row:
                continue
            if ch_row["mime"] == "application/vnd.google-apps.folder":
                cf, cb = calc(ch, depth + 1)
                f_count += cf
                b_sum += cb
            else:
                f_count += 1
                b_sum += ch_row["size"] or 0
        memo[key] = (f_count, b_sum)
        return (f_count, b_sum)

    for key, r in by_id.items():
        if r["mime"] == "application/vnd.google-apps.folder":
            calc(key)

    return by_id, children, memo


@app.get("/api/overview")
def overview():
    roots = get_cloud_roots()
    accounts = rows("SELECT account, "
                    "SUM(CASE WHEN mime<>'application/vnd.google-apps.folder' AND trashed=0 THEN 1 ELSE 0 END) files, "
                    "SUM(CASE WHEN mime='application/vnd.google-apps.folder' AND trashed=0 THEN 1 ELSE 0 END) folders, "
                    "COALESCE(SUM(CASE WHEN trashed=0 THEN size ELSE 0 END),0) bytes, "
                    "SUM(trashed) trashed, SUM(shared) shared FROM files "
                    "WHERE deleted=0 GROUP BY account")
    for a in accounts:
        a["can_write"] = dd.can_write(a["account"])

    types = rows("SELECT mime, COUNT(*) n, COALESCE(SUM(size),0) bytes FROM files "
                 "WHERE deleted=0 AND trashed=0 AND mime<>'application/vnd.google-apps.folder' "
                 "GROUP BY mime ORDER BY bytes DESC LIMIT 8")
    last = rows("SELECT account, MAX(finished) finished FROM runs GROUP BY account")

    dup_groups = rows("SELECT COUNT(*) c FROM (SELECT md5 FROM files WHERE deleted=0 AND trashed=0 "
                      "AND md5 IS NOT NULL GROUP BY md5 HAVING COUNT(*)>1)")
    dup_count = dup_groups[0]["c"] if dup_groups else 0

    large_cnt = rows("SELECT COUNT(*) c FROM files WHERE deleted=0 AND trashed=0 AND size>=1048576")
    large_count = large_cnt[0]["c"] if large_cnt else 0

    by_id, _, memo = compute_folder_metrics()
    top_folders = []
    for (acc, fid), r in by_id.items():
        if r["mime"] == "application/vnd.google-apps.folder":
            parent_id = r["parent"]
            if not parent_id or (acc, parent_id) not in by_id:
                fc, bs = memo.get((acc, fid), (0, 0))
                top_folders.append({
                    "account": acc,
                    "id": fid,
                    "name": r["name"],
                    "files": fc,
                    "bytes": bs
                })
    top_folders.sort(key=lambda x: (x["bytes"], x["files"]), reverse=True)

    return jsonify(accounts=accounts, types=types, last=last,
                   top_folders=top_folders[:30],
                   dup_count=dup_count, large_count=large_count,
                   local_available=bool(roots))


@app.get("/api/browse")
def browse():
    acc = request.args.get("account", "")
    folder_id = request.args.get("folder_id", "")
    q = request.args.get("q", "").strip().lower()

    by_id, children, memo = compute_folder_metrics()
    all_items = rows("SELECT account, id, name, mime, size, modified, owner, link, parent, shared "
                     "FROM files WHERE deleted=0 AND trashed=0")

    folders_out = []
    files_out = []
    breadcrumbs = []

    if q:
        for r in all_items:
            if acc and r["account"] != acc:
                continue
            if q in (r["name"] or "").lower():
                if r["mime"] == "application/vnd.google-apps.folder":
                    fc, bs = memo.get((r["account"], r["id"]), (0, 0))
                    folders_out.append({**r, "files": fc, "bytes": bs})
                else:
                    files_out.append(r)
    elif folder_id and acc:
        curr = (acc, folder_id)
        seen = 0
        while curr in by_id and seen < 40:
            row = by_id[curr]
            breadcrumbs.append({"account": acc, "id": row["id"], "name": row["name"]})
            curr = (acc, row["parent"])
            seen += 1
        breadcrumbs.reverse()

        for r in all_items:
            if r["account"] == acc and r["parent"] == folder_id:
                if r["mime"] == "application/vnd.google-apps.folder":
                    fc, bs = memo.get((r["account"], r["id"]), (0, 0))
                    folders_out.append({**r, "files": fc, "bytes": bs})
                else:
                    files_out.append(r)
    else:
        for r in all_items:
            if acc and r["account"] != acc:
                continue
            p_in_db = r["parent"] and (r["account"], r["parent"]) in by_id
            if not p_in_db:
                if r["mime"] == "application/vnd.google-apps.folder":
                    fc, bs = memo.get((r["account"], r["id"]), (0, 0))
                    folders_out.append({**r, "files": fc, "bytes": bs})
                else:
                    files_out.append(r)

        if not files_out:
            recent = [r for r in all_items
                      if r["mime"] != "application/vnd.google-apps.folder"
                      and (not acc or r["account"] == acc)]
            recent.sort(key=lambda x: x["modified"] or "", reverse=True)
            files_out = recent[:60]

    folders_out.sort(key=lambda x: (x["bytes"], x["files"]), reverse=True)
    files_out.sort(key=lambda x: x["modified"] or "", reverse=True)

    return jsonify(folders=folders_out[:60], files=files_out[:250], breadcrumbs=breadcrumbs)


@app.get("/api/large")
def large():
    min_mb = float(request.args.get("min_mb", 1))
    acc = request.args.get("account")
    sql = ("SELECT account,id,name,mime,size,modified,owner,link FROM files WHERE deleted=0 "
           "AND trashed=0 AND mime<>'application/vnd.google-apps.folder' AND size>=? ")
    args = [min_mb * 1024 * 1024]
    if acc:
        sql += "AND account=? "
        args.append(acc)
    return jsonify(rows(sql + "ORDER BY size DESC LIMIT 200", args))


@app.get("/api/duplicates")
def duplicates():
    groups = []
    for g in rows("SELECT md5, COUNT(*) n, MAX(size) size FROM files WHERE deleted=0 "
                  "AND trashed=0 AND md5 IS NOT NULL GROUP BY md5 HAVING n>1 "
                  "ORDER BY size*(n-1) DESC LIMIT 100"):
        g["files"] = rows("SELECT account,id,name,mime,modified,owner,link FROM files "
                          "WHERE md5=? AND deleted=0 AND trashed=0 ORDER BY modified",
                          (g["md5"],))
        groups.append(g)
    return jsonify(groups)


@app.post("/api/trash")
def trash():
    items = (request.get_json() or {}).get("items", [])
    done, errors = 0, []
    con = dd.db()
    for it in items:
        acc, fid = it.get("account"), it.get("id")
        if not dd.can_write(acc):
            errors.append(f"{acc}: sin permiso de escritura, repite add-account {acc}")
            continue
        try:
            dd.get_service(acc).files().update(fileId=fid, body={"trashed": True}).execute()
            con.execute("UPDATE files SET trashed=1 WHERE account=? AND id=?", (acc, fid))
            done += 1
        except SystemExit as e:
            errors.append(str(e))
        except Exception as e:
            errors.append(f"{fid}: {e}")
    con.commit()
    return jsonify(done=done, errors=sorted(set(errors)))


@app.post("/api/sync")
def sync():
    con = dd.db()
    for p in sorted(dd.TOKENS.glob("*.json")):
        dd.sync_account(con, p.stem)
    return jsonify(ok=True)


def scan_local(limit=500):
    roots = get_cloud_roots()
    files, total, cloud_only = [], 0, 0
    for root in roots:
        acc_label = root.name.replace("GoogleDrive-", "")
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n == ".DS_Store" or n.startswith("Icon"):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if st.st_flags & DATALESS:
                    cloud_only += 1
                    continue
                local_bytes = st.st_blocks * 512
                if local_bytes > 0:
                    total += local_bytes
                    files.append({
                        "path": p,
                        "name": n,
                        "bytes": local_bytes,
                        "account": acc_label,
                        "folder": os.path.relpath(dirpath, root),
                        "modified": st.st_mtime
                    })
    files.sort(key=lambda f: f["bytes"], reverse=True)
    return {"roots": [str(r) for r in roots], "total": total,
            "cloud_only": cloud_only, "count": len(files), "files": files[:limit]}


@app.get("/api/local")
def local():
    return jsonify(scan_local())


@app.post("/api/reveal")
def reveal():
    """Muestra los archivos en Finder (macOS no ofrece una API para quitar la
    descarga de Drive; se hace en Finder con clic derecho > Quitar descarga)."""
    roots = get_cloud_roots()
    paths = (request.get_json() or {}).get("paths", [])[:20]
    ok = []
    for p in paths:
        real = Path(p).resolve()
        if any(r in real.parents for r in roots) and real.exists():
            ok.append(str(real))
    if ok:
        subprocess.run(["open", "-R", *ok])
    return jsonify(shown=len(ok))


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Drive Archotype Mac</title><style>
:root {
  --outer: #e5e6eb;
  --shell: #f5f6f8;
  --panel: #ffffff;
  --card: #f8f9fb;
  --card-hover: #f1f4f9;
  --ink: #14171f;
  --ink-soft: #454b57;
  --mut: #858c9b;
  --line: #eceef2;
  --pri: #2f6feb;
  --red: #e5484d;
  --chk: #181a20;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", Segoe UI, sans-serif;
  background: var(--outer);
  color: var(--ink);
  height: 100vh;
  padding: 16px;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
}
.shell {
  display: grid;
  grid-template-columns: 262px 1fr;
  height: 100%;
  background: var(--shell);
  border-radius: 24px;
  padding: 10px;
  gap: 8px;
  box-shadow: 0 24px 60px rgba(15, 23, 42, 0.08), 0 0 0 1px rgba(255,255,255,0.7) inset;
  transition: grid-template-columns .22s ease;
}
.shell.collapsed { grid-template-columns: 68px 1fr; }
.shell.collapsed .hide-col { display: none !important; }
.shell.collapsed .brand { justify-content: center; padding: 14px 4px; }
.shell.collapsed .nav-item { justify-content: center; padding: 10px 6px; }

/* SIDEBAR */
aside {
  display: flex;
  flex-direction: column;
  padding: 8px 10px 12px;
  overflow-y: auto;
  user-select: none;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 10px 18px;
}
.brand-title {
  font-weight: 700;
  font-size: 16px;
  letter-spacing: -0.02em;
  flex: 1;
}
.icon-btn {
  border: 0;
  background: transparent;
  color: var(--mut);
  cursor: pointer;
  padding: 6px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.icon-btn:hover { background: rgba(0,0,0,0.05); color: var(--ink); }

.sec-label {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--mut);
  padding: 14px 12px 6px;
  letter-spacing: 0.01em;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 10px;
  color: var(--ink-soft);
  cursor: pointer;
  font-weight: 500;
  font-size: 13.5px;
  transition: background .12s, color .12s, box-shadow .12s;
  margin-bottom: 2px;
}
.nav-item:hover { background: rgba(0,0,0,0.035); color: var(--ink); }
.nav-item.active {
  background: var(--panel);
  color: var(--ink);
  font-weight: 600;
  box-shadow: 0 2px 7px rgba(15, 23, 42, 0.05), 0 0 0 1px rgba(0,0,0,0.03);
}
.nav-item .cnt {
  margin-left: auto;
  font-size: 12px;
  color: var(--mut);
  font-weight: 500;
}
.nav-item svg { width: 16px; height: 16px; flex-shrink: 0; opacity: 0.78; }
.nav-item.active svg { opacity: 1; }

.tree-group { margin-bottom: 2px; }
.tree-children {
  margin-left: 19px;
  padding-left: 9px;
  border-left: 1px solid #e2e5ec;
  margin-top: 2px;
  margin-bottom: 4px;
}
.chev {
  width: 14px;
  height: 14px;
  transition: transform .15s ease;
  color: var(--mut);
}
.chev.open { transform: rotate(90deg); }

.tag-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  margin: 0 4px;
}

/* MAIN PANEL */
main {
  background: var(--panel);
  border-radius: 18px;
  box-shadow: 0 2px 12px rgba(15, 23, 42, 0.03), 0 0 0 1px rgba(0,0,0,0.035);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 30px 12px;
  border-bottom: 1px solid var(--line);
  gap: 16px;
}
.crumbs {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
  color: var(--mut);
  flex-wrap: wrap;
}
.crumbs span.crumb {
  cursor: pointer;
  color: var(--mut);
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.crumbs span.crumb:hover { color: var(--ink); }
.crumbs span.crumb.current { color: var(--ink-soft); font-weight: 500; }
.crumbs .dot { color: #c6cbd6; }

.top-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.search-box {
  display: flex;
  align-items: center;
  gap: 7px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 6px 11px;
  width: 220px;
  transition: border-color .15s, background .15s;
}
.search-box:focus-within {
  background: #fff;
  border-color: #c2cadb;
}
.search-box input {
  border: 0;
  background: transparent;
  outline: none;
  font-size: 13px;
  width: 100%;
  color: var(--ink);
}
.btn-sync {
  border: 1px solid var(--line);
  background: #fff;
  color: var(--ink);
  font-weight: 500;
  font-size: 13px;
  padding: 7px 13px;
  border-radius: 10px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.03);
}
.btn-sync:hover { background: var(--card); }
.btn-sync:disabled { opacity: 0.55; cursor: default; }

.content {
  padding: 22px 30px 90px;
  overflow-y: auto;
  flex: 1;
}

.title-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 22px;
  gap: 16px;
  flex-wrap: wrap;
}
.title-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.title-left h1 {
  font-size: 20px;
  font-weight: 650;
  margin: 0;
  letter-spacing: -0.02em;
}
.divider-v {
  width: 1px;
  height: 18px;
  background: var(--line);
}
.view-pills {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.pill-btn {
  border: 0;
  background: transparent;
  color: var(--mut);
  font-size: 13px;
  font-weight: 500;
  padding: 7px 12px;
  border-radius: 10px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.pill-btn:hover { color: var(--ink); background: rgba(0,0,0,0.03); }
.pill-btn.on {
  background: #f2f4f8;
  color: var(--ink);
  font-weight: 600;
}

.subhead {
  font-size: 14px;
  font-weight: 600;
  margin: 4px 0 14px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.subhead span { color: var(--mut); font-weight: 500; }

/* FOLDER CARDS */
.folder-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(205px, 1fr));
  gap: 16px;
  margin-bottom: 28px;
}
.folder-card {
  background: var(--card);
  border: 1px solid #eef0f5;
  border-radius: 18px;
  padding: 20px 16px 18px;
  text-align: center;
  cursor: pointer;
  position: relative;
  transition: transform .16s ease, box-shadow .16s ease, background .16s ease;
}
.folder-card:hover {
  background: #fff;
  transform: translateY(-2px);
  box-shadow: 0 12px 26px rgba(15, 23, 42, 0.06), 0 0 0 1px rgba(0,0,0,0.04);
}
.folder-card .card-badge {
  position: absolute;
  top: 12px;
  right: 12px;
  font-size: 10.5px;
  color: var(--mut);
  background: rgba(255,255,255,0.85);
  padding: 2px 7px;
  border-radius: 999px;
  border: 1px solid var(--line);
}
.folder-card .card-dot {
  position: absolute;
  top: 12px;
  left: 12px;
  width: 15px;
  height: 15px;
  border-radius: 5px;
  background: #fff;
  border: 1px solid #e1e4ec;
}
.folder-svg {
  width: 86px;
  height: 70px;
  margin: 6px auto 12px;
  display: block;
  filter: drop-shadow(0 8px 14px rgba(59, 130, 246, 0.22));
}
.folder-name {
  font-weight: 600;
  font-size: 13.5px;
  color: var(--ink);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin-bottom: 3px;
}
.folder-meta {
  font-size: 12px;
  color: var(--mut);
}

/* FILE TABLE & GRID */
.table-wrap {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
}
.table-wrap th {
  text-align: left;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--mut);
  padding: 10px 10px;
  border-bottom: 1px solid var(--line);
  user-select: none;
}
.table-wrap td {
  padding: 13px 10px;
  border-bottom: 1px solid #f2f4f7;
  font-size: 13.5px;
  vertical-align: middle;
}
.table-wrap tr:hover td { background: #fafbfc; }
.table-wrap tr.selected td { background: #f4f7ff; }

.file-cell {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-width: 0;
}
.file-cell a, .file-cell span.fname {
  color: var(--ink);
  font-weight: 500;
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: block;
  flex: 1;
  min-width: 0;
}
.file-cell a:hover { color: var(--pri); }

.files-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 14px;
}
.file-card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  position: relative;
}
.file-card:hover { background: #fff; box-shadow: 0 8px 20px rgba(15,23,42,0.05); }
.file-card.selected { border-color: var(--ink); background: #f4f7ff; }
.file-card-top { display: flex; justify-content: space-between; align-items: center; }

/* CUSTOM CHECKBOX */
.chk {
  appearance: none;
  -webkit-appearance: none;
  width: 17px;
  height: 17px;
  border: 1.5px solid #cfd4e0;
  border-radius: 5px;
  background: #fff;
  cursor: pointer;
  display: inline-grid;
  place-content: center;
  margin: 0;
  transition: all .12s ease;
}
.chk:hover { border-color: #9aa3b5; }
.chk:checked {
  background: var(--chk);
  border-color: var(--chk);
}
.chk:checked::after {
  content: "";
  width: 9px;
  height: 5px;
  border-left: 2px solid #fff;
  border-bottom: 2px solid #fff;
  transform: rotate(-45deg) translate(1px, -1px);
}
.chk.minus:checked::after {
  width: 8px;
  height: 0;
  border-left: 0;
  border-bottom: 2px solid #fff;
  transform: none;
}

/* AVATAR PILL */
.owner-cell {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--ink-soft);
}
.avatar {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}
.av-1 { background: linear-gradient(135deg, #6366f1, #ec4899); }
.av-2 { background: linear-gradient(135deg, #3b82f6, #06b6d4); }

/* METRIC CARDS */
.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
  margin-bottom: 24px;
}
.stat-card {
  background: var(--card);
  border: 1px solid #eef0f5;
  border-radius: 16px;
  padding: 16px 18px;
}
.stat-card .big { font-size: 24px; font-weight: 700; margin: 4px 0; letter-spacing: -0.02em; }
.bar { height: 7px; background: #e6e9f2; border-radius: 99px; overflow: hidden; margin-top: 8px; }
.bar i { display: block; height: 100%; background: linear-gradient(90deg, #3b82f6, #60a5fa); border-radius: 99px; }

/* FLOATING ACTION BAR */
.action-bar {
  position: absolute;
  bottom: 20px;
  left: 50%;
  transform: translateX(-50%);
  background: #181a20;
  color: #fff;
  padding: 10px 16px 10px 20px;
  border-radius: 14px;
  display: flex;
  align-items: center;
  gap: 18px;
  box-shadow: 0 18px 38px rgba(15, 23, 42, 0.25);
  z-index: 20;
}
.action-bar.hide { display: none; }
.action-bar .act-btn {
  border: 0;
  background: var(--red);
  color: #fff;
  font-weight: 600;
  font-size: 13px;
  padding: 8px 14px;
  border-radius: 9px;
  cursor: pointer;
}
.action-bar .act-btn.blue { background: var(--pri); }

.toast {
  padding: 11px 16px;
  border-radius: 12px;
  margin-bottom: 16px;
  background: #eef4ff;
  color: #1e3a8a;
  font-size: 13px;
  border: 1px solid #dbeafe;
}
.toast.err { background: #fef2f2; color: #991b1b; border-color: #fecaca; }
.filter-strip {
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--card);
  border: 1px solid var(--line);
  padding: 10px 14px;
  border-radius: 12px;
  margin-bottom: 16px;
  font-size: 13px;
}
.filter-strip input, .filter-strip select {
  padding: 5px 9px;
  border-radius: 8px;
  border: 1px solid #d5d9e2;
  background: #fff;
  font-size: 13px;
}
.ico {
  width: 17px; height: 17px; stroke: currentColor; stroke-width: 1.8;
  fill: none; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0;
}
.filter-wrap { margin-bottom: 16px; }
.filter-split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  background: #fff;
}
.flt-cell {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 12px 10px;
  font-size: 14px;
  font-weight: 500;
  color: var(--ink);
  cursor: pointer;
  user-select: none;
  transition: background .15s;
}
.flt-cell:hover { background: var(--card); }
.flt-cell + .flt-cell { border-left: 1px solid var(--line); }
.flt-native {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  opacity: 0;
  cursor: pointer;
  font-size: 15px;
}
.chips-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 2px 4px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: #eff1f5;
  color: #1f2433;
  border: 0;
  border-radius: 999px;
  padding: 6px 13px;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s;
}
.chip:hover { background: #e3e6ee; }
.chip svg { width: 12px; height: 12px; stroke: #687082; stroke-width: 2.2; }
</style></head><body>

<svg width="0" height="0" style="position:absolute">
  <defs>
    <linearGradient id="fBack" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#56a7f8"/>
      <stop offset="100%" stop-color="#2e7de1"/>
    </linearGradient>
    <linearGradient id="fFront" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#8fd0ff" stop-opacity="0.92"/>
      <stop offset="100%" stop-color="#4898f0" stop-opacity="0.97"/>
    </linearGradient>
    <symbol id="mac-folder" viewBox="0 0 96 78">
      <path d="M8 16 C8 11, 12 8, 17 8 L35 8 C39 8, 41 10, 44 13 L48 17 L79 17 C84 17, 88 21, 88 26 L88 64 C88 69, 84 72, 79 72 L17 72 C12 72, 8 69, 8 64 Z" fill="url(#fBack)"/>
      <rect x="15" y="19" width="66" height="22" rx="4" fill="#ffffff" fill-opacity="0.88"/>
      <rect x="19" y="23" width="46" height="14" rx="3" fill="#e8f1ff" fill-opacity="0.7"/>
      <path d="M6 30 C6 25, 10 22, 15 22 L81 22 C86 22, 90 25, 90 30 L88 65 C88 70, 84 73, 79 73 L17 73 C12 73, 8 70, 8 65 Z" fill="url(#fFront)" stroke="#a8dcff" stroke-width="0.8"/>
    </symbol>
  </defs>
</svg>

<div class="shell" id="shell">
  <aside>
    <div class="brand">
      <span class="brand-title hide-col">Drive Archotype Mac</span>
      <button class="icon-btn" id="collapseBtn" title="Contraer barra lateral">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="16" rx="3"/><line x1="9" y1="4" x2="9" y2="20"/></svg>
      </button>
    </div>

    <div class="sec-label hide-col">Vistas</div>
    <div id="nav-top"></div>

    <div class="sec-label hide-col">Espacios de Drive</div>
    <div id="nav-spaces" class="hide-col"></div>

    <div class="sec-label hide-col">Etiquetas</div>
    <div id="nav-tags" class="hide-col"></div>
  </aside>

  <main>
    <div class="topbar">
      <div class="crumbs" id="crumbs"></div>
      <div class="top-actions">
        <div class="search-box">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#858c9b" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="20" y1="20" x2="16.5" y2="16.5"/></svg>
          <input type="text" id="searchInput" placeholder="Buscar archivos o carpetas…">
        </div>
        <button class="btn-sync" id="syncBtn"><svg class="ico" style="width:14px;height:14px" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 0-15.36-6.36L3 8m0-5v5h5M3 12a9 9 0 0 0 15.36 6.36L21 16m0 5v-5h-5"/></svg> Sincronizar</button>
      </div>
    </div>

    <div class="content">
      <div id="toast"></div>
      <div class="title-bar">
        <div class="title-left">
          <h1 id="pageTitle">Dashboard</h1>
          <div class="divider-v"></div>
          <div class="view-pills">
            <button class="pill-btn on" id="btnList">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg>
              List View
            </button>
            <button class="pill-btn" id="btnGrid">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>
              Grid
            </button>
          </div>
        </div>
        <div class="mut" id="lastSync" style="font-size:12px"></div>
      </div>

      <div id="viewBody"></div>
    </div>

    <div class="action-bar hide" id="actionBar">
      <span id="selCount">0 seleccionados</span>
      <button class="act-btn" id="actBtn">Acción</button>
    </div>
  </main>
</div>

<script>
const $ = s => document.querySelector(s);
const fmt = b => {
  b = Number(b) || 0;
  for (const u of ['B','KB','MB','GB','TB']) {
    if (b < 1024) return b.toFixed(b < 10 && u !== 'B' ? 1 : 0) + ' ' + u;
    b /= 1024;
  }
  return b.toFixed(1) + ' PB';
};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtDate = iso => {
  if (!iso) return '—';
  const d = new Date(typeof iso === 'number' ? iso * 1000 : iso);
  if (isNaN(d)) return String(iso).slice(0, 10);
  return d.toLocaleDateString('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' });
};
const api = (u, body) => fetch(u, body ? {
  method: 'POST',
  headers: {'Content-Type': 'application/json', 'X-Panel': '1'},
  body: JSON.stringify(body)
} : {}).then(r => r.json());

const state = {
  mode: 'dashboard', // dashboard | browse | large | dups | mac
  account: '',
  folderId: '',
  folderName: '',
  q: '',
  fileType: 'all',
  sortBy: 'date_desc',
  layout: 'list', // list | grid
  minMb: 1,
  ov: null,
  expanded: { cuenta1: true, cuenta2: true },
  currentItems: [],
  actionHandler: null
};

function matchFileType(f, t) {
  if (!t || t === 'all') return true;
  const m = String(f.mime || '').toLowerCase();
  const n = String(f.name || '').toLowerCase();
  if (t === 'image') return m.startsWith('image/') || /\.(png|jpe?g|gif|webp|svg|heic)$/.test(n);
  if (t === 'video') return m.startsWith('video/') || /\.(mp4|mov|mkv|avi|webm)$/.test(n);
  if (t === 'pdf') return m.includes('pdf') || n.endsWith('.pdf');
  if (t === 'design') return /\.(psd|ai|aep|indd|fig|sketch)$/.test(n) || m.includes('photoshop') || m.includes('illustrator');
  if (t === 'doc') return m.includes('word') || m.includes('pages') || m.includes('document') || m.includes('spreadsheet') || m.includes('presentation') || /\.(pages|docx?|xlsx?|pptx?|txt|csv)$/.test(n);
  if (t === 'zip') return m.includes('zip') || m.includes('rar') || m.includes('compressed') || /\.(zip|rar|7z|tar|gz)$/.test(n);
  return true;
}

function applyFilterSort(rawFiles) {
  const filtered = rawFiles.filter(f => matchFileType(f, state.fileType));
  const sb = state.sortBy;
  filtered.sort((a, b) => {
    if (sb === 'date_desc') return String(b.modified || '').localeCompare(String(a.modified || ''));
    if (sb === 'date_asc') return String(a.modified || '').localeCompare(String(b.modified || ''));
    if (sb === 'size_desc') return (Number(b.size ?? b.bytes) || 0) - (Number(a.size ?? a.bytes) || 0);
    if (sb === 'size_asc') return (Number(a.size ?? a.bytes) || 0) - (Number(b.size ?? b.bytes) || 0);
    if (sb === 'name_asc') return String(a.name || '').localeCompare(String(b.name || ''));
    return 0;
  });
  return filtered;
}

function toast(msg, err=false) {
  $('#toast').innerHTML = msg ? `<div class="toast ${err?'err':''}">${msg}</div>` : '';
}

function ownerBadge(account, ownerEmail) {
  const is2 = String(account).includes('2');
  const label = ownerEmail ? ownerEmail.split('@')[0] : (account === 'cuenta1' ? 'Trabajo (C1)' : account === 'cuenta2' ? 'Personal (C2)' : account);
  const init = (label[0] || 'U').toUpperCase();
  return `<span class="owner-cell"><span class="avatar ${is2?'av-2':'av-1'}">${esc(init)}</span>${esc(label)}</span>`;
}

function renderSidebar() {
  const ov = state.ov;
  if (!ov) return;
  const totalFiles = ov.accounts.reduce((a, x) => a + (x.files || 0), 0);

  const topItems = [
    { id: 'dashboard', label: 'Dashboard', cnt: totalFiles, icon: '<svg class="ico" viewBox="0 0 24 24"><path d="M3 10.5L12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1v-9.5z"/></svg>' },
    { id: 'browse-all', label: 'Documentos', cnt: ov.top_folders.length, icon: '<svg class="ico" viewBox="0 0 24 24"><path d="M21 8v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8m18 0l-2.5-4H5.5L3 8m18 0H3m7 4h4"/></svg>' },
    { id: 'large', label: 'Archivos grandes', cnt: ov.large_count, icon: '<svg class="ico" viewBox="0 0 24 24"><path d="M21.21 15.89A10 10 0 1 1 8 2.83"/><path d="M22 12A10 10 0 0 0 12 2v10z"/></svg>' },
    { id: 'dups', label: 'Duplicados', cnt: ov.dup_count, icon: '<svg class="ico" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>' },
    { id: 'mac', label: 'En tu Mac', cnt: '', icon: '<svg class="ico" viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2.5"/><path d="M8 21h8m-4-4v4"/></svg>' }
  ];

  $('#nav-top').innerHTML = topItems.map(it => {
    const isAct = (state.mode === it.id) || (it.id === 'browse-all' && state.mode === 'browse' && !state.account);
    return `<div class="nav-item ${isAct?'active':''}" data-nav="${it.id}">
      ${it.icon}<span class="hide-col">${it.label}</span>
      ${it.cnt !== '' ? `<span class="cnt hide-col">${it.cnt}</span>` : ''}
    </div>`;
  }).join('');

  $('#nav-top').querySelectorAll('[data-nav]').forEach(el => {
    el.onclick = () => {
      const k = el.dataset.nav;
      state.q = ''; $('#searchInput').value = '';
      if (k === 'browse-all') {
        state.mode = 'browse'; state.account = ''; state.folderId = ''; state.folderName = 'Documentos';
      } else {
        state.mode = k; state.account = ''; state.folderId = '';
      }
      loadView();
    };
  });

  // Spaces tree per account
  $('#nav-spaces').innerHTML = ov.accounts.map(a => {
    const acc = a.account;
    const isOpen = !!state.expanded[acc];
    const subFolders = ov.top_folders.filter(f => f.account === acc);
    const accActive = state.mode === 'browse' && state.account === acc && !state.folderId;
    const niceName = acc === 'cuenta1' ? 'Trabajo · cuenta1' : acc === 'cuenta2' ? 'Personal · cuenta2' : acc;

    return `<div class="tree-group">
      <div class="nav-item ${accActive?'active':''}" data-acc="${esc(acc)}">
        <svg class="chev ${isOpen?'open':''}" data-toggle="${esc(acc)}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><polyline points="9 6 15 12 9 18"/></svg>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
        <span>${esc(niceName)}</span>
        <span class="cnt">${a.files}</span>
      </div>
      ${isOpen && subFolders.length ? `<div class="tree-children">
        ${subFolders.map(f => {
          const fAct = state.mode === 'browse' && state.folderId === f.id;
          return `<div class="nav-item ${fAct?'active':''}" data-facc="${esc(acc)}" data-fid="${esc(f.id)}" data-fname="${esc(f.name)}">
            <svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(f.name)}</span>
            <span class="cnt">${f.files}</span>
          </div>`;
        }).join('')}
      </div>` : ''}
    </div>`;
  }).join('');

  $('#nav-spaces').querySelectorAll('[data-toggle]').forEach(tg => {
    tg.onclick = e => {
      e.stopPropagation();
      const ac = tg.dataset.toggle;
      state.expanded[ac] = !state.expanded[ac];
      renderSidebar();
    };
  });
  $('#nav-spaces').querySelectorAll('[data-acc]').forEach(el => {
    el.onclick = () => {
      state.mode = 'browse';
      state.account = el.dataset.acc;
      state.folderId = '';
      state.folderName = el.dataset.acc;
      loadView();
    };
  });
  $('#nav-spaces').querySelectorAll('[data-fid]').forEach(el => {
    el.onclick = () => {
      state.mode = 'browse';
      state.account = el.dataset.facc;
      state.folderId = el.dataset.fid;
      state.folderName = el.dataset.fname;
      loadView();
    };
  });

  // Tags quick filter
  const tags = [
    { label: 'Pesados (> 10 MB)', color: '#ef4444', act: () => { state.mode = 'large'; state.minMb = 10; loadView(); } },
    { label: 'Duplicados', color: '#f59e0b', act: () => { state.mode = 'dups'; loadView(); } },
    { label: 'Copia local en Mac', color: '#10b981', act: () => { state.mode = 'mac'; loadView(); } }
  ];
  $('#nav-tags').innerHTML = tags.map((t, i) => `
    <div class="nav-item" data-tag="${i}">
      <span class="tag-dot" style="background:${t.color}"></span>
      <span>${t.label}</span>
    </div>
  `).join('');
  $('#nav-tags').querySelectorAll('[data-tag]').forEach((el, i) => el.onclick = tags[i].act);
}

function setCrumbs(parts) {
  $('#crumbs').innerHTML = parts.map((p, idx) => {
    const isLast = idx === parts.length - 1;
    return `${idx > 0 ? '<span class="dot">•</span>' : ''}
      <span class="crumb ${isLast?'current':''}" data-cidx="${idx}">
        ${idx === 0 ? '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' : ''}
        ${esc(p.label)}
      </span>`;
  }).join('');
  $('#crumbs').querySelectorAll('[data-cidx]').forEach(el => {
    const fn = parts[Number(el.dataset.cidx)].onClick;
    if (fn) el.onclick = fn;
  });
}

function renderFoldersSection(folders, title='Recent') {
  if (!folders || !folders.length) return '';
  return `<div class="subhead">${esc(title)} <span>${folders.length}</span></div>
  <div class="folder-grid">
    ${folders.map(f => `<div class="folder-card" data-open-acc="${esc(f.account)}" data-open-id="${esc(f.id)}" data-open-name="${esc(f.name)}">
      <span class="card-dot"></span>
      <span class="card-badge">${esc(f.account)}</span>
      <svg class="folder-svg"><use href="#mac-folder"/></svg>
      <div class="folder-name" title="${esc(f.name)}">${esc(f.name)}</div>
      <div class="folder-meta">${f.files} Files • ${fmt(f.bytes)}</div>
    </div>`).join('')}
  </div>`;
}

function bindFolderCards() {
  document.querySelectorAll('[data-open-id]').forEach(card => {
    card.onclick = () => {
      state.mode = 'browse';
      state.account = card.dataset.openAcc;
      state.folderId = card.dataset.openId;
      state.folderName = card.dataset.openName;
      loadView();
    };
  });
}

function renderSelectableFiles(rawItems, actionLabel, actionBlue, onAction) {
  const items = applyFilterSort(rawItems);
  state.currentItems = items;
  state.actionHandler = onAction;
  $('#actionBar').classList.add('hide');

  const typeOpts = [
    ['all', 'Todos los tipos'],
    ['image', 'Imágenes / SVG'],
    ['video', 'Videos (.mp4/.mov)'],
    ['design', 'Diseño (.psd/.ai/.aep)'],
    ['pdf', 'PDF'],
    ['doc', 'Docs / Pages'],
    ['zip', 'Comprimidos (.zip)']
  ];
  const sortOpts = [
    ['date_desc', 'Más recientes'],
    ['date_asc', 'Más viejos'],
    ['size_desc', 'Más pesados'],
    ['size_asc', 'Más livianos'],
    ['name_asc', 'Nombre (A-Z)']
  ];

  const typeLabel = (typeOpts.find(x => x[0] === state.fileType) || typeOpts[0])[1];
  const sortLabel = (sortOpts.find(x => x[0] === state.sortBy) || sortOpts[0])[1];
  const xSvg = '<svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>';

  const barHtml = `<div class="filter-wrap">
    <div class="filter-split">
      <label class="flt-cell">
        <svg class="ico" viewBox="0 0 24 24"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="9" cy="6" r="2.2" fill="#fff"/><circle cx="15" cy="12" r="2.2" fill="#fff"/><circle cx="10" cy="18" r="2.2" fill="#fff"/></svg>
        <span>Filter</span>
        <select class="flt-native" id="selType">${typeOpts.map(([k,v])=>`<option value="${k}" ${state.fileType===k?'selected':''}>${v}</option>`).join('')}</select>
      </label>
      <label class="flt-cell">
        <svg class="ico" viewBox="0 0 24 24"><path d="M6 4v16m0 0l-3-3m3 3l3-3"/><line x1="12" y1="6" x2="20" y2="6"/><line x1="12" y1="12" x2="18" y2="12"/><line x1="12" y1="18" x2="15" y2="18"/></svg>
        <span>Sort</span>
        <select class="flt-native" id="selSort">${sortOpts.map(([k,v])=>`<option value="${k}" ${state.sortBy===k?'selected':''}>${v}</option>`).join('')}</select>
      </label>
    </div>
    <div class="chips-bar">
      <button type="button" class="chip" id="chipType" title="Quitar o cambiar filtro">${xSvg}<span>${esc(typeLabel)}</span></button>
      <button type="button" class="chip" id="chipSort" title="Restaurar orden">${xSvg}<span>${esc(sortLabel)}</span></button>
      <span class="mut" style="margin-left:auto;font-size:12px;font-weight:500">${items.length} de ${rawItems.length} archivos</span>
    </div>
  </div>`;

  if (!items.length) {
    setTimeout(() => {
      if ($('#selType')) $('#selType').onchange = e => { state.fileType = e.target.value; loadView(); };
      if ($('#selSort')) $('#selSort').onchange = e => { state.sortBy = e.target.value; loadView(); };
      if ($('#chipType')) $('#chipType').onclick = () => { if (state.fileType !== 'all') { state.fileType = 'all'; loadView(); } };
      if ($('#chipSort')) $('#chipSort').onclick = () => { if (state.sortBy !== 'date_desc') { state.sortBy = 'date_desc'; loadView(); } };
    }, 0);
    return barHtml + `<div class="stat-card mut" style="text-align:center;padding:32px">No hay archivos con ese filtro en esta vista.</div>`;
  }

  let html = barHtml;
  if (state.layout === 'grid') {
    html += `<div class="files-grid">
      ${items.map((it, i) => `<div class="file-card" data-row="${i}">
        <div class="file-card-top">
          <input type="checkbox" class="chk item-chk" data-i="${i}">
          <span class="mut" style="font-size:12px">${fmt(it.size ?? it.bytes)}</span>
        </div>
        <div class="file-cell" style="margin:6px 0">
          ${it.link ? `<a href="${esc(it.link)}" target="_blank" title="${esc(it.name)}">${esc(it.name)}</a>` : `<span class="fname" title="${esc(it.name)}">${esc(it.name)}</span>`}
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:auto">
          ${ownerBadge(it.account, it.owner)}
          <span class="mut" style="font-size:11.5px">${fmtDate(it.modified)}</span>
        </div>
      </div>`).join('')}
    </div>`;
  } else {
    html += `<table class="table-wrap">
      <thead>
        <tr>
          <th style="width:36px"><input type="checkbox" class="chk minus" id="masterChk"></th>
          <th>File name</th>
          <th>Date added</th>
          <th>Added by</th>
          <th style="text-align:right">Size</th>
        </tr>
      </thead>
      <tbody>
        ${items.map((it, i) => `<tr data-row="${i}">
          <td><input type="checkbox" class="chk item-chk" data-i="${i}"></td>
          <td>
            <div class="file-cell">
              ${it.link ? `<a href="${esc(it.link)}" target="_blank" title="${esc(it.name)}">${esc(it.name)}</a>` : `<span class="fname" title="${esc(it.name)}">${esc(it.name)}</span>`}
              ${it.badge ? `<span class="mut" style="font-size:11.5px;background:#eef0f5;padding:2px 7px;border-radius:6px">${esc(it.badge)}</span>` : ''}
            </div>
          </td>
          <td style="font-weight:500">${fmtDate(it.modified)}</td>
          <td>${ownerBadge(it.account, it.owner)}</td>
          <td style="text-align:right;color:var(--ink-soft);font-weight:500">${fmt(it.size ?? it.bytes)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`;
  }

  setTimeout(() => {
    if ($('#selType')) $('#selType').onchange = e => { state.fileType = e.target.value; loadView(); };
    if ($('#selSort')) $('#selSort').onchange = e => { state.sortBy = e.target.value; loadView(); };
    if ($('#chipType')) $('#chipType').onclick = () => { if (state.fileType !== 'all') { state.fileType = 'all'; loadView(); } };
    if ($('#chipSort')) $('#chipSort').onclick = () => { if (state.sortBy !== 'date_desc') { state.sortBy = 'date_desc'; loadView(); } };
    const chks = [...document.querySelectorAll('.item-chk')];
    const master = $('#masterChk');
    const actBar = $('#actionBar');
    const actBtn = $('#actBtn');
    actBtn.textContent = actionLabel;
    actBtn.className = 'act-btn ' + (actionBlue ? 'blue' : '');

    function syncSelection() {
      const sel = chks.filter(c => c.checked);
      chks.forEach(c => {
        const row = document.querySelector(`[data-row="${c.dataset.i}"]`);
        if (row) row.classList.toggle('selected', c.checked);
      });
      if (master) master.checked = sel.length > 0;
      if (sel.length > 0) {
        const sumB = sel.reduce((acc, c) => acc + (Number(items[c.dataset.i].size ?? items[c.dataset.i].bytes) || 0), 0);
        $('#selCount').textContent = `${sel.length} seleccionado(s) · ${fmt(sumB)}`;
        actBar.classList.remove('hide');
      } else {
        actBar.classList.add('hide');
      }
    }

    chks.forEach(c => c.onchange = syncSelection);
    if (master) {
      master.onchange = () => {
        const target = master.checked;
        chks.forEach(c => c.checked = target);
        syncSelection();
      };
    }
    actBtn.onclick = () => {
      const chosen = chks.filter(c => c.checked).map(c => items[c.dataset.i]);
      if (chosen.length && state.actionHandler) state.actionHandler(chosen);
    };
  }, 0);

  return html;
}

async function trashSelected(chosen) {
  const sum = fmt(chosen.reduce((a, x) => a + (Number(x.size) || 0), 0));
  if (!confirm(`¿Mandar ${chosen.length} archivo(s) (${sum}) a la papelera de Google Drive?\n\nEs reversible durante 30 días desde la papelera.`)) return;
  toast('Enviando a la papelera…');
  const r = await api('/api/trash', { items: chosen.map(f => ({ account: f.account, id: f.id })) });
  toast(`✅ ${r.done} archivo(s) enviados a la papelera.` + (r.errors.length ? '<br>⚠️ ' + r.errors.map(esc).join('<br>') : ''), r.errors.length && !r.done);
  loadView(true);
}

async function loadView(forceOv=false) {
  if (!state.ov || forceOv) {
    state.ov = await api('/api/overview');
    const last = state.ov.last.map(l => l.finished).filter(Boolean).sort().pop();
    $('#lastSync').textContent = last ? 'Última sync: ' + last : 'Sin sincronizar';
  }
  renderSidebar();
  $('#viewBody').innerHTML = `<div class="mut" style="padding:20px 0">Cargando…</div>`;

  if (state.mode === 'dashboard') {
    $('#pageTitle').textContent = 'Dashboard';
    setCrumbs([{ label: 'Docs', onClick: () => { state.mode='dashboard'; loadView(); } }, { label: 'Overview' }]);
    const d = await api('/api/browse');
    const ov = state.ov;
    const statsHtml = `<div class="stats-row">
      ${ov.accounts.map(a => `<div class="stat-card">
        <div class="mut" style="font-size:12px;font-weight:600">${esc(a.account === 'cuenta1' ? 'Trabajo · cuenta1' : a.account === 'cuenta2' ? 'Personal · cuenta2' : a.account)}</div>
        <div class="big">${fmt(a.bytes)}</div>
        <div class="mut" style="font-size:12.5px">${a.files} archivos · ${a.folders} carpetas</div>
        <div class="bar"><i style="width:${Math.min(100, (a.bytes / (15*1024*1024*1024))*100)}%"></i></div>
      </div>`).join('')}
    </div>`;
    $('#viewBody').innerHTML = statsHtml
      + renderFoldersSection(ov.top_folders.slice(0, 6), 'Recent')
      + `<div class="subhead">Archivos recientes <span>${d.files.length}</span></div>`
      + renderSelectableFiles(d.files, '🗑 Mandar a la papelera', false, trashSelected);
    bindFolderCards();
  }
  else if (state.mode === 'browse') {
    const params = new URLSearchParams({ account: state.account, folder_id: state.folderId, q: state.q });
    const d = await api('/api/browse?' + params.toString());
    const title = state.q ? `Búsqueda: "${state.q}"` : (state.folderName || 'Documentos');
    $('#pageTitle').textContent = title;

    const crumbs = [{ label: 'Docs', onClick: () => { state.mode='browse'; state.account=''; state.folderId=''; state.folderName='Documentos'; loadView(); } }];
    if (state.account) {
      crumbs.push({ label: state.account, onClick: () => { state.mode='browse'; state.folderId=''; state.folderName=state.account; loadView(); } });
    }
    d.breadcrumbs.forEach(b => {
      crumbs.push({ label: b.name, onClick: () => { state.mode='browse'; state.account=b.account; state.folderId=b.id; state.folderName=b.name; loadView(); } });
    });
    setCrumbs(crumbs);

    $('#viewBody').innerHTML = renderFoldersSection(d.folders, 'Folders')
      + renderSelectableFiles(d.files, '🗑 Mandar a la papelera', false, trashSelected);
    bindFolderCards();
  }
  else if (state.mode === 'large') {
    $('#pageTitle').textContent = 'Archivos grandes';
    setCrumbs([{ label: 'Docs', onClick: () => { state.mode='dashboard'; loadView(); } }, { label: 'Archivos grandes' }]);
    const d = await api(`/api/large?min_mb=${state.minMb}&account=${state.account}`);
    const accs = state.ov.accounts.map(a => a.account);
    const filterHtml = `<div class="filter-strip">
      <span>Mayores de</span>
      <input type="number" id="fltMb" value="${state.minMb}" min="0" step="1" style="width:74px"> <span>MB</span>
      <span style="margin-left:8px">Cuenta:</span>
      <select id="fltAcc"><option value="">Todas</option>${accs.map(a => `<option value="${esc(a)}" ${a===state.account?'selected':''}>${esc(a)}</option>`).join('')}</select>
      <button class="btn-sync" id="fltBtn">Filtrar</button>
    </div>`;
    $('#viewBody').innerHTML = filterHtml + renderSelectableFiles(d, '🗑 Mandar a la papelera', false, trashSelected);
    $('#fltBtn').onclick = () => {
      state.minMb = Number($('#fltMb').value) || 0;
      state.account = $('#fltAcc').value;
      loadView();
    };
  }
  else if (state.mode === 'dups') {
    $('#pageTitle').textContent = 'Duplicados';
    setCrumbs([{ label: 'Docs', onClick: () => { state.mode='dashboard'; loadView(); } }, { label: 'Duplicados' }]);
    const groups = await api('/api/duplicates');
    const flat = [];
    groups.forEach(g => {
      g.files.forEach((f, idx) => {
        flat.push({ ...f, size: g.size, badge: idx === 0 ? 'Original' : 'Copia idéntica' });
      });
    });
    $('#viewBody').innerHTML = `<div class="filter-strip">Los archivos con contenido idéntico (MD5) aparecen seguidos. Conserva el original y selecciona las copias.</div>`
      + renderSelectableFiles(flat, '🗑 Mandar copias a la papelera', false, trashSelected);
  }
  else if (state.mode === 'mac') {
    $('#pageTitle').textContent = 'En tu Mac';
    setCrumbs([{ label: 'Docs', onClick: () => { state.mode='dashboard'; loadView(); } }, { label: 'Espacio local en Mac' }]);
    const d = await api('/api/local');
    const topCard = `<div class="stats-row">
      <div class="stat-card">
        <div class="mut" style="font-size:12px">Ocupando disco en tu Mac ahora</div>
        <div class="big">${fmt(d.total)}</div>
        <div class="mut" style="font-size:12.5px">${d.count} archivos descargados · ${d.cloud_only} solo en la nube (0 B en Mac)</div>
      </div>
    </div>`;
    const items = d.files.map(f => ({ ...f, badge: f.folder }));
    $('#viewBody').innerHTML = topCard + renderSelectableFiles(items, '🔍 Mostrar en Finder para Quitar descarga', true, async chosen => {
      const r = await api('/api/reveal', { paths: chosen.map(x => x.path) });
      toast(`Se mostraron ${r.shown} archivo(s) en Finder. Haz clic derecho → <b>Quitar descarga</b> para liberar el espacio en tu Mac.`);
    });
  }
}

$('#collapseBtn').onclick = () => $('#shell').classList.toggle('collapsed');
$('#btnList').onclick = () => { state.layout = 'list'; $('#btnList').classList.add('on'); $('#btnGrid').classList.remove('on'); loadView(); };
$('#btnGrid').onclick = () => { state.layout = 'grid'; $('#btnGrid').classList.add('on'); $('#btnList').classList.remove('on'); loadView(); };

let searchTimer = null;
$('#searchInput').oninput = e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    state.mode = 'browse';
    loadView();
  }, 220);
};

$('#syncBtn').onclick = async () => {
  $('#syncBtn').disabled = true;
  toast('Sincronizando cuentas con Google Drive…');
  await api('/api/sync', {});
  toast('✅ Sincronización completada.');
  $('#syncBtn').disabled = false;
  loadView(true);
};

loadView(true);
</script></body></html>"""

if __name__ == "__main__":
    threading.Timer(1, lambda: webbrowser.open("http://127.0.0.1:8765")).start()
    app.run(host="127.0.0.1", port=8765)
