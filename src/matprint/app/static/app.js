import * as THREE from 'three';
import { STLLoader } from '/static/vendor/three/STLLoader.js';
import { OrbitControls } from '/static/vendor/three/OrbitControls.js';

// ---------------------------------------------------------------- helpers
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmt = (v, d = 3) => (v === null || v === undefined || Number.isNaN(v) ? '–' : Number(v).toFixed(d));
let uidN = 0;
const uid = () => `d${++uidN}`;

async function api(path, body, { raw, headers } = {}) {
  const opts = body === undefined ? {} : {
    method: 'POST',
    headers: raw ? headers : { 'Content-Type': 'application/json' },
    body: raw ? body : JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok || data.error) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), bad ? 7000 : 3500);
}

function busy(text) {
  $('#busy-text').textContent = text || 'Working…';
  $('#busy').hidden = !text;
}

const TYPE_LABELS = { hole: 'Hole', contour: 'Outside', position: 'Spacing', z: 'Height' };
const SETTING_LABELS = { wall_loops: 'Walls', infill_pct: 'Infill %', note: 'Note' };
const CHEM_OPTIONS = [['', 'No requirement'], ['short', 'Short exposure'], ['resistant', 'Resistant']];

// ---------------------------------------------------------------- state
const state = {
  settings: {},
  materials: [],
  model: null,
  dims: [],
  result: null,
  rejected: new Set(),
  selected: null,
  checkSeq: 0,
  printUrl: null,
};

// ---------------------------------------------------------------- 3D viewer
class Viewer {
  constructor(el) {
    this.el = el;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    el.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.1, 10000);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.addEventListener('change', () => this.render());
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(1, -1.5, 2);
    this.camera.add(key);
    this.scene.add(this.camera);
    this.modelGroup = new THREE.Group();
    this.hlGroup = new THREE.Group();
    this.gridGroup = new THREE.Group();
    this.scene.add(this.gridGroup, this.modelGroup, this.hlGroup);
    this.loader = new STLLoader();
    new ResizeObserver(() => this.resize()).observe(el);
    const loop = () => { if (this.controls.update()) this.render(); requestAnimationFrame(loop); };
    loop();
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  render() { this.renderer.render(this.scene, this.camera); }

