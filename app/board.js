'use strict';
// Board window renderer: keyed DOM, rewritten only where the snapshot changed.
(() => {
  const MAX_SLOTS = 6; // the unknown-badge bucket can collect many players
  const $ = (id) => document.getElementById(id);
  KF.inject();
  const grid = $('cards');
  const cards = new Map(); // team key -> <article>
  let snap = null;
  let ov = null; // overlay state from the main process
  let engine = { alive: true, reason: '' };

  // --- cards ----------------------------------------------------------------------------------
  function cardFor(key) {
    let el = cards.get(key);
    if (!el) {
      el = document.createElement('article');
      el.className = 'card hot-able';
      el.innerHTML = `<header class="card-h"><span class="badge num"></span><span class="out-tag">탈락</span>
          <span class="maybe-tag" title="본 팀원은 모두 쓰러졌고, 아직 안 보인 팀원이 없으면 이미 전멸했습니다">전멸 가능</span>
          <span class="kills" title="처치">${KF.glyph('kill')}<b class="num"></b></span></header>
        <ul class="slots"></ul>
        <footer class="card-f"><span class="sum"></span><span class="knocks"></span></footer>`;
      cards.set(key, el);
    }
    return el;
  }

  function fillCard(el, team) {
    const badge = el.querySelector('.badge');
    KF.text(badge, KF.badge(team.id));
    badge.classList.toggle('unknown', team.id == null);
    el.classList.toggle('out', team.out);
    el.classList.toggle('maybe', Boolean(team.maybe_out));
    // one seen member is weak evidence and common early on: the same words, quieter
    el.classList.toggle('maybe-weak', Boolean(team.maybe_out) && team.players.length === 1);
    KF.attr(el, 'aria-label', KF.teamSummary(team));
    KF.text(el.querySelector('.kills b'), team.kills);
    const knocked = team.players.filter((p) => p.st === 'knocked').length;
    const left = team.maybe_out ? `0~${team.left}` : team.left; // wiped already, or not
    KF.html(el.querySelector('.sum'), team.out ? '탈락' : `남은 <b>${left}</b> · 기절 <b>${knocked}</b>`);
    KF.html(el.querySelector('.knocks'), `기절시킴 <b>${team.knocks}</b>`);
    if (el.dataset.at !== String(team.at)) { el.dataset.at = team.at; KF.heat(el, team.at); }

    const all = KF.slots(team);
    const shown = all.slice(0, MAX_SLOTS);
    const sig = shown.map((s) => `${s.kind}${s.name}${s.at}/${s.back}`).join() + all.length;
    if (el.dataset.sig === sig) return;
    el.dataset.sig = sig;
    const ul = el.querySelector('.slots');
    const UNSEEN = { assumed: ['미확인', '피드에 아직 안 보인 팀원 (생존 가정)'],
                     unsure: ['생존 불명', '피드에 안 보인 팀원: 없을 수도 있어 팀이 이미 전멸했을 수 있음'] };
    ul.innerHTML = shown.map((s) => {
      const [unseen, why] = UNSEEN[s.kind] ?? UNSEEN.assumed;
      const name = s.name ? KF.esc(s.name) : unseen;
      const label = `${s.name ?? '미확인 슬롯'} ${KF.STATUS[s.kind]}`;
      const st = s.kind === 'knocked' ? '<span class="st">기절</span>' : '';
      return `<li class="slot hot-able s-${s.kind}" aria-label="${KF.esc(label)}">${KF.glyph(s.kind)}
        <span class="nm" title="${KF.esc(s.name ?? why)}">${name}</span>${st}
        <span class="st back" hidden>${KF.glyph('back')}복귀</span></li>`;
    }).join('') + (all.length > MAX_SLOTS ? `<li class="slot more-slots">+ ${all.length - MAX_SLOTS}명</li>` : '');
    shown.forEach((s, i) => {
      const li = ul.children[i];
      KF.heat(li, s.at);
      if (s.back && s.back + KF.HOLD > KF.now()) {
        const tag = li.querySelector('.back');
        tag.hidden = false;
        setTimeout(() => { tag.hidden = true; }, s.back + KF.HOLD - KF.now());
      }
    });
  }

  function renderCards() {
    grid.querySelector('.waiting')?.remove();
    if (!snap.teams.length) {
      grid.replaceChildren();
      cards.clear();
      grid.innerHTML = '<p class="waiting">킬피드 이벤트를 기다리는 중…</p>';
      return;
    }
    // by badge number, like the Tk board: a team is always in the same place
    const teams = [...snap.teams].sort((a, b) => (a.id == null) - (b.id == null) || (a.id ?? 0) - (b.id ?? 0));
    const keys = new Set(teams.map((t) => String(t.id)));
    for (const [k, el] of cards) if (!keys.has(k)) { el.remove(); cards.delete(k); }
    teams.forEach((t, i) => {
      const el = cardFor(String(t.id));
      fillCard(el, t);
      if (grid.children[i] !== el) grid.insertBefore(el, grid.children[i] ?? null);
    });
  }

  // --- feed -----------------------------------------------------------------------------------
  const evKey = (e) => [e.clock, e.kind, e.killer, e.kt, e.victim, e.vt].join('|');

  function evItem(e) {
    const li = document.createElement('li');
    li.className = 'ev hot-able';
    li.dataset.key = evKey(e);
    const extra = (e.weapon ? ` · ${KF.esc(e.weapon)}` : '') + (e.hs ? ' · 헤드샷' : '');
    li.innerHTML = `<span class="ev-t num">${KF.esc(e.clock)}</span>${KF.eventGlyph(e.kind)}
      <span class="ev-who">${KF.who(e, 14)}</span>
      <span class="ev-what"><span class="k-${KF.eventKind(e.kind)}">${KF.VERB[e.kind] ?? KF.esc(e.kind)}</span>${extra}</span>`;
    return li;
  }

  /** Newest first. Only events not on screen yet are built; a new match rebuilds the list. */
  function renderFeed() {
    const list = $('feed');
    const events = snap.events;
    if (!events.length) {
      if (!list.querySelector('.feed-empty')) list.innerHTML = '<li class="feed-empty">아직 이벤트가 없습니다</li>';
      return;
    }
    const top = list.firstElementChild?.dataset.key;
    let fresh = events.findIndex((e) => evKey(e) === top);
    if (fresh === -1) { list.replaceChildren(); fresh = events.length; }
    if (!fresh) return;
    list.prepend(...events.slice(0, fresh).map(evItem));
    while (list.children.length > events.length) list.lastElementChild.remove();
    KF.heat(list.firstElementChild, snap.last_at);
  }

  // --- header, toolbar, ROI map --------------------------------------------------------------
  function renderHead() {
    KF.count($('players'), KF.playersLeft(snap));
    KF.count($('teams'), KF.teamsLeft(snap));
    // the min also drops never-seen badges (lobby leavers), so count the flagged teams themselves
    const maybe = snap.teams.filter((t) => t.maybe_out).length;
    KF.html($('totals'), `킬 <b>${snap.kills}</b> · 기절 중 <b>${snap.knocked}</b>`
      + (maybe ? ` · 전멸 가능 <b>${maybe}</b>팀` : ''));
    KF.attr($('teams'), 'title', KF.teamsLeft(snap).includes('~')
      ? '최솟값은 전멸 가능 팀과, 한 번도 안 보인 팀 가운데 이탈했을 수 있는 팀을 뺀 값입니다' : '');
    KF.gauge($('gauge'), snap);
    renderCapture();
    const track = $('track');
    track.setAttribute('aria-checked', String(snap.tracking));
    KF.text(track.querySelector('.sw-val'), snap.tracking ? '켜짐' : '꺼짐');
    track.disabled = !engine.alive;
    tick();
  }

  function renderCapture() {
    const pill = $('capture');
    let markup;
    if (!engine.alive) markup = `${KF.glyph('warn')}${KF.esc(engine.reason || '엔진 연결 안 됨')}`;
    else if (!snap) markup = `${KF.glyph('pause')}엔진 시작 중…`;
    else if (snap.error) markup = `${KF.glyph('warn')}캡처 오류: ${KF.esc(snap.status)}`;
    else markup = `${KF.glyph(snap.tracking ? 'live' : 'pause')}${KF.esc(snap.status)}`;
    pill.classList.toggle('err', !engine.alive || Boolean(snap?.error));
    KF.html(pill, markup);
  }

  function renderOverlayState() {
    if (!ov) return;
    const sw = $('overlay');
    sw.setAttribute('aria-checked', String(ov.wanted));
    KF.text(sw.querySelector('.sw-val'), ov.wanted ? '켜짐' : '꺼짐');
    const notes = [];
    if (ov.editing) notes.push('위치 조정 중');
    else if (ov.wanted && !ov.visible) notes.push('표시할 팀이 없어 숨김');
    if (!ov.hotkey) notes.push('Ctrl+Alt+O 등록 실패');
    KF.text($('overlay-note'), notes.join(' · '));
    const edit = $('ov-edit');
    edit.setAttribute('aria-pressed', String(ov.editing));
    KF.text(edit, ov.editing ? '위치 확정' : '오버레이 위치 조정');
    const roi = $('roi-edit');
    roi.disabled = ov.roiEditing;
    KF.text(roi, ov.roiEditing ? 'ROI 지정 중…' : 'ROI 조정');
    place($('ov-box'), ov.rect);
    $('ov-box').classList.toggle('off', !ov.visible);
    $('screen').style.aspectRatio = `${ov.screen[0]} / ${ov.screen[1]}`;
  }

  function place(el, r) {
    el.hidden = !r;
    if (!r) return;
    const [x, y, w, h] = r;
    Object.assign(el.style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });
  }

  /** Once a second: coarse clock labels only, DOM touched only when their text changes. */
  function tick() {
    if (!snap || document.hidden) return;
    const basis = KF.playersBasis(snap);
    KF.text($('players-label'), basis === 'estimate' ? '명 남음 · 추정' : basis === 'stale' ? '명 · 마지막 관측' : '명 남음 · 화면');
    const age = basis === 'stale' ? ` (${Math.floor((KF.now() - snap.hud.at) / 1000)}초 전 관측)` : '';
    KF.text($('basis'), snap.basis + age);
  }
  setInterval(tick, 1000);

  // --- wiring ---------------------------------------------------------------------------------
  $('track').addEventListener('click', () => {
    if (snap) kfl.send('engine:cmd', { cmd: 'tracking', on: !snap.tracking });
  });
  $('overlay').addEventListener('click', () => kfl.send('overlay:toggle'));
  $('ov-edit').addEventListener('click', () => kfl.send('overlay:edit', !ov?.editing));
  $('roi-edit').addEventListener('click', () => kfl.send('roi:open'));
  $('settings').addEventListener('click', () => kfl.send('settings:open'));

  function render() {
    renderHead();
    renderCards();
    renderFeed();
    place($('roi-killfeed'), snap.rois?.killfeed);
    place($('roi-alive'), snap.rois?.alive);
    if (snap.mode === 'demo') KF.text($('roi-note'), '데모 모드: ROI는 저장하지 않고 이번 실행에만 씁니다.');
  }
  // Minimized or covered by the game (Chromium tracks occlusion): skip the work, catch up once.
  let behind = false;
  kfl.on('snapshot', (s) => {
    snap = s;
    KF.sync(s);
    if (document.hidden) behind = true;
    else render();
  });
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && behind) { behind = false; render(); }
  });
  kfl.on('overlay:state', (s) => { ov = s; renderOverlayState(); });
  kfl.on('engine', (e) => { engine = e; renderCapture(); $('track').disabled = !e.alive; });
  if (innerHeight < 760) $('roi').open = false; // short window: the feed gets the room
  renderCapture();
  kfl.send('ui:ready');
})();
