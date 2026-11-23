// Monsters tab of the preview site (2026-11-21). One view per monster variant: the 3D model at the chosen level,
// the conversion image, the colour legend, and the LOD / approval choice. Loaded on demand by app.js, which passes
// its own helpers in (this file imports nothing from app.js). Server side: monsters.py.
const LODS = ['0p8', '0p6', '0p4', '0p2', '0p1'];
const lodLabel = level => level.replace('p', '.') + '%';
const count = n => typeof n === 'number' ? n.toLocaleString() + ' faces' : '';
let styled = false;

function style() {
  if (styled) return;
  styled = true;
  const css = document.createElement('style');
  css.textContent = `
.monster-layout{display:flex;width:100%;height:100%;min-height:0}
.monster-layout .viewer-shell{flex:1 1 auto;min-width:0}
.monster-empty{display:flex;align-items:center;justify-content:center;width:100%;height:100%;color:#aab6c8}
.monster-empty img{max-width:100%;max-height:100%;object-fit:contain}
.monster-side{flex:0 0 300px;overflow:auto;background:#1f2631;border-left:1px solid #374252;padding:.7rem;display:flex;flex-direction:column;gap:.8rem}
.monster-side h3{margin:0 0 .35rem;font-size:.8rem;text-transform:uppercase;letter-spacing:.04em;color:#9fb0c8}
.monster-side img{width:100%;border-radius:8px;background-color:#353b46;background-image:linear-gradient(45deg,#414855 25%,transparent 25%),linear-gradient(-45deg,#414855 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#414855 75%),linear-gradient(-45deg,transparent 75%,#414855 75%);background-size:20px 20px;background-position:0 0,0 10px,10px -10px,-10px 0;cursor:zoom-in}
.monster-roles{display:grid;grid-template-columns:auto 1fr;gap:.35rem .6rem;align-items:start;font-size:.85rem}
.monster-roles .swatch{width:1.1rem;height:1.1rem;border-radius:4px;border:1px solid #0006;margin-top:.1rem}
.monster-roles b{display:block;color:#e6ecf5}
.monster-roles span{color:#aab6c8}
.monster-facts{font-size:.85rem;color:#c9d1dd;display:grid;gap:.25rem}
.monster-facts a{color:#91baff}
.monster-chosen{margin-left:auto;color:#aab6c8;white-space:nowrap}
@media (max-width:900px){.monster-layout{flex-direction:column}.monster-side{flex:0 0 auto;max-height:40%;border-left:0;border-top:1px solid #374252}}`;
  document.head.append(css);
}

const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