  async load(url, { color = 0x9aa3b0, fit = true } = {}) {
    const geom = await this.loader.loadAsync(url);
    geom.computeVertexNormals();
    this.modelGroup.clear();
    const mesh = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({ color, metalness: 0.05, roughness: 0.65 }));
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geom, 25),
      new THREE.LineBasicMaterial({ color: 0x3b4250, transparent: true, opacity: 0.55 }));
    this.modelGroup.add(mesh, edges);
    geom.computeBoundingBox();
    this.box = geom.boundingBox.clone();
    this.drawGrid();
    if (fit) this.fit();
    this.render();
  }

  drawGrid() {
    this.gridGroup.clear();
    const b = this.box, size = b.getSize(new THREE.Vector3());
    const span = Math.ceil(Math.max(size.x, size.y) * 1.6 / 10) * 10 || 100;
    const grid = new THREE.GridHelper(span, span / 5, 0x8892a0, 0xb8bec8);
    grid.rotation.x = Math.PI / 2;
    const c = b.getCenter(new THREE.Vector3());
    grid.position.set(c.x, c.y, b.min.z - 0.01);
    grid.material.transparent = true;
    grid.material.opacity = 0.35;
    this.gridGroup.add(grid);
  }

  fit() {
    const b = this.box, c = b.getCenter(new THREE.Vector3()), r = b.getSize(new THREE.Vector3()).length() / 2;
    const dist = r / Math.sin((this.camera.fov * Math.PI) / 360) * 1.1;
    this.camera.position.set(c.x + dist * 0.55, c.y - dist * 0.7, c.z + dist * 0.55);
    this.camera.near = dist / 100; this.camera.far = dist * 20; this.camera.updateProjectionMatrix();
    this.controls.target.copy(c);
    this.controls.update();
  }

  highlight(feature, dimType) {
    this.hlGroup.clear();
    if (!feature || !this.box) { this.render(); return; }
    const color = dimType === 'hole' ? 0xe5484d : dimType === 'z' ? 0x8e4ec6 : 0x2f9e44;
    const mat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.45, side: THREE.DoubleSide, depthTest: false });
    const lineMat = new THREE.LineBasicMaterial({ color, depthTest: false });
    const b = this.box;
    if ((feature.kind === 'hole' || feature.kind === 'boss') && feature.size) {
      const [z0, z1] = feature.z_range, h = Math.max(z1 - z0, 0.2), r = feature.size / 2;
      const cyl = new THREE.Mesh(new THREE.CylinderGeometry(r, r, h, 64, 1, true), mat);
      cyl.rotation.x = Math.PI / 2;
      cyl.position.set(feature.center[0], feature.center[1], (z0 + z1) / 2);
      for (const z of [z0, z1]) {
        const ring = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(
          Array.from({ length: 65 }, (_, i) => new THREE.Vector3(
            feature.center[0] + r * Math.cos(i / 32 * Math.PI), feature.center[1] + r * Math.sin(i / 32 * Math.PI), z))), lineMat);
        this.hlGroup.add(ring);
      }
      cyl.renderOrder = 2;
      this.hlGroup.add(cyl);
    } else if (feature.kind === 'pocket' && feature.size) {
      const [z0, z1] = feature.z_range, s = feature.size;
      const box = new THREE.Mesh(new THREE.BoxGeometry(s, s, Math.max(z1 - z0, 0.2)), mat);
      box.position.set(feature.center[0], feature.center[1], (z0 + z1) / 2);
      this.hlGroup.add(box);
    } else if (feature.id === 'BBOX_X' || feature.id === 'BBOX_Y' || feature.id === 'Z_HEIGHT') {
      const off = Math.max(b.getSize(new THREE.Vector3()).length() * 0.04, 2);
      let p0, p1;
      if (feature.id === 'BBOX_X') { p0 = new THREE.Vector3(b.min.x, b.min.y - off, b.min.z); p1 = new THREE.Vector3(b.max.x, b.min.y - off, b.min.z); }
      else if (feature.id === 'BBOX_Y') { p0 = new THREE.Vector3(b.max.x + off, b.min.y, b.min.z); p1 = new THREE.Vector3(b.max.x + off, b.max.y, b.min.z); }
      else { p0 = new THREE.Vector3(b.max.x + off, b.min.y - off, b.min.z); p1 = new THREE.Vector3(b.max.x + off, b.min.y - off, b.max.z); }
      const dir = p1.clone().sub(p0), len = dir.length(); dir.normalize();
      const head = Math.min(len * 0.12, off * 1.5);
      this.hlGroup.add(new THREE.ArrowHelper(dir, p0.clone().add(dir.clone().multiplyScalar(len / 2)), len / 2, color, head, head * 0.5));
      this.hlGroup.add(new THREE.ArrowHelper(dir.clone().negate(), p0.clone().add(dir.clone().multiplyScalar(len / 2)), len / 2, color, head, head * 0.5));
    }
    this.render();
  }
}

let viewer = null;

// ---------------------------------------------------------------- tabs
$$('.tab').forEach((b) => b.addEventListener('click', () => {
  $$('.tab').forEach((t) => t.classList.toggle('active', t === b));
  $$('.tabpage').forEach((p) => (p.hidden = p.id !== `tab-${b.dataset.tab}`));
  if (b.dataset.tab === 'cal') refreshCalibration();
  if (b.dataset.tab === 'mat') renderMaterialsTable();
}));
function showTab(name) { $(`.tab[data-tab="${name}"]`).click(); }

// ---------------------------------------------------------------- materials
async function loadMaterials() {
  const s = state.settings;
  state.materials = await api(`/api/materials?printer=${encodeURIComponent(s.printer)}&profile=${encodeURIComponent(s.profile)}`);
  for (const sel of [$('#material'), $('#cal-material')]) {
    const keep = sel.value;
    sel.innerHTML = state.materials.map((m) =>
      `<option value="${esc(m.name)}">${esc(m.name)}${m.usable ? (m.s_source === 'calibration' ? ' ✓ calibrated' : '') : ' (needs calibration)'}</option>`).join('');
    sel.value = keep || s.material || 'PLA';
    if (!sel.value) sel.value = 'PLA';
  }
  renderMaterialInfo();
}

const material = (name) => state.materials.find((m) => m.name === name);

function renderMaterialInfo() {
  const m = material($('#material').value);
  if (!m) return;
  const src = (s) => (s === 'calibration' ? 'your calibration' : s === 'table' ? 'typical value' : 'not available');
  const parts = [];
  parts.push(m.s_pct != null ? `Shrinkage ${fmt(m.s_pct, 2)}% (${src(m.s_source)})` : 'No shrinkage data yet');
  if (m.s_min != null) parts.push(`varies ${fmt(m.s_min, 2)}–${fmt(m.s_max, 2)}%`);
  parts.push(m.h_mm != null ? `hole offset ${fmt(m.h_mm, 2)} mm (${src(m.h_source)})` : 'no hole offset yet');
  if (m.max_temp_c != null) parts.push(`max ${m.max_temp_c} °C`);
  $('#material-info').innerHTML = esc(parts.join(' · ')) +
    (m.usable ? '' : ` <button class="btn link" id="go-cal">Calibrate ${esc(m.name)}</button>`);
  const go = $('#go-cal');
  if (go) go.onclick = () => { $('#cal-material').value = m.name; showTab('cal'); };
}

