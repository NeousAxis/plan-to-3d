/* PlanCAD Stage — moteur de scène : caméras de rendu, vue caméra, capture.
   Architecture reprise d'ArtCraft (pagescene) et réécrite pour Three.js r128 :
   moteur ↔ bus d'événements ↔ état ↔ interface. Les caméras vivent dans
   plan.json (MODEL.cameras, coordonnées du plan en mètres, y vers le bas) :
   une seule source de vérité, exportée avec le plan.

   Convention d'une caméra : {id, label, room, x, y, h, yaw, pitch, focal, aspect}
     x, y   position sur le plan (m)      h      hauteur de l'objectif (m)
     yaw    cap en degrés (0 = nord du plan, 90 = est)   pitch  inclinaison (°, + = vers le haut)
     focal  focale en mm (capteur de 24 mm de haut)      aspect « 16:9 », « 3:2 »…

   Dépend des globales de cad.html : THREE, R3, MODEL, SEL, _map3, cv, g, T2,
   X, Y, edge, fpLights, exitFP, resize3d, draw2d. */
'use strict';
(function () {
  const LAYER = 1;                       // objets d'édition : jamais capturés
  const handlers = {};
  const Bus = {
    on(e, f) { (handlers[e] = handlers[e] || []).push(f); },
    emit(e, d) { (handlers[e] || []).forEach(f => { try { f(d); } catch (err) { console.error('Stage', e, err); } }); },
  };
  const S = { cams: [], sel: null, view: null, placing: false, catalog: null, gizmoMode: 'translate' };
  const DEF = { focal: 17, focalSmallRoom: 14, smallRoomM2: 4.5, eye: 1.5, aspect: '16:9' };

  /* ── maths ── */
  const DEG = Math.PI / 180;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const normYaw = d => ((d % 360) + 540) % 360 - 180;            // ]-180, 180]
  const aspectNum = a => { const [w, h] = String(a).split(':').map(Number); return w / h || 16 / 9; };
  const fovFromFocal = f => 2 * Math.atan(((S.catalog && S.catalog.sensorHeightMm) || 24) / (2 * f)) / DEG;
  const hfov = (vf, asp) => 2 * Math.atan(Math.tan(vf * DEG / 2) * asp) / DEG;
  const sizeFor = a => (S.catalog && S.catalog.sizes[a]) || [1280, 720];
  const wallH = () => (MODEL && MODEL.wall_height) || 2.5;
  const ctr = () => ({ x: (_map3.bb.x0 + _map3.bb.x1) / 2, y: (_map3.bb.y0 + _map3.bb.y1) / 2 });
  const find = id => S.cams.find(c => c.id === id);
  const roomAt = (x, y) => MODEL && MODEL.rooms.find(r => r.rect && x >= r.rect[0] && x <= r.rect[2] && y >= r.rect[1] && y <= r.rect[3]);
  const yawFromDir = (dx, dy) => Math.atan2(dx, -dy) / DEG;      // direction du plan → cap

  function world(c) {                    // plan → monde Three (même repère que build3d)
    return { x: _map3.mX(c.x), y: c.h, z: _map3.mZ(c.y), ry: -c.yaw * DEG, rx: c.pitch * DEG };
  }
  function poseCamera(cam, c, w, h) {    // place une PerspectiveCamera sur la caméra c
    const p = world(c);
    cam.position.set(p.x, p.y, p.z);
    cam.rotation.order = 'YXZ';
    cam.rotation.set(p.rx, p.ry, 0);
    cam.fov = fovFromFocal(c.focal);
    if (w && h) cam.aspect = w / h;
    cam.updateProjectionMatrix();
  }

  /* ── état : création, édition, persistance dans MODEL.cameras ── */
  function normalize(c) {
    const cam = Object.assign({ id: 'cam' + Math.random().toString(36).slice(2, 7), label: '', room: '',
      x: 0, y: 0, h: DEF.eye, yaw: 0, pitch: 0, focal: DEF.focal, aspect: DEF.aspect }, c);
    ['x', 'y', 'h', 'yaw', 'pitch', 'focal'].forEach(k => { cam[k] = +cam[k] || 0; });
    cam.focal = cam.focal || DEF.focal;
    cam.h = cam.h || DEF.eye;
    cam.yaw = normYaw(cam.yaw);
    return cam;
  }
  let syncTimer = null;
  function persist() {
    if (!MODEL) return;
    MODEL.cameras = S.cams.map(c => ({ id: c.id, label: c.label, room: c.room, x: +c.x.toFixed(3), y: +c.y.toFixed(3),
      h: +c.h.toFixed(2), yaw: +c.yaw.toFixed(1), pitch: +c.pitch.toFixed(1), focal: c.focal, aspect: c.aspect }));
    try { localStorage.setItem('plancad_cams:' + planKey(), JSON.stringify(MODEL.cameras)); } catch (e) { /* stockage indisponible */ }
  }
  function changed(full) {               // full = reconstruire les objets 3D
    persist();
    if (full !== false) rebuildHelpers();
    if (typeof draw2d === 'function') draw2d();
    Bus.emit('change', S);
  }
  function changedSoon() {               // pendant les glissés : au plus ~12 fois/s
    if (syncTimer) return;
    syncTimer = setTimeout(() => { syncTimer = null; changed(); }, 80);
  }
  function planKey() { return (MODEL && (MODEL.project || '')) + '|' + (new URLSearchParams(location.search).get('plan') || 'default'); }

  function labelFor(room) {               // « LIVING ROOM », puis « LIVING ROOM (2) »…
    const base = room || 'Caméra';
    if (!S.cams.some(c => c.label === base)) return base;
    let n = 2;
    while (S.cams.some(c => c.label === `${base} (${n})`)) n++;
    return `${base} (${n})`;
  }
  function addCam(c) {
    const cam = normalize(c);
    if (!cam.room) { const r = roomAt(cam.x, cam.y); cam.room = r ? r.name : ''; }
    if (!cam.label) cam.label = labelFor(cam.room);
    S.cams.push(cam);
    S.sel = cam.id;
    changed();
    return cam;
  }
  function updateCam(id, patch) {
    const c = find(id);
    if (!c) return;
    Object.assign(c, patch);
    Object.assign(c, normalize(c));
    if ('x' in patch || 'y' in patch) { const r = roomAt(c.x, c.y); if (r) c.room = r.name; }
    changed();
  }
  function removeCam(id) {
    if (S.view === id) exitView();
    S.cams = S.cams.filter(c => c.id !== id);
    if (S.sel === id) S.sel = null;
    changed();
  }
  function select(id) { S.sel = id; changed(); }

  /* caméra automatique : depuis l'angle le plus proche de la porte, visée au
     centre de la pièce (vue d'entrée la plus large et la plus naturelle) */
  function autoCam(room) {
    if (!room || !room.rect) return null;
    const [x0, y0, x1, y1] = room.rect, cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    let px = cx, py = cy;
    const d = (room.doors || [])[0];
    if (d) { const e = edge(room.rect, d.wall); px = e[0] + (e[2] - e[0]) * d.at; py = e[1] + (e[3] - e[1]) * d.at; }
    const m = Math.min(0.35, (x1 - x0) / 4, (y1 - y0) / 4);
    const kx = px < cx ? x0 + m : x1 - m, ky = py < cy ? y0 + m : y1 - m;
    const area = (x1 - x0) * (y1 - y0);
    return addCam({ x: kx, y: ky, h: DEF.eye, yaw: yawFromDir(cx - kx, cy - ky), pitch: 0, room: room.name,
      focal: area < DEF.smallRoomM2 ? DEF.focalSmallRoom : DEF.focal, aspect: DEF.aspect });
  }
  function autoAll() {
    MODEL.rooms.filter(r => r.rect && !S.cams.some(c => c.room === r.name)).forEach(autoCam);
    S.sel = S.cams.length ? S.cams[S.cams.length - 1].id : null;
    changed();
  }
  function fromFP() {                    // pose actuelle de la visite → caméra de rendu
    if (!R3.fp) return null;
    const o = ctr();
    return addCam({ x: R3.eye.x + o.x, y: R3.eye.z + o.y, h: 1.6, yaw: -R3.yaw / DEG, pitch: R3.pitch / DEG,
      focal: DEF.focal, aspect: DEF.aspect });
  }

  /* ── objets 3D : pyramide de visée (calque d'édition) + volume de sélection invisible ── */
  let helpers = null, gizmo = null;
  function helperFor(c) {
    const grp = new THREE.Group();
    grp.name = '::CAM::' + c.id;
    grp.userData.camId = c.id;
    const vf = fovFromFocal(c.focal), D = 0.55;
    const hh = Math.tan(vf * DEG / 2) * D, hw = hh * aspectNum(c.aspect);
    const P = [[0, 0, 0], [-hw, hh, -D], [hw, hh, -D], [hw, -hh, -D], [-hw, -hh, -D]];
    const seg = [0, 1, 0, 2, 0, 3, 0, 4, 1, 2, 2, 3, 3, 4, 4, 1];
    const pts = [];
    seg.forEach(i => pts.push(...P[i]));
    pts.push(-hw * 0.35, hh * 1.08, -D, 0, hh * 1.45, -D, 0, hh * 1.45, -D, hw * 0.35, hh * 1.08, -D);   // repère « haut »
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    const col = c.id === S.sel ? 0xffa21a : 0x1f6fd1;
    const lines = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: col, depthTest: false, transparent: true }));
    lines.renderOrder = 999;
    grp.add(lines);
    const body = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.11, 0.2),
      new THREE.MeshBasicMaterial({ color: col, depthTest: false, transparent: true, opacity: 0.9 }));
    body.position.z = 0.1;
    body.renderOrder = 999;
    grp.add(body);
    const proxy = new THREE.Mesh(new THREE.BoxGeometry(Math.max(0.3, 2 * hw), Math.max(0.3, 2 * hh), D + 0.25),
      new THREE.MeshBasicMaterial({ visible: false }));
    proxy.position.z = -D / 2 + 0.12;
    proxy.userData.camId = c.id;
    grp.add(proxy);
    grp.traverse(o => o.layers.set(LAYER));
    const p = world(c);
    grp.position.set(p.x, p.y, p.z);
    grp.rotation.order = 'YXZ';
    grp.rotation.set(p.rx, p.ry, 0);
    return grp;
  }
  function rebuildHelpers() {
    if (!R3.scene || !_map3) return;
    if (gizmo && gizmo.object) gizmo.detach();
    if (helpers && helpers.parent) helpers.parent.remove(helpers);
    helpers = new THREE.Group();
    helpers.name = '::STAGE::';
    S.cams.forEach(c => { if (c.id !== S.view) helpers.add(helperFor(c)); });
    R3.scene.add(helpers);
    attachGizmo();
  }

  /* gizmo de déplacement / rotation (TransformControls d'exemple de Three r128) */
  function ensureGizmo() {
    if (gizmo || !THREE.TransformControls) return gizmo;
    gizmo = new THREE.TransformControls(R3.camera, R3.renderer.domElement);
    gizmo.setSize(0.75);
    gizmo.addEventListener('dragging-changed', e => {
      S.dragging = e.value;
      R3.controls.enabled = !e.value && !S.view && !R3.fp;
      if (!e.value) changed();
    });
    gizmo.addEventListener('objectChange', () => {
      const o = gizmo.object;
      const c = o && find(o.userData.camId);
      if (!c) return;
      const k = ctr();
      c.x = o.position.x + k.x;
      c.y = o.position.z + k.y;
      c.h = clamp(o.position.y, 0.3, wallH() - 0.08);
      o.position.y = c.h;
      c.yaw = normYaw(-o.rotation.y / DEG);
      c.pitch = clamp(o.rotation.x / DEG, -60, 60);
      const r = roomAt(c.x, c.y);
      if (r) c.room = r.name;
      persist();
      if (typeof draw2d === 'function') draw2d();
      Bus.emit('change', S);
    });
    return gizmo;
  }
  function attachGizmo() {
    if (!ensureGizmo() || !R3.scene) return;
    if (gizmo.parent !== R3.scene) { if (gizmo.parent) gizmo.parent.remove(gizmo); R3.scene.add(gizmo); }
    const grp = S.sel && !S.view && !R3.fp && helpers && helpers.children.find(h => h.userData.camId === S.sel);
    if (grp) {
      gizmo.attach(grp);
      gizmo.setMode(S.gizmoMode);
      gizmo.setSpace(S.gizmoMode === 'rotate' ? 'local' : 'world');
      gizmo.showX = true; gizmo.showY = true; gizmo.showZ = S.gizmoMode !== 'rotate';
    } else if (gizmo.object) gizmo.detach();
  }
  function setGizmoMode(m) { S.gizmoMode = m; attachGizmo(); Bus.emit('change', S); }

  /* ── vue caméra : on regarde par l'objectif, cadre exact + caches ── */
  const V = { saved: null, drag: null };
  let mattes = null;
  function frameRect() {
    const W = R3.renderer.domElement.clientWidth || 1, H = R3.renderer.domElement.clientHeight || 1;
    const c = find(S.view), ta = aspectNum(c ? c.aspect : DEF.aspect);
    let rw, rh;
    if (W / H > ta) { rh = H * 0.92; rw = rh * ta; } else { rw = W * 0.94; rh = rw / ta; }
    return { W, H, w: rw, h: rh, x: (W - rw) / 2, y: (H - rh) / 2 };
  }
  function ensureMattes() {
    if (mattes) return mattes;
    mattes = document.createElement('div');
    mattes.className = 'st-mattes';
    mattes.innerHTML = '<div class="st-m t"></div><div class="st-m b"></div><div class="st-m l"></div><div class="st-m r"></div>' +
      '<div class="st-frame"><span class="st-flabel"></span><span class="st-fhint">glisser : regarder · ZQSD/WASD : marcher · R/F : hauteur · molette : focale · Échap : sortir</span></div>';
    document.querySelector('#pane3d').appendChild(mattes);
    return mattes;
  }
  function layoutMattes() {
    if (!mattes) return;
    mattes.style.display = S.view ? 'block' : 'none';
    if (!S.view) return;
    const r = frameRect(), c = find(S.view);
    const q = s => mattes.querySelector(s);
    Object.assign(q('.t').style, { left: 0, top: 0, width: r.W + 'px', height: r.y + 'px' });
    Object.assign(q('.b').style, { left: 0, top: (r.y + r.h) + 'px', width: r.W + 'px', height: (r.H - r.y - r.h) + 'px' });
    Object.assign(q('.l').style, { left: 0, top: r.y + 'px', width: r.x + 'px', height: r.h + 'px' });
    Object.assign(q('.r').style, { left: (r.x + r.w) + 'px', top: r.y + 'px', width: (r.W - r.x - r.w) + 'px', height: r.h + 'px' });
    Object.assign(q('.st-frame').style, { left: r.x + 'px', top: r.y + 'px', width: r.w + 'px', height: r.h + 'px' });
    q('.st-flabel').textContent = `${c.label} · ${c.focal} mm · ${c.aspect} · h ${c.h.toFixed(2)} m`;
  }
  function enterView(id) {
    const c = find(id);
    if (!c) return;
    if (R3.fp) exitFP();
    if (S.view) exitView(true);
    V.saved = { pos: R3.camera.position.clone(), rot: R3.camera.rotation.clone(), tgt: R3.controls.target.clone() };
    S.view = id;
    S.sel = id;
    R3.controls.enabled = false;
    R3.camera.layers.disable(LAYER);
    fpLights(true);
    R3.ceilings.forEach(m => { m.visible = true; });
    (R3.people || []).forEach(p => { p.visible = false; });
    R3.lr.domElement.style.display = 'none';
    ensureMattes();
    rebuildHelpers();
    layoutMattes();
    Bus.emit('view', id);
    Bus.emit('change', S);
  }
  function exitView(silent) {
    if (!S.view) return;
    S.view = null;
    V.drag = null;
    const cam = R3.camera;
    cam.clearViewOffset();
    cam.fov = 55;
    if (V.saved) { cam.position.copy(V.saved.pos); cam.rotation.copy(V.saved.rot); R3.controls.target.copy(V.saved.tgt); }
    cam.layers.enable(LAYER);
    fpLights(false);
    R3.ceilings.forEach(m => { m.visible = false; });
    (R3.people || []).forEach(p => { p.visible = true; });
    R3.lr.domElement.style.display = '';
    R3.controls.enabled = true;
    resize3d();
    layoutMattes();
    rebuildHelpers();
    if (!silent) { Bus.emit('view', null); Bus.emit('change', S); }
  }
  function tick(dt) {
    const c = find(S.view);
    if (!c) { exitView(); return; }
    const k = R3.keys, sp = 1.4 * dt, yaw = c.yaw * DEG;
    const fx = Math.sin(yaw), fy = -Math.cos(yaw), rx = Math.cos(yaw), ry = Math.sin(yaw);
    let moved = false;
    if (k.KeyW || k.KeyZ || k.ArrowUp) { c.x += fx * sp; c.y += fy * sp; moved = true; }
    if (k.KeyS || k.ArrowDown) { c.x -= fx * sp; c.y -= fy * sp; moved = true; }
    if (k.KeyD || k.ArrowRight) { c.x += rx * sp; c.y += ry * sp; moved = true; }
    if (k.KeyA || k.KeyQ || k.ArrowLeft) { c.x -= rx * sp; c.y -= ry * sp; moved = true; }
    if (k.KeyR) { c.h = Math.min(wallH() - 0.1, c.h + sp * 0.6); moved = true; }
    if (k.KeyF) { c.h = Math.max(0.4, c.h - sp * 0.6); moved = true; }
    const bb = _map3.bb;
    c.x = clamp(c.x, bb.x0 + 0.1, bb.x1 - 0.1);
    c.y = clamp(c.y, bb.y0 + 0.1, bb.y1 - 0.1);
    const r = frameRect(), cam = R3.camera;
    poseCamera(cam, c, r.w, r.h);
    cam.setViewOffset(r.w, r.h, -r.x, -r.y, r.W, r.H);
    if (moved) { const rm = roomAt(c.x, c.y); if (rm) c.room = rm.name; changedSoon(); }
  }

  /* ── capture : rendu net, sans objets d'édition, à la taille du format ── */
  function capture(id, opts) {
    opts = opts || {};
    const c = find(id);
    if (!c || !R3.scene) return Promise.reject(new Error('caméra introuvable'));
    const [w, h] = sizeFor(c.aspect);
    const ren = R3.renderer, cam = R3.camera;
    const st = { pr: ren.getPixelRatio(), pos: cam.position.clone(), rot: cam.rotation.clone(), fov: cam.fov,
      aspect: cam.aspect, view: cam.view && cam.view.enabled ? Object.assign({}, cam.view) : null, mask: cam.layers.mask,
      ceil: R3.ceilings.map(m => m.visible), people: (R3.people || []).map(p => p.visible),
      giz: gizmo ? gizmo.visible : null, interior: R3.fp || !!S.view };
    const selFloor = (typeof SEL === 'number' && SEL >= 0 && R3.floors[SEL]) ? R3.floors[SEL] : null;
    if (selFloor) selFloor.material.color.setHex(R3.baseCol[SEL]);
    let dataUrl;
    try {
      cam.layers.disable(LAYER);
      if (gizmo) gizmo.visible = false;
      R3.ceilings.forEach(m => { m.visible = true; });
      fpLights(true);
      (R3.people || []).forEach(p => { p.visible = !!opts.people; });
      cam.clearViewOffset();
      poseCamera(cam, c, w, h);
      ren.setPixelRatio(1);
      ren.setSize(w, h, false);
      ren.render(R3.scene, cam);
      dataUrl = ren.domElement.toDataURL('image/png');      // synchrone : tampon encore intact
    } finally {
      ren.setPixelRatio(st.pr);
      cam.position.copy(st.pos);
      cam.rotation.copy(st.rot);
      cam.fov = st.fov;
      cam.aspect = st.aspect;
      cam.layers.mask = st.mask;
      R3.ceilings.forEach((m, i) => { m.visible = st.ceil[i]; });
      (R3.people || []).forEach((p, i) => { p.visible = st.people[i]; });
      if (gizmo && st.giz !== null) gizmo.visible = st.giz;
      fpLights(st.interior);
      if (selFloor) selFloor.material.color.setHex(0xd9b64a);
      resize3d();
      if (st.view) cam.setViewOffset(st.view.fullWidth, st.view.fullHeight, st.view.offsetX, st.view.offsetY, st.view.width, st.view.height);
      else cam.clearViewOffset();
      cam.updateProjectionMatrix();
    }
    return fetch(dataUrl).then(r => r.blob()).then(blob => {
      const shot = { id: 's' + Date.now().toString(36), camId: c.id, label: c.label, room: c.room, aspect: c.aspect,
        focal: c.focal, w, h, blob, url: URL.createObjectURL(blob), time: Date.now() };
      Bus.emit('capture', shot);
      return shot;
    });
  }

  /* ── plan 2D : glyphes des caméras (cône de champ), placement et glissés ── */
  function draw2dHook() {
    if (!MODEL || !(T2.s > 0)) return;
    S.cams.forEach(c => {
      const px = X(c.x), py = Y(c.y), sel = c.id === S.sel;
      const half = hfov(fovFromFocal(c.focal), aspectNum(c.aspect)) * DEG / 2;
      const th = (c.yaw - 90) * DEG, len = 1.3 * T2.s;
      g.save();
      g.fillStyle = sel ? 'rgba(255,162,26,.20)' : 'rgba(31,111,209,.13)';
      g.strokeStyle = sel ? '#e08a00' : '#1f6fd1';
      g.lineWidth = sel ? 1.8 : 1.2;
      g.beginPath(); g.moveTo(px, py); g.arc(px, py, len, th - half, th + half); g.closePath(); g.fill(); g.stroke();
      g.translate(px, py); g.rotate(th);
      g.fillStyle = sel ? '#e08a00' : '#1f6fd1';
      g.fillRect(-8, -6, 12, 12);
      g.beginPath(); g.moveTo(4, -4); g.lineTo(10, -7); g.lineTo(10, 7); g.lineTo(4, 4); g.closePath(); g.fill();
      g.restore();
      if (sel) {
        g.save(); g.fillStyle = '#fff'; g.strokeStyle = '#e08a00'; g.lineWidth = 2;
        const hx = px + Math.cos(th) * len * 0.82, hy = py + Math.sin(th) * len * 0.82;
        g.beginPath(); g.arc(hx, hy, 6, 0, 7); g.fill(); g.stroke(); g.restore();
      }
      g.save();
      g.font = (sel ? 'bold ' : '') + '11px system-ui';
      g.fillStyle = sel ? '#7a4a00' : '#174f97';
      g.textAlign = 'left';
      g.fillText(c.label, px + 10, py + 16);
      g.restore();
    });
    if (S.placing) {
      const msg = 'Clique dans une pièce pour poser la caméra (Échap : annuler)';
      g.save(); g.font = '12px system-ui'; g.textAlign = 'left';
      g.fillStyle = 'rgba(28,36,51,.85)'; g.fillRect(10, 10, g.measureText(msg).width + 18, 26);
      g.fillStyle = '#fff'; g.fillText(msg, 19, 27); g.restore();
    }
  }
  function planPoint(e) {
    const b = cv.getBoundingClientRect();
    return { x: (e.clientX - b.left - T2.ox) / T2.s, y: (e.clientY - b.top - T2.oy) / T2.s, px: e.clientX - b.left, py: e.clientY - b.top };
  }
  let drag2 = null, swallow = false;
  function hit2d(p) {
    const sel = find(S.sel);
    if (sel) {
      const th = (sel.yaw - 90) * DEG, len = 1.3 * T2.s;
      const hx = X(sel.x) + Math.cos(th) * len * 0.82, hy = Y(sel.y) + Math.sin(th) * len * 0.82;
      if (Math.hypot(p.px - hx, p.py - hy) <= 9) return { cam: sel, mode: 'aim' };
    }
    for (let i = S.cams.length - 1; i >= 0; i--) {
      const c = S.cams[i];
      if (Math.hypot(p.px - X(c.x), p.py - Y(c.y)) <= 11) return { cam: c, mode: 'move' };
    }
    return null;
  }
  function onDown2(e) {
    if (!MODEL || e.button !== 0) return;
    const p = planPoint(e);
    if (S.placing) {
      S.placing = false;
      swallow = true;
      const r = roomAt(p.x, p.y);
      const tx = r ? (r.rect[0] + r.rect[2]) / 2 : ctr().x, ty = r ? (r.rect[1] + r.rect[3]) / 2 : ctr().y;
      const area = r ? (r.rect[2] - r.rect[0]) * (r.rect[3] - r.rect[1]) : 20;
      addCam({ x: p.x, y: p.y, yaw: (Math.hypot(tx - p.x, ty - p.y) > 0.2) ? yawFromDir(tx - p.x, ty - p.y) : 0,
        room: r ? r.name : '', focal: area < DEF.smallRoomM2 ? DEF.focalSmallRoom : DEF.focal });
      return;
    }
    const h = hit2d(p);
    if (!h) return;
    swallow = true;
    drag2 = h;
    if (S.sel !== h.cam.id) select(h.cam.id);
    e.preventDefault();
  }
  function onMove2(e) {
    if (!drag2) {
      if (MODEL && T2.s > 0) cv.style.cursor = S.placing ? 'crosshair' : (hit2d(planPoint(e)) ? 'grab' : '');
      return;
    }
    const p = planPoint(e), c = drag2.cam;
    if (drag2.mode === 'move') {
      const bb = _map3.bb;
      c.x = clamp(p.x, bb.x0 + 0.05, bb.x1 - 0.05);
      c.y = clamp(p.y, bb.y0 + 0.05, bb.y1 - 0.05);
      const r = roomAt(c.x, c.y);
      if (r) c.room = r.name;
    } else c.yaw = normYaw(yawFromDir(p.x - c.x, p.y - c.y));
    cv.style.cursor = 'grabbing';
    changedSoon();
  }
  function onUp2() { if (drag2) { drag2 = null; changed(); } }

  /* ── 3D : clic sur une pyramide = sélection ; double-clic = vue caméra ── */
  const ray = new THREE.Raycaster();
  let down3 = null;
  function pick3(e) {
    if (!helpers) return null;
    const el = R3.renderer.domElement, b = el.getBoundingClientRect();
    const v = new THREE.Vector2(((e.clientX - b.left) / b.width) * 2 - 1, -((e.clientY - b.top) / b.height) * 2 + 1);
    ray.layers.set(LAYER);
    ray.setFromCamera(v, R3.camera);
    const hit = ray.intersectObjects(helpers.children, true).find(i => i.object.userData.camId);
    return hit ? hit.object.userData.camId : null;
  }
  function onDown3(e) {
    if (S.view) { V.drag = { x: e.clientX, y: e.clientY }; return; }
    down3 = { x: e.clientX, y: e.clientY };
  }
  function onMove3(e) {
    if (!S.view || !V.drag) return;
    const c = find(S.view);
    if (!c) return;
    c.yaw = normYaw(c.yaw + (e.clientX - V.drag.x) * 0.18);
    c.pitch = clamp(c.pitch - (e.clientY - V.drag.y) * 0.18, -60, 60);
    V.drag = { x: e.clientX, y: e.clientY };
    changedSoon();
  }
  function onUp3(e) {
    if (S.view) { V.drag = null; return; }
    if (!down3 || R3.fp || S.dragging || (gizmo && gizmo.axis)) { down3 = null; return; }
    const moved = Math.hypot(e.clientX - down3.x, e.clientY - down3.y);
    down3 = null;
    if (moved > 4) return;
    const id = pick3(e);
    if (id && id !== S.sel) select(id);
  }
  function onDbl3(e) {
    if (S.view) { exitView(); return; }
    const id = pick3(e);
    if (id) enterView(id);
  }
  function onWheel3(e) {
    if (!S.view) return;
    e.preventDefault();
    const c = find(S.view), F = (S.catalog && S.catalog.focals) || [14, 17, 20, 24, 28, 35, 50];
    let i = F.indexOf(c.focal);
    if (i < 0) i = F.findIndex(f => f >= c.focal);
    i = clamp(i + (e.deltaY > 0 ? -1 : 1), 0, F.length - 1);
    c.focal = F[i];
    changed();
  }

  function onKey(e) {
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable) return;
    if (e.code === 'Escape') {
      if (S.placing) { S.placing = false; changed(false); }
      else if (S.view) exitView();
    }
    if (R3.fp && e.code === 'KeyC') fromFP();
  }

  /* ── cycle de vie, appelé par cad.html ── */
  function init() {
    R3.camera.layers.enable(LAYER);
    const el = R3.renderer.domElement;
    el.addEventListener('pointerdown', onDown3);
    addEventListener('pointermove', onMove3);
    addEventListener('pointerup', onUp3);
    el.addEventListener('dblclick', onDbl3);
    el.addEventListener('wheel', onWheel3, { passive: false });
    cv.addEventListener('pointerdown', onDown2);
    addEventListener('pointermove', onMove2);
    addEventListener('pointerup', onUp2);
    cv.addEventListener('click', e => { if (swallow) { swallow = false; e.stopImmediatePropagation(); } }, true);
    addEventListener('keydown', onKey);
    addEventListener('resize', () => setTimeout(layoutMattes, 0));
    fetch('stage/catalog.json').then(r => r.json()).then(cat => {
      S.catalog = cat;
      Object.assign(DEF, { focal: cat.defaults.focal, focalSmallRoom: cat.defaults.focalSmallRoom,
        smallRoomM2: cat.defaults.smallRoomM2, eye: cat.defaults.eye, aspect: cat.defaults.aspect });
      Bus.emit('catalog', cat);
      changed();
    }).catch(err => console.warn('Stage : catalogue illisible', err));
  }
  function modelChanged() {
    if (S.view) exitView(true);
    let cams = (MODEL && MODEL.cameras) || null;
    if (!cams) { try { cams = JSON.parse(localStorage.getItem('plancad_cams:' + planKey()) || 'null'); } catch (e) { cams = null; } }
    S.cams = (cams || []).map(normalize);
    S.sel = null;
    S.placing = false;
    if (MODEL && S.cams.length) persist();
    Bus.emit('change', S);
  }
  function sceneBuilt() { rebuildHelpers(); }
  function startPlacing() { if (S.view) exitView(); S.placing = true; changed(false); }

  window.Stage = { Bus, S, init, modelChanged, sceneBuilt, tick, draw2d: draw2dHook,
    drivesCamera: () => !!S.view, addCam, autoCam, autoAll, fromFP, updateCam, removeCam, select,
    enterView, exitView, capture, setGizmoMode, startPlacing, find, roomAt, fovFromFocal, hfov, aspectNum, sizeFor };
})();
