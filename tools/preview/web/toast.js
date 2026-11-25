// One toast at a time, optional Undo button. Default lifetime 8 s (DESIGN.md section 5).
import { $, h } from './util.js';

let timer = null;

/**
 * toast('Approved X', {undo: async () => {...}, ms: 8000})
 * The Undo button hides the toast first, then runs the callback.
 */
export function toast(message, { undo = null, ms = 8000 } = {}) {
  const box = $('toast');
  box.replaceChildren(document.createTextNode(message));
  if (undo) {
    box.append(document.createTextNode(' · '), h('button', {
      text: 'Undo',
      type: 'button',
      onclick: () => { hide(); undo(); },
    }));
  }
  box.style.display = 'block';
  clearTimeout(timer);
  timer = setTimeout(hide, ms);
}

export function hide() {
  clearTimeout(timer);
  $('toast').style.display = 'none';
}