function renderMaterialsTable() {
  const chem = (c) => Object.entries(c).filter(([, v]) => v !== 'unknown')
    .map(([k, v]) => `${k}: ${v === 'not' ? 'not resistant' : v === 'short' ? 'short exposure' : 'resistant'}`).join(', ') || '';
  $('#mat-table').innerHTML = `<tr><th>Material</th><th>Shrinkage</th><th>Range</th><th>Hole offset</th><th>Max °C</th><th>Chemical resistance</th><th>Notes</th></tr>` +
    state.materials.map((m) => `<tr><td><b>${esc(m.name)}</b>${m.s_source === 'calibration' ? ' <span class="badge ok">calibrated</span>' : ''}${m.usable ? '' : ' <span class="badge warn">needs calibration</span>'}</td>
      <td>${m.s_pct != null ? fmt(m.s_pct, 2) + '%' : ''}</td><td>${m.s_min != null ? `${fmt(m.s_min, 2)}–${fmt(m.s_max, 2)}%` : ''}</td>
      <td>${m.h_mm != null ? fmt(m.h_mm, 2) + ' mm' : ''}</td><td>${m.max_temp_c ?? ''}</td><td>${esc(chem(m.chemical))}</td><td class="muted">${esc(m.notes)}</td></tr>`).join('');
}

// ---------------------------------------------------------------- opening models
async function openWithDialog() {
  try {
    busy('Waiting for you to pick a file…');
    const r = await api('/api/open-dialog', {});
    busy();
    if (r.unavailable) { $('#file-input').click(); return; }
    if (r.cancelled) return;
    await setModel(r);
  } catch (e) { busy(); showOpenError(e); }
}

async function upload(file) {
  try {
    busy(`Reading ${file.name}…`);
    const r = await api('/api/upload', file, { raw: true, headers: { 'X-Filename': encodeURIComponent(file.name) } });
    busy();
    await setModel(r);
  } catch (e) { busy(); showOpenError(e); }
}

function showOpenError(e) {
  if (state.model) { toast(e.message, true); return; }
  const el = $('#empty-error');
  el.textContent = e.message;
  el.hidden = false;
}

async function setModel(info, keepDims = false) {
  state.model = info;
  if (!keepDims) state.dims = info.dimensions.map((d) => ({ ...d, key: uid() }));
  state.selected = null;
  state.result = null;
  state.printUrl = null;
  $('#empty').hidden = true;
  $('#work').hidden = false;
  $('#empty-error').hidden = true;
  $('#file-name').textContent = info.name;
  $('#file-name').title = info.path || info.name;
  $('#file-kind').textContent = info.kind.toUpperCase();
  $('#file-kind').className = 'badge' + (info.kind === 'stl' ? ' warn' : '');
  $('#up-axis').value = info.up;
  $('#model-warnings').innerHTML = info.warnings.map((w) => `<div class="alert warn small">${esc(w)}</div>`).join('');
  const nextTo = $('input[name=save][value=next_to_original]');
  nextTo.disabled = info.source !== 'disk';
  nextTo.parentElement.title = info.source !== 'disk' ? 'The file was dragged in, so its folder is unknown' : '';
  syncSaveMode();
  setView('original');
  $('[data-view=print]').disabled = true;
  $('#build-result').innerHTML = '';
  renderDims();
  if (!viewer) viewer = new Viewer($('#viewer'));
  try {
    await viewer.load(`/api/model/${info.id}/preview.stl?t=${Date.now()}`);
  } catch (e) { toast(`Couldn't show the 3D preview: ${e.message}`, true); }
  scheduleCheck(0);
}

function setView(which) {
  $$('#view-toggle button').forEach((b) => b.classList.toggle('active', b.dataset.view === which));
  if (!viewer || !state.model) return;
  const url = which === 'print' ? state.printUrl : `/api/model/${state.model.id}/preview.stl`;
  if (url) viewer.load(url, { color: which === 'print' ? 0x5b8def : 0x9aa3b0, fit: false }).then(() => highlightSelected());
}

