'use strict';
// Shared by the board and the overlay: glyph sprite, labels, the engine clock, change highlights.
// Shape always carries the meaning, colour only repeats it (same rules as the Tk app):
// alive = filled circle, assumed (squad slot never seen) = dashed ring, knocked = diamond,
// dead = cross, comeback = up-triangle, unsure (never-seen slot of a team that may be wiped) =
// thin solid ring, always next to the words "전멸 가능".
const KF = (() => {
  const HOLD = 4000; // ms a change stays highlighted (Tk FLASH_S)
  const COOL = [600, 300]; // the last 600 ms at half strength, the last 300 ms fainter still
  const VERB = { kill: '처치', knock: '기절', finish: '마무리', bleedout: '출혈사',
                 suicide: '자살', environment: '환경사', unknown: '?' };
  const STATUS = { alive: '생존', knocked: '기절', dead: '사망', assumed: '미확인', unsure: '생존 불명' };

  const SPRITE = `<svg class="sprite" width="0" height="0" aria-hidden="true">
    <symbol id="g-alive" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.2" fill="currentColor"/></symbol>
    <symbol id="g-assumed" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-dasharray="2.6 2.1"/></symbol>
    <symbol id="g-unsure" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.3"/></symbol>
    <symbol id="g-knocked" viewBox="0 0 16 16"><path d="M8 .9 15.1 8 8 15.1.9 8Z" fill="currentColor"/></symbol>
    <symbol id="g-dead" viewBox="0 0 16 16"><path d="M3.6 3.6l8.8 8.8M12.4 3.6l-8.8 8.8" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"/></symbol>
    <symbol id="g-back" viewBox="0 0 16 16"><path d="M8 2.5 14 12.5H2Z" fill="currentColor"/></symbol>
    <symbol id="g-other" viewBox="0 0 16 16"><rect x="3.4" y="3.4" width="9.2" height="9.2" rx="1" fill="none" stroke="currentColor" stroke-width="2"/></symbol>
    <symbol id="g-kill" viewBox="0 0 16 16"><g fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="8" cy="8" r="4.4"/><path d="M8 .8v3.4M8 11.8v3.4M.8 8h3.4M11.8 8h3.4"/></g></symbol>
    <symbol id="g-live" viewBox="0 0 16 16"><circle cx="8" cy="8" r="4.5" fill="currentColor"/><circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-opacity=".45" stroke-width="1.5"/></symbol>
    <symbol id="g-pause" viewBox="0 0 16 16"><path d="M5 3v10M11 3v10" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/></symbol>
    <symbol id="g-warn" viewBox="0 0 16 16"><path d="M8 1.5 15 14H1Z" fill="currentColor"/><path d="M8 6v3.6M8 11.6v.4" stroke="#0A0D12" stroke-width="1.8" stroke-linecap="round"/></symbol>
  </svg>`;

  const glyph = (kind, cls = '') =>
    `<svg class="g g-${kind} ${cls}" aria-hidden="true" focusable="false"><use href="#g-${kind}"/></svg>`;

  // Engine timestamps are its monotonic ms; keep our own estimate of its clock between snapshots.
  let skew = 0;
  const sync = (snap) => { skew = snap.now - performance.now(); };
  const now = () => performance.now() + skew;

  const badge = (id) => (id == null ? '??' : String(id).padStart(2, '0'));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  const clip = (s, n) => (!s ? '-' : s.length <= n ? s : `${s.slice(0, n - 1)}…`);

  /** Coarse on purpose: a ticking seconds counter in peripheral vision is itself a distraction. */
  function ago(at) {
    const s = Math.max(0, (now() - at) / 1000);
    if (s < 10) return '방금';
    if (s < 60) return `${Math.floor(s / 10) * 10}초 전`;
    return `${Math.floor(s / 60)}분 전`;
  }

  function text(el, value) {
    const v = String(value);
    if (el.textContent !== v) el.textContent = v;
  }

  function attr(el, name, value) {
    if (el.getAttribute(name) !== value) el.setAttribute(name, value);
  }

  /** innerHTML of markup built here (every name already escaped), written only when it changed. */
  function html(el, markup) {
    if (el.dataset.html !== markup) { el.dataset.html = markup; el.innerHTML = markup; }
  }

  /** "12 SnowFox → 07 MadWolf" with names clipped to n; just the victim when nobody got credit. */
  function who(e, n) {
    const side = (team, name) => `<span class="tb">${badge(team)}</span>${esc(clip(name, n))}`;
    return e.killer || e.kt != null
      ? `${side(e.kt, e.killer)}<span class="arrow">→</span>${side(e.vt, e.victim)}`
      : side(e.vt, e.victim);
  }

  /** The 2 px divider gauge: players left out of the starting field; empty while that is unknown. */
  function gauge(el, snap) {
    const total = snap.start_teams ? snap.start_teams * snap.squad : 0;
    el.style.transform = `scaleX(${total ? Math.min(1, snap.players_left / total) : 0})`;
  }

  /** Light `el` with `cls` until HOLD ms after `at`; 'hot' also dims in two steps before it goes
   *  out. Timers, not animations: 1-3 repaints per change, nothing while idle. */
  function heat(el, at, cls = 'hot') {
    el._heat ??= {};
    for (const t of el._heat[cls] ?? []) clearTimeout(t);
    const left = at ? at + HOLD - now() : 0;
    if (cls === 'hot') el.classList.remove('cool1', 'cool2');
    if (left <= 0) { el.classList.remove(cls); return; }
    el.classList.add(cls);
    const timers = [setTimeout(() => el.classList.remove(...(cls === 'hot' ? [cls, 'cool1', 'cool2'] : [cls])), left)];
    if (cls === 'hot') {
      COOL.forEach((before, i) => {
        if (left > before) timers.push(setTimeout(() => el.classList.add(`cool${i + 1}`), left - before));
      });
    }
    el._heat[cls] = timers;
  }

  /** One entry per squad slot. Slots never seen in the feed are assumed alive (Team.pips), unless
   *  everyone seen is down: then the team may already be wiped and they are unsure. */
  function slots(team) {
    const seen = team.players.map((p) => ({ ...p, kind: p.st }));
    const pad = Math.max(0, team.slots - seen.length);
    const kind = team.maybe_out ? 'unsure' : 'assumed';
    return seen.concat(Array.from({ length: pad }, () => ({ name: null, st: 'alive', kind, at: 0, back: 0 })));
  }

  /** Until a start count or a confirmed top badge sets the lobby size, the engine's counts cover
   *  only the teams seen so far ("1 team left" at the first kill): unknown, not a count. */
  const knowsLobby = (snap) => Boolean(snap.start_teams);

  /** "13", or "6~13" while underfilled teams may already be wiped (Ledger.teams_left_min). */
  function teamsLeft(snap) {
    if (!knowsLobby(snap)) return '—';
    const min = snap.teams_left_min ?? snap.teams_left;
    return min < snap.teams_left ? `${min}~${snap.teams_left}` : String(snap.teams_left);
  }

  const playersLeft = (snap) => (snap.hud || knowsLobby(snap) ? String(snap.players_left) : '—');

  /** Put a header number in place; "—" (not known yet) goes dim instead of bold white. */
  function count(el, value) {
    text(el, value);
    el.classList.toggle('none', value === '—');
  }

  const eventKind = (kind) => (kind === 'knock' ? 'knock' : kind === 'kill' || kind === 'finish' ? 'kill' : 'other');
  const EVENT_GLYPH = { knock: 'knocked', kill: 'dead', other: 'other' };
  const eventGlyph = (kind) => glyph(EVENT_GLYPH[eventKind(kind)]);

  function playersBasis(snap) {
    if (!snap.hud) return 'estimate';
    return now() - snap.hud.at >= 5000 ? 'stale' : 'hud';
  }

  function teamSummary(team) {
    const n = (st) => team.players.filter((p) => p.st === st).length;
    const left = team.out ? '탈락' : team.maybe_out ? `전멸 가능 · 남은 0~${team.left}` : `남은 ${team.left}`;
    return `${badge(team.id)}팀 · ${left} · 기절 ${n('knocked')} · 사망 ${n('dead')} · 처치 ${team.kills}`;
  }

  function inject() { document.body.insertAdjacentHTML('afterbegin', SPRITE); }

  return { HOLD, VERB, STATUS, glyph, sync, now, badge, esc, clip, ago, text, attr, html, who, gauge,
           heat, slots, teamsLeft, playersLeft, count, eventKind, eventGlyph, playersBasis,
           teamSummary, inject };
})();
