from __future__ import annotations

from kfocr.domain.models import EventType
from kfocr.ui.teams import KNOCK_TIMEOUT_NS, Ledger, Status, make_event

K, F, D = EventType.KNOCK, EventType.FINISH, EventType.KILL


def test_hud_count_overrides_squad_assumptions_without_double_subtracting_kills(monkeypatch):
    from kfocr.domain.models import AliveCount

    clock = [10.0]
    monkeypatch.setattr("kfocr.ui.teams.time.monotonic", lambda: clock[0])
    ledger = Ledger(teams=25)
    ledger.observe_alive_count(AliveCount(96, 0, 0))
    ledger.apply(make_event("a", 7, "b", 25))
    assert ledger.players_left == 96
    assert ledger.teams[25].remaining == 3  # HUD count does not invent team membership.
    ledger.observe_alive_count(AliveCount(95, 1, 400_000_000))
    assert ledger.players_left == 95
    assert "화면" in ledger.basis and "추정" in ledger.basis
    clock[0] = 20.0
    assert "10초 전" in ledger.basis  # lost HUD is labelled, not presented as current.


def squad_down(ledger: Ledger, team: int = 25, count: int = 4, kind: EventType = D) -> None:
    for i in range(count):
        ledger.apply(make_event("a1", 7, f"b{i}", team, kind))


def test_knock_then_finish_counts_one_kill_and_one_knock():
    ledger = Ledger()
    ledger.apply(make_event("a1", 7, "b1", 25, K))
    b = ledger.teams[25]
    assert (b.knocked, b.dead, b.remaining) == (1, 0, 4)
    assert ledger.teams[7].knocks == 1 and ledger.teams[7].kills == 0

    ledger.apply(make_event("a2", 7, "b1", 25, F))
    assert (b.knocked, b.dead, b.remaining) == (0, 1, 3)
    assert ledger.teams[7].kills == 1 and ledger.players_left == 3 + 4


def test_all_members_knocked_wipes_team_but_not_while_slots_are_unseen():
    ledger = Ledger()
    squad_down(ledger, count=3, kind=K)
    b = ledger.teams[25]
    assert not b.eliminated and b.remaining == 4  # 4th slot never seen: assumed standing
    ledger.apply(make_event("a1", 7, "b3", 25, K))
    assert b.eliminated and b.remaining == 0
    assert all(p.status is Status.DEAD for p in b.players.values())  # knocked => shown dead
    assert ledger.teams_left == 1


def test_dead_player_seen_as_killer_revives_and_unwipes_team():
    ledger = Ledger()
    squad_down(ledger)
    assert ledger.teams[25].eliminated
    ledger.apply(make_event("b2", 25, "c1", 30, K))  # recall / self-AED / comeback
    b = ledger.teams[25]
    assert b.players["b2"].status is Status.ALIVE and b.players["b2"].revived > 0
    assert not b.eliminated and b.remaining == 1 and ledger.teams_left == 3  # 7, 25 (back), 30


def test_dead_player_seen_as_victim_again_is_back_but_repeat_death_is_not_a_revive():
    ledger = Ledger()
    squad_down(ledger, count=2)
    ledger.apply(make_event("a1", 7, "b0", 25, K))  # dead b0 knocked again => came back
    assert ledger.teams[25].players["b0"].status is Status.KNOCKED
    ledger.apply(make_event("a1", 7, "b1", 25, D))  # duplicate death stays dead
    assert ledger.teams[25].players["b1"].status is Status.DEAD
    assert ledger.teams[25].players["b1"].revived == 0


def test_knock_without_a_death_expires_to_alive():
    ledger = Ledger()
    ledger.apply(make_event("a1", 7, "b1", 25, K, ns=0))
    ledger.apply(make_event("a1", 7, "c1", 30, D, ns=KNOCK_TIMEOUT_NS - 1))
    assert ledger.teams[25].players["b1"].status is Status.KNOCKED
    ledger.apply(make_event("a1", 7, "c2", 30, D, ns=KNOCK_TIMEOUT_NS))
    assert ledger.teams[25].players["b1"].status is Status.ALIVE
    ledger.apply(make_event("a1", 7, "b2", 25, K, ns=KNOCK_TIMEOUT_NS))
    ledger.tick(ledger.last_at + 1e6)  # quiet feed: the GUI clock expires it too
    assert ledger.teams[25].players["b2"].status is Status.ALIVE


def test_unknown_team_uses_known_name_then_bucket_then_rehomes():
    ledger = Ledger()
    ledger.apply(make_event("a1", 7, "b1", 25))
    ledger.apply(make_event("a1", None, "b2", 25, K))  # killer badge unreadable
    assert ledger.teams[7].knocks == 1 and None not in ledger.teams

    ledger.apply(make_event("x1", None, "z9", None, K))  # nobody known
    assert ledger.teams[None].players["z9"].status is Status.KNOCKED
    assert ledger.teams[None].remaining == 2  # x1 + z9 only; the bucket assumes no squad size
    assert 25 in ledger.teams and ledger.players_left == 3 + 4

    ledger.apply(make_event("a1", 7, "z9", 30, K))  # badge readable -> re-homed
    assert ledger.teams[30].players["z9"].status is Status.KNOCKED
    assert "z9" not in ledger.teams[None].players


def test_ocr_spelling_variants_join_one_player_but_numbered_squadmates_stay_apart():
    ledger = Ledger()
    ledger.apply(make_event("LA11AL", 23, "x1", 5))
    ledger.apply(make_event("pporidw", 18, "LA11ALLA", 23))   # trailing icon debris
    ledger.apply(make_event("Zo-o7", 7, "y1", 6))
    ledger.apply(make_event("pporidwL1", 18, "Z0-07", None))  # icon debris; O/0 drift, no badge
    for name in ("kim1", "kim2", "ace_01", "ace_02"):
        ledger.apply(make_event("pporidw", 18, name, 30))
    ledger.apply(make_event("Bob", 40, "x2", 5))
    ledger.apply(make_event("pporidw", 18, "Bobby", 40))      # a squadmate, not a partial read
    ledger.apply(make_event("pporidw", 18, "Kim0", None))
    ledger.apply(make_event("pporidw", 18, "KimO", None))      # orphans fold together too

    assert list(ledger.teams[23].players) == ["LA11AL"] and ledger.teams[23].dead == 1
    assert list(ledger.teams[18].players) == ["pporidw"]
    assert ledger.teams[7].dead == 1
    assert ledger.teams[30].dead == 4
    assert list(ledger.teams[40].players) == ["Bob", "Bobby"]
    assert list(ledger.teams[None].players) == ["Kim0"]
    ledger.apply(make_event("pporidw", 18, "da_kyeongg", None, K))
    ledger.apply(make_event("pporidw", 18, "kyeongg", 27, K))
    ledger.apply(make_event("pporidw", 18, "da_kyeongg", 27))  # exact orphan wins over a partial read
    assert ledger.teams[27].players["da_kyeongg"].status is Status.DEAD
    assert "da_kyeongg" not in ledger.teams[None].players
