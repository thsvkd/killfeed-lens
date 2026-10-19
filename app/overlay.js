'use strict';
// Overlay renderer: rewrites only what changed; no animation runs unless something just changed.
(() => {
  const ROWS = 12;
  const $ = (id) => document.getElementById(id);
  KF.inject();
  const list = $('rows');
  const rows = new Map(); // team key -> <li>
  let snap = null;

  function rowFor(key) {
    let li = rows.get(key);
    if (!li) {
      li = document.createElement('li');
      li.className = 'row hot-able';
      li.innerHTML = `<span class="badge num"></span><span class="pips"></span>
        <span class="kills">${KF.glyph('kill')}<b class="num"></b></span>`;
      rows.set(key, li);
    }
    return li;
  }

  function fillRow(li, team) {
    KF.paintBadge(li.firstElementChild, team.id);
    KF.tone(li, team);
    KF.text(li.querySelector('.kills b'), team.kills);
    KF.attr(li, 'aria-label', KF.teamSummary(team));
    if (li.dataset.at !== String(team.at)) {
      li.dataset.at = team.at;
      KF.heat(li, team.at);
    }
    const slots = KF.slots(team);
    const sig = slots.map((s) => `${s.kind}${s.at}/${s.back}`).join();
    if (li.dataset.sig === sig) return;
    li.dataset.sig = sig;
    const pips = li.querySelector('.pips');
    // a pip lit for a knock or kill goes red, for anything else (first seen, comeback) yellow
    pips.innerHTML = slots.map((s) => `<span class="pip hot-able${s.st === 'alive' ? '' : ' loss'}">${KF.glyph(s.kind)}</span>`).join('')
      + (team.maybe_out // words, not just a fainter ring; quieter on one seen member (weak, early)
        ? `<span class="maybe-tag${team.players.length === 1 ? ' weak' : ''}">전멸 가능</span>` : '');
    slots.forEach((s, i) => KF.heat(pips.children[i], Math.max(s.at, s.back)));
  }

  /** Reorder keyed rows in place. No slide animation: at 144 Hz with software compositing every
   *  animated frame costs CPU, and the moved row is already lit by its change highlight. */
  function order(keys) {
    for (const [k, li] of rows) if (!keys.includes(k)) { li.remove(); rows.delete(k); }
    keys.forEach((k, i) => {
      const li = rows.get(k);
      if (list.children[i] !== li) list.insertBefore(li, list.children[i] ?? null);
    });
  }

  function render() {
    if (!snap) return;
    const t0 = KF.now();
    const live = snap.teams.filter((t) => !t.out); // engine sends teams ranked (Ledger.ranked)
    const hot = live.slice(ROWS).filter((t) => t.at && t0 - t.at < KF.HOLD).slice(0, 3); // never hide a change
    const shown = live.slice(0, ROWS - hot.length).concat(hot);

    KF.count($('players'), KF.playersLeft(snap));
    KF.count($('teams'), KF.teamsLeft(snap));
    KF.gauge($('gauge'), snap);

    const flag = $('flag');
    const off = !snap.tracking;
    flag.hidden = !(off || snap.error);
    flag.className = `flag${snap.error ? ' err' : ''}`;
    KF.html(flag, snap.error ? `${KF.glyph('warn')}캡처 오류` : off ? `${KF.glyph('pause')}추적 꺼짐` : '');

    const keys = shown.map((t) => String(t.id));
    shown.forEach((t) => fillRow(rowFor(String(t.id)), t));
    order(keys);
    if (!shown.length && !list.querySelector('.empty')) list.innerHTML = '<li class="empty">대기 중…</li>';
    else if (shown.length) list.querySelector('.empty')?.remove();

    const more = $('more');
    more.hidden = live.length <= shown.length;
    KF.text(more, `+ ${live.length - shown.length}팀 더`);

    renderLast();
    tick();
  }

  function renderLast() {
    const e = snap.events[0];
    const last = $('last');
    last.hidden = !e;
    if (!e) return;
    const sig = `${snap.last_at}|${e.clock}|${e.killer}|${e.victim}|${e.kind}`;
    if (last.dataset.sig === sig) return;
    last.dataset.sig = sig;
    $('last-icon').innerHTML = KF.eventGlyph(e.kind);
    $('last-who').innerHTML = KF.who(e, 10);
    $('last-verb').className = `k-${KF.eventKind(e.kind)}`;
    KF.text($('last-verb'), KF.VERB[e.kind] ?? e.kind);
    KF.heat(last, snap.last_at);
  }

  /** Once a second: only the coarse clock labels; DOM is touched only when their text changes. */
  function tick() {
    if (!snap || document.hidden) return;
    if (snap.events[0]) KF.text($('last-ago'), KF.ago(snap.last_at));
    const basis = KF.playersBasis(snap);
    KF.text($('players-label'), basis === 'estimate' ? '남은 인원 · 추정' : basis === 'stale' ? '인원 · 마지막 관측' : '남은 인원 · 화면');
  }
  setInterval(tick, 1000);

  new ResizeObserver(() => {
    const r = document.body.getBoundingClientRect();
    kfl.send('overlay:size', { w: Math.ceil(r.width), h: Math.ceil(r.height) });
  }).observe(document.body, { box: 'border-box' });

  kfl.on('snapshot', (s) => { snap = s; KF.sync(s); render(); });
  kfl.on('overlay:editing', (on) => document.body.classList.toggle('editing', on));
  kfl.send('ui:ready');
})();
