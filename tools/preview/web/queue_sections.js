// Queue tab v1 sections (DESIGN_PHASE4.md E, H): Mode / pause, Services, Credits and disk, Views, Conversions, Holds, Settings.
// Fed by GET /api/v2/queue (keys mode, services, credits, disk, views, conversions, holds, settings); a section whose key is missing is hidden
// (the services section then says service management is off). Every write is POST /api/v2/queue {op, ...} through api.queuePost.
//
// Each section rebuilds its DOM only when the structural part of its data changes (`sig`); volatile numbers (uptime, elapsed, idle time, log
// tails) are updated in place by `live`, so a poll every 3 s never drops keyboard focus or an open <details>. The settings form is built
// once and never overwrites a field the user has edited.
import { api } from './api.js';
import { confirmDialog } from './confirm.js';
import { toast } from './toast.js';
import { ago, badge, bytesText, button, duration, h, pretty, storage } from './util.js';
import { noteServices } from './status.js';
import { estimateConfirmLines, estimateLine, estimateTitle } from './queue_estimate.js';

const SERVICE_ORDER = ['supervisor', 'views', 'convert'];
const SERVICE_TONE = { running: 'ok', idle: 'info', stopping: 'warn', stopped: '', crashed: 'error', held: 'error', duplicate: 'error' };
const VIEW_TONE = { running: 'info', pending: 'info', queued: 'info', done: 'ok', failed: 'error' };
const MODE_BUTTONS = [
  { key: 'auto', mode: 'views-run', label: "Auto - images always, conversions only when I'm away" },
  { key: 'pause-conversions', mode: 'pause-conversions', label: 'Pause conversions' },
  { key: 'pause', mode: 'pause', label: 'Pause everything' },
  { key: 'run', mode: 'run', label: 'Run everything now' },
];
const DISK_WARN = 30 * 1024 ** 3;
const DISK_ERROR = 10 * 1024 ** 3;
const SERVICE_OFF_TEXT = 'service management is off';
const UNMANAGED_TEXT = 'Service management is off - the server was started without --manage-services';
const LOST_MS = 60000;           // a service request that is neither pending (services.pending) nor draining (the service's stop_pending) for this long: outcome not recorded
const CHECK_MS = 330000;         // a Tripo balance check runs up to ~300 s
const REQ_VERB = { start: 'start', stop: 'stop', restart: 'restart' };
const REQ_TONE = { requested: 'info', pending: 'warn', done: 'ok', failed: 'error', lost: 'warn' };

const asList = value => (Array.isArray(value) ? value : value == null || value === '' ? [] : [value]);
const text = value => (typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value));
const linesOf = value => (Array.isArray(value) ? value.map(text) : text(value).split(/\r?\n/)).join('\n');
const setOf = data => String(data ?? '').split('--')[0];
const asMap = value => (value && typeof value === 'object' && !Array.isArray(value) ? value : {});

/** The service map out of `services.status` ({services: {name: {...}}, convert_jobs, errors}) or of the bare map. */
function serviceMap(data) {
  const raw = data?.services;
  if (!raw || typeof raw !== 'object') return null;
  const map = raw.services && typeof raw.services === 'object' ? raw.services : raw;
  return Object.fromEntries(Object.entries(map).filter(([, v]) => v && typeof v === 'object'));
}

/** A section: <details> with its title; `update(data)` rebuilds the body when the signature changes and runs `live` every time. */
function makeSection(id, title, { open = true, sig, build, hidden = () => false }) {
  const opened = storage.get('queueOpen', {});
  const body = h('div', { class: 'q-body' });
  const extra = h('span', { class: 'q-sec-extra' });
  const node = h('details', {
    class: 'q-sec', dataset: { sec: id }, open: id in opened ? !!opened[id] : open,
    ontoggle: event => { if (event.target === node) storage.set('queueOpen', { ...storage.get('queueOpen', {}), [id]: node.open }); },
  }, h('summary', null, h('h3', { text: title }), extra), body);
  let last = null;
  let live = null;
  let want = null;       // a control that had the focus when it was replaced by a disabled one (a write in flight): refocused once it is enabled again
  return {
    node,
    update(data, pending) {
      const skip = hidden(data);
      node.hidden = !!skip;
      if (skip) return;
      const next = JSON.stringify([sig(data), pending]);
      if (next !== last) {
        last = next;
        const focused = document.activeElement;
        const lost = !focused || focused === document.body;
        const keep = (focused && body.contains(focused) ? focused.dataset?.fk : null) || (lost ? want : null);
        const built = build(data);
        body.replaceChildren(...built.nodes.filter(Boolean));
        extra.replaceChildren(...asList(built.extra));
        live = built.live || null;
        const again = keep ? body.querySelector(`[data-fk="${CSS.escape(keep)}"]`) : null;
        again?.focus({ preventScroll: true });
        want = keep && !(again && document.activeElement === again) ? keep : null;
      }
      live?.(data);
    },
  };
}

const note = (value, cls = '') => h('p', { class: `q-note ${cls}`.trim(), text: value });
const keyedButton = (fk, label, onclick, cls = '', title = '', disabled = false) => {
  const node = button(label, onclick, cls, title);
  node.dataset.fk = fk;
  if (disabled) node.disabled = true;
  return node;
};

