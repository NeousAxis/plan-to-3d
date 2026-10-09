#!/usr/bin/env python3
"""render_router.py — routeur de rendu de PlanCAD Stage.

Architecture reprise d'ArtCraft (réécrite, pas copiée : leur licence l'interdit) :
catalogue de modèles à capacités (web/stage/catalog.json) puis
build(requête) → plan validé → estimate(plan) → réservation du budget → send(plan) → PNG.

Moteurs : Cloudflare Workers AI (FLUX.2 klein / dev avec images de référence,
FLUX.1 schnell en texte seul) et Spaces Hugging Face via gradio_client.
Jamais de moteur payant ni Google : le coût Cloudflare (en neurones) est estimé
puis réservé AVANT l'appel, et tout dépassement du quota gratuit est refusé net.

  python3 render_router.py models
  python3 render_router.py estimate --model flux-2-klein-4b [--aspect 16:9] [--refs 1]
  python3 render_router.py render --model flux-2-klein-4b --ref capture.png \\
          --prompt "..." -o rendu.png [--aspect 16:9] [--seed 7] [--provider cloudflare]
"""
import argparse
import base64
import fcntl
import io
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(HERE, "web", "stage", "catalog.json")
LEDGER_PATH = os.path.expanduser("~/.cache/plan_to_image/cf_neurons.json")
CF_API = "https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{model}"