// ---------------------------------------------------------------- dimensions table
function renderDims() {
  const body = $('#dims-body');
  body.innerHTML = state.dims.map((d) => `
    <tr data-key="${d.key}" class="${d.critical ? '' : 'off'} ${state.selected === d.key ? 'sel' : ''}">
      <td><input type="checkbox" data-f="critical" ${d.critical ? 'checked' : ''} title="Critical"></td>
      <td><input type="text" data-f="name" value="${esc(d.name)}">${d.feature ? `<div class="feat">${esc(d.feature)}</div>` : ''}<div class="flag" data-out="flags"></div></td>
      <td><select data-f="type">${Object.entries(TYPE_LABELS).map(([v, l]) => `<option value="${v}" ${d.type === v ? 'selected' : ''}>${l}</option>`).join('')}</select></td>
      <td class="num"><input type="number" data-f="nominal" step="0.01" value="${d.nominal}"></td>
      <td class="num"><input type="number" data-f="minus" step="0.01" min="0" value="${d.minus}"></td>
      <td class="num"><input type="number" data-f="plus" step="0.01" min="0" value="${d.plus}"></td>
      <td class="num"><span class="pv" data-out="pv">–</span></td>
      <td data-out="hold"></td>
      <td><button class="del" title="Remove" data-del>×</button></td>
    </tr>`).join('');
  $$('tr', body).forEach((tr) => {
    const d = state.dims.find((x) => x.key === tr.dataset.key);
    $$('[data-f]', tr).forEach((inp) => {
      inp.addEventListener(inp.type === 'checkbox' || inp.tagName === 'SELECT' ? 'change' : 'input', () => {
        const f = inp.dataset.f;
        d[f] = inp.type === 'checkbox' ? inp.checked : inp.type === 'number' ? parseFloat(inp.value) : inp.value;
        if (f === 'critical') tr.classList.toggle('off', !d.critical);
        if (f === 'type') highlightSelected();
        scheduleCheck();
      });
      inp.addEventListener('focus', () => selectRow(tr, d));
    });
    $('[data-del]', tr).addEventListener('click', (e) => {
      e.stopPropagation();
      state.dims = state.dims.filter((x) => x !== d);
      renderDims();
      scheduleCheck();
    });
    tr.addEventListener('click', () => selectRow(tr, d));
  });
  fillDimResults();
}

function selectRow(tr, d) {
  if (state.selected === d.key) return;
  state.selected = d.key;
  $$('#dims-body tr').forEach((r) => r.classList.toggle('sel', r === tr));
  highlightSelected();
}

function highlightSelected() {
  if (!viewer || !state.model) return;
  const d = state.dims.find((x) => x.key === state.selected);
  const f = d && state.model.features.find((x) => x.id === d.feature);
  viewer.highlight(f, d?.type);
}

function fillDimResults() {
  const r = state.result;
  const critical = state.dims.filter((d) => d.critical);
  for (const tr of $$('#dims-body tr')) {
    const d = state.dims.find((x) => x.key === tr.dataset.key);
    const i = critical.indexOf(d);
    const res = r && i >= 0 ? r.dimensions[i] : null;
    $('[data-out=pv]', tr).textContent = res ? fmt(res.print_value, 3) : '–';
    $('[data-out=pv]', tr).title = res ? `Correction: ${res.correction}` : '';
    const hold = $('[data-out=hold]', tr);
    if (!res) hold.innerHTML = d.critical ? '' : '<span class="muted small">ignored</span>';
    else {
      const label = { holds: ['ok', 'Yes'], fails: ['bad', 'No'], unknown: ['warn', 'Unknown'], 'n/a': ['', 'Not compensated'] }[res.hold];
      const tip = res.spread_mm != null ? `Expected spread ${fmt(res.spread_mm)} mm vs tolerance band ${fmt(res.upper - res.lower)} mm` : '';
      hold.innerHTML = `<span class="badge ${label[0]}" title="${esc(tip)}">${label[1]}</span>`;
    }
    $('[data-out=flags]', tr).textContent = res && res.flags.length ? res.flags.join(' · ') : '';
  }
}

$('#add-dim').addEventListener('click', () => {
  const d = { key: uid(), name: 'New dimension', type: 'contour', feature: '', nominal: 10, minus: 0.1, plus: 0.1, critical: true };
  state.dims.push(d);
  state.selected = d.key;
  renderDims();
  scheduleCheck();
});

// ---------------------------------------------------------------- checking
function payload() {
  const chems = {};
  $$('.chem').forEach((s) => { if (s.value) chems[s.dataset.chem] = s.value; });
  return {
    model_id: state.model.id,
    material: $('#material').value,
    printer: $('#printer').value.trim() || 'Bambu Lab',
    profile: $('#profile').value.trim() || 'default',
    print_settings: {
      wall_loops: parseInt($('#walls').value, 10) || 2,
      infill_pct: parseFloat($('#infill').value),
      line_width_mm: parseFloat($('#line-width').value) || 0.42,
      purpose: $('#purpose').value,
    },
    hard_requirements: { max_temp_c: $('#max-temp').value === '' ? null : parseFloat($('#max-temp').value), chemicals: chems },
    dimensions: state.dims.filter((d) => Number.isFinite(d.nominal)).map((d) => ({
      name: d.name, type: d.type, nominal: d.nominal, minus: d.minus || 0, plus: d.plus || 0, feature: d.feature, critical: d.critical,
    })),
    rejected: [...state.rejected],
  };
}

