// Small DOM and formatting helpers shared by every module.

export const $ = id => document.getElementById(id);

/** Replace underscores with spaces ("body_armor" -> "body armor"). */
export const pretty = s => String(s ?? '').replaceAll('_', ' ');

/** "0p2" -> "0.2%" (LOD keys are the share of the high-poly faces the Blender reduction keeps). */
export const lodLabel = level => String(level).replace('p', '.') + '%';

export const faceText = n => (typeof n === 'number' ? n.toLocaleString('en-US') + ' faces' : '');

/**
 * Tiny hyperscript: h('button', {class: 'primary', text: 'Go', onclick: fn}, child, ...).
 * Props: class, text, style (object), dataset (object), onXxx handlers, DOM properties, else attributes.
 * null / undefined / false props and children are skipped.
 */
export function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') node[key.toLowerCase()] = value;
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value === true ? '' : value);
  }
  appendAll(node, children);
  return node;
}

function appendAll(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function button(label, onclick, cls = '', title = '') {
  return h('button', { text: label, onclick, class: cls, title, type: 'button' });
}

export function badge(text, tone = '') {
  // "building n/m" pulses so a set that is still working is not mistaken for a finished one
  const busy = /^building/i.test(text) ? ' busy' : '';
  return h('span', { class: 'badge' + (tone ? ' tone-' + tone : '') + busy, text, title: text });
}

/** An <img> that does not take part in layout until loaded. */
export function picture(url, { lazy = true, cls = '' } = {}) {
  const img = h('img', { alt: '', class: cls });
  if (lazy) img.loading = 'lazy';
  img.src = url;
  return img;
}

/** Concept image stacked over/under the race base (CSS body[data-base] decides how). */
export function layered(url, base, lazy = true) {
  const box = h('span', { class: 'layered' });
  if (base) box.append(picture(base, { lazy, cls: 'base' }));
  box.append(picture(url, { lazy }));
  return box;
}

export const storage = {
  get(key, fallback = null) {
    try {
      const raw = localStorage.getItem('v2.' + key);
      return raw == null ? fallback : JSON.parse(raw);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem('v2.' + key, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

/** True while keyboard focus is in a control that uses the keys itself. */
export function typing(target = document.activeElement) {
  return ['INPUT', 'SELECT', 'TEXTAREA'].includes(target?.tagName);
}

/**
 * True when a key event belongs to a control that activates or edits itself (button, input, select, textarea, link, summary,
 * editable text): global shortcuts must leave it alone, or Enter on a focused Reject button would run "approve".
 */
export function interactive(target) {
  if (!(target instanceof Element)) return false;
  if (target.isContentEditable) return true;
  return !!target.closest('button, input, select, textarea, a[href], summary, [contenteditable="true"]');
}

/** A shortcut may only fire for a plain key press (no Ctrl / Alt / Meta) that is not meant for a control. */
export const shortcutKey = event => !event.ctrlKey && !event.altKey && !event.metaKey && !interactive(event.target);

/** `<label>text <control></label>` for the bottom bar. */
export const field = (text, control, title = '') => h('label', { title }, text, control);

/** A checkbox drawn as a toggle button (blue when on, see style.css `.model-actions`). */
export function toggle(text, checked, onchange, title = '') {
  const input = h('input', { type: 'checkbox', checked: !!checked, onchange: () => onchange(input.checked) });
  return { input, label: h('label', { title }, input, text) };
}

/** <select> from [[value, text], ...]; keeps `value` when it is still an option. */
export function fillSelect(select, options, value) {
  select.replaceChildren(...options.map(([v, text]) => h('option', { value: v, text })));
  if (options.some(([v]) => v === value)) select.value = value;
  return select;
}

/** "42s", "3m 05s", "2h 10m" for a number of seconds ('' when it is not a number). */
export function duration(seconds) {
  if (seconds == null || Number.isNaN(+seconds)) return '';
  const s = Math.max(0, Math.round(+seconds));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s` : `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}

/** "123.4 GB" for a byte count ('' when it is not a number). */
export function bytesText(bytes) {
  if (bytes == null || Number.isNaN(+bytes)) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = Math.max(0, +bytes);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value >= 100 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

/** "5 min ago" / "in 2 h" for an ISO time or epoch seconds ('' when unparseable). Times without a zone are read as local time. */
export function ago(when, now = Date.now()) {
  if (when == null || when === '') return '';
  const ms = typeof when === 'number' ? when * 1000 : Date.parse(when);
  if (Number.isNaN(ms)) return '';
  const s = Math.round((now - ms) / 1000);
  const a = Math.abs(s);
  if (a < 5) return 'just now';
  const text = a < 60 ? `${a} s` : a < 3600 ? `${Math.round(a / 60)} min` : a < 172800 ? `${Math.round(a / 3600)} h` : `${Math.round(a / 86400)} d`;
  return s < 0 ? `in ${text}` : `${text} ago`;
}

/** Enter and Space are the keys that activate a focused button (Enter on keydown, Space on keyup). */
export const isActivationKey = event => event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar';

/** A repeat (held key) of an activation key: it must never activate anything. */
export const activationRepeat = event => event.repeat === true && isActivationKey(event);

/**
 * A held Enter (or Space) key repeats the click of a focused button (and the implicit submit of an input): swallow the repeats so a held
 * key never fires a write twice. Put it on a container; it listens in the capture phase. A handler on `document` that stops the event
 * (confirm.js) must call `activationRepeat` itself: the event never reaches a container below it.
 */
export function blockRepeat(node) {
  node.addEventListener('keydown', event => {
    if (activationRepeat(event) && event.target instanceof Element && event.target.closest('button, input')) event.preventDefault();
  }, true);
}