export function createSections({ reload }) {
  const busy = new Set();
  let data = null;
  const memo = { serviceOff: null, check: null, checkNote: null };
  const requests = [];     // service requests this page sent: {id, name, action, sent, status: requested|pending|done|failed|lost, text}
  const logOpen = new Set();
  const list = [];

  const redraw = () => { for (const section of list) section.update(data, [...busy].sort()); };

  /** Run one write: the control is disabled while it is in flight, errors are toasted, the page is reloaded afterwards. */
  async function run(key, label, fn) {
    if (busy.has(key)) return null;
    busy.add(key);
    redraw();
    try {
      return await fn();
    } catch (error) {
      toast(`${label} failed: ${error.message}`);
      return null;
    } finally {
      busy.delete(key);
      redraw();
      reload();
    }
  }

  // ---- mode and pause ---------------------------------------------------------------------------------------------------
  /** The conversion hold (.codex_jobs/_batch/CONVERT_HOLD, DESIGN_PHASE4.md I.1): `convert_hold: {since, reason}` of queue.mode or queue.services. */
  const convertHold = d => {
    const hold = d?.mode?.convert_hold ?? d?.services?.convert_hold;
    return hold ? (typeof hold === 'object' ? hold : { reason: text(hold) }) : null;
  };
  const convertHoldNote = hold => {
    const n = note(`Conversions are held (CONVERT_HOLD)${hold.since ? ` since ${hold.since}` : ''}${hold.reason ? `: ${hold.reason}` : ''}. The convert worker starts no job and `
      + 'every paid step waits until the supervisor has completed a tick after its (re)start.', 'warn');
    n.dataset.row = 'convert-hold';
    return n;
  };
  async function setMode(button1) {
    if (button1.key === 'run') {
      const ok = await confirmDialog({
        title: 'Run everything now?', confirm: 'Run now', danger: true,
        lines: ['Conversions (Tripo browser, Blender, GPU) start even while you are at the PC, and the idle pause is switched off.',
          'A full pipeline load crashed the GPU before: use this only when you are not working on the machine. Auto goes back to the normal behaviour.',
          ...estimateConfirmLines(data?.run_all_estimate)],
      });
      if (!ok) return;
    }
    await run('mode', 'Mode', async () => {
      await api.queuePost({ op: 'mode', mode: button1.key });
      toast(`Mode: ${button1.label}`, { ms: 2500 });
    });
  }

  const modeSection = makeSection('mode', 'Mode and pause', {
    hidden: d => !d?.mode,
    sig: d => ({ mode: d.mode.mode, pause: d.mode.pause, run: d.mode.supervisor_running, warning: d.mode.warning, away: d.mode.away, hold: convertHold(d) }),
    build(d) {
      const snap = d.mode;
      const current = snap.mode?.mode;
      const buttons = MODE_BUTTONS.map(entry => {
        const active = current === entry.mode;
        const node = keyedButton(`mode:${entry.key}`, entry.label, () => setMode(entry), active ? 'primary' : '',
          active ? 'This is the current mode' : '', busy.has('mode'));
        node.classList.add('q-mode');
        node.dataset.mode = entry.key;
        node.setAttribute('aria-pressed', String(active));
        return node;
      });
      const pause = snap.pause;
      const hold = convertHold(d);
      const nodes = [
        h('div', { class: 'q-mode-row' }, ...buttons),
        h('div', { class: 'q-line' },
          h('span', { text: 'Current: ' }), h('b', { class: 'q-mode-label', text: snap.mode?.label || current || 'unknown' }),
          snap.mode?.since ? h('span', { class: 'q-dim', text: ` since ${snap.mode.since}` }) : null,
          pause
            ? badge(`PAUSE: ${pause.reason || 'no reason given'}${pause.since ? ` · since ${pause.since}` : ''}${asList(pause.scope).length ? ` · ${asList(pause.scope).join(' + ')}` : ''}`, 'warn')
            : badge('not paused', 'ok')),
        h('div', { class: 'q-line' }, h('span', { class: 'q-idle' })),
        h('div', { class: 'q-line q-estimate', dataset: { row: 'run-estimate' } }),
        snap.supervisor_running === false ? note(snap.warning || 'supervisor not running: the mode has no effect on conversions', 'bad') : null,
        hold ? convertHoldNote(hold) : null,
      ];
      pause && nodes[1].lastChild.classList.add('q-pause-chip');
      return {
        nodes,
        extra: [pause ? badge('paused', 'warn') : null, hold ? badge('conversions held', 'warn') : null].filter(Boolean),
        live(next) {
          const s = next.mode;
          const idle = nodes[2].firstChild;
          const mins = s.idle_seconds == null ? null : s.idle_seconds / 60;
          idle.textContent = s.idle_seconds == null ? 'User idle time unknown'
            : `You have been idle for ${mins < 1 ? `${Math.round(s.idle_seconds)} s` : `${Math.round(mins)} min`} (${s.idle_source || 'unknown source'}): `
              + (s.away ? 'away (conversions may run)' : 'at the PC (conversions wait under Auto)');
          const est = nodes[3];                    // the Run everything now estimate (queue.run_all_estimate): updated in place, never rebuilds the buttons
          const line = estimateLine(next.run_all_estimate);
          if (est.textContent !== line) est.textContent = line;
          est.title = estimateTitle(next.run_all_estimate);
          est.hidden = !line;
        },
      };
    },
  });

  // ---- services ----------------------------------------------------------------------------------------------------------
  const names = map => [...SERVICE_ORDER.filter(n => map[n]), ...Object.keys(map).filter(n => !SERVICE_ORDER.includes(n))];
  // The server only ACCEPTS a service request ({ok, pending: true, request}); the manager thread carries it out later (a convert stop waits
  // for the current job). The outcome is the `queue.services.events` entry with the same `request` id; until then the id is in
  // `queue.services.pending`. Requests sent from this page are tracked; pending ones of other tabs / earlier page loads are listed too.
  function trackRequest(id, name, action) {
    requests.push({ id, name, action, sent: Date.now(), quietSince: Date.now(), status: 'requested', text: '' });
    while (requests.length > 8) { const at = requests.findIndex(r => r.final); requests.splice(at < 0 ? 0 : at, 1); }
  }

  function reconcileRequests(d) {
    const svc = asMap(d?.services);
    const pending = asList(svc.pending);
    const events = asList(svc.events);
    const map = serviceMap(d) || {};
    for (const r of requests) {
      if (r.final) continue;
      const id = String(r.id);
      const mine = events.filter(e => e && e.request != null && String(e.request) === id);
      const last = mine[mine.length - 1];
      const outcome = [...mine].reverse().find(e => e.ok === false || (e.ok === true && !e.pending));
      const entry = pending.find(p => String(p?.id) === id);
      const draining = r.action !== 'start' && !!map[r.name]?.stop_pending;      // a long Stop / Restart that still drains is not lost, whatever the pending list says
      if (entry || draining) r.quietSince = null;
      else if (r.quietSince == null) r.quietSince = Date.now();
      let status;
      let detail = '';
      if (outcome) { status = outcome.ok === false ? 'failed' : 'done'; detail = text(outcome.text); }
      else if (entry) { status = 'pending'; detail = last ? text(last.text) : entry.running ? 'running' : 'waiting in the queue'; }
      else if (draining) { status = 'pending'; detail = last ? text(last.text) : 'draining (waiting for the work in flight)'; }
      else if (Date.now() - r.quietSince > LOST_MS) { status = 'lost'; detail = last ? text(last.text) : ''; }
      else if (last?.pending) { status = 'pending'; detail = text(last.text); }
      else status = 'requested';
      r.status = status;
      r.text = detail;
      if (['done', 'failed', 'lost'].includes(status)) {
        r.final = true;
        toast(status === 'done' ? `${r.name}: ${r.action} done${detail ? ` - ${detail}` : ''}`
          : status === 'failed' ? `${r.name}: ${r.action} failed${detail ? `: ${detail}` : ''}`
            : `${r.name}: ${r.action} finished, but no outcome was recorded`, { ms: status === 'failed' ? 8000 : 4000 });
      }
    }
  }

  /** Requests to show for a service: this page's own, plus the server's pending ones this page did not send. */
  function requestsFor(d, name) {
    const mine = requests.filter(r => r.name === name);
    const known = new Set(requests.map(r => String(r.id)));
    const foreign = asList(d?.services?.pending).filter(p => p && p.name === name && !known.has(String(p.id)))
      .map(p => ({ id: p.id, name, action: p.action, status: 'pending', text: p.running ? 'running' : 'waiting in the queue', foreign: true }));
    return [...mine, ...foreign];
  }

  async function serviceAction(name, action) {
    await run(`svc:${name}`, `${pretty(action)} ${name}`, async () => {
      try {
        const result = await api.queuePost({ op: 'service', name, action });
        if (result?.pending || result?.request != null) {
          if (result.request != null) trackRequest(result.request, name, action);
          toast(`${name}: ${action} requested...`, { ms: 3000 });
        } else {
          toast(`${name}: ${action} - ${result?.message || 'accepted by the server'}`, { ms: 4000 });
        }
      } catch (error) {
        if (error.conflict && (/service management|management is off/i.test(error.message) || /off|disabled/i.test(error.code || ''))) {
          memo.serviceOff = error.message || SERVICE_OFF_TEXT;       // the server does not manage services: show that instead of the buttons
          toast(memo.serviceOff, { ms: 5000 });
          return;
        }
        throw error;
      }
    });
  }

  const SERVICE_WORDS = {
    supervisor: ['the supervisor', 'Claims, gates and the idle pause stop working until it is started again. It resumes frozen processes on its way out.',
      'It stops and starts again, which loads new supervisor code. The PAUSE file is kept.'],
    views: ['the view runner', 'Running Codex image jobs carry on; the next runner takes them over through their .done markers.',
      'The runner is stopped and started again; running Codex jobs carry on.'],
    convert: ['the convert worker', 'It exits after the conversion in flight, before it takes the next job.',
      'It exits after the conversion in flight, then starts again.'],
  };
  async function askService(svc, action) {
    const [who, stopText, restartText] = SERVICE_WORDS[svc.name] || [svc.name, '', ''];
    const inFlight = svc.name === 'convert' && asList(svc.convert_jobs).length;
    const ok = await confirmDialog({
      title: `${action === 'stop' ? 'Stop' : 'Restart'} ${who}?`, confirm: action === 'stop' ? 'Stop' : 'Restart', danger: action === 'stop',
      lines: [action === 'stop' ? stopText : restartText, inFlight ? 'A conversion is running right now, so this waits for it to finish.' : ''].filter(Boolean),
    });
    if (ok) serviceAction(svc.name, action);
  }

  function requestLine(r) {
    const words = { requested: 'requested...', pending: `requested, still pending${r.text ? `: ${r.text}` : ''}`, done: `done${r.text ? `: ${r.text}` : ''}`,
      failed: `failed${r.text ? `: ${r.text}` : ''}`, lost: 'finished, but no outcome was recorded' };
    return h('div', { class: `svc-req tone-${REQ_TONE[r.status]}`, dataset: { request: r.id, status: r.status } },
      h('span', { text: `${REQ_VERB[r.action] || r.action}: ${words[r.status]}` }),
      r.final ? button('dismiss', () => { requests.splice(requests.indexOf(r), 1); redraw(); }, '', 'Hide this result') : null);
  }

  function serviceRow(svc) {
    const name = svc.name;
    const working = busy.has(`svc:${name}`);
    const off = !!memo.serviceOff;
    const running = svc.state === 'running';
    const inFlight = name === 'convert' && asList(svc.convert_jobs).length > 0;
    const reqs = svc.requests || [];
    const waiting = !!svc.stop_pending || reqs.some(r => !r.final);       // a request of this service is not finished: no second one
    const buttons = off ? [] : [
      ['stopped', 'crashed', 'held', 'idle'].includes(svc.state)
        ? keyedButton(`svc:${name}:start`, svc.state === 'crashed' ? 'Start now' : 'Start', () => serviceAction(name, 'start'), 'primary', `Start ${name}`, working) : null,
      running ? keyedButton(`svc:${name}:stop`, inFlight ? 'Stop after job' : 'Stop', () => askService(svc, 'stop'), 'danger',
        inFlight ? 'The worker exits after the conversion in flight' : `Stop ${name}`, working || waiting) : null,
      running ? keyedButton(`svc:${name}:restart`, 'Restart', () => askService(svc, 'restart'), '', `Stop and start ${name} again`, working || waiting) : null,
    ];
    const uptime = h('span', { class: 'svc-uptime' });
    const message = svc.message || svc.error;
    const logs = svc.logs || {};
    const pres = [];
    const logBlock = (title, lines) => {
      if (!lines) return null;
      const pre = h('pre');
      pres.push([pre, lines]);
      return h('div', { class: 'svc-logpart' }, h('div', { class: 'q-dim', text: title }), pre);
    };
    const log = (logs.out || logs.err || logs.events) ? h('details', {
      class: 'q-log svc-log', open: logOpen.has(name), dataset: { fk: `svc:${name}:log` },
      ontoggle: event => { if (event.target.open) logOpen.add(name); else logOpen.delete(name); },
    }, h('summary', { text: 'log tails' }),
    logBlock(`events - ${name === 'convert' ? 'convert_events.log' : 'events.log'}`, 'events'),
    logBlock(logs.out_file || 'stdout', 'out'),
    logBlock(logs.err_file || 'stderr', 'err')) : null;
    const row = h('div', { class: 'svc-row' + (SERVICE_TONE[svc.state] === 'error' ? ' bad' : ''), dataset: { service: name, state: svc.state } },
      h('b', { class: 'svc-name', text: name }),
      badge(svc.state, SERVICE_TONE[svc.state] ?? ''),
      h('span', { class: 'svc-meta', text: [svc.pid ? `pid ${svc.pid}` : '', running ? (svc.adopted ? 'adopted' : 'started by the app') : ''].filter(Boolean).join(' · ') }),
      uptime,
      svc.enabled === false ? badge('off in settings', 'warn') : null,
      h('span', { class: 'queue-actions svc-actions' }, working ? h('span', { class: 'q-dim', text: 'working...' }) : null, ...buttons),
      message ? h('div', { class: `svc-message${['crashed', 'held', 'duplicate'].includes(svc.state) ? ' bad' : ''}`, text: message }) : null,
      svc.stop_pending && !reqs.some(r => !r.final && r.action !== 'start') ? h('div', { class: 'svc-req tone-warn', dataset: { pending: 'stop' },
        text: name === 'convert' ? 'stop requested; waiting for the current job' : 'stop requested; waiting' }) : null,
      ...reqs.map(requestLine),
      running && svc.command_differs === true && !waiting ? h('div', { class: 'svc-req tone-warn', dataset: { pending: 'apply' },
        text: 'runs with another backend setting than the saved one; Restart to apply it' }) : null,
      svc.duplicates?.length ? h('div', { class: 'svc-message bad', text: `Two instances: pids ${svc.duplicates.map(d => d.pid).join(', ')}. Stop the extra one by hand; the app never does it.` }) : null,
      log);
    return {
      row,
      live(next) {
        const mine = serviceMap(next)?.[name];
        if (!mine) return;
        uptime.textContent = mine.state === 'running' && mine.uptime != null ? `up ${duration(mine.uptime)}`
          : mine.restart_in != null ? `restarts in ${duration(mine.restart_in)}` : '';
        const content = { events: mine.logs?.events, out: mine.logs?.out, err: mine.logs?.err };
        for (const [pre, kind] of pres) {
          const value = linesOf(content[kind]);
          if (pre.textContent !== value) pre.textContent = value;
          pre.parentElement.hidden = !value;
        }
      },
    };
  }

  /** absent (an older server) | unmanaged (managed === false: no --manage-services) | scanning (no scan has finished yet) | ok */
  function servicesMode(d) {
    const raw = d?.services;
    if (!raw || typeof raw !== 'object') return 'absent';
    if (raw.managed === false) return 'unmanaged';
    if (raw.scanned === false || (raw.managed === true && !raw.last_tick)) return 'scanning';
    return 'ok';
  }

  const servicesSection = makeSection('services', 'Services', {
    sig: d => {
      const mode = servicesMode(d);
      const map = serviceMap(d);
      if (mode !== 'ok' || !map) return { mode, memo: memo.serviceOff };
      return {
        mode,
        memo: memo.serviceOff,
        errors: [d.services.errors, d.services.manager_errors],
        requests: names(map).flatMap(n => requestsFor(d, n)).map(r => [r.id, r.name, r.action, r.status, r.text, !!r.final]),
        services: Object.fromEntries(Object.entries(map).map(([name, s]) => [name, [s.state, s.enabled, s.pid, s.adopted, s.stop_pending, s.message, s.error, s.duplicates, s.convert_jobs,
          s.restart_in != null, !!(s.logs?.out || s.logs?.err || s.logs?.events), s.logs?.out_file, s.logs?.err_file, s.command_differs]])),
      };
    },
    build(d) {
      const mode = servicesMode(d);
      const map = serviceMap(d);
      const flag = (text1, tone, state) => { const n = note(text1, tone); n.dataset.state = state; return n; };
      if (mode === 'absent' || !map) return { nodes: [flag(memo.serviceOff || SERVICE_OFF_TEXT, 'bad', 'off')], extra: badge('off', 'warn') };
      if (mode === 'unmanaged') return { nodes: [flag(UNMANAGED_TEXT, 'warn', 'unmanaged')], extra: badge('not managed', 'warn') };
      if (mode === 'scanning') return { nodes: [flag('Scanning for the running services... (the first scan takes a few seconds)', 'warn', 'scanning')], extra: badge('scanning...', 'warn') };
      const ordered = names(map);
      const rows = ordered.map(n => serviceRow({ name: n, ...map[n], requests: requestsFor(d, n) }));
      const problems = [...asList(d.services?.errors), ...asList(d.services?.manager_errors)];
      const scan = h('div', { class: 'q-dim', dataset: { row: 'scan' } });
      const nodes = [
        memo.serviceOff ? note(memo.serviceOff, 'bad') : null,
        ...problems.map(p => note(text(p), 'bad')),
        h('div', { class: 'svc-list' }, ...rows.map(r => r.row)),
        scan,
      ];
      const bad = ordered.filter(n => ['crashed', 'held', 'duplicate'].includes(map[n].state)).length;
      return {
        nodes, extra: bad || problems.length ? badge(`${bad || problems.length} problem${(bad || problems.length) === 1 ? '' : 's'}`, 'error') : badge('ok', 'ok'),
        live(next) {
          rows.forEach(r => r.live(next));
          const tick = next.services?.last_tick;
          scan.textContent = tick ? `Last scan ${ago(tick)}${next.services.interval ? ` (every ${next.services.interval} s)` : ''}` : '';
        },
      };
    },
  });

  // ---- credits and disk --------------------------------------------------------------------------------------------------
  // check_tripo starts the balance check on a worker thread ({ok: true, status: 'running', pending: true}); the result arrives up to ~300 s later as
  // `credits.check.last` ({status: ok | busy | error, credits | message}) while `credits.check.running` is true. {ok: false, status: 'busy'} = refused at once.
  const checkKey = c => JSON.stringify([c?.started, c?.finished, c?.last?.status, c?.last?.checked_at, c?.last?.credits, c?.last?.message]);
  const BUSY_TEXT = 'busy: a conversion uses the Tripo browser';
  function checkResult(last) {
    if (!last) return null;
    if (last.status === 'ok') return { tone: '', text: `Balance check: ${last.credits} credits${last.checked_at ? ` (${ago(last.checked_at)})` : ''}`, title: '' };
    if (last.status === 'busy') return { tone: 'warn', text: `Balance check ${BUSY_TEXT}`, title: last.message || '' };
    return { tone: 'bad', text: `Balance check failed: ${last.message || 'no message'}`, title: '' };
  }

  async function checkTripo() {
    const before = checkKey(data?.credits?.check);
    memo.checkNote = null;
    await run('check_tripo', 'Check balance', async () => {
      const result = await api.queuePost({ op: 'check_tripo' });
      if (result?.status === 'running' || (result?.ok && result?.pending)) {
        memo.check = { before, sent: Date.now(), seen: false };
        toast('Balance check started - checking...', { ms: 3000 });
        return;
      }
      const shown = result?.status === 'ok' ? checkResult({ status: 'ok', credits: result.credits, checked_at: result.checked_at })
        : result?.status === 'busy' ? { tone: 'warn', text: `Balance check not run - ${BUSY_TEXT}`, title: result.message || '' }
          : { tone: 'bad', text: `Balance check failed: ${result?.message || result?.status || 'no answer'}`, title: '' };
      memo.checkNote = { ...shown, key: before };
      toast(shown.text, { ms: 5000 });
    });
  }

  /** Follow a started check through `credits.check`: it ends when it stops running and a new result (or time) shows up. */
  function reconcileCheck(d) {
    const c = d?.credits?.check;
    if (!memo.check || !c) return;
    if (c.running) memo.check.seen = true;
    if (c.running) return;
    const age = Date.now() - memo.check.sent;
    const changed = checkKey(c) !== memo.check.before;
    if (changed || memo.check.seen) {
      const shown = checkResult(c.last);
      memo.check = null;
      if (shown) toast(shown.text, { ms: 5000 });
    } else if (age > LOST_MS) {
      memo.check = null;
      toast('Balance check: no result was recorded', { ms: 4000 });
    }
  }

  const creditsSection = makeSection('credits', 'Credits and disk', {
    hidden: d => !d?.credits && d?.disk == null,
    sig: d => ({ credits: d.credits, disk: d.disk, check: !!memo.check, note: memo.checkNote }),
    build(d) {
      const tripo = d.credits?.tripo;
      const hyper = d.credits?.hyper3d;
      const disk = typeof d.disk === 'object' && d.disk ? (d.disk.free ?? d.disk.free_bytes) : d.disk;
      const total = typeof d.disk === 'object' && d.disk ? (d.disk.total ?? d.disk.total_bytes) : null;
      const diskTone = disk == null ? '' : disk < DISK_ERROR ? 'error' : disk < DISK_WARN ? 'warn' : 'ok';
      const check = d.credits?.check;
      const checking = busy.has('check_tripo') || !!memo.check || !!check?.running;
      const nodes = [];
      if (tripo) {
        nodes.push(h('div', { class: 'q-line', dataset: { row: 'tripo' } },
          h('b', { text: 'Tripo' }),
          h('span', { text: ` ${tripo.text || tripo.mode || ''}` }),
          tripo.credits != null ? badge(`${tripo.credits} credits${tripo.checked_at ? ` (checked ${ago(tripo.checked_at)})` : ''}`, tripo.ok === false ? 'warn' : 'info') : h('span', { class: 'q-dim', text: ' · balance never checked' }),
          tripo.ok === false && tripo.error ? h('span', { class: 'q-bad', text: ` last check failed: ${tripo.error}` }) : null,
          keyedButton('credits:check', checking ? 'Checking...' : 'Check balance', checkTripo, '',
            'Runs studio.mjs balance (only while no conversion uses the Tripo browser profile)', checking)));
      }
      if (checking) {
        const n = note(`Checking the Tripo balance...${check?.started ? ` (started ${ago(check.started)})` : ''} it can take a few minutes`, 'warn');
        n.dataset.row = 'check';
        n.dataset.state = 'running';
        nodes.push(n);
      } else {
        const shown = memo.checkNote && memo.checkNote.key === checkKey(check) ? memo.checkNote : checkResult(check?.last);
        if (shown) {
          const n = note(shown.text, shown.tone);
          n.dataset.row = 'check';
          n.dataset.state = shown === memo.checkNote ? 'refused' : check.last.status;
          if (shown.title) n.title = shown.title;
          nodes.push(n);
        }
      }
      if (hyper) {
        nodes.push(h('div', { class: 'q-line', dataset: { row: 'hyper3d' } },
          h('b', { text: 'Hyper3D' }),
          hyper.credits != null ? h('span', { text: ` last recorded balance ${hyper.credits} credits (${hyper.at ? `${ago(hyper.at)}, ` : ''}${hyper.stage || 'a submission'})` })
            : h('span', { class: 'q-dim', text: ' no balance recorded in any submission yet' })));
      }
      if (disk != null) {
        nodes.push(h('div', { class: 'q-line', dataset: { row: 'disk' } },
          h('b', { text: 'Disk free' }), badge(`${bytesText(disk)}${total ? ` of ${bytesText(total)}` : ''}`, diskTone),
          diskTone === 'error' ? h('span', { class: 'q-bad', text: ' almost full: new files may fail' }) : diskTone === 'warn' ? h('span', { class: 'q-dim', text: ' getting low' }) : null));
      }
      return { nodes, extra: disk != null ? badge(bytesText(disk), diskTone) : null };
    },
  });

  // ---- views ------------------------------------------------------------------------------------------------------------
  async function viewRetry(name) {
    await run(`view:${name}`, 'Retry', async () => {
      await api.queuePost({ op: 'view_retry', name });
      toast(`View task ${name} will run again`, { ms: 3000 });
    });
  }

  const viewsSection = makeSection('views', 'Views', {
    hidden: d => !d?.views,
    sig: d => ({ counts: d.views.counts, running: asList(d.views.running).map(r => [r.name, r.job]), failed: asList(d.views.failed) }),
    build(d) {
      const v = d.views;
      const counts = Object.entries(asMap(v.counts));
      const running = asList(v.running);
      const failed = asList(v.failed);
      const elapsed = [];
      const nodes = [
        h('div', { class: 'q-line', dataset: { row: 'view-counts' } }, ...(counts.length ? counts.map(([s, n]) => badge(`${s} ${n}`, VIEW_TONE[s] || '')) : [h('span', { class: 'q-dim', text: 'No view tasks' })])),
        ...running.map(r => {
          const t = h('span', { class: 'q-time' });
          elapsed.push([r.name, t]);
          return h('div', { class: 'q-item', dataset: { view: r.name, state: 'running' } }, h('span', { class: 'q-what', text: r.name }), h('span', { class: 'q-dim', text: r.job || '' }), t);
        }),
        ...failed.map(f => {
          const tail = linesOf(f.log_tail);
          const busyNow = busy.has(`view:${f.name}`);
          return h('div', { class: 'q-item failed', dataset: { view: f.name, state: 'failed' } },
            h('span', { class: 'q-what', text: f.name }), badge(`failed${f.attempts != null ? ` after ${f.attempts} attempts` : ''}`, 'error'),
            h('span', { class: 'queue-actions' }, keyedButton(`view:${f.name}:retry`, busyNow ? 'Retrying...' : 'Retry', () => viewRetry(f.name), 'primary',
              'Set the task back to pending with 0 attempts; the runner picks it up', busyNow)),
            tail ? h('details', { class: 'q-log', dataset: { fk: `view:${f.name}:log` } }, h('summary', { text: 'log tail' }), h('pre', { text: tail })) : null);
        }),
      ];
      return {
        nodes,
        extra: failed.length ? badge(`${failed.length} failed`, 'error') : running.length ? badge(`${running.length} running`, 'info') : null,
        live(next) {
          const byName = Object.fromEntries(asList(next.views?.running).map(r => [r.name, r]));
          for (const [name, node] of elapsed) node.textContent = byName[name]?.elapsed != null ? duration(byName[name].elapsed) : '';
        },
      };
    },
  });

  // ---- conversions -------------------------------------------------------------------------------------------------------
  async function resetFails(job) {
    await run(`reset:${job}`, 'Reset', async () => {
      await api.queuePost({ op: 'convert_reset', job });
      toast(`Failure count of ${job} cleared`, { ms: 3000 });
    });
  }

  async function release(set) {
    await run(`release:${set}`, 'Release', async () => {
      const result = await api.queuePost({ op: 'release', set });
      const excluded = Object.keys(asMap(result?.excluded)).length;
      const lines = asList(result?.added).length;
      toast(excluded ? `${set} is added but still excluded by a ! line in convert_sets.txt`
        : lines ? `Released ${set}: ${lines} line${lines === 1 ? '' : 's'} added to the convert list` : `${set} was already in the convert list`, { ms: 5000 });
    });
  }

  const conversionsSection = makeSection('conversions', 'Conversions', {
    hidden: d => !d?.conversions,
    sig: d => ({ current: d.conversions.current, fails: d.conversions.fails, held: d.conversions.held, events: d.conversions.events_tail }),
    build(d) {
      const c = d.conversions;
      const current = asList(c.current);
      const fails = asList(c.fails);
      const held = asList(c.held);
      const events = linesOf(c.events_tail);
      const nodes = [
        current.length ? h('div', { class: 'q-list', dataset: { row: 'current' } }, ...current.map(j => h('div', { class: 'q-item', dataset: { job: j.job } },
          badge('converting', 'info'), h('span', { class: 'q-what', text: `${j.job}${j.module ? ` · ${pretty(j.module)}` : ''}` }),
          j.holder_pid ? h('span', { class: 'q-dim', text: `pid ${j.holder_pid}` }) : null)))
          : h('div', { class: 'q-line', dataset: { row: 'current' } }, h('span', { class: 'q-dim', text: 'No conversion is running' })),
        ...fails.map(f => {
          const working = busy.has(`reset:${f.job}`);
          return h('div', { class: 'q-item failed', dataset: { job: f.job, kind: 'fails' } },
            badge(`${f.count} failure${f.count === 1 ? '' : 's'}`, 'error'), h('span', { class: 'q-what', text: f.job }),
            h('span', { class: 'queue-actions' }, keyedButton(`reset:${f.job}`, working ? 'Resetting...' : 'Reset', () => resetFails(f.job), '',
              'Delete the .fails file so the worker tries this job again', working)));
        }),
        ...held.map(j => {
          const working = busy.has(`release:${j.set}`);
          const excluded = j.reason === 'excluded';
          // `owner`: the job is listed, but the worker's owner check refuses it (its record names another parent set). Release cannot
          // fix an ownership conflict, so there is no button: the server's `detail` says what the record names.
          const owner = j.reason === 'owner';
          return h('div', { class: 'q-item held', dataset: { job: j.job, kind: 'held', reason: j.reason } },
            badge(excluded ? 'held: excluded' : owner ? 'held: owner not verified' : 'held: not in convert list', owner ? 'error' : 'warn'),
            h('span', { class: 'q-what', text: j.job }),
            h('span', { class: 'q-dim', text: `${j.stage}${j.modules_left != null ? ` · ${j.modules_left} module${j.modules_left === 1 ? '' : 's'} left` : ''}` }),
            owner
              ? h('span', { class: 'q-dim', dataset: { detail: 'owner' }, text: `${j.detail || 'the job record does not name this set as its owner'} (Release does not help: fix the job's owner by hand)` })
              : excluded
              ? h('span', { class: 'q-dim', text: `excluded by ${asList(j.exclude).join(', ') || 'a ! line'}: remove that line in convert_sets.txt` })
              : h('span', { class: 'queue-actions' }, keyedButton(`release:${j.set}`, working ? 'Releasing...' : 'Release', () => release(j.set), 'primary',
                `Add ${j.set} to the convert list`, working)));
        }),
        events ? h('details', { class: 'q-log', open: true, dataset: { fk: 'conv:events' } }, h('summary', { text: 'convert_events.log' }), h('pre', { text: events })) : null,
      ];
      const parts = [fails.length ? badge(`${fails.length} failing`, 'error') : null, held.length ? badge(`${held.length} held`, 'warn') : null,
        current.length ? badge(`${current.length} running`, 'info') : null].filter(Boolean);
      return { nodes, extra: parts };
    },
  });

  // ---- holds -------------------------------------------------------------------------------------------------------------
  /** The reviewed images of a colour-drift hold: [{path, sha256}] from the hold record (`images`); accept_drift is bound to these hashes. */
  const driftImages = hold => asList(hold.images ?? hold.drift_images)
    .filter(i => i && typeof i.path === 'string' && typeof i.sha256 === 'string' && i.sha256).map(i => ({ path: i.path, sha256: i.sha256 }));

  async function acceptDrift(hold) {
    const images = driftImages(hold);
    if (!images.length) return;
    const list = h('ul', { class: 'q-drift-images' }, ...images.map(i => h('li', null, h('span', { text: i.path }), h('span', { class: 'q-dim', text: ` sha256 ${i.sha256.slice(0, 12)}...` }))));
    const ok = await confirmDialog({
      title: 'Accept this colour drift?', confirm: 'Accept drift', danger: true, content: list,
      lines: [...asList(hold.reasons).map(text),
        `Accepting tells the supervisor these ${images.length} image${images.length === 1 ? '' : 's'} may go on with their current colours. The decision is bound to the files' hashes: an image that changes later needs a new decision.`],
    });
    if (!ok) return;
    await run(`drift:${hold.key}`, 'Accept drift', async () => {
      const result = await api.queuePost({ op: 'accept_drift', key: hold.key, images });
      const n = asList(result?.accepted).length || images.length;
      toast(`Accepted the colour drift of ${n} image${n === 1 ? '' : 's'} (${hold.key})`, { ms: 5000 });
    });
  }

  const holdsSection = makeSection('holds', 'Holds', {
    hidden: d => !Array.isArray(d?.holds),
    sig: d => d.holds,
    build(d) {
      const holds = d.holds;
      const nodes = holds.length ? holds.map(hold => {
        const set = setOf(hold.set);
        let accept = null;
        if (hold.drift) {
          const images = driftImages(hold);
          const working = busy.has(`drift:${hold.key}`);
          accept = h('span', { class: 'queue-actions' },
            keyedButton(`drift:${hold.key}`, working ? 'Accepting...' : 'Accept', () => acceptDrift(hold), 'primary',
              images.length ? 'Accept the colour drift of the listed images (a confirmation lists them)' : 'no image hashes in this hold', working || !images.length),
            images.length ? null : h('span', { class: 'q-dim', text: 'no image hashes in this hold' }));
        }
        return h('div', { class: 'q-item held', dataset: { hold: hold.key || set, drift: hold.drift ? 'true' : 'false' } },
          set ? h('a', { class: 'q-set', href: `#approved/${encodeURIComponent(`set:${set}`)}`, text: pretty(set), title: 'Open this set in the Approved tab' }) : h('span', { class: 'q-what', text: pretty(hold.key) }),
          h('div', { class: 'q-reasons' }, ...asList(hold.reasons).map(r => h('div', { text: text(r) }))),
          hold.key && set ? h('span', { class: 'q-dim', text: hold.key }) : null,
          accept);
      }) : [h('div', { class: 'q-line' }, h('span', { class: 'q-dim', text: 'The supervisor holds nothing.' }))];
      return { nodes, extra: holds.length ? badge(`${holds.length}`, 'warn') : null };
    },
  });

  // ---- settings ----------------------------------------------------------------------------------------------------------
  const settingsSection = makeSettings();

  function makeSettings() {
    const NUMBERS = [
      ['view_parallel', 'Parallel view jobs', '1 to 16 Codex image jobs at once', 1, 16],
      ['layer_workers_present', 'Layer builds at once, me at the PC', '0 to 16 (0 = none while I am here)', 0, 16],
      ['layer_workers_away', 'Layer builds at once, me away', '0 to 16', 0, 16],
      ['build_stall_minutes', 'Stalled build after (minutes)', 'minutes without log output, 1 to 1440', 1, 1440],
    ];
    const SERVICES = [['supervisor', 'supervisor'], ['views', 'view runner'], ['convert', 'convert worker']];
    const fields = {};          // key -> {kind, input, error}
    const touched = new Set();
    let server = null;
    const formError = h('div', { class: 'q-form-error', role: 'alert', hidden: true });
    const saved = h('span', { class: 'q-dim q-saved' });
    const save = button('Save', null, 'primary', 'Save the changed settings');
    const revert = button('Revert', null, '', 'Forget the edits');
    save.type = 'submit';

    const errorFor = key => fields[key].error;
    function add(key, label, control, hint, kind) {
      const error = h('div', { class: 'q-field-error', role: 'alert', hidden: true });
      fields[key] = { kind, input: control, error };
      control.dataset.setting = key;
      control.id = `set-${key.replace('.', '-')}`;
      const markDirty = () => { touched.add(key); error.hidden = true; saved.textContent = ''; changed(); };
      control.addEventListener('input', markDirty);
      control.addEventListener('change', markDirty);
      return h('div', { class: 'q-field' }, h('label', { htmlFor: control.id, text: label }), control, hint ? h('span', { class: 'q-dim', text: hint }) : null, error);
    }
    const rows = [
      ...NUMBERS.map(([key, label, hint, min, max]) => add(key, label, h('input', { type: 'number', min, max, step: 1, inputMode: 'numeric' }), hint, 'num')),
      add('tripo', 'Backend', h('select', null,
        h('option', { value: 'off', text: 'off (Hyper3D only, no Tripo credits)' }),
        h('option', { value: 'auto', text: 'auto (Tripo first, Hyper3D when it runs out)' }),
        h('option', { value: 'pixal3d', text: 'pixal3d (Pixal3D only: free, local GPU, ~4.5 min/module)' }),
        h('option', { value: 'auto_pixal3d', text: 'auto_pixal3d (Tripo first, Pixal3D when it runs out)' })),
        'off = --no-tripo, pixal3d = --backend pixal3d, auto_pixal3d = --fallback pixal3d. A change restarts the convert worker after its running job', 'select'),
    ];
    // allowed_hosts: a list editor (one text input per host, Add / remove); the control element carries its value through `.listValue` / `.setList`
    const hostList = h('div', { class: 'q-list-edit', role: 'group', 'aria-label': 'Allowed hosts' });
    const hostRows = h('div', { class: 'q-list-rows' });
    const hostItem = (value = '') => {
      const input = h('input', { type: 'text', value, placeholder: 'host name or address, e.g. my-pc.local', autocomplete: 'off', spellcheck: false });
      input.setAttribute('aria-label', 'Allowed host');
      const row = h('div', { class: 'q-list-row' }, input,
        button('Remove', () => { row.remove(); hostList.dispatchEvent(new Event('input', { bubbles: true })); }, '', 'Remove this host'));
      return row;
    };
    hostList.listValue = () => [...hostRows.querySelectorAll('input')].map(i => i.value.trim()).filter(Boolean);
    hostList.setList = values => { hostRows.replaceChildren(...asList(values).map(v => hostItem(String(v)))); };
    hostList.append(hostRows, button('Add host', () => { const row = hostItem(); hostRows.append(row); row.querySelector('input').focus(); hostList.dispatchEvent(new Event('input', { bubbles: true })); }, '', 'Add a host name'));
    rows.push(add('allowed_hosts', 'Allowed hosts', hostList, 'Host names the server answers to besides localhost. A new host works at once; removing one needs a server restart.', 'list'));
    const checks = SERVICES.map(([name, label]) => {
      const input = h('input', { type: 'checkbox' });
      const row = add(`services.${name}`, '', input, '', 'check');
      row.querySelector('label').replaceWith(h('label', { htmlFor: input.id, class: 'check' }, input, `Keep the ${label} running`));
      input.dataset.setting = `services.${name}`;
      return row;
    });
    const form = h('form', { class: 'q-settings', noValidate: true, onsubmit: event => { event.preventDefault(); saveNow(); } },
      h('div', { class: 'q-fields' }, ...rows), h('div', { class: 'q-fields' }, h('div', { class: 'q-dim', text: 'Services that should be running (the app starts and restarts them):' }), ...checks),
      formError, h('div', { class: 'q-form-actions' }, save, revert, saved));

    const serverValue = key => (key.startsWith('services.') ? server?.services?.[key.slice(9)] : server?.[key]);
    const inputValue = key => {
      const f = fields[key];
      return f.kind === 'check' ? f.input.checked : f.kind === 'num' ? f.input.value.trim() : f.kind === 'list' ? f.input.listValue() : f.input.value;
    };
    const differs = key => (fields[key].kind === 'num' ? inputValue(key) !== String(serverValue(key) ?? '')
      : fields[key].kind === 'list' ? JSON.stringify(inputValue(key)) !== JSON.stringify(asList(serverValue(key)).map(String))
        : inputValue(key) !== serverValue(key));
    function show(key) {
      const f = fields[key];
      const value = serverValue(key);
      if (f.kind === 'check') f.input.checked = !!value;
      else if (f.kind === 'list') { if (differs(key)) f.input.setList(value); }
      else f.input.value = value == null ? '' : String(value);
    }
    function changed() {
      for (const key of [...touched]) if (!differs(key)) touched.delete(key);
      save.disabled = !touched.size || busy.has('settings');
      revert.disabled = !touched.size;
    }
    revert.onclick = () => {
      for (const key of touched) { show(key); fields[key].error.hidden = true; }
      touched.clear();
      formError.hidden = true;
      changed();
    };
    function problem(key, message) {
      const f = fields[key];
      if (f) { f.error.textContent = message; f.error.hidden = false; return true; }
      return false;
    }

    async function saveNow() {
      if (!touched.size || busy.has('settings')) return;
      formError.hidden = true;
      for (const f of Object.values(fields)) f.error.hidden = true;
      const patch = {};
      let bad = false;
      for (const key of touched) {
        if (key.startsWith('services.')) { (patch.services ||= {})[key.slice(9)] = inputValue(key); continue; }
        if (fields[key].kind === 'num') {
          const value = inputValue(key);
          if (!/^-?\d+$/.test(value)) { problem(key, 'must be a whole number'); bad = true; continue; }
          patch[key] = Number(value);
        } else {
          patch[key] = inputValue(key);
        }
      }
      if (bad) return;
      busy.add('settings');
      save.disabled = true;
      let ok = false;
      try {
        const result = await api.queuePost({ op: 'settings', patch });
        ok = true;
        const next = result?.settings && typeof result.settings === 'object' ? result.settings : (result && 'view_parallel' in result ? result : null);
        if (next) server = next;
        touched.clear();
        for (const key of Object.keys(fields)) show(key);
        saved.textContent = 'Saved';
        toast(result?.convert_restart ? `Settings saved; ${result.convert_restart.message}` : 'Settings saved', { ms: result?.convert_restart ? 6000 : 2500 });
      } catch (error) {
        const problems = asList(error.body?.problems).length ? asList(error.body.problems).map(text) : String(error.message).split(/;\s*/);
        const loose = [];
        for (const message of problems) {
          const match = /^(services\.[a-z_]+|[a-z_]+)\b/.exec(message);
          if (!(match && problem(match[1], message))) loose.push(message);
        }
        if (loose.length) { formError.replaceChildren(...loose.map(m => h('div', { text: m }))); formError.hidden = false; }
        if (error.status !== 400) toast(`Settings not saved: ${error.message}`);
      } finally {
        busy.delete('settings');
        changed();
        if (ok) reload();
      }
    }

    const node = h('details', { class: 'q-sec', dataset: { sec: 'settings' }, open: !!storage.get('queueOpen', {}).settings,
      ontoggle: event => { if (event.target === node) storage.set('queueOpen', { ...storage.get('queueOpen', {}), settings: node.open }); } },
    h('summary', null, h('h3', { text: 'Settings' })), h('div', { class: 'q-body' }, form));
    changed();
    return {
      node,
      update(d) {
        node.hidden = !d?.settings;
        if (!d?.settings) return;
        server = d.settings;
        for (const key of Object.keys(fields)) if (!touched.has(key)) show(key);
        changed();
      },
    };
  }

  list.push(modeSection, servicesSection, creditsSection, viewsSection, conversionsSection, holdsSection, settingsSection);
  const node = h('div', { class: 'q-sections' }, ...list.map(s => s.node));
  return {
    node,
    /** Feed the latest GET /api/v2/queue response. */
    update(next) {
      data = next;
      if (servicesMode(next) === 'ok' && next.services.managed === true) memo.serviceOff = null;     // management is on after all
      reconcileRequests(next);
      reconcileCheck(next);
      noteServices(servicesMode(next) === 'absent' ? null : { managed: next.services.managed !== false, scanned: servicesMode(next) !== 'scanning' });
      redraw();
    },
    /** The Queue tab is closed: the header chip goes back to what the state says. */
    dispose() { noteServices(null); },
  };
}