let checkTimer = null;
function scheduleCheck(delay = 300) {
  if (state.printUrl) {
    // Something changed after the files were made: they no longer match the page.
    state.printUrl = null;
    $('[data-view=print]').disabled = true;
    setView('original');
    $('#build-result').insertAdjacentHTML('afterbegin',
      '<div class="alert warn stale">You changed something after these files were created. Create them again to update them.</div>');
  }
  clearTimeout(checkTimer);
  checkTimer = setTimeout(runCheck, delay);
}

async function runCheck() {
  if (!state.model) return;
  const seq = ++state.checkSeq;
  try {
    const r = await api('/api/check', payload());
    if (seq !== state.checkSeq) return;
    state.result = r;
    renderResult();
  } catch (e) {
    if (seq !== state.checkSeq) return;
    $('#verdict').innerHTML = `<div class="alert bad">${esc(e.message)}</div>`;
  }
}

function renderResult() {
  const r = state.result;
  fillDimResults();
  const mat = r.material;
  const crit = r.dimensions.filter((d) => d.type !== 'z');
  const failing = r.dimensions.filter((d) => d.hold === 'fails');
  const unknown = r.dimensions.filter((d) => d.hold === 'unknown');
  let html = '';
  if (!r.can_build) {
    html += `<div class="alert warn"><b>${esc(mat)} has no shrinkage data yet.</b> Calibrate it on the Calibrate tab, or pick another material.</div>`;
  } else if (!crit.length) {
    html += `<div class="alert warn">Tick at least one dimension to check.</div>`;
  } else if (!failing.length && !unknown.length) {
    html += `<div class="alert ok"><b>Yes.</b> ${esc(mat)} can hold all ${crit.length} critical dimension${crit.length > 1 ? 's' : ''}.</div>`;
  } else if (failing.length) {
    html += `<div class="alert bad"><b>${failing.length} dimension${failing.length > 1 ? 's' : ''} can't be held reliably in ${esc(mat)}:</b> ${esc(failing.map((d) => d.name).join(', '))}. ${esc(mat)}'s shrinkage varies too much from print to print for ${failing.length > 1 ? 'these tolerances' : 'this tolerance'}.</div>`;
  }
  for (const f of r.hard_failures) html += `<div class="alert bad">${esc(mat)} misses a requirement: ${esc(f)}</div>`;
  for (const w of r.warnings) html += `<div class="alert warn small">${esc(w)}</div>`;
  $('#verdict').innerHTML = html;

  let alts = '';
  if (r.alternatives.length) {
    alts += `<p class="muted small">These materials can hold every critical dimension${r.hard_failures.length || $('#max-temp').value || $$('.chem').some((s) => s.value) ? ' and meet your requirements' : ''}, closest match first:</p>`;
    alts += r.alternatives.map((a) => `<div class="alt"><div><div class="name">${esc(a.material)}</div><div class="why">${esc(a.tradeoff)}</div>
      <div class="why">Worst case: ${esc(a.worst)}</div></div><button class="btn" data-use="${esc(a.material)}">Use ${esc(a.material)}</button></div>`).join('');
  } else if (failing.length) {
    alts += `<p class="muted">No material in the list can hold every critical dimension${$('#max-temp').value ? ' and meet your requirements' : ''}. Calibrating ${esc(mat)} on your printer may narrow its range, or loosen the tolerance.</p>`;
  }
  for (const n of r.needs_calibration) {
    alts += `<div class="alt"><div><div class="name">${esc(n.material)} <span class="badge warn">needs calibration</span></div><div class="why">${esc(n.tradeoff)}. It might hold these tolerances, but there's no shrinkage data for it yet.</div></div>
      <button class="btn" data-cal="${esc(n.material)}">Calibrate it</button></div>`;
  }
  $('#alternatives').innerHTML = alts;
  $$('[data-use]').forEach((b) => b.addEventListener('click', () => {
    $('#material').value = b.dataset.use;
    onMaterialChange();
    toast(`Switched to ${b.dataset.use}`);
  }));
  $$('[data-cal]').forEach((b) => b.addEventListener('click', () => { $('#cal-material').value = b.dataset.cal; showTab('cal'); }));

  const sugg = r.settings_suggestions;
  let sh = '';
  if (!sugg.length) sh = '<p class="muted small">No changes needed.</p>';
  for (const s of sugg) {
    const what = s.value != null ? `${SETTING_LABELS[s.setting] || s.setting} → ${s.value}` : 'Recommendation';
    const on = !state.rejected.has(s.rule);
    sh += `<label class="sugg">${s.value != null ? `<input type="checkbox" data-rule="${s.rule}" ${on ? 'checked' : ''}>` : '<span style="width:16px"></span>'}
      <div><div class="what">${esc(what)} <span class="badge">${s.rule}</span>${s.provisional ? ' <span class="badge warn" title="From general practice; confirm with a test print">provisional</span>' : ''}</div>
      <div class="why">${esc(s.reason.replace(/^whole part: /, ''))}</div></div></label>`;
  }
  if (r.final_settings) sh += `<p class="small"><b>Print files will use ${r.final_settings.wall_loops} walls and ${r.final_settings.infill_pct}% infill.</b> All slicer-side compensation will be off.</p>`;
  $('#suggestions').innerHTML = sh;
  $$('[data-rule]').forEach((c) => c.addEventListener('change', () => {
    if (c.checked) state.rejected.delete(c.dataset.rule); else state.rejected.add(c.dataset.rule);
    scheduleCheck(0);
  }));
  $('#build-btn').disabled = !r.can_build;
}

