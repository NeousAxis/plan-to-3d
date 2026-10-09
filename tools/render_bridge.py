#!/usr/bin/env python3
"""render_bridge.py — pont local entre PlanCAD Stage (navigateur) et render_router.py.

Même rôle que les commandes Tauri d'ArtCraft : le navigateur n'appelle jamais
Cloudflare lui-même (les identifiants restent sur la machine), il passe par ce
pont qui sert aussi web/ (PlanCAD) sur la même origine.

  python3 tools/render_bridge.py [--port 8790]
  puis ouvrir http://127.0.0.1:8790/cad.html

API (JSON) :
  GET  /api/health              → {ok, version}
  GET  /api/models              → catalogue + moteurs exécutables + budget du jour
  POST /api/media               → corps = PNG ou JPEG ; → {id, url, w, h}
  POST /api/estimate            → {model, media:[id], aspect, provider?} → coût estimé
  POST /api/generate/image      → {model, prompt, media:[id], aspect, seed, provider?, meta?} → {task}
  GET  /api/tasks[?limit=N]     → {tasks:[…]}
  GET  /api/tasks/<id>          → {task}
  GET  /media/<fichier>         → captures et rendus

Tâches persistées en SQLite (work/stage/tasks.db) avec les états d'ArtCraft :
pending → started → complete_success | complete_failure. Un seul ouvrier en
arrière-plan, donc un seul appel moteur à la fois.
"""
import argparse
import functools
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
STATE = os.path.join(ROOT, "work", "stage")
MEDIA = os.path.join(STATE, "media")
sys.path.insert(0, ROOT)
import render_router as rr  # noqa: E402

VERSION = 1
MAX_UPLOAD = 20 * 1024 * 1024
GH_PAGES = "https://neousaxis.github.io"
MEDIA_ID = re.compile(r"^[mt]_[0-9a-f]{12}$")


# ───────────────────────── tâches (SQLite) ─────────────────────────

