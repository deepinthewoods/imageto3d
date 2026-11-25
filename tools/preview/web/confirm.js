// A confirmation dialog over the whole page (the Queue tab's Stop / Restart / Run now). Keyboard rules: focus starts on Cancel (so a
// stray or held Enter can only cancel), Esc cancels, Tab stays inside, and no key of the page behind it (arrows, shortcuts) gets through.
// Nothing is confirmed by a key repeat: the Confirm button is only activated by a click, or by ONE fresh Enter / Space press on it after the
// user tabbed there. A held Enter / Space (event.repeat) is swallowed by the document capture handler below (it stops every key, so the
// overlay's own blockRepeat never sees the event); the click that a held Space ends in on release is ignored too.
import { activationRepeat, blockRepeat, button, h, isActivationKey } from './util.js';

let current = null;

/**
 * confirmDialog({title, lines: [text], confirm: 'Stop', danger: true}) -> Promise<boolean>
 * A second call while one is open resolves false at once.
 */
export function confirmDialog({ title, lines = [], confirm = 'Confirm', danger = false, content = null } = {}) {   // PHASE 5: `content` (a Node: ticks, a view list) sits between the lines and the buttons
  if (current) return Promise.resolve(false);
  return new Promise(resolve => {
    const previous = document.activeElement;
    let overlay = null;
    let held = false;                // an activation key repeated since its last fresh press: the click it ends in is not a decision
    const done = value => {
      if (!overlay) return;
      overlay.remove();
      overlay = null;
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('keyup', onKeyUp, true);
      current = null;
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
      resolve(value);
    };
    const cancel = button('Cancel', () => done(false), '', 'Esc');
    const ok = button(confirm, event => { if (!(held && event?.detail === 0)) done(true); }, danger ? 'danger' : 'primary');
    cancel.dataset.fk = 'confirm-cancel';
    ok.dataset.fk = 'confirm-ok';
    function onKey(event) {
      if (activationRepeat(event)) { held = true; event.preventDefault(); }          // a held Enter / Space never activates a button
      else if (isActivationKey(event)) held = false;                                    // a fresh press starts over
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); done(false); return; }
      if (event.key === 'Tab') {
        event.preventDefault();
        // PHASE 5: Tab also walks the controls of `content` (Cancel, content controls..., Confirm); with no content it is the old two-way toggle
        const order = [cancel, ...(content ? [...content.querySelectorAll('input, select, button')] : []), ok];
        const at = order.indexOf(document.activeElement);
        order[(at + (event.shiftKey ? -1 : 1) + order.length) % order.length].focus();
      }
      event.stopPropagation();           // the page behind (modal arrows, A, Enter, B ...) must not see any key while this is open
    }
    function onKeyUp(event) {            // releasing a held Space would click the focused button: that click is ignored, then the flag clears
      if (isActivationKey(event) && held) { event.preventDefault(); setTimeout(() => { held = false; }); }
    }
    overlay = h('div', { class: 'q-confirm', role: 'alertdialog', 'aria-modal': 'true', 'aria-label': title },
      h('div', { class: 'confirm-box' },
        h('h3', { text: title }),
        ...lines.map(text => h('p', { text })),
        content,
        h('div', { class: 'confirm-buttons' }, cancel, ok)));
    blockRepeat(overlay);
    current = overlay;
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('keyup', onKeyUp, true);
    document.body.append(overlay);
    cancel.focus();
  });
}
