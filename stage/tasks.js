/* PlanCAD Stage — file de tâches de rendu (reprise de la TaskQueue d'ArtCraft).
   États : pending → started → complete_success | complete_failure.
   Une tâche à la fois (respect des quotas gratuits). Les métadonnées sont gardées
   dans le navigateur ; les images du pont local restent sur disque (work/stage/media). */
'use strict';
(function () {
  const KEY = 'plancad_stage_tasks_v1';
  const handlers = [];
  const T = { list: [], running: false };
  const emit = () => handlers.forEach(f => { try { f(T); } catch (e) { console.error('Tasks', e); } });
  const durable = u => (u && /^(https?:|\/media\/)/.test(u) ? u : null);   // URL web ou image du pont local

  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify(T.list.slice(0, 60).map(t => {
        const o = Object.assign({}, t);
        delete o._refs;
        o.captureUrl = durable(t.captureUrl);
        o.resultUrl = durable(t.resultRemote || t.resultUrl);
        delete o.resultRemote;
        return o;
      })));
    } catch (e) { /* stockage plein ou indisponible */ }
  }
  function load() {
    try { T.list = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { T.list = []; }
    T.list.forEach(t => {
      if (t.status === 'pending' || t.status === 'started') {
        t.status = 'complete_failure';
        t.error = { code: 'interrupted', message: 'page rechargée pendant le rendu' };
      }
    });
    emit();
  }

  /* req : {model, provider?, prompt, aspect, seed, capture:{blob,url,w,h}, cam:{id,label,room}, meta?} */
  function enqueue(req) {
    const m = Router.R.catalog.models.find(x => x.id === req.model);
    const t = { id: 'j' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5), status: 'pending',
      model: req.model, modelLabel: m ? m.label : req.model, provider: req.provider || '', prompt: req.prompt,
      aspect: req.aspect, seed: req.seed, camId: req.cam && req.cam.id, camLabel: req.cam && req.cam.label,
      room: req.cam && req.cam.room, captureUrl: req.capture.url, captureSize: [req.capture.w, req.capture.h],
      progressMs: (m && m.progressMs) || 20000, createdAt: Date.now(), _refs: [req.capture.blob] };
    T.list.unshift(t);
    save();
    emit();
    pump();
    return t;
  }

  async function pump() {
    if (T.running) return;
    const t = T.list.slice().reverse().find(x => x.status === 'pending');
    if (!t) return;
    T.running = true;
    t.status = 'started';
    t.startedAt = Date.now();
    emit();
    try {
      const plan = Router.build({ model: t.model, provider: t.provider, prompt: t.prompt, aspect: t.aspect, seed: t.seed,
        refs: t._refs || [], refSizes: [t.captureSize], meta: { camera: t.camLabel, room: t.room } });
      t.provider = plan.provider.id;
      t.cost = Router.estimate(plan);
      emit();
      const res = await Router.send(plan, bt => {
        t.bridgeTaskId = bt.id;
        if (bt.input_urls && bt.input_urls[0]) t.captureUrl = Router.R.bridge + bt.input_urls[0];
        save();
      });
      t.resultUrl = res.url;
      t.resultRemote = res.remoteUrl || null;
      t.status = 'complete_success';
    } catch (e) {
      t.status = 'complete_failure';
      t.error = { code: e.code || 'internal', message: e.message || String(e) };
    }
    t.finishedAt = Date.now();
    delete t._refs;
    T.running = false;
    save();
    emit();
    pump();
  }

  function progress(t) {
    if (t.status === 'complete_success' || t.status === 'complete_failure') return 100;
    if (t.status !== 'started') return 0;
    return Math.min(95, (Date.now() - t.startedAt) / t.progressMs * 100);
  }
  function remove(id) { T.list = T.list.filter(t => t.id !== id || t.status === 'started'); save(); emit(); }
  function clearDone() { T.list = T.list.filter(t => t.status === 'pending' || t.status === 'started'); save(); emit(); }

  window.Tasks = { T, on: f => handlers.push(f), load, enqueue, progress, remove, clearDone };
})();
