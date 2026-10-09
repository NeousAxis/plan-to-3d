/* PlanCAD Stage — catalogue, routeur et moteurs de rendu.
   Architecture reprise d'ArtCraft (model-list + artcraft_router), réécrite :
   build(requête) → plan validé → estimate(plan) → send(plan).

   Deux moteurs gratuits, sans carte, sans Google :
   - « bridge »  : pont local tools/render_bridge.py → Cloudflare Workers AI.
                   Les identifiants restent sur la machine, jamais dans la page.
   - « hf-space » : Space Hugging Face appelé depuis le navigateur (@gradio/client),
                   quota GPU gratuit, marche aussi sur la page GitHub publique. */
'use strict';
(function () {
  const GRADIO = 'https://cdn.jsdelivr.net/npm/@gradio/client@2.7.0/dist/index.min.js';
  const BRIDGE_PORT = 8790;

  class RouterError extends Error {
    constructor(code, message) { super(message); this.code = code; }
  }
  const R = { catalog: null, bridge: null, bridgeInfo: null, hfToken: '' };
  try { R.hfToken = localStorage.getItem('plancad_hf_token') || ''; } catch (e) { /* stockage indisponible */ }
  const LOCAL_PAGE = /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(location.origin);
  const OPT_IN = 'plancad_bridge_optin';

  async function load() {
    R.catalog = await fetch('stage/catalog.json').then(r => r.json());
    return R.catalog;
  }
  /* Une page publique qui appelle 127.0.0.1 fait afficher par Chrome une demande
     d'autorisation « réseau local » : on ne sonde donc le pont que depuis une page
     locale, ou après un clic (force) dont le succès est mémorisé.
     Renvoie true (pont trouvé), false (absent) ou null (pas sondé). */
  async function probeBridge(force) {
    let optIn = false;
    try { optIn = localStorage.getItem(OPT_IN) === '1'; } catch (e) { /* stockage indisponible */ }
    if (!LOCAL_PAGE && !force && !optIn) { R.bridge = null; R.bridgeInfo = null; return null; }
    const bases = [];
    if (LOCAL_PAGE && location.port === String(BRIDGE_PORT)) bases.push('');
    bases.push(`http://127.0.0.1:${BRIDGE_PORT}`);
    for (const b of bases) {
      try {
        const r = await fetch(b + '/api/models', { cache: 'no-store' });
        if (r.ok) {
          R.bridgeInfo = await r.json();
          R.bridge = b;
          if (!LOCAL_PAGE) { try { localStorage.setItem(OPT_IN, '1'); } catch (e) { /* stockage indisponible */ } }
          return true;
        }
      } catch (e) { /* pont absent */ }
    }
    R.bridge = null;
    R.bridgeInfo = null;
    return false;
  }

  const denied = m => (R.catalog.deny.creators || []).includes(m.creator) || !m.free;
  function available(p) {
    if (p.runtime === 'bridge') {
      if (R.bridge === null) return false;
      const bm = R.bridgeInfo && R.bridgeInfo.models;
      if (!bm) return true;
      return bm.some(m => m.providers.some(q => q.id === p.id && q.available));
    }
    if (p.runtime === 'browser') return navigator.onLine !== false;
    return false;
  }
  function models(filter) { return R.catalog.models.filter(m => !denied(m) && (!filter || filter(m))); }
  function providersOf(m) { return m.providers.map(p => Object.assign({}, p, { available: available(p) })); }
  const providerLabel = p => p.id === 'cloudflare' ? 'Cloudflare (pont local)' : p.id === 'hf-space' ? 'Hugging Face (navigateur)' : p.id;

  function outSize(aspect, p, dims) {
    let [w, h] = dims || R.catalog.sizes[aspect] || [1280, 720];
    const k = Math.min(1, (p.maxSide || 2048) / Math.max(w, h));
    return [Math.max(256, Math.round(w * k / 16) * 16), Math.max(256, Math.round(h * k / 16) * 16)];
  }
  function fitted(w, h, maxPx) { const k = Math.min(1, maxPx / Math.max(w, h)); return [Math.max(1, Math.floor(w * k)), Math.max(1, Math.floor(h * k))]; }

  /* build : valide la requête contre les capacités du modèle et choisit le moteur */
  function build(req) {
    const m = R.catalog.models.find(x => x.id === req.model);
    if (!m) throw new RouterError('unsupported_model', 'modèle inconnu : ' + req.model);
    if (denied(m)) throw new RouterError('forbidden_model', R.catalog.deny.reason);
    const caps = m.capabilities, refs = req.refs || [];
    if (!(req.prompt || '').trim()) throw new RouterError('invalid_input', 'consigne vide');
    if (refs.length && !caps.editImages) throw new RouterError('invalid_input', m.label + " n'accepte pas d'image de référence : il ne suivrait pas le plan");
    if (!refs.length && !caps.textToImage) throw new RouterError('invalid_input', m.label + ' exige la capture comme référence');
    if (refs.length > caps.maxRefs) throw new RouterError('invalid_input', `${m.label} accepte au plus ${caps.maxRefs} référence(s)`);
    const provs = m.providers.filter(p => (!req.provider || p.id === req.provider) && available(p));
    if (!provs.length) {
      const needsBridge = m.providers.every(p => p.runtime === 'bridge');
      throw new RouterError('unsupported_provider', `aucun moteur disponible pour ${m.label}` +
        (needsBridge ? ' : lancer « python3 tools/render_bridge.py » puis ouvrir http://127.0.0.1:8790/cad.html' : ''));
    }
    const p = provs[0];
    const [w, h] = outSize(req.aspect, p);
    const refSizes = (req.refSizes || refs.map(() => [w, h])).map(([rw, rh]) => fitted(rw, rh, p.maxRefPx || 1024));
    return { model: m, provider: p, prompt: req.prompt.trim(), refs, refSizes, aspect: req.aspect, w, h,
      seed: req.seed | 0, meta: req.meta || {} };
  }

  /* estimate : même barème que render_router.py (le pont revérifie le budget) */
  const tiles = (w, h) => Math.ceil(w / 512) * Math.ceil(h / 512);
  function estimate(plan) {
    const c = plan.provider.cost;
    if (c.unit === 'gpu-s') return { unit: 'gpu-s', amount: c.estimate, label: `≈ ${c.estimate} s de GPU gratuit Hugging Face` };
    const out = tiles(plan.w, plan.h), inp = plan.refSizes.reduce((s, [w, h]) => s + tiles(w, h), 0);
    let n;
    if ('inputTilePerStep' in c) n = (plan.provider.steps || 20) * (c.inputTilePerStep * inp + c.outputTilePerStep * out);
    else if ('firstMp' in c) {
      const mp = Math.ceil(plan.w * plan.h / 1048576), inMp = plan.refSizes.reduce((s, [w, h]) => s + Math.ceil(w * h / 1048576), 0);
      n = c.firstMp + c.extraMp * Math.max(0, mp - 1) + c.inputMp * inMp;
    } else if ('perStep' in c) n = c.outputTile * out + c.perStep * (plan.provider.steps || 8);
    else n = c.inputTile * inp + c.outputTile * out;
    n = Math.round(n * 10) / 10;
    return { unit: 'neurons', amount: n, label: `≈ ${Math.round(n)} neurones Cloudflare (quota gratuit)` };
  }

  /* ── moteur « bridge » : envoi au pont local, qui garde la file de tâches ── */
  async function bridgeJSON(path, opts) {
    const r = await fetch(R.bridge + path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const e = j.error || {};
      throw new RouterError(e.code || 'provider_error', e.message || ('pont local : HTTP ' + r.status));
    }
    return j;
  }
  async function sendBridge(plan, onTask) {
    const media = [];
    for (const blob of plan.refs) {
      const j = await bridgeJSON('/api/media', { method: 'POST', headers: { 'Content-Type': 'image/png' }, body: blob });
      media.push(j.id);
    }
    const { task } = await bridgeJSON('/api/generate/image', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model: plan.model.id, provider: plan.provider.id, prompt: plan.prompt, media,
        aspect: plan.aspect, seed: plan.seed, meta: plan.meta }) });
    if (onTask) onTask(task);
    for (;;) {
      await new Promise(res => setTimeout(res, 1500));
      const { task: t } = await bridgeJSON('/api/tasks/' + task.id);
      if (t.status === 'complete_success') return { url: R.bridge + t.result_url, bridgeTask: t };
      if (t.status === 'complete_failure') throw new RouterError(t.error_code || 'provider_error', t.error_message || 'échec du rendu');
    }
  }

  /* ── moteur « hf-space » : Space Hugging Face depuis le navigateur ── */
  let gradioMod = null;
  const clients = {};
  async function gradio() { gradioMod = gradioMod || import(GRADIO); return gradioMod; }
  function shrink(blob, maxPx) {
    return createImageBitmap(blob).then(bmp => {
      const [w, h] = fitted(bmp.width, bmp.height, maxPx);
      if (w === bmp.width && h === bmp.height) return blob;
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      c.getContext('2d').drawImage(bmp, 0, 0, w, h);
      return new Promise(res => c.toBlob(res, 'image/png'));
    });
  }
  async function sendHF(plan) {
    const { Client, handle_file } = await gradio();
    const p = plan.provider;
    const opts = R.hfToken ? { hf_token: R.hfToken, token: R.hfToken } : {};
    try {
      clients[p.space] = clients[p.space] || await Client.connect(p.space, opts);
      const refs = await Promise.all(plan.refs.map(b => shrink(b, p.maxRefPx || 1024)));
      let params;
      if (p.api === 'flux2-klein') {
        params = { prompt: plan.prompt, input_images: refs.map(b => ({ image: handle_file(b), caption: null })),
          mode_choice: 'Distilled (4 steps)', seed: plan.seed, randomize_seed: false, width: plan.w, height: plan.h,
          num_inference_steps: 4, guidance_scale: 1.0, prompt_upsampling: false };
      } else if (p.api === 'kontext') {
        params = { input_image: handle_file(refs[0]), prompt: plan.prompt, seed: plan.seed, randomize_seed: false,
          guidance_scale: 2.5, steps: 28 };
      } else if (p.api === 'qwen-edit') {
        params = { image: handle_file(refs[0]), prompt: plan.prompt, seed: plan.seed, randomize_seed: false,
          true_guidance_scale: 4.0, num_inference_steps: 30, rewrite_prompt: false };
      } else throw new RouterError('unsupported_provider', 'API de Space inconnue : ' + p.api);
      const res = await clients[p.space].predict(p.endpoint, params);
      const out = res && res.data && res.data[0];
      const url = out && (out.url || (out.path && `https://${p.space.replace('/', '-').replace(/\./g, '-').toLowerCase()}.hf.space/gradio_api/file=${out.path}`));
      if (!url) throw new RouterError('provider_error', 'réponse du Space sans image');
      const blob = await fetch(url).then(r => r.blob());
      return { url: URL.createObjectURL(blob), remoteUrl: url, blob };
    } catch (e) {
      if (e instanceof RouterError) throw e;
      const msg = (e && (e.message || e.title || e.toString())) || 'erreur inconnue';
      const quota = /quota|gpu|exceeded|limit/i.test(msg);
      delete clients[p.space];
      throw new RouterError(quota ? 'quota_exceeded' : 'provider_error',
        `Hugging Face (${p.space}) : ${msg.slice(0, 220)}` + (quota && !R.hfToken ? ' — un jeton HF gratuit (réglages) donne plus de quota' : ''));
    }
  }

  function send(plan, onTask) {
    if (plan.provider.runtime === 'bridge') return sendBridge(plan, onTask);
    if (plan.provider.id === 'hf-space') return sendHF(plan);
    return Promise.reject(new RouterError('unsupported_provider', plan.provider.id));
  }

  /* ── consigne : composée à partir de plan.json (pièce, cotes, ouvertures, mobilier) ── */
  const ROOM_EN = [[/LIVING|SEJOUR|SÉJOUR|SALON/i, 'living room'], [/BED|CHAMBRE/i, 'bedroom'], [/KITCHEN|CUISINE/i, 'kitchen'],
    [/BATH|SDB|SALLE DE BAIN/i, 'bathroom'], [/SHOWER|DOUCHE/i, 'shower room'], [/\bWC\b|TOILET/i, 'toilet room'],
    [/CHANGING|DRESSING/i, 'walk-in dressing room'], [/HALL|ENTR|DGT|COULOIR|CORRIDOR/i, 'entrance hall'],
    [/OFFICE|BUREAU/i, 'home office'], [/CELLIER|LAUNDRY|BUANDERIE/i, 'utility room'], [/DINING|REPAS/i, 'dining room']];
  const FURN_EN = [[/lit double|double bed/i, 'double bed'], [/lit simple|lit\b|bed/i, 'bed'], [/armoire|penderie|wardrobe/i, 'wardrobe'],
    [/canap|sofa/i, 'sofa'], [/table basse|coffee/i, 'coffee table'], [/table à manger|table a manger|dining/i, 'dining table with chairs'],
    [/meuble tv|tv/i, 'low TV unit'], [/plan de travail|cuisine|counter/i, 'kitchen counter'], [/frigo|réfrig|fridge/i, 'fridge'],
    [/baignoire|bath/i, 'bathtub'], [/douche|shower/i, 'shower'], [/lave-mains|lave mains/i, 'small hand basin'],
    [/lavabo|vasque|sink|basin/i, 'washbasin'], [/wc|cuvette|toilet/i, 'toilet'], [/bureau|desk/i, 'desk'], [/chaise|chair/i, 'chair']];
  const en = (s, table) => { const hit = table.find(([re]) => re.test(s)); return hit ? hit[1] : s; };

  function promptFor(room, styleId, extra) {
    const style = (R.catalog.styles.find(s => s.id === styleId) || R.catalog.styles[0]).text;
    const H = (typeof MODEL !== 'undefined' && MODEL && MODEL.wall_height) || 2.5;
    const parts = [];
    if (room && room.rect) {
      const w = (room.rect[2] - room.rect[0]).toFixed(2), d = (room.rect[3] - room.rect[1]).toFixed(2);
      parts.push(`Photorealistic interior photograph of a ${en(room.name, ROOM_EN)} (${w} × ${d} m, ${H} m ceiling).`);
    } else parts.push('Photorealistic interior photograph of this room.');
    parts.push('Image 1 is a plain 3D layout render of this exact room: keep the same camera position and angle, the same walls, doors and windows, and the position, size and number of every object. Only replace the flat colours with real materials, textures and light.');
    const furn = room && (room.furniture || []).map(f => en(f.item, FURN_EN));
    if (furn && furn.length) parts.push('Furniture to keep in place: ' + [...new Set(furn)].join(', ') + '.');
    const nWin = room ? (room.windows || []).length : 0;
    parts.push(nWin ? `Soft natural daylight through the ${nWin > 1 ? nWin + ' windows' : 'window'}.` : 'No window: warm artificial lighting from recessed ceiling spots.');
    parts.push(`Style: ${style}.`);
    if (extra) parts.push(extra);
    parts.push('Architectural photography, straight verticals, realistic proportions, no people, no text.');
    return parts.join(' ');
  }

  window.Router = { R, RouterError, load, probeBridge, models, providersOf, providerLabel, available, build, estimate, send, promptFor,
    setHfToken(t) { R.hfToken = (t || '').trim(); try { localStorage.setItem('plancad_hf_token', R.hfToken); } catch (e) { /* idem */ } } };
})();