class RouterError(Exception):
    """Erreur typée du routeur : code stable (pour l'UI) + message lisible."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self):
        return {"code": self.code, "message": self.message}


# ───────────────────────── catalogue ─────────────────────────

def load_catalog(path=CATALOG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_model(cat, model_id):
    model = next((m for m in cat["models"] if m["id"] == model_id), None)
    if model is None:
        raise RouterError("unsupported_model", f"modèle inconnu : {model_id}")
    if model.get("creator") in cat.get("deny", {}).get("creators", []):
        raise RouterError("forbidden_model", cat["deny"].get("reason", "modèle interdit"))
    if not model.get("free"):
        raise RouterError("forbidden_model", "modèle payant refusé (règle : gratuit, sans carte)")
    return model


def hf_available():
    try:
        import gradio_client  # noqa: F401
        return True
    except ImportError:
        return False


def provider_runnable(provider):
    """Ce que le Python sait exécuter : Cloudflare (via les identifiants
    wrangler) et les Spaces HF (si gradio_client est installé)."""
    if provider["id"] == "cloudflare":
        return True
    if provider["id"] == "hf-space":
        return hf_available()
    return False


# ───────────────────────── build : requête → plan ─────────────────────────

@dataclass
class RenderRequest:
    model: str
    prompt: str
    refs: list = field(default_factory=list)      # octets PNG/JPEG
    aspect: str = "16:9"
    width: int = 0
    height: int = 0
    seed: int = 7
    provider: str = ""                            # "" = premier moteur exécutable


@dataclass
class Plan:
    model: dict
    provider: dict
    prompt: str
    refs: list          # PNG redimensionnés, prêts à envoyer
    ref_sizes: list     # [(w, h)]
    width: int
    height: int
    seed: int


def output_size(cat, aspect, provider, width=0, height=0):
    if width and height:
        w, h = int(width), int(height)
    else:
        if aspect not in cat["sizes"]:
            raise RouterError("invalid_input", f"format inconnu : {aspect}")
        w, h = cat["sizes"][aspect]
    side = provider.get("maxSide") or 2048
    k = min(1.0, side / max(w, h))
    w, h = max(256, int(round(w * k / 16)) * 16), max(256, int(round(h * k / 16)) * 16)
    return w, h


def fit_ref(data, max_px):
    """Réduit une image de référence pour qu'aucun côté ne dépasse max_px."""
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGB")
    if max(img.size) > max_px:
        k = max_px / max(img.size)
        img = img.resize((max(1, int(img.width * k)), max(1, int(img.height * k))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), img.size


def build(req, cat=None):
    cat = cat or load_catalog()
    model = find_model(cat, req.model)
    caps = model["capabilities"]
    if not (req.prompt or "").strip():
        raise RouterError("invalid_input", "consigne vide")
    if req.refs and not caps.get("editImages"):
        raise RouterError("invalid_input", f"{model['label']} n'accepte pas d'image de référence : il ne suivrait pas le plan")
    if not req.refs and not caps.get("textToImage"):
        raise RouterError("invalid_input", f"{model['label']} exige une image de référence (la capture)")
    if len(req.refs) > caps.get("maxRefs", 0):
        raise RouterError("invalid_input", f"{model['label']} accepte au plus {caps.get('maxRefs', 0)} référence(s)")
    providers = [p for p in model["providers"] if not req.provider or p["id"] == req.provider]
    providers = [p for p in providers if provider_runnable(p)]
    if not providers:
        raise RouterError("unsupported_provider", f"aucun moteur exécutable ici pour {model['label']}"
                          + (f" via {req.provider}" if req.provider else ""))
    prov = providers[0]
    w, h = output_size(cat, req.aspect, prov, req.width, req.height)
    refs, sizes = [], []
    for r in req.refs:
        png, size = fit_ref(r, prov.get("maxRefPx") or 1024)
        refs.append(png)
        sizes.append(size)
    return Plan(model, prov, req.prompt.strip(), refs, sizes, w, h, int(req.seed))


# ───────────────────────── estimate : coût avant envoi ─────────────────────────

def _tiles(w, h):
    return math.ceil(w / 512) * math.ceil(h / 512)


def estimate(plan):
    c = plan.provider["cost"]
    if c["unit"] == "gpu-s":
        return {"unit": "gpu-s", "amount": c["estimate"],
                "label": f"≈ {c['estimate']} s de GPU gratuit Hugging Face"}
    out_t = _tiles(plan.width, plan.height)
    in_t = sum(_tiles(w, h) for w, h in plan.ref_sizes)
    if "inputTilePerStep" in c:                                   # FLUX.2 dev
        steps = plan.provider.get("steps", 20)
        n = steps * (c["inputTilePerStep"] * in_t + c["outputTilePerStep"] * out_t)
    elif "firstMp" in c:                                          # FLUX.2 klein 9B
        mp = math.ceil(plan.width * plan.height / 1048576)
        in_mp = sum(math.ceil(w * h / 1048576) for w, h in plan.ref_sizes)
        n = c["firstMp"] + c["extraMp"] * max(0, mp - 1) + c["inputMp"] * in_mp
    elif "perStep" in c:                                          # FLUX.1 schnell
        n = c["outputTile"] * out_t + c["perStep"] * plan.provider.get("steps", 8)
    else:                                                         # FLUX.2 klein 4B
        n = c["inputTile"] * in_t + c["outputTile"] * out_t
    n = round(n, 1)
    return {"unit": "neurons", "amount": n, "label": f"≈ {n:g} neurones Cloudflare (quota gratuit)"}


# ───────────────────────── budget Cloudflare (neurones) ─────────────────────────

class Ledger:
    """Compteur journalier (UTC, comme Cloudflare) des neurones réservés.
    Verrou fichier : plusieurs processus (pont + CLI) partagent le même budget."""

    def __init__(self, cap, path=LEDGER_PATH):
        self.cap = cap
        self.path = path

    def _locked(self, fn):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                raw = f.read().strip()
                state = json.loads(raw) if raw else {}
                today = time.strftime("%Y-%m-%d", time.gmtime())
                if state.get("day") != today:
                    state = {"day": today, "neurons": 0.0, "calls": 0}
                result = fn(state)
                f.seek(0)
                f.truncate()
                json.dump(state, f)
                return result
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def reserve(self, amount):
        def op(s):
            if s["neurons"] + amount > self.cap:
                raise RouterError("quota_exceeded",
                                  f"budget Cloudflare gratuit atteint : {s['neurons']:.0f} + {amount:.0f} "
                                  f"> {self.cap} neurones aujourd'hui (UTC). Réessayer demain, rien n'est facturé.")
            s["neurons"] += amount
            s["calls"] += 1
            return self.cap - s["neurons"]
        return self._locked(op)

    def refund(self, amount):
        def op(s):
            s["neurons"] = max(0.0, s["neurons"] - amount)
            s["calls"] = max(0, s["calls"] - 1)
        self._locked(op)

    def status(self):
        def op(s):
            return {"day": s["day"], "used": round(s["neurons"], 1), "cap": self.cap,
                    "left": round(self.cap - s["neurons"], 1), "calls": s["calls"]}
        return self._locked(op)


def ledger_for(cat):
    return Ledger(cat.get("budget", {}).get("cloudflareNeuronsCap", 9000))


# ───────────────────────── send : appel du moteur ─────────────────────────

def _cf_creds():
    sys.path.insert(0, HERE)
    import plan_to_image  # identifiants : variables d'env, sinon OAuth wrangler
    return plan_to_image._cf_creds()


def _legacy_quota_tick():
    """Garde historique de plan_to_image (compte les APPELS Cloudflare)."""
    sys.path.insert(0, HERE)
    import plan_to_image
    if os.environ.get("P2I_BYPASS_QUOTA"):
        return
    try:
        plan_to_image._quota_check_and_increment()
    except RuntimeError as e:
        raise RouterError("quota_exceeded", str(e)) from None


def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (name, data) in files.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{name}\"\r\n"
                     f"Content-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _cf_request(plan, acct, tok):
    model = plan.provider["model"]
    if "flux-2" in model:
        fields = {"prompt": plan.prompt, "width": plan.width, "height": plan.height, "seed": plan.seed}
        if plan.provider.get("steps"):
            fields["steps"] = plan.provider["steps"]
        files = {f"input_image_{i}": (f"ref{i}.png", d) for i, d in enumerate(plan.refs)}
        body, ctype = _multipart(fields, files)
    else:
        body = json.dumps({"prompt": plan.prompt, "width": plan.width, "height": plan.height,
                           "seed": plan.seed, "num_steps": plan.provider.get("steps", 8)}).encode()
        ctype = "application/json"
    return urllib.request.Request(CF_API.format(acct=acct, model=model), data=body, method="POST",
                                  headers={"Authorization": f"Bearer {tok}", "Content-Type": ctype,
                                           "User-Agent": "plancad-stage/1.0"})


def _send_cloudflare(plan, timeout=300):
    acct, tok = _cf_creds()
    if not acct or not tok:
        raise RouterError("missing_credentials", "identifiants Cloudflare absents : lancer `wrangler login` une fois")
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(_cf_request(plan, acct, tok), timeout=timeout) as r:
                payload = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "ignore")
            if e.code == 401 and attempt == 1:
                # jeton OAuth wrangler expiré (durée 1 h) : `wrangler whoami` le rafraîchit
                subprocess.run(["wrangler", "whoami"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60, check=False)
                acct, tok = _cf_creds()
                continue
            if e.code == 429:
                raise RouterError("quota_exceeded", f"Cloudflare limite le débit (429) : {detail}") from None
            raise RouterError("provider_error", f"Cloudflare HTTP {e.code} : {detail}") from None
        except urllib.error.URLError as e:
            raise RouterError("provider_error", f"Cloudflare injoignable : {e.reason}") from None
    if not payload.get("success"):
        raise RouterError("provider_error", f"Cloudflare a refusé : {str(payload)[:300]}")
    return base64.b64decode(payload["result"]["image"])


def _send_hf(plan):
    from gradio_client import Client, handle_file
    import tempfile
    api = plan.provider["api"]
    tmp = []
    try:
        for i, d in enumerate(plan.refs):
            fd, p = tempfile.mkstemp(suffix=f"_ref{i}.png")
            with os.fdopen(fd, "wb") as f:
                f.write(d)
            tmp.append(p)
        client = Client(plan.provider["space"], hf_token=os.environ.get("HF_TOKEN") or None, verbose=False)
        ep = plan.provider["endpoint"]
        if api == "flux2-klein":
            res = client.predict(prompt=plan.prompt,
                                 input_images=[{"image": handle_file(p), "caption": None} for p in tmp],
                                 mode_choice="Distilled (4 steps)", seed=plan.seed, randomize_seed=False,
                                 width=plan.width, height=plan.height, num_inference_steps=4,
                                 guidance_scale=1.0, prompt_upsampling=False, api_name=ep)
        elif api == "kontext":
            res = client.predict(input_image=handle_file(tmp[0]), prompt=plan.prompt, seed=plan.seed,
                                 randomize_seed=False, guidance_scale=2.5, steps=28, api_name=ep)
        elif api == "qwen-edit":
            res = client.predict(image=handle_file(tmp[0]), prompt=plan.prompt, seed=plan.seed,
                                 randomize_seed=False, true_guidance_scale=4.0, num_inference_steps=30,
                                 rewrite_prompt=False, api_name=ep)
        else:
            raise RouterError("unsupported_provider", f"API de Space inconnue : {api}")
        out = res[0]["path"] if isinstance(res[0], dict) else res[0]
        with open(out, "rb") as f:
            return f.read()
    except RouterError:
        raise
    except Exception as e:  # noqa: BLE001  (quota ZeroGPU, Space en pause, réseau…)
        msg = str(e)
        code = "quota_exceeded" if "quota" in msg.lower() else "provider_error"
        raise RouterError(code, f"Hugging Face ({plan.provider['space']}) : {msg[:300]}") from None
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass


def send(plan, ledger=None, cat=None):
    """Exécute le plan. Le coût Cloudflare est réservé avant l'appel et
    remboursé si l'appel échoue."""
    if plan.provider["id"] == "hf-space":
        return _send_hf(plan)
    if plan.provider["id"] != "cloudflare":
        raise RouterError("unsupported_provider", f"moteur non pris en charge : {plan.provider['id']}")
    ledger = ledger or ledger_for(cat or load_catalog())
    cost = estimate(plan)["amount"]
    ledger.reserve(cost)
    try:
        _legacy_quota_tick()
        return _send_cloudflare(plan)
    except BaseException:
        ledger.refund(cost)
        raise


def render(req, cat=None, ledger=None):
    cat = cat or load_catalog()
    plan = build(req, cat)
    return send(plan, ledger or ledger_for(cat), cat), plan


# ───────────────────────── CLI ─────────────────────────

def _cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models", help="liste les modèles et leurs moteurs")
    e = sub.add_parser("estimate", help="coût estimé d'un rendu")
    r = sub.add_parser("render", help="rend une image")
    for p in (e, r):
        p.add_argument("--model", default=None)
        p.add_argument("--aspect", default="16:9")
        p.add_argument("--provider", default="")
    e.add_argument("--refs", type=int, default=1)
    r.add_argument("--ref", action="append", default=[], help="image de référence (capture PlanCAD)")
    r.add_argument("--prompt", required=True)
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    cat = load_catalog()
    if a.cmd == "models":
        for m in cat["models"]:
            provs = ", ".join(f"{p['id']}{'' if provider_runnable(p) else ' (indisponible ici)'}" for p in m["providers"])
            print(f"{m['id']:22s} {m['label']:30s} refs≤{m['capabilities']['maxRefs']}  [{provs}]")
        print("budget Cloudflare :", ledger_for(cat).status())
        return
    model = a.model or cat["defaults"]["model"]
    try:
        if a.cmd == "estimate":
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", tuple(cat["sizes"].get(a.aspect, [1280, 720]))).save(buf, format="PNG")
            fake = buf.getvalue()
            plan = build(RenderRequest(model, "estimation", [fake] * a.refs, a.aspect, provider=a.provider), cat)
            print(json.dumps({"model": model, "provider": plan.provider["id"], "size": [plan.width, plan.height],
                              **estimate(plan), "budget": ledger_for(cat).status()}, ensure_ascii=False))
            return
        refs = [open(p, "rb").read() for p in a.ref]
        t = time.time()
        png, plan = render(RenderRequest(model, a.prompt, refs, a.aspect, seed=a.seed, provider=a.provider), cat)
        with open(a.out, "wb") as f:
            f.write(png)
        print(f"OK {plan.model['id']} via {plan.provider['id']} {plan.width}×{plan.height} "
              f"en {time.time() - t:.1f}s → {a.out}  ({estimate(plan)['label']})")
    except RouterError as err:
        print(f"ERREUR [{err.code}] {err.message}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    _cli()