// ---------------------------------------------------------------- building
function syncSaveMode() {
  let mode = state.settings.save_mode || 'next_to_original';
  if (mode === 'next_to_original' && state.model && state.model.source !== 'disk') mode = 'folder';
  $$('input[name=save]').forEach((r) => (r.checked = r.value === mode));
  $('#out-dir').value = state.settings.output_dir || '';
}

async function saveSettings(updates) {
  Object.assign(state.settings, updates);
  try { await api('/api/settings', updates); } catch { /* not critical */ }
}

$$('input[name=save]').forEach((r) => r.addEventListener('change', () => saveSettings({ save_mode: r.value })));
$('#out-dir').addEventListener('change', () => saveSettings({ output_dir: $('#out-dir').value.trim(), save_mode: 'folder' }).then(syncSaveMode));
$('#pick-folder').addEventListener('click', async () => {
  try {
    const r = await api('/api/folder-dialog', {});
    if (r.unavailable) { toast('No folder picker on this computer. Type the folder path instead.', true); $('#out-dir').focus(); return; }
    if (r.cancelled) return;
    await saveSettings({ output_dir: r.path, save_mode: 'folder' });
    syncSaveMode();
  } catch (e) { toast(e.message, true); }
});

$('#build-btn').addEventListener('click', async () => {
  if (!state.model) return;
  const mode = $('input[name=save]:checked')?.value || 'folder';
  try {
    busy('Creating print files…');
    const r = await api('/api/build', { ...payload(), save_mode: mode, output_dir: $('#out-dir').value.trim() });
    busy();
    state.result = r;
    renderResult();
    const stl = r.files.find((f) => f.kind === 'stl');
    state.printUrl = stl ? stl.url : null;
    $('[data-view=print]').disabled = !stl;
    $('[data-view=print]').title = '';
    const label = { step: 'Print model (STEP, for CAD)', stl: 'Print model (STL)', '3mf': 'Print model with settings (3MF, open in Bambu Studio)',
      settings: 'Slicer settings (text)', report: 'Report', json: 'Report data (JSON)' };
    $('#build-result').innerHTML = `<div class="alert ok">Saved ${r.files.length} files to <b>${esc(r.output_dir)}</b> in ${fmt(r.elapsed_s, 1)} s.</div>
      <ul class="files">${r.files.map((f) => `<li><div><div>${esc(label[f.kind] || f.kind)}</div><div class="fname">${esc(f.name)}</div></div>
        ${f.kind === 'report' ? `<a class="btn small" href="${f.url}" target="_blank" rel="noopener">Open</a>` : ''}
        <a class="btn small" href="${f.url}?download=1" download="${esc(f.name)}">Download</a></li>`).join('')}</ul>`;
    setView('print');
    toast('Print files created');
  } catch (e) { busy(); toast(e.message, true); }
});

// ---------------------------------------------------------------- wiring: adjust tab
$('#open-btn').addEventListener('click', openWithDialog);
$('#open-another').addEventListener('click', openWithDialog);
$('#file-input').addEventListener('change', (e) => { const f = e.target.files[0]; if (f) upload(f); e.target.value = ''; });
['dragenter', 'dragover'].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); $('#drop').classList.add('over'); }));
['dragleave', 'drop'].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); if (ev === 'drop' || e.target === document.documentElement) $('#drop').classList.remove('over'); }));
document.addEventListener('drop', (e) => { const f = e.dataTransfer?.files?.[0]; if (f) upload(f); });

