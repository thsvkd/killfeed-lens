'use strict';
// Shared by the board and the overlay: glyph sprite, labels, the engine clock, change highlights.
// Shape always carries the meaning, colour only repeats it:
// alive = filled circle, estimated (knock timed out without a death row) = half-filled circle,
// assumed (squad slot never seen) = dashed ring, knocked = diamond,
// dead = red cross, unsure (never-seen slot that may not be alive: its team may be wiped, next to
// the words "전멸 가능", or the HUD count leaves room for fewer than half of them) = question
// mark. A comeback is a plain alive glyph, only lit for a moment.
// Colour on events: attacker side blue, victim side red; team badges wear the game's team colour.
const KF = (() => {
  const HOLD = 4000; // ms a change stays highlighted (Tk FLASH_S)
  const FADE = 900; // ms the highlight fades out, ending at HOLD
  // Fixed per team NUMBER in the game (medians of real badge crops); 26-50 are darker than 1-25.
  // 11, 12, 14 and 23 are 3-13% darker than measured so the white digits keep 4.5:1.
  const TEAM_COLORS = [null,
    '#203E98', '#037A73', '#962709', '#045073', '#52248A', '#742B1E', '#364A0B', '#92274D', '#745227', '#5E113C',
    '#88751F', '#43807D', '#566369', '#A06C17', '#385261', '#AB5F79', '#0A534E', '#643B11', '#5A771F', '#664672',
    '#3A7C60', '#62798F', '#946D5B', '#3A3572', '#6A6615', '#1E3463', '#025957', '#6D2207', '#044455', '#47225F',
    '#63291C', '#2E390A', '#5F1F37', '#4C371F', '#4D0E31', '#665B21', '#365A59', '#3B4448', '#745C17', '#293B42',
    '#414F62', '#0A403E', '#442E0D', '#44561C', '#503A5B', '#366153', '#505F6E', '#725448', '#343055', '#565412'];
  const VERB = { kill: '처치', knock: '기절', finish: '마무리', bleedout: '출혈사',
                 suicide: '자살', environment: '환경사', unknown: '?' };
  const STATUS = { alive: '생존', estimated: '생존 추정', knocked: '기절', dead: '사망', assumed: '미확인', unsure: '생존 불명' };

  const SPRITE = `<svg class="sprite" width="0" height="0" aria-hidden="true">
    <symbol id="g-alive" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6.2" fill="currentColor"/></symbol>
    <symbol id="g-estimated" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.7"/><path d="M8 2.4a5.6 5.6 0 0 0 0 11.2Z" fill="currentColor"/></symbol>
    <symbol id="g-assumed" viewBox="0 0 16 16"><circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-dasharray="2.6 2.1"/></symbol>
    <symbol id="g-unsure" viewBox="0 0 16 16"><path d="M5.5 6.1C5.5 4.5 6.6 3.4 8.1 3.4s2.5 1 2.5 2.4c0 1.9-2.5 2.1-2.5 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="8.1" cy="12.6" r="1.15" fill="currentColor"/></symbol>
    <symbol id="g-knocked" viewBox="0 0 16 16"><path d="M8 .9 15.1 8 8 15.1.9 8Z" fill="currentColor"/></symbol>
    <symbol id="g-dead" viewBox="0 0 16 16"><path d="M3.6 3.6l8.8 8.8M12.4 3.6l-8.8 8.8" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"/></symbol>
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
  /** Class that sets --tc to the team's colour (inject() writes the rules); '' for the "??" bucket. */
  const teamClass = (id) => {
    if (id == null || id < 1) return '';
    return `tc${id > 50 ? ((id - 26) % 25) + 26 : id}`; // solo lobbies count past 50
  };
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

  /** Team badge in the game's colour, to put into markup built here. */
  const tb = (id) => `<span class="tb ${teamClass(id) || 'unknown'}">${badge(id)}</span>`;

  /** A badge element (overlay row, board card): its text and colour class. */
  function paintBadge(el, id) {
    text(el, badge(id));
    const c = teamClass(id);
    if (el._tc !== c) { if (el._tc) el.classList.remove(el._tc); if (c) el.classList.add(c); el._tc = c; }
    el.classList.toggle('unknown', id == null);
  }

  /** "12 SnowFox → 07 MadWolf" with names clipped to n, attacker blue and victim red; just the
   *  victim when nobody got credit. */
  function who(e, n) {
    const side = (role, team, name) => `<span class="${role}">${tb(team)}${esc(clip(name, n))}</span>`;
    return e.killer || e.kt != null
      ? `${side('side-atk', e.kt, e.killer)}<span class="arrow">→</span>${side('side-vic', e.vt, e.victim)}`
      : side('side-vic', e.vt, e.victim);
  }

  /** The 2 px divider gauge: players left out of the starting field; empty while that is unknown. */
  function gauge(el, snap) {
    const total = snap.start_teams ? snap.start_teams * snap.squad : 0;
    el.style.transform = `scaleX(${total ? Math.min(1, snap.players_left / total) : 0})`;
  }

  /** Continuous fade only during the final FADE ms. A delayed animation holds without repainting;
   *  on completion it is removed. Rebuilt nodes start at the event's current age. */
  function heat(el, at) {
    if (el._heatAt === at) return;
    el._heatAt = at;
    el._heat?.cancel();
    el._heat = null;
    const left = at ? at + HOLD - now() : 0;
    el.classList.toggle('hot', left > 0);
    if (left <= 0) return;
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const animation = el.animate([{ opacity: 1 }, { opacity: 0 }], {
      pseudoElement: '::before', duration: reduce ? 0 : FADE,
      delay: left - (reduce ? 0 : FADE), easing: 'linear', fill: 'both',
    });
    animation.startTime = document.timeline.currentTime;
    el._heat = animation;
    animation.onfinish = () => {
      el.classList.remove('hot');
      animation.cancel();
      el._heat = null;
    };
  }

  /** Colour of a team's highlight: blue when its latest change was dealing a kill or knock, red when
   *  it lost a member, yellow for a comeback or anything else (`at` newer than both) and whenever
   *  the engine sends no atk/hit. */
  function tone(el, team) {
    const a = team.atk ?? 0;
    const h = team.hit ?? 0;
    const t = team.at > Math.max(a, h) ? '' : a > h ? 'atk' : h > a ? 'hit' : ''; // one clock read per event
    el.classList.toggle('atk', t === 'atk');
    el.classList.toggle('hit', t === 'hit');
  }

  /** How a team's never-seen squad slots are drawn: assumed | unsure. Older engines only know
   *  maybe_out. */
  const unseen = (team) => team.unseen ?? (team.maybe_out ? 'unsure' : 'assumed');

  /** One entry per squad slot. Of the slots never seen in the feed, `team.assumed` (as many as the
   *  HUD count leaves room for, Ledger.draw) are drawn assumed alive and the rest unsure; older
   *  engines send only `team.unseen` for all of them. Players come as drawn: alive, knocked, dead,
   *  or unsure (the HUD count has no room for them). */
  function slots(team) {
    const seen = team.players.map((p) => ({ ...p, kind: p.st === 'alive' && p.sure === false ? 'estimated' : p.st }));
    const pad = Math.max(0, team.slots - seen.length);
    const assumed = team.assumed ?? (unseen(team) === 'assumed' ? pad : 0);
    return seen.concat(Array.from({ length: pad }, (_, i) => (
      { name: null, st: 'alive', kind: i < assumed ? 'assumed' : 'unsure', at: 0, back: 0 })));
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

  function inject() {
    document.body.insertAdjacentHTML('afterbegin', SPRITE);
    // a constructed sheet, not a <style> or style="": the CSP allows no inline styles
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(TEAM_COLORS.map((c, n) => (n ? `.tc${n}{--tc:${c}}` : '')).join(''));
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  }

  return { HOLD, VERB, STATUS, glyph, sync, now, esc, clip, ago, text, attr, html, who, gauge,
           paintBadge, heat, tone, slots, teamsLeft, playersLeft, count, eventKind, eventGlyph, playersBasis,
           teamSummary, inject };
})();
