/* PlanCAD Stage — interface : panneau caméras / réglages / rendu photo / file d'attente,
   et visionneuse (capture, rendu, côte à côte). Ne parle au moteur que par
   l'API de Stage, Router et Tasks (même découpage qu'ArtCraft : l'interface lit
   l'état et appelle des actions, jamais l'inverse). */
'use strict';
(function () {
  const $ = (s, el) => (el || document).querySelector(s);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const UI = { open: false, promptDirty: false, lastCamForPrompt: null, ready: false, toastTimer: null };

  const PANEL = `
  <div class="st-head"><b>🎬 Stage</b><span class="st-sub">caméras · capture · rendu photo</span>
    <button class="st-x" data-act="close" title="Fermer">×</button></div>
  <section>
    <h4>Caméras</h4>
    <div class="st-tools">
      <button data-act="place">＋ Placer sur le plan</button>
      <button data-act="auto" title="Caméra automatique pour la pièce sélectionnée sur le plan">Auto : pièce choisie</button>
      <button data-act="autoall">Auto : toutes les pièces</button>
    </div>
    <p class="st-hint">Plan 2D : glisser une caméra pour la déplacer, sa poignée ronde pour l'orienter.
      3D : double-clic sur une caméra pour regarder à travers. En visite 🚶, touche C = caméra ici.</p>
    <ul class="st-cams"></ul>
  </section>
  <section class="st-edit" hidden>
    <h4>Réglages de <span class="st-ename"></span></h4>
    <div class="st-grid">
      <label>Nom<input data-f="label"></label>
      <label>Focale<select data-f="focal"></select></label>
      <label>Format<select data-f="aspect"></select></label>
      <label>Hauteur (m)<input data-f="h" type="number" step="0.05" min="0.4" max="2.4"></label>
      <label>Cap (°)<input data-f="yaw" type="number" step="5"></label>
      <label>Inclinaison (°)<input data-f="pitch" type="number" step="2" min="-60" max="60"></label>
    </div>
    <div class="st-tools">
      <button data-act="view">👁 Regarder à travers</button>
      <button data-act="gizmo-translate" class="st-gz">↔ Déplacer (3D)</button>
      <button data-act="gizmo-rotate" class="st-gz">⟳ Tourner (3D)</button>
    </div>
  </section>
  <section class="st-render">
    <h4>Rendu photo</h4>
    <div class="st-grid">
      <label class="st-wide">Modèle<select data-r="model"></select></label>
      <label class="st-wide">Moteur<select data-r="provider"></select></label>
      <label>Style<select data-r="style"></select></label>
      <label>Graine<input data-r="seed" type="number" step="1"></label>
    </div>
    <div class="st-cost"></div>
    <label class="st-wide st-plabel">Consigne <button class="st-link" data-act="reprompt">↺ recomposer depuis le plan</button>
      <textarea data-r="prompt" rows="6"></textarea></label>
    <div class="st-tools st-main">
      <button data-act="capture">📸 Capturer</button>
      <button data-act="render" class="st-primary">✨ Capturer et rendre</button>
    </div>
    <div class="st-engines"></div>
    <details class="st-hf"><summary>Jeton Hugging Face (facultatif)</summary>
      <p class="st-hint">Sans jeton, le quota GPU gratuit est celui d'un visiteur anonyme. Un jeton gratuit
        (huggingface.co → Settings → Access Tokens, lecture seule) augmente ce quota. Il reste dans ce navigateur.</p>
      <input data-r="hftoken" type="password" placeholder="hf_…" autocomplete="off"></details>
  </section>
  <section>
    <h4>File d'attente <button class="st-link" data-act="clear">vider</button></h4>
    <div class="st-tasks"></div>
  </section>
  <div class="st-toast" hidden></div>`;

  const LIGHTBOX = `
  <div class="st-lb-box">
    <div class="st-lb-bar"><b class="st-lb-title"></b>
      <span class="st-lb-tabs"><button data-tab="capture">Capture 3D</button><button data-tab="result">Rendu IA</button><button data-tab="both">Côte à côte</button></span>
      <button class="st-x" data-act="lb-close">×</button></div>
    <div class="st-lb-img"></div>
    <div class="st-lb-info"></div>
    <div class="st-tools"><a class="st-btn" data-act="lb-dl" download>⬇ Télécharger</a>
      <button data-act="lb-render" class="st-primary">✨ Rendre cette capture</button>
      <button data-act="lb-again">↻ Nouvelle graine</button></div>
  </div>`;

  /* ── construction ── */
  function build() {
    const head = $('header');
    const btn = document.createElement('button');
    btn.id = 'bstage';
    btn.textContent = '🎬 Stage';
    btn.title = 'Caméras de rendu, capture et rendu photo';
    head.insertBefore(btn, $('label.btn', head));
    btn.onclick = () => toggle();
    const panel = document.createElement('aside');
    panel.id = 'stage';
    panel.className = 'st-panel';
    panel.innerHTML = PANEL;
    document.body.appendChild(panel);
    const lb = document.createElement('div');
    lb.className = 'st-lightbox';
    lb.hidden = true;
    lb.innerHTML = LIGHTBOX;
    document.body.appendChild(lb);
    panel.addEventListener('click', onClick);
    lb.addEventListener('click', onClick);
    panel.addEventListener('change', onField);
    panel.addEventListener('input', onInput);
    setInterval(tickProgress, 500);
  }
  function toggle(force) {
    UI.open = force == null ? !UI.open : force;
    $('#stage').classList.toggle('on', UI.open);
    $('#bstage').classList.toggle('on', UI.open);
    $('#main').style.right = UI.open ? '360px' : '0';
    requestAnimationFrame(() => {                 // les panneaux ont changé de taille
      if (typeof MODEL !== 'undefined' && MODEL) dispatchEvent(new Event('resize'));
      else if (typeof resize3d === 'function') resize3d();
    });
    if (UI.open) refreshEngines();
  }
  function toast(msg, ms) {
    const t = $('.st-toast');
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(UI.toastTimer);
    UI.toastTimer = setTimeout(() => { t.hidden = true; }, ms || 4200);
  }

  /* ── rendu de l'état ── */
  function renderCams() {
    const S = Stage.S, ul = $('.st-cams');
    if (!S.cams.length) { ul.innerHTML = '<li class="st-empty">Aucune caméra : « Auto : toutes les pièces » en pose une par pièce.</li>'; }
    else ul.innerHTML = S.cams.map(c => `
      <li class="${c.id === S.sel ? 'sel' : ''}${c.id === S.view ? ' viewing' : ''}" data-cam="${c.id}">
        <span class="st-cname">${esc(c.label)}</span><span class="st-cmeta">${c.focal} mm · ${esc(c.aspect)}</span>
        <span class="st-cact"><button data-act="cam-view" title="Regarder à travers">👁</button><button data-act="cam-shot" title="Capturer">📸</button><button data-act="cam-del" title="Supprimer">🗑</button></span>
      </li>`).join('');
    const c = Stage.find(S.sel), ed = $('.st-edit');
    ed.hidden = !c;
    if (c) {
      $('.st-ename').textContent = c.label;
      ['label', 'focal', 'aspect'].forEach(f => { const el = $(`[data-f="${f}"]`); if (document.activeElement !== el) el.value = c[f]; });
      ['h', 'yaw', 'pitch'].forEach(f => { const el = $(`[data-f="${f}"]`); if (document.activeElement !== el) el.value = (+c[f]).toFixed(f === 'h' ? 2 : 0); });
      ed.querySelectorAll('.st-gz').forEach(b => b.classList.toggle('on', b.dataset.act === 'gizmo-' + S.gizmoMode));
      $('[data-act="view"]').textContent = S.view === c.id ? '⏏ Sortir de la vue caméra' : '👁 Regarder à travers';
    }
    if (UI.ready && (!UI.promptDirty || UI.lastCamForPrompt !== S.sel)) recomposePrompt();
    updateCost();
  }
  function selectedModel() { const cat = Router.R.catalog; return cat && cat.models.find(m => m.id === $('[data-r="model"]').value); }
  function fillRenderControls(cat) {
    const ms = Router.models(m => m.capabilities.editImages || m.capabilities.textToImage);
    $('[data-r="model"]').innerHTML = ms.map(m => `<option value="${m.id}">${esc(m.label)}${m.capabilities.editImages ? '' : ' ⚠'}</option>`).join('');
    $('[data-r="model"]').value = cat.defaults.model;
    $('[data-r="style"]').innerHTML = cat.styles.map(s => `<option value="${s.id}">${esc(s.label)}</option>`).join('');
    $('[data-r="style"]').value = cat.defaults.style;
    $('[data-r="seed"]').value = cat.defaults.seed;
    $('[data-f="focal"]').innerHTML = cat.focals.map(f => `<option value="${f}">${f} mm</option>`).join('');
    $('[data-f="aspect"]').innerHTML = Object.keys(cat.sizes).map(a => `<option value="${a}">${a} (${cat.sizes[a].join('×')})</option>`).join('');
    $('[data-r="hftoken"]').value = Router.R.hfToken;
    fillProviders();
  }
  function fillProviders() {
    const m = selectedModel();
    if (!m) return;
    const sel = $('[data-r="provider"]'), prev = sel.value;
    const ps = Router.providersOf(m);
    sel.innerHTML = ps.map(p => `<option value="${p.id}" ${p.available ? '' : 'disabled'}>${esc(Router.providerLabel(p))}${p.available ? '' : ' (indisponible)'}</option>`).join('');
    const pick = (UI.providerChosen && ps.find(p => p.id === prev && p.available)) || ps.find(p => p.available) || ps[0];   // sinon le 1er du catalogue (Cloudflare si le pont répond)
    if (pick) sel.value = pick.id;
    updateCost();
  }
  function updateCost() {
    const box = $('.st-cost');
    if (!box || !Router.R.catalog) return;
    const m = selectedModel(), c = Stage.find(Stage.S.sel);
    if (!m) { box.textContent = ''; return; }
    const warn = m.capabilities.editImages ? '' : '<div class="st-warn">⚠ Texte seul : ce modèle invente une pièce, il ne suit pas le plan.</div>';
    try {
      const size = Stage.sizeFor(c ? c.aspect : Router.R.catalog.defaults.aspect);
      const plan = Router.build({ model: m.id, provider: $('[data-r="provider"]').value, prompt: 'x',
        aspect: c ? c.aspect : Router.R.catalog.defaults.aspect, refs: m.capabilities.editImages ? [null] : [], refSizes: [size] });
      const est = Router.estimate(plan);
      const b = Router.R.bridgeInfo && Router.R.bridgeInfo.budget;
      const left = est.unit === 'neurons' && b ? ` · reste ${Math.round(b.left)} / ${b.cap} aujourd'hui` : '';
      box.innerHTML = `${esc(est.label)}${left} · sortie ${plan.w}×${plan.h}<div class="st-blurb">${esc(m.blurb || '')}</div>${warn}`;
    } catch (e) { box.innerHTML = `<span class="st-err">${esc(e.message)}</span>${warn}`; }
  }
  function recomposePrompt() {
    const c = Stage.find(Stage.S.sel);
    const room = c && MODEL && MODEL.rooms.find(r => r.name === c.room);
    $('[data-r="prompt"]').value = Router.promptFor(room, $('[data-r="style"]').value);
    UI.promptDirty = false;
    UI.lastCamForPrompt = Stage.S.sel;
  }
  function refreshEngines(force) {
    if (!UI.ready) return;                         // catalogue pas encore chargé : start() rappellera
    Router.probeBridge(force).then(found => {
      $('.st-engines').innerHTML = found ? '<span class="st-ok">● Pont local actif</span> : Cloudflare FLUX.2 disponible.'
        : found === false ? '<span class="st-off">● Pont local absent</span> : Cloudflare indisponible, rendu par Hugging Face depuis le navigateur. '
          + 'Pour Cloudflare : <code>python3 tools/render_bridge.py</code> puis <code>http://127.0.0.1:8790/cad.html</code>.'
        : '<span class="st-ok">● Hugging Face</span> : rendu depuis le navigateur, rien à installer. '
          + '<button class="st-link" data-act="bridge">Utiliser mon pont local (Cloudflare)</button>';
      fillProviders();
    });
  }

  const STATUS = { pending: 'en attente', started: 'en cours', complete_success: 'terminé', complete_failure: 'échec' };
  function renderTasks() {
    const box = $('.st-tasks'), list = Tasks.T.list;
    if (!list.length) { box.innerHTML = '<p class="st-empty">Aucun rendu pour l\'instant.</p>'; return; }
    box.innerHTML = list.map(t => `
      <div class="st-task ${t.status}" data-task="${t.id}">
        <div class="st-thumbs">
          ${t.captureUrl ? `<img src="${esc(t.captureUrl)}" data-act="lb-open" title="Capture 3D">` : '<span class="st-noimg">capture</span>'}
          <span class="st-arrow">→</span>
          ${t.resultUrl ? `<img src="${esc(t.resultUrl)}" data-act="lb-open" title="Rendu IA">` : `<span class="st-noimg">${t.status === 'complete_failure' ? '✗' : '…'}</span>`}
        </div>
        <div class="st-tmeta"><b>${esc(t.camLabel || '')}</b> · ${esc(t.modelLabel)}${t.provider ? ' · ' + esc(t.provider) : ''}
          <span class="st-tstat">${STATUS[t.status] || t.status}${t.cost ? ' · ' + esc(t.cost.label) : ''}</span>
          ${t.error ? `<span class="st-err">${esc(t.error.message)}</span>` : ''}</div>
        <div class="st-bar"><i style="width:${Tasks.progress(t)}%"></i></div>
        <button class="st-tx" data-act="task-del" title="Retirer">×</button>
      </div>`).join('');
  }
  function tickProgress() {
    document.querySelectorAll('.st-task.started').forEach(el => {
      const t = Tasks.T.list.find(x => x.id === el.dataset.task);
      if (t) el.querySelector('.st-bar i').style.width = Tasks.progress(t) + '%';
    });
  }

  /* ── visionneuse ── */
  const LB = { shot: null, task: null, tab: 'capture' };
  function openLightbox(o) {
    Object.assign(LB, { shot: o.shot || null, task: o.task || null, tab: o.tab || (o.task && o.task.resultUrl ? 'result' : 'capture') });
    $('.st-lightbox').hidden = false;
    drawLightbox();
  }
  function drawLightbox() {
    const t = LB.task, s = LB.shot;
    const cap = s ? s.url : t && t.captureUrl, res = t && t.resultUrl;
    $('.st-lb-title').textContent = (s ? s.label : t && t.camLabel) || 'Capture';
    document.querySelectorAll('.st-lb-tabs button').forEach(b => {
      b.classList.toggle('on', b.dataset.tab === LB.tab);
      b.disabled = (b.dataset.tab !== 'capture' && !res) || (b.dataset.tab !== 'result' && !cap);
    });
    const img = u => u ? `<img src="${esc(u)}">` : '';
    $('.st-lb-img').className = 'st-lb-img' + (LB.tab === 'both' ? ' both' : '');
    $('.st-lb-img').innerHTML = LB.tab === 'both' ? img(cap) + img(res) : img(LB.tab === 'result' ? res : cap);
    const dl = LB.tab === 'result' ? res : cap;
    const a = $('[data-act="lb-dl"]');
    a.href = dl || '#';
    a.download = ((s ? s.label : t && t.camLabel) || 'plancad').replace(/[^\w-]+/g, '_') + (LB.tab === 'result' ? '_rendu.png' : '_capture.png');
    $('[data-act="lb-render"]').hidden = !s;
    $('[data-act="lb-again"]').hidden = !t || !t.captureUrl;
    $('.st-lb-info').innerHTML = t ? `<b>${esc(t.modelLabel)}</b> via ${esc(t.provider || '?')} · graine ${t.seed} · ${STATUS[t.status]}${t.cost ? ' · ' + esc(t.cost.label) : ''}
        <div class="st-honest">Rendu IA à partir de la capture : vérifier la fidélité (murs, ouvertures, mobilier) en « Côte à côte ».</div>
        <details><summary>Consigne envoyée</summary><p>${esc(t.prompt)}</p></details>`
      : s ? `${esc(s.room || '')} · ${s.focal} mm · ${s.w}×${s.h} : capture brute de PlanCAD (géométrie exacte du plan).` : '';
  }

  /* ── actions ── */
  function currentRequest(capture, cam, seedBump) {
    const seed = (parseInt($('[data-r="seed"]').value, 10) || 0) + (seedBump || 0);
    if (seedBump) $('[data-r="seed"]').value = seed;
    return { model: $('[data-r="model"]').value, provider: $('[data-r="provider"]').value, prompt: $('[data-r="prompt"]').value,
      aspect: cam.aspect, seed, capture, cam: { id: cam.id, label: cam.label, room: cam.room } };
  }
  async function doCapture(render) {
    const c = Stage.find(Stage.S.sel);
    if (!c) { toast('Sélectionne d\'abord une caméra (ou « Auto : toutes les pièces »).'); return; }
    const shot = await Stage.capture(c.id);
    if (!render) { openLightbox({ shot }); return; }
    enqueueShot(shot, c, 0);
  }
  function enqueueShot(shot, cam, bump) {
    try {
      const req = currentRequest(shot, cam, bump);
      Router.build(Object.assign({ refs: [shot.blob], refSizes: [[shot.w, shot.h]] }, req));   // validation immédiate
      Tasks.enqueue(req);
      toast('Rendu lancé : ' + cam.label);
    } catch (e) { toast(e.message, 7000); }
  }
  async function rerender(t) {
    const blob = await fetch(t.captureUrl).then(r => r.blob());
    const cam = Stage.find(t.camId) || { id: t.camId, label: t.camLabel, room: t.room, aspect: t.aspect };
    const shot = { blob, url: t.captureUrl, w: t.captureSize[0], h: t.captureSize[1] };
    enqueueShot(shot, Object.assign({}, cam, { aspect: t.aspect }), 1);
  }
  function onClick(e) {
    const tab = e.target.closest('.st-lb-tabs [data-tab]');   // onglets de la visionneuse
    if (tab) { if (!tab.disabled) { LB.tab = tab.dataset.tab; drawLightbox(); } return; }
    const el = e.target.closest('[data-act]');
    const camEl = e.target.closest('[data-cam]'), taskEl = e.target.closest('[data-task]');
    if (camEl && (!el || !el.closest('.st-cact'))) Stage.select(camEl.dataset.cam);   // clic sur une ligne
    if (!el) return;
    const act = el.dataset.act, S = Stage.S;
    const camId = camEl && camEl.dataset.cam, task = taskEl && Tasks.T.list.find(t => t.id === taskEl.dataset.task);
    switch (act) {
      case 'close': toggle(false); break;
      case 'bridge': refreshEngines(true); break;
      case 'place': Stage.startPlacing(); if ($('#main').classList.contains('v3d')) $('#bsp').click(); break;
      case 'auto': {
        const r = (typeof SEL === 'number' && SEL >= 0) ? MODEL.rooms[SEL] : null;
        if (!r || !r.rect) toast('Clique d\'abord une pièce sur le plan 2D.');
        else Stage.autoCam(r);
        break;
      }
      case 'autoall': Stage.autoAll(); break;
      case 'view': S.view === S.sel ? Stage.exitView() : Stage.enterView(S.sel); break;
      case 'gizmo-translate': Stage.setGizmoMode('translate'); break;
      case 'gizmo-rotate': Stage.setGizmoMode('rotate'); break;
      case 'cam-view': Stage.enterView(camId); break;
      case 'cam-shot': Stage.select(camId); doCapture(false); break;
      case 'cam-del': Stage.removeCam(camId); break;
      case 'capture': doCapture(false); break;
      case 'render': doCapture(true); break;
      case 'reprompt': recomposePrompt(); break;
      case 'clear': Tasks.clearDone(); break;
      case 'task-del': Tasks.remove(task.id); break;
      case 'lb-open': openLightbox({ task, tab: e.target.title === 'Rendu IA' ? 'result' : 'capture' }); break;
      case 'lb-close': $('.st-lightbox').hidden = true; break;
      case 'lb-render': {
        const cam = Stage.find(LB.shot.camId);
        if (cam) { enqueueShot(LB.shot, cam, 0); $('.st-lightbox').hidden = true; }
        break;
      }
      case 'lb-again': rerender(LB.task); $('.st-lightbox').hidden = true; break;
      default: break;
    }
  }
  function onField(e) {
    const f = e.target.dataset.f, r = e.target.dataset.r, S = Stage.S;
    if (f && S.sel) {
      const v = e.target.value;
      Stage.updateCam(S.sel, { [f]: (f === 'label' || f === 'aspect') ? v : +v });
    }
    if (r === 'model') fillProviders();
    if (r === 'provider') { UI.providerChosen = true; updateCost(); }
    if (r === 'style' && !UI.promptDirty) recomposePrompt();
    if (r === 'hftoken') Router.setHfToken(e.target.value);
  }
  function onInput(e) {
    if (e.target.dataset.r === 'prompt') UI.promptDirty = true;
    if (e.target.dataset.f === 'label' && Stage.S.sel) Stage.updateCam(Stage.S.sel, { label: e.target.value });
  }
  document.addEventListener('click', e => {
    const lb = $('.st-lightbox');
    if (lb && !lb.hidden && e.target === lb) lb.hidden = true;
  });

  /* ── démarrage ── */
  function start() {
    build();
    Stage.Bus.on('change', renderCams);
    Tasks.on(renderTasks);
    Router.load().then(cat => {
      fillRenderControls(cat);
      UI.ready = true;
      renderCams();
      refreshEngines();
    }).catch(err => toast('Catalogue des moteurs illisible : ' + err.message, 8000));
    Tasks.load();
    renderTasks();
    renderCams();
    if (new URLSearchParams(location.search).get('stage') === '1') toggle(true);
  }
  window.StageUI = { start, toggle, openLightbox };
})();
