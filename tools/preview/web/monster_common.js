// Shared bits of the monster dialogs (monster_views.js = Approved, monster3d.js = 3D / Completed): how a 409 of the monster verbs is
// worded, and the read-only `damaged` state. Review of phase 5 (findings 8 and 12): the request carries the exact files / LOD digest the
// user saw, and the server answers 409 `changed` (the views or the model are not what the user saw) or `missing` (a ticked file is gone).

const first = (...values) => values.find(value => value != null && value !== '' && !(Array.isArray(value) && !value.length));

/**
 * The toast text of a 409 from a monster verb. `kind`: 'views' (convert / reconvert), 'model' (approve). Always a string.
 *  - changed: "the views changed; check the ticks again" / "the model changed since you opened it"
 *  - missing: names the file the server says is gone
 *  - anything else: the server's own message (a plain stale rev just says it reloaded)
 */
export function conflictMessage(error, kind = 'views') {
  if (error?.code === 'changed') {
    return kind === 'model' ? 'The model changed since you opened it; it is loaded again' : 'The views changed; check the ticks again';
  }
  if (error?.code === 'missing') {
    const body = error.body || {};
    const file = first(body.file, body.files, body.missing);
    return `A ticked picture is missing${file ? `: ${[].concat(file).join(', ')}` : ''}${file ? '' : error.message ? ` (${error.message})` : ''}`;
  }
  return error?.stale ? 'Changed elsewhere; reloaded' : (error?.message || 'Changed elsewhere');
}

/** A variant the server reports as damaged (unreadable state): read-only, no action buttons, only the error. */
export function isDamaged(item) {
  return !!(item && (item.damaged === true || (item.damaged && typeof item.damaged === 'object') || typeof item.damaged === 'string'
    || item.state === 'damaged' || item.status === 'damaged'));
}

/** The error text of a damaged item. */
export function damageText(item) {
  const d = item?.damaged;
  const detail = first(typeof d === 'string' ? d : null, d && typeof d === 'object' ? first(d.error, d.message, d.why) : null, item?.damaged_error, item?.error);
  return `This variant is damaged and read-only${detail ? `: ${detail}` : ''}`;
}

/** The file name of a view item (`file` from the server, else `<name>.png`). */
export const viewFile = view => view?.file || `${view?.name}.png`;