export function showMonster(item, api) {
  const { el, button, badge, picture, toast, makeViewer, setViewer, isOpen, rerender } = api;
  style();
  const body = el('modal-body');
  const layout = node('div', 'monster-layout'), shell = node('div', 'viewer-shell'), wrap = node('div', 'viewer-wrap'), side = node('div', 'monster-side');
  shell.append(wrap);
  layout.append(shell, side);
  body.append(layout);

  const badges = el('modal-badges');
  const drawBadges = () => {
    badges.replaceChildren(badge(item.status));
    if (item.lod) badges.append(badge('LOD ' + lodLabel(item.lod)));
    if (item.approved) badges.append(badge('approved'));
  };
  drawBadges();

  // side panel: images, colour legend, conversion facts
  const section = (title, ...children) => { const s = node('section'); s.append(node('h3', '', title), ...children); side.append(s); return s; };
  for (const [title, url] of [['Conversion image', item.image], ['Source image', item.source_image]]) {
    if (!url) continue;
    const img = picture(url, false);
    img.title = 'Open full size';
    img.onclick = () => window.open(url, '_blank');
    section(title, img);
  }
  if (item.roles?.length) {
    const grid = node('div', 'monster-roles');
    for (const role of item.roles) {
      const swatch = node('span', 'swatch');
      swatch.style.background = role.hex || '#888';
      const text = node('div');
      text.append(node('b', '', role.role), node('span', '', (role.parts || []).join(', ')));
      grid.append(swatch, text);
    }
    section('Colour coding', grid);
  }
  const facts = node('div', 'monster-facts');
  facts.append(node('div', '', `Status: ${item.status}`));
  if (item.record?.model_url) { const a = node('a', '', 'Hyper3D model page'); a.href = item.record.model_url; a.target = '_blank'; a.rel = 'noreferrer'; facts.append(a); }
  if (item.highpoly_faces) facts.append(node('div', '', `High-poly: ${count(item.highpoly_faces)}`));
  section('Conversion', facts);

  // controls: which model to look at, wireframe, the LOD choice and approval
  const controls = el('actions');
  controls.classList.add('model-actions');
  const mk = (label, input) => { const holder = node('label'); holder.append(document.createTextNode(label), input); return holder; };
  const modelSelect = node('select');
  for (const level of LODS) {
    const lod = item.lods?.[level];
    if (lod) modelSelect.add(new Option(['LOD ' + lodLabel(level), count(lod.faces)].filter(Boolean).join(' · '), level));
  }
  if (item.highpoly) modelSelect.add(new Option(['High-poly source', count(item.highpoly_faces)].filter(Boolean).join(' · '), 'highpoly'));
  const hasModel = modelSelect.options.length > 0;
  if (item.lod && item.lods?.[item.lod]) modelSelect.value = item.lod;
  const wire = node('input'); wire.type = 'checkbox';
  // Sections: the same level with one material per section (skull, each feather, each wrap), when it has been split
  const hasSections = LODS.some(level => item.lods?.[level]?.sections);
  const split = node('input'); split.type = 'checkbox'; split.checked = hasSections;
  const sectionList = node('div', 'monster-roles');
  const sectionPanel = hasSections ? section('Sections (one material each)', sectionList) : null;
  function drawSections() {
    if (!sectionPanel) return;
    const lod = item.lods?.[modelSelect.value];
    sectionPanel.hidden = !(split.checked && lod?.sections);
    sectionList.replaceChildren();
    for (const material of (lod?.section_materials || [])) {
      const swatch = node('span', 'swatch');
      swatch.style.background = material.hex || '#888';
      const text = node('div');
      text.append(node('b', '', material.name), node('span', '', [material.kind, count(material.faces)].filter(Boolean).join(' · ')));
      sectionList.append(swatch, text);
    }
    for (const warning of (lod?.section_warnings || [])) { const note = node('span', '', warning); note.style.gridColumn = '1 / -1'; sectionList.append(note); }
  }
  const chosen = node('span', 'monster-chosen');
  const use = button('Use this LOD', () => save({ lod: modelSelect.value }), 'primary');
  const approve = button('', () => save({ approved: !item.approved }));
  const urlOf = value => value === 'highpoly' ? item.highpoly : (split.checked && item.lods?.[value]?.sections) || item.lods?.[value]?.url;

  function sync() {
    const value = modelSelect.value;
    use.disabled = !hasModel || value === 'highpoly' || value === item.lod;
    use.title = value === 'highpoly' ? 'The high-poly source is for comparison; choose one of the reduced levels' : 'Record this level as the one to rig and export';
    approve.textContent = item.approved ? 'Unapprove' : 'Approve';
    approve.className = item.approved ? '' : 'primary';
    approve.disabled = !item.lod;
    approve.title = item.lod ? '' : 'Choose a LOD first';
    chosen.textContent = item.lod ? `Chosen: LOD ${lodLabel(item.lod)}${item.approved ? ' · approved' : ''}` : hasModel ? 'No LOD chosen yet' : '';
    drawBadges();
  }

  async function save(patch) {
    try {
      const response = await fetch('/api/monsters/choice', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: item.id, ...patch }) });
      const result = await response.json();
      if (!response.ok) throw Error(result.error);
      item.lod = result.lod || null;
      item.approved = result.approved;
      sync();
      rerender();
      toast('lod' in patch ? `LOD ${lodLabel(item.lod)} chosen for ${item.name}` : item.approved ? `${item.name} approved` : `${item.name} unapproved`);
    } catch (error) { toast(`Not saved: ${error.message}`); }
  }

  let token = 0, current = null;
  async function load() {
    const mine = ++token, url = urlOf(modelSelect.value);
    if (!url) return;
    const keep = current ? current.view() : null;
    try {
      const made = await makeViewer(wrap, [url]);
      if (mine !== token || !isOpen()) { made.dispose(); return; }
      current = made;
      setViewer(made);
      if (keep) made.setView(keep, false);
      made.wireframe(wire.checked);
    } catch (error) { if (mine === token) toast(`Model not loaded: ${error.message}`); }
  }

  if (hasModel) {
    modelSelect.onchange = () => { sync(); drawSections(); load(); };
    wire.onchange = () => current?.wireframe(wire.checked);
    split.onchange = () => { drawSections(); load(); };
    controls.append(mk('Model', modelSelect), mk('Wireframe', wire), ...(hasSections ? [mk('Sections', split)] : []), use, approve, chosen);
    sync();
    drawSections();
    load();
  } else {
    const empty = node('div', 'monster-empty');
    if (item.image) empty.append(picture(item.image, false)); else empty.textContent = 'No image or model yet';
    wrap.replaceWith(empty);
    controls.append(node('span', 'monster-chosen', 'Not converted yet'));
  }
}
