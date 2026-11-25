// The one modal dialog (markup lives in index.html). A view (concept, approved, 3D, review) opens it with
// openModal({...}) and fills `body` / `actions` / `infoBody`; closing disposes the view through `onClose`.
// Keyboard: the view's `onKey` runs first (return true = handled); then Esc closes and the arrows step prev/next.
import { $, h, shortcutKey } from './util.js';

let current = null;   // {onClose, onKey, onNav}
let overlay = null;

function resetDom() {
  $('modal-badges').replaceChildren();
  $('modal-body').replaceChildren();
  $('modal-body').onclick = null;
  $('actions').replaceChildren();
  $('actions').className = '';
  $('info-body').replaceChildren();
  $('info').hidden = true;
  $('info').open = false;
  $('set-pos').textContent = '';
}

/**
 * Open (or re-open for the next card) the dialog. Returns the controller for the view.
 * spec: {title, badges: [Node], info: bool, actionsClass, onClose, onKey, onPrev, onNext, onDismiss}
 * onDismiss runs when the user closes the dialog (X, shade, Esc); onClose runs on every teardown.
 */
export function openModal(spec = {}) {
  if (current) teardown();
  resetDom();
  const mine = { onClose: spec.onClose, onKey: spec.onKey, onPrev: spec.onPrev, onNext: spec.onNext, onDismiss: spec.onDismiss };
  current = mine;
  $('modal').hidden = false;
  focusDialog();
  $('modal-title').textContent = spec.title || '';
  for (const node of spec.badges || []) $('modal-badges').append(node);
  $('info').hidden = !spec.info;
  if (spec.actionsClass) $('actions').className = spec.actionsClass;
  const controller = {
    body: $('modal-body'),
    actions: $('actions'),
    infoBody: $('info-body'),
    setTitle: text => { $('modal-title').textContent = text; },
    setBadges: nodes => $('modal-badges').replaceChildren(...nodes),
    /** Previous / next buttons and the "3 / 40" position; pass null functions to disable a side. */
    setNav({ index, total, hasPrev, hasNext }) {
      $('prev-set').disabled = !hasPrev;
      $('next-set').disabled = !hasNext;
      $('set-pos').textContent = total ? `${index + 1} / ${total}` : '';
    },
    /** The view registers its key handler and its teardown (it is created after the dialog opened). */
    setKeyHandler: fn => { mine.onKey = fn; },
    setTeardown: fn => { mine.onClose = fn; },
    close: () => closeModal(),
  };
  controller.setNav({ index: 0, total: 0, hasPrev: !!spec.onPrev, hasNext: !!spec.onNext });
  return controller;
}

function teardown() {
  closeOverlay();
  const old = current;
  current = null;
  try { old?.onClose?.(); } catch (error) { console.error(error); }
}

/** Close the dialog (a dismissal by the user: the router also drops the history entry). */
export function closeModal({ silent = false } = {}) {
  if (!current) return;
  const dismiss = current.onDismiss;
  teardown();
  $('modal').hidden = true;
  resetDom();
  if (!silent) dismiss?.();
}

export const isModalOpen = () => !$('modal').hidden;

const dialog = () => document.querySelector('#modal .dialog');

/** Keyboard focus goes into the dialog (not the card underneath), so a key the opening gesture repeats lands on the dialog body. */
export function focusDialog() {
  dialog()?.focus({ preventScroll: true });
}

/** True when the key event targets the dialog body itself (nothing inside it has focus): the only place Enter = approve works. */
export const onDialogBody = event => event.target === dialog() || event.target === document.body;

// ---- fresh key presses for mutation shortcuts ------------------------------------------------------------------------
// A held key repeats (`event.repeat`); a shortcut that CHANGES something (approve, reject, provisional ...) must come from one fresh
// press: repeats are ignored, and once a shortcut fired (or opened a confirmation, or moved to the next card) its key stays "spent"
// until it is released (keyup) or the window loses focus, so a key held across a dialog change never fires a second action.
const spent = new Set();
const keyId = event => String(event.key).toLowerCase();
/** True for a fresh press of a key that has not fired a mutation shortcut since it went down. */
export const freshKey = event => !event.repeat && !spent.has(keyId(event));
/**
 * Run `fn` for a mutation shortcut only on a fresh press, and spend the key. Always returns true (the event is handled, repeats are
 * swallowed too so they do not fall through to Esc-closes / arrows).
 */
export function mutationKey(event, fn) {
  event.preventDefault();
  if (!freshKey(event)) return true;
  spent.add(keyId(event));
  fn();
  return true;
}

/** Full size picture over the dialog; click or Esc closes it. */
export function showImage(url) {
  closeOverlay();
  overlay = h('div', { class: 'fullsize', onclick: closeOverlay }, h('img', { src: url, alt: '' }));
  $('modal').append(overlay);
}

function closeOverlay() {
  overlay?.remove();
  overlay = null;
}

export function initModal() {
  $('close').onclick = () => closeModal();
  document.querySelector('#modal .shade').onclick = () => closeModal();
  $('prev-set').onclick = () => current?.onPrev?.();
  $('next-set').onclick = () => current?.onNext?.();
  // a click on a dialog button leaves focus on it; hand it back to the dialog so the shortcuts keep working afterwards
  dialog().addEventListener('click', event => {
    if (event.target instanceof Element && event.target.closest('button')) setTimeout(() => { if (isModalOpen() && !overlay && !document.querySelector('.q-confirm')) focusDialog(); }, 0);   // PHASE 5: not while a confirmation opened by the click has the focus
  });
  document.addEventListener('keyup', event => spent.delete(keyId(event)));
  window.addEventListener('blur', () => spent.clear());
  document.addEventListener('keydown', event => {
    if (!current || event.defaultPrevented || event.ctrlKey || event.altKey || event.metaKey) return;
    if (overlay) {
      if (event.key === 'Escape') { event.preventDefault(); closeOverlay(); }
      return;
    }
    if (!shortcutKey(event)) return;      // a focused button / input / select / textarea handles its own keys
    if (current.onKey?.(event) === true) return;
    if (event.key === 'Escape') closeModal();
    else if (event.key === 'ArrowRight') current.onNext?.();
    else if (event.key === 'ArrowLeft') current.onPrev?.();
  });
}