class TaskStore:
    COLS = ("id", "status", "model", "provider", "prompt", "aspect", "seed", "media_in", "media_out",
            "cost_unit", "cost_amount", "error_code", "error_message", "meta",
            "created_at", "started_at", "finished_at")

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("""CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, status TEXT NOT NULL, model TEXT, provider TEXT, prompt TEXT,
                aspect TEXT, seed INTEGER, media_in TEXT, media_out TEXT, cost_unit TEXT,
                cost_amount REAL, error_code TEXT, error_message TEXT, meta TEXT,
                created_at REAL, started_at REAL, finished_at REAL)""")
            # une tâche restée « started » vient d'un arrêt brutal du pont
            self.db.execute("""UPDATE tasks SET status='complete_failure', error_code='interrupted',
                error_message='pont arrêté pendant le rendu', finished_at=? WHERE status='started'""",
                            (time.time(),))
            self.db.commit()

    def _row(self, row):
        t = dict(zip(self.COLS, row))
        t["media_in"] = json.loads(t["media_in"] or "[]")
        t["meta"] = json.loads(t["meta"] or "{}")
        t["result_url"] = f"/media/{t['media_out']}.png" if t["media_out"] else None
        t["input_urls"] = [f"/media/{m}.png" for m in t["media_in"]]
        return t

    def add(self, **kw):
        tid = "t_" + uuid.uuid4().hex[:12]
        kw.update(id=tid, status="pending", created_at=time.time())
        kw["media_in"] = json.dumps(kw.get("media_in") or [])
        kw["meta"] = json.dumps(kw.get("meta") or {}, ensure_ascii=False)
        cols = [c for c in self.COLS if c in kw]
        with self.lock:
            self.db.execute(f"INSERT INTO tasks({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                            [kw[c] for c in cols])
            self.db.commit()
        return self.get(tid)

    def update(self, tid, **kw):
        with self.lock:
            self.db.execute(f"UPDATE tasks SET {','.join(f'{k}=?' for k in kw)} WHERE id=?",
                            [*kw.values(), tid])
            self.db.commit()

    def get(self, tid):
        with self.lock:
            row = self.db.execute(f"SELECT {','.join(self.COLS)} FROM tasks WHERE id=?", (tid,)).fetchone()
        return self._row(row) if row else None

    def list(self, limit=50):
        with self.lock:
            rows = self.db.execute(f"SELECT {','.join(self.COLS)} FROM tasks ORDER BY created_at DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def next_pending(self):
        with self.lock:
            row = self.db.execute(f"SELECT {','.join(self.COLS)} FROM tasks WHERE status='pending' "
                                  "ORDER BY created_at LIMIT 1").fetchone()
        return self._row(row) if row else None


def media_path(mid):
    return os.path.join(MEDIA, mid + ".png")


def read_media(mid):
    if not MEDIA_ID.match(mid or "") or not os.path.exists(media_path(mid)):
        raise rr.RouterError("invalid_input", f"image inconnue : {mid}")
    with open(media_path(mid), "rb") as f:
        return f.read()


def worker(store, wake, cat):
    ledger = rr.ledger_for(cat)
    while True:
        task = store.next_pending()
        if task is None:
            wake.wait(2.0)
            wake.clear()
            continue
        store.update(task["id"], status="started", started_at=time.time())
        print(f"▶ {task['id']} {task['model']} ({task['provider'] or 'auto'})", flush=True)
        try:
            refs = [read_media(m) for m in task["media_in"]]
            req = rr.RenderRequest(task["model"], task["prompt"], refs, task["aspect"] or "16:9",
                                   seed=task["seed"] or 7, provider=task["provider"] or "")
            png, plan = rr.render(req, cat, ledger)
            with open(media_path(task["id"]), "wb") as f:
                f.write(png)
            cost = rr.estimate(plan)
            store.update(task["id"], status="complete_success", media_out=task["id"], provider=plan.provider["id"],
                         cost_unit=cost["unit"], cost_amount=cost["amount"], finished_at=time.time())
            print(f"✓ {task['id']} via {plan.provider['id']} ({cost['label']})", flush=True)
        except rr.RouterError as e:
            store.update(task["id"], status="complete_failure", error_code=e.code, error_message=e.message,
                         finished_at=time.time())
            print(f"✗ {task['id']} [{e.code}] {e.message}", flush=True)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            store.update(task["id"], status="complete_failure", error_code="internal",
                         error_message=str(e)[:300], finished_at=time.time())


# ───────────────────────── HTTP ─────────────────────────

class Handler(SimpleHTTPRequestHandler):
    store = None
    wake = None
    cat = None
    port = 8790

    # -- origine : seulement PlanCAD servi ici, ou la page GitHub du projet
    def _allowed_origin(self):
        origin = self.headers.get("Origin")
        allowed = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}", GH_PAGES}
        return origin, (origin is None or origin in allowed)

    def _cors(self, origin):
        if origin and origin != f"http://127.0.0.1:{self.port}":
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, code, obj):
        origin, _ = self._allowed_origin()
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors(origin)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, err, code=400):
        if isinstance(err, rr.RouterError):
            status = {"quota_exceeded": 429, "missing_credentials": 503, "provider_error": 502,
                      "forbidden_model": 403}.get(err.code, code)
            return self._json(status, {"error": err.to_dict()})
        return self._json(code, {"error": {"code": "invalid_input", "message": str(err)}})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise rr.RouterError("invalid_input", "fichier trop lourd (20 Mo max)")
        return self.rfile.read(n)

    def do_OPTIONS(self):
        origin, ok = self._allowed_origin()
        if not ok:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._cors(origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if self.headers.get("Access-Control-Request-Private-Network"):
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            origin, ok = self._allowed_origin()
            if not ok:
                return self._json(403, {"error": {"code": "forbidden_origin", "message": origin}})
            if url.path == "/api/health":
                return self._json(200, {"ok": True, "version": VERSION})
            if url.path == "/api/models":
                return self._json(200, self._models())
            if url.path == "/api/tasks":
                limit = int((parse_qs(url.query).get("limit") or ["50"])[0])
                return self._json(200, {"tasks": self.store.list(min(limit, 200))})
            m = re.match(r"^/api/tasks/(t_[0-9a-f]{12})$", url.path)
            if m:
                t = self.store.get(m.group(1))
                return self._json(200, {"task": t}) if t else self._json(404, {"error": {"code": "not_found"}})
            return self._json(404, {"error": {"code": "not_found"}})
        if url.path.startswith("/media/"):
            name = os.path.basename(url.path)
            path = os.path.join(MEDIA, name)
            if not re.match(r"^[mt]_[0-9a-f]{12}\.png$", name) or not os.path.exists(path):
                return self._json(404, {"error": {"code": "not_found"}})
            with open(path, "rb") as f:
                data = f.read()
            origin, _ = self._allowed_origin()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self._cors(origin)
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()

    def do_POST(self):
        origin, ok = self._allowed_origin()
        if not ok:
            return self._json(403, {"error": {"code": "forbidden_origin", "message": origin}})
        path = urlparse(self.path).path
        try:
            if path == "/api/media":
                return self._media()
            if path in ("/api/estimate", "/api/generate/image"):
                req = json.loads(self._body() or b"{}")
                return self._generate(req, dry=(path == "/api/estimate"))
            return self._json(404, {"error": {"code": "not_found"}})
        except rr.RouterError as e:
            return self._error(e)
        except (ValueError, KeyError) as e:
            return self._error(e)

    def _media(self):
        data = self._body()
        if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8")):
            raise rr.RouterError("invalid_input", "image PNG ou JPEG attendue")
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data))
        mid = "m_" + uuid.uuid4().hex[:12]
        os.makedirs(MEDIA, exist_ok=True)
        if not data.startswith(b"\x89PNG"):
            out = io.BytesIO()
            img.convert("RGB").save(out, format="PNG")
            data = out.getvalue()
        with open(media_path(mid), "wb") as f:
            f.write(data)
        return self._json(200, {"id": mid, "url": f"/media/{mid}.png", "w": img.width, "h": img.height})

    def _generate(self, req, dry):
        media = req.get("media") or []
        refs = [read_media(m) for m in media]
        rreq = rr.RenderRequest(req.get("model") or self.cat["defaults"]["model"], req.get("prompt") or "x",
                                refs, req.get("aspect") or "16:9", seed=int(req.get("seed") or 7),
                                provider=req.get("provider") or "")
        plan = rr.build(rreq, self.cat)
        cost = rr.estimate(plan)
        budget = rr.ledger_for(self.cat).status()
        if dry:
            return self._json(200, {**cost, "provider": plan.provider["id"], "size": [plan.width, plan.height],
                                    "budget": budget})
        if cost["unit"] == "neurons" and cost["amount"] > budget["left"]:
            raise rr.RouterError("quota_exceeded", f"il reste {budget['left']:.0f} neurones gratuits aujourd'hui, "
                                                   f"ce rendu en demande ≈ {cost['amount']:.0f}")
        task = self.store.add(model=rreq.model, provider=plan.provider["id"], prompt=rreq.prompt,
                              aspect=rreq.aspect, seed=rreq.seed, media_in=media, cost_unit=cost["unit"],
                              cost_amount=cost["amount"], meta=req.get("meta") or {})
        self.wake.set()
        return self._json(200, {"task": task})

    def _models(self):
        out = []
        for m in self.cat["models"]:
            provs = [{**p, "available": rr.provider_runnable(p)} for p in m["providers"]]
            out.append({**m, "providers": provs})
        return {"models": out, "budget": rr.ledger_for(self.cat).status(),
                "defaults": self.cat["defaults"], "version": VERSION}

    def end_headers(self):
        # fichiers de PlanCAD toujours revalidés : une modification se voit au rechargement
        if not self.path.startswith(("/api/", "/media/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or "") and not self.path.startswith("/api/tasks"):
            sys.stderr.write("  " + (fmt % args) + "\n")


def main():
    ap = argparse.ArgumentParser(description="Pont local PlanCAD Stage → moteurs de rendu gratuits")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    os.makedirs(MEDIA, exist_ok=True)
    Handler.store = TaskStore(os.path.join(STATE, "tasks.db"))
    Handler.wake = threading.Event()
    Handler.cat = rr.load_catalog()
    Handler.port = a.port
    threading.Thread(target=worker, args=(Handler.store, Handler.wake, Handler.cat), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), functools.partial(Handler, directory=WEB))
    print(f"PlanCAD Stage : http://127.0.0.1:{a.port}/cad.html  (Ctrl-C pour arrêter)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