$('#up-axis').addEventListener('change', async () => {
  try {
    busy('Turning the part…');
    const r = await api('/api/orient', { model_id: state.model.id, up: $('#up-axis').value });
    busy();
    await setModel(r);
    toast('Dimensions re-detected for the new orientation');
  } catch (e) { busy(); toast(e.message, true); }
});
$$('#view-toggle button').forEach((b) => b.addEventListener('click', () => { if (!b.disabled) setView(b.dataset.view); }));

function onMaterialChange() {
  renderMaterialInfo();
  saveSettings({ material: $('#material').value });
  scheduleCheck(0);
}
$('#material').addEventListener('change', onMaterialChange);
for (const id of ['#printer', '#profile']) {
  $(id).addEventListener('change', async () => {
    await saveSettings({ printer: $('#printer').value.trim() || 'Bambu Lab', profile: $('#profile').value.trim() || 'default' });
    await loadMaterials();
    scheduleCheck(0);
  });
}
for (const id of ['#walls', '#infill', '#line-width', '#purpose', '#max-temp']) $(id).addEventListener('input', () => scheduleCheck());
$$('.chem').forEach((s) => {
  s.innerHTML = CHEM_OPTIONS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  s.addEventListener('change', () => scheduleCheck(0));
});

// ---------------------------------------------------------------- calibrate tab
const COUPON_KEY = 'matprint.coupons';
function savedCoupons() { try { return JSON.parse(localStorage.getItem(COUPON_KEY) || '[]'); } catch { return []; } }
function rememberCoupon(meta) {
  const list = [meta, ...savedCoupons()].slice(0, 12);
  try { localStorage.setItem(COUPON_KEY, JSON.stringify(list)); } catch { /* private window */ }
}
function standardCoupon() {
  return { material: $('#cal-material').value, printer: state.settings.printer, profile: state.settings.profile,
    square_designed: 20, square_model: 20, holes: [4, 6, 8].map((d) => ({ designed: d, model: d })), compensated: false, standard: true };
}
function couponLabel(m) {
  return `${m.material} · ${m.square_designed} mm square · Ø${m.holes.map((h) => h.designed).join('/')}${m.compensated ? ' · confirmation' : ''}${m.created ? ' · ' + m.created : ''}`;
}
function renderCouponSelect(selectIndex = 0) {
  const list = savedCoupons();
  $('#cal-coupon').innerHTML = list.map((m, i) => `<option value="${i}">${esc(couponLabel(m))}</option>`).join('') +
    `<option value="std">Standard coupon (20 mm square, Ø4/6/8 holes, not pre-corrected) in ${esc($('#cal-material').value)}</option>`;
  $('#cal-coupon').value = list.length ? String(selectIndex) : 'std';
  renderHoleInputs();
}
function currentCoupon() {
  const v = $('#cal-coupon').value;
  return v === 'std' ? standardCoupon() : savedCoupons()[parseInt(v, 10)];
}
function renderHoleInputs() {
  const c = currentCoupon();
  $('#m-holes').innerHTML = c.holes.map((h, i) => `<label>Hole Ø${h.designed} (mm) <input type="number" step="0.01" data-hole="${i}"></label>`).join('');
}
$('#cal-coupon').addEventListener('change', renderHoleInputs);
$('#cal-material').addEventListener('change', () => { if ($('#cal-coupon').value === 'std') renderCouponSelect(); });

$('#coupon-btn').addEventListener('click', async () => {
  const holes = $('#cal-holes').value.split(/[,; ]+/).map(parseFloat).filter((v) => v > 0);
  try {
    busy('Creating the coupon…');
    const r = await api('/api/coupon', {
      material: $('#cal-material').value, square: parseFloat($('#cal-square').value) || 20, holes,
      compensated: $('#cal-confirm').checked, printer: state.settings.printer, profile: state.settings.profile,
      output_dir: state.settings.output_dir,
    });
    busy();
    r.meta.created = new Date().toLocaleDateString();
    rememberCoupon(r.meta);
    renderCouponSelect(0);
    $('#coupon-result').innerHTML = `<div class="alert ok">Saved to <b>${esc(r.output_dir)}</b></div><ul class="files">` +
      r.files.filter((f) => f.kind !== 'json').map((f) => `<li><span class="fname">${esc(f.name)}</span><a class="btn small" href="${f.url}?download=1" download="${esc(f.name)}">Download</a></li>`).join('') + '</ul>';
  } catch (e) { busy(); toast(e.message, true); }
});

$('#calibrate-btn').addEventListener('click', async () => {
  const c = currentCoupon();
  const sqx = parseFloat($('#m-sqx').value), sqy = parseFloat($('#m-sqy').value);
  if (!(sqx > 0 && sqy > 0)) { toast('Enter the measured square size in X and Y', true); return; }
  const holes = $$('[data-hole]').map((i) => parseFloat(i.value));
  const holesGiven = holes.filter((v) => v > 0);
  if (holesGiven.length && holesGiven.length !== holes.length) { toast('Enter every hole, or leave them all empty', true); return; }
  const dried = $('#m-dried').value === '' ? null : $('#m-dried').value === 'yes';
  try {
    const r = await api('/api/calibrate', { coupon: c, square_x: sqx, square_y: sqy, holes: holesGiven,
      dried, walls: parseInt($('#m-walls').value, 10) || null, infill: parseFloat($('#m-infill').value) });
    let msg;
    if (r.confirmed === true) msg = `<div class="alert ok"><b>Confirmed.</b> The worst error was ${fmt(r.worst_error_mm)} mm, within ±0.05 mm. The calibration is unchanged.</div>`;
    else {
      const rec = r.record;
      msg = `<div class="alert ${r.confirmed === false ? 'warn' : 'ok'}">${r.confirmed === false ? `Still ${fmt(r.worst_error_mm)} mm off, so this coupon was added as a new measurement. ` : ''}
        <b>Saved for ${esc(c.material)}:</b> this coupon gave shrinkage ${fmt(r.s_pct)}%${r.h_mm != null ? ` and hole offset ${r.h_mm >= 0 ? '+' : ''}${fmt(r.h_mm)} mm` : ''}.
        Using shrinkage ${fmt(rec.s_pct)}%${rec.h_mm != null ? `, hole offset ${fmt(rec.h_mm)} mm` : ''} from ${rec.runs} coupon${rec.runs > 1 ? 's' : ''}.
        ${rec.s_range ? `Print-to-print range ${fmt(rec.s_range[0])}–${fmt(rec.s_range[1])}%.` : 'Print a second coupon to measure print-to-print variation.'}
        Then print a confirmation coupon to check.</div>`;
    }
    if (dried === null) msg += '<p class="muted small">Tip: record whether the filament was dried. Damp filament prints holes tighter.</p>';
    $('#calibrate-result').innerHTML = msg;
    renderCalList(r.calibration);
    await loadMaterials();
    if (state.model) scheduleCheck(0);
  } catch (e) { toast(e.message, true); }
});

async function refreshCalibration() {
  renderCouponSelect($('#cal-coupon').value && $('#cal-coupon').value !== 'std' ? parseInt($('#cal-coupon').value, 10) : 0);
  try { renderCalList(await api('/api/calibration')); } catch (e) { toast(e.message, true); }
}

function renderCalList(list) {
  if (!list.length) { $('#cal-list').innerHTML = '<p class="muted">Nothing calibrated yet. The built-in typical values are used.</p>'; return; }
  $('#cal-list').innerHTML = `<table class="cal-table"><tr><th>Material</th><th>Printer / profile</th><th>Shrinkage</th><th>Hole offset</th><th>Print-to-print range</th><th>Coupons</th><th>Last</th><th></th></tr>` +
    list.map((c, i) => `<tr><td><b>${esc(c.material)}</b></td><td>${esc(c.printer)} / ${esc(c.profile)}</td><td>${fmt(c.s_pct)}%</td>
      <td>${c.h_mm != null ? fmt(c.h_mm) + ' mm' : '–'}</td><td>${c.s_range ? `${fmt(c.s_range[0])}–${fmt(c.s_range[1])}%` : 'needs 2+ coupons'}</td>
      <td>${c.runs}</td><td>${esc(c.last)}</td><td><button class="btn small" data-clear="${i}">Forget</button></td></tr>`).join('') + '</table>';
  $$('[data-clear]').forEach((b) => b.addEventListener('click', async () => {
    const c = list[parseInt(b.dataset.clear, 10)];
    if (!confirm(`Forget the calibration for ${c.material} on ${c.printer} / ${c.profile}?`)) return;
    renderCalList(await api('/api/calibration/clear', c));
    await loadMaterials();
    if (state.model) scheduleCheck(0);
  }));
}

// ---------------------------------------------------------------- start
async function start() {
  const cfg = await api('/api/config');
  state.settings = cfg.settings;
  $('#version').textContent = `v${cfg.version}`;
  $('#printer').value = state.settings.printer;
  $('#profile').value = state.settings.profile;
  await loadMaterials();
  setInterval(() => fetch('/api/ping').catch(() => {}), 5000);
}
start().catch((e) => toast(e.message, true));
