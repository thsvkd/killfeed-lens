from __future__ import annotations

from itertools import count

from kfocr.domain.models import EventType
from kfocr.ui.teams import KNOCK_TIMEOUT_NS, Ledger, Status, make_event

K, F, D = EventType.KNOCK, EventType.FINISH, EventType.KILL
_feed_clock = count(0, 2_000_000_000)  # a badge counts once seen again a second later


def ev(*args, **kwargs):
    kwargs.setdefault("ns", next(_feed_clock))
    return make_event(*args, **kwargs)


def test_hud_count_overrides_squad_assumptions_without_double_subtracting_kills(monkeypatch):
    from kfocr.domain.models import AliveCount

    clock = [10.0]
    monkeypatch.setattr("kfocr.ui.teams.time.monotonic", lambda: clock[0])
    ledger = Ledger(teams=25)
    ledger.observe_alive_count(AliveCount(96, 0, 0))
    ledger.apply(ev("a", 7, "b", 25))
    assert ledger.players_left == 96
    assert ledger.teams[25].remaining == 3  # HUD count does not invent team membership.
    ledger.observe_alive_count(AliveCount(95, 1, 400_000_000))
    assert ledger.players_left == 95
    assert "화면" in ledger.basis and "추정" in ledger.basis
    clock[0] = 20.0
    assert "10초 전" in ledger.basis  # lost HUD is labelled, not presented as current.


def squad_down(ledger: Ledger, team: int = 25, count: int = 4, kind: EventType = D) -> None:
    for i in range(count):
        ledger.apply(ev("a1", 7, f"b{i}", team, kind))


def test_knock_then_finish_counts_one_kill_and_one_knock():
    ledger = Ledger()
    ledger.apply(ev("a1", 7, "b1", 25, K))
    b = ledger.teams[25]
    assert (b.knocked, b.dead, b.remaining) == (1, 0, 4)
    assert ledger.teams[7].knocks == 1 and ledger.teams[7].kills == 0

    ledger.apply(ev("a2", 7, "b1", 25, F))
    assert (b.knocked, b.dead, b.remaining) == (0, 1, 3)
    # Unseen teams up to the highest badge (25) count as full squads.
    assert ledger.teams[7].kills == 1 and ledger.players_left == 3 + 4 + 23 * 4


def test_all_members_knocked_wipes_team_but_not_while_slots_are_unseen():
    ledger = Ledger()
    squad_down(ledger, count=3, kind=K)
    b = ledger.teams[25]
    assert not b.eliminated and b.remaining == 4  # 4th slot never seen: assumed standing
    ledger.apply(ev("a1", 7, "b3", 25, K))
    assert b.eliminated and b.remaining == 0
    assert all(p.status is Status.DEAD for p in b.players.values())  # knocked => shown dead
    assert ledger.teams_left == 24  # badges number 25 teams; one is wiped


def test_dead_player_seen_as_killer_revives_and_unwipes_team():
    ledger = Ledger()
    squad_down(ledger)
    assert ledger.teams[25].eliminated and ledger.teams_left == 24
    ledger.apply(ev("b2", 25, "c1", 30, K))  # recall / self-AED / comeback
    b = ledger.teams[25]
    assert b.players["b2"].status is Status.ALIVE and b.players["b2"].revived > 0
    assert not b.eliminated and b.remaining == 1 and ledger.teams_left == 25  # 25 (back); badge 30 seen once


def test_dead_player_seen_as_victim_again_is_back_but_repeat_death_is_not_a_revive():
    ledger = Ledger()
    squad_down(ledger, count=2)
    ledger.apply(ev("a1", 7, "b0", 25, K))  # dead b0 knocked again => came back
    assert ledger.teams[25].players["b0"].status is Status.KNOCKED
    ledger.apply(ev("a1", 7, "b1", 25, D))  # duplicate death stays dead
    assert ledger.teams[25].players["b1"].status is Status.DEAD
    assert ledger.teams[25].players["b1"].revived == 0


def test_knock_without_a_death_expires_to_alive():
    ledger = Ledger()
    ledger.apply(ev("a1", 7, "b1", 25, K, ns=0))
    ledger.apply(ev("a1", 7, "c1", 30, D, ns=KNOCK_TIMEOUT_NS - 1))
    assert ledger.teams[25].players["b1"].status is Status.KNOCKED
    ledger.apply(ev("a1", 7, "c2", 30, D, ns=KNOCK_TIMEOUT_NS))
    assert ledger.teams[25].players["b1"].status is Status.ALIVE
    ledger.apply(ev("a1", 7, "b2", 25, K, ns=KNOCK_TIMEOUT_NS))
    ledger.tick(ledger.last_at + 1e6)  # quiet feed: the GUI clock expires it too
    assert ledger.teams[25].players["b2"].status is Status.ALIVE


def test_unknown_team_uses_known_name_then_bucket_then_rehomes():
    ledger = Ledger()
    ledger.apply(ev("a1", 7, "b1", 25))
    ledger.apply(ev("a1", None, "b2", 25, K))  # killer badge unreadable
    assert ledger.teams[7].knocks == 1 and None not in ledger.teams

    ledger.apply(ev("x1", None, "z9", None, K))  # nobody known
    assert ledger.teams[None].players["z9"].status is Status.KNOCKED
    assert ledger.teams[None].remaining == 2  # x1 + z9 only; the bucket assumes no squad size
    assert 25 in ledger.teams and ledger.players_left == 3 + 4 + 23 * 4

    ledger.apply(ev("a1", 7, "z9", 30, K))  # badge readable -> re-homed
    assert ledger.teams[30].players["z9"].status is Status.KNOCKED
    assert "z9" not in ledger.teams[None].players


def test_ocr_spelling_variants_join_one_player_but_numbered_squadmates_stay_apart():
    ledger = Ledger()
    ledger.apply(ev("LA11AL", 23, "x1", 5))
    ledger.apply(ev("pporidw", 18, "LA11ALLA", 23))   # trailing icon debris
    ledger.apply(ev("Zo-o7", 7, "y1", 6))
    ledger.apply(ev("pporidwL1", 18, "Z0-07", None))  # icon debris; O/0 drift, no badge
    for name in ("kim1", "kim2", "ace_01", "ace_02"):
        ledger.apply(ev("pporidw", 18, name, 30))
    ledger.apply(ev("Bob", 40, "x2", 5))
    ledger.apply(ev("pporidw", 18, "Bobby", 40))      # a squadmate, not a partial read
    ledger.apply(ev("pporidw", 18, "Kim0", None))
    ledger.apply(ev("pporidw", 18, "KimO", None))      # orphans fold together too

    assert list(ledger.teams[23].players) == ["LA11AL"] and ledger.teams[23].dead == 1
    assert list(ledger.teams[18].players) == ["pporidw"]
    assert ledger.teams[7].dead == 1
    assert ledger.teams[30].dead == 4
    assert list(ledger.teams[40].players) == ["Bob", "Bobby"]
    assert list(ledger.teams[None].players) == ["Kim0"]
    ledger.apply(ev("pporidw", 18, "da_kyeongg", None, K))
    ledger.apply(ev("pporidw", 18, "kyeongg", 27, K))
    ledger.apply(ev("pporidw", 18, "da_kyeongg", 27))  # exact orphan wins over a partial read
    assert ledger.teams[27].players["da_kyeongg"].status is Status.DEAD
    assert "da_kyeongg" not in ledger.teams[None].players

def test_squad_size_follows_the_highest_confirmed_badge():
    duo = Ledger()
    duo.apply(ev("a1", 41, "b1", 3))
    assert duo.squad_size == 4  # one sighting of badge 41 is not yet evidence
    duo.apply(ev("a1", 41, "b2", 3))
    assert duo.squad_size == 2 and duo.teams[3].eliminated and duo.teams_left == 40
    duo.apply(ev("a1", 71, "c1", 5))  # a single misread badge
    assert duo.squad_size == 2
    fixed = Ledger(squad_size=4)
    for victim in ("b1", "b2"):
        fixed.apply(ev("a1", 41, victim, 3))
    assert fixed.squad_size == 4 and not fixed.teams[3].eliminated


def test_a_badge_read_late_moves_an_unknown_team_victim_home():
    from dataclasses import replace

    ledger = Ledger(squad_size=2)
    kill = ev("a1", 7, "b1", None)
    ledger.apply(kill)
    assert ledger.teams[None].dead == 1
    ledger.amend(replace(kill, victim_team=25))
    assert None not in ledger.teams and ledger.teams[25].dead == 1
    assert ledger.recent[-1].victim_team == 25


def test_a_late_badge_that_completes_a_wipe_bleeds_the_knocked_out():
    from dataclasses import replace

    ledger = Ledger(squad_size=2)
    ledger.apply(ev("a1", 7, "b1", 25, K))
    last = ev("a1", 7, "b2", None)
    ledger.apply(last)
    ledger.amend(replace(last, victim_team=25))
    assert ledger.teams[25].eliminated and ledger.teams[25].dead == 2  # no revive in 90 s


def test_short_squads_cannot_add_up_past_the_lobby():
    ledger = Ledger()
    ledger.apply(ev("a1", 30, "b1", 3))
    ledger.apply(ev("a1", 30, "b2", 3))  # badge 30 confirmed: 30 full squads would be 120 players
    assert ledger.squad_size == 4 and ledger.players_left == 100 - 2


def test_kills_made_with_an_unread_badge_follow_the_player_home():
    ledger = Ledger()
    ledger.apply(ev("ace", None, "v1", 9))
    ledger.apply(ev("ace", None, "v2", 9, K))
    assert ledger.teams[None].kills == 1 and ledger.teams[None].knocks == 1
    ledger.apply(ev("ace", 12, "v3", 9))  # badge readable at last
    assert None not in ledger.teams
    assert (ledger.teams[12].kills, ledger.teams[12].knocks) == (2, 1)


def test_one_bad_frame_cannot_confirm_a_badge_and_lobby_leavers_widen_the_range():
    duo = Ledger()
    duo.apply(make_event("a1", 41, "b1", 3, ns=0))
    duo.apply(make_event("a2", 41, "b2", 3, ns=33_000_000))  # the next row of the same frame
    assert duo.top_badge == 0
    duo.apply(make_event("a1", 41, "b3", 4, ns=2_000_000_000))
    assert duo.top_badge == 41 and duo.squad_size == 2
    # 41 - wiped team 3 = 40; team 4 may be wiped; 2 of the 38 never-seen badges may be leavers.
    assert (duo.teams_left_min, duo.teams_left) == (37, 40)
    assert Ledger(teams=41).teams_left_min == 41  # a known start count needs no allowance


def test_the_hud_count_bounds_teams_left_without_trusting_seen_alive_members():
    from kfocr.domain.models import AliveCount

    ledger = Ledger(squad_size=4, teams=25)
    for team in (3, 4, 5):  # three wiped-looking teams: three dead, one slot never seen
        squad_down(ledger, team, 3)
    ledger.apply(ev("x1", 9, "y1", 8))  # 9 has someone standing; 8 only a dead member
    ledger.apply(ev("z1", 11, "w1", 12, K))
    assert (ledger.teams_left_min, ledger.teams_left) == (21, 25)  # no HUD: the old guess

    ledger.observe_alive_count(AliveCount(5, 0, 0))
    # at least 5 / 4 squads; the standing 7, 9, 11, 12 (a1, x1, z1, w1 knocked) take 4 of the 5
    # players, and the fifth most likely fills one of their 12 empty slots (10 taken as filled)
    assert (ledger.teams_left_min, ledger.teams_left) == (2, 4)
    drawn = ledger.draw()  # the 4 seen standing, then one never-seen slot of the latest teams
    assert ledger.placed == 4 and [(k, a) for k, (_, a) in drawn.items() if a] in ([(11, 1)], [(12, 1)])
    assert "unsure" not in {st for states, _ in drawn.values() for st in states.values()}

    ledger.observe_alive_count(AliveCount(3, 0, 0))  # fewer than the feed shows standing:
    assert (ledger.teams_left_min, ledger.teams_left) == (1, 3)  # missed deaths, HUD wins
    drawn = ledger.draw()  # the one seen standing longest ago is drawn unsure, no slot assumed
    assert drawn[7][0]["a1"] == "unsure" and not any(a for _, a in drawn.values())
    ledger.observe_alive_count(AliveCount(90, 0, 0))
    assert (ledger.teams_left_min, ledger.teams_left) == (23, 25)
    assert all(a == ledger.teams[k].total - len(ledger.teams[k].players)  # room for every slot
               for k, (_, a) in ledger.draw().items() if not ledger.teams[k].eliminated
               and not ledger.teams[k].maybe_out)


def test_ranked_puts_the_teams_the_board_knows_best_first():
    ledger = Ledger(squad_size=4, teams=25)
    ledger.apply(ev("a1", 7, "b1", 9))  # 7: one seen, 9: one dead
    squad_down(ledger, 12, 2)
    for name in ("c1", "c2", "c3", "c4"):  # 15: the whole squad seen, all standing
        ledger.apply(ev(name, 15, "e1", 30, K))  # 30: one seen, knocked
    ledger.apply(ev("d1", None, "b2", 9))  # unknown badge
    squad_down(ledger, 20, 4)  # eliminated
    # 7 and 30 have 3 unseen slots (7 has a kill); 9 and 12 may be wiped (2 unseen slots each)
    assert [t.id for t in ledger.ranked()] == [15, 7, 30, 9, 12, None, 20]


def test_a_full_squad_takes_a_close_misread_as_a_member_but_not_a_stranger():
    ledger = Ledger(squad_size=4, teams=25)
    for name in ("Bravo", "Charlie", "Delta", "Echo"):
        ledger.apply(ev(name, 7, "v" + name, 9, K))
    ledger.apply(ev("Charlle", 7, "w1", 9))  # one letter off a member of the full squad
    ledger.apply(ev("Zulu-Wolf", 7, "w2", 9))  # unlike any: someone else under a misread badge
    assert sorted(ledger.teams[7].players) == ["Bravo", "Charlie", "Delta", "Echo", "Zulu-Wolf"]
    assert ledger.teams[7].players["Charlie"].kills == 1
    ledger.apply(ev("x1", 11, "Br", 7))  # too short to be a PUBG name: part of a member's
    assert ledger.teams[7].players["Bravo"].status is Status.DEAD and "Br" not in ledger.teams[7].players


def test_a_short_read_goes_to_the_member_it_is_most_like():
    ledger = Ledger(squad_size=4, teams=25)
    for name in ("Alexander", "Lex9"):
        ledger.apply(ev(name, 7, "v" + name, 9, K))
    ledger.apply(ev("x1", 11, "Lex", 7))  # part of both names; far more of Lex9's
    assert ledger.teams[7].players["Lex9"].status is Status.DEAD
    assert ledger.teams[7].players["Alexander"].status is Status.ALIVE

def test_recalled_players_hold_their_share_of_the_hud_count_until_the_feed_shows_them():
    from kfocr.domain.models import AliveCount

    ledger = Ledger(squad_size=4, teams=25)
    for victim in ("b1", "b2", "b3"):
        ledger.apply(ev("a1", 7, victim, 9))
    held = lambda count, s: ledger.observe_alive_count(AliveCount(count, 0, int(s * 1e9)))  # noqa: E731
    held(10, 0), held(10, 3)
    held(12, 4), held(12, 7)  # a recall wave brings two back
    assert ledger.returned == 2
    ledger.apply(ev("b1", 9, "c1", 11))  # one of them shows up: alive again, one still unplaced
    assert ledger.teams[9].players["b1"].status is Status.ALIVE and ledger.returned == 1
    held(99, 8), held(99, 11)  # a misread jump is not a recall wave
    assert ledger.returned == 1


def test_a_count_misread_for_a_moment_is_no_recall_wave():
    from kfocr.domain.models import AliveCount

    ledger = Ledger(squad_size=4, teams=25)
    for victim in ("b1", "b2", "b3", "b4", "b5", "b6"):
        ledger.apply(ev("a1", 7, victim, 9))
    for n, s in ((30, 0), (30, 3), (24, 4), (24, 4.5), (30, 5), (30, 8), (37, 9), (30, 10), (30, 14)):
        ledger.observe_alive_count(AliveCount(n, 0, int(s * 1e9)))
    assert ledger.returned == 0


def test_a_knocked_teammate_bleeding_out_with_the_squad_is_not_a_second_death():
    from kfocr.domain.models import AliveCount

    ledger = Ledger(squad_size=2, teams=50)
    ledger.apply(ev("a1", 7, "b1", 9, K))
    ledger.apply(ev("a1", 7, "b2", 9))  # the last one standing dies: b1 bleeds out with it
    for n, s in ((10, 0), (10, 3), (12, 4), (12, 7)):
        ledger.observe_alive_count(AliveCount(n, 0, int(s * 1e9)))
    ledger.apply(ev("a1", 7, "b1", 9, ns=ledger.last_ns + 2_000_000_000))  # its bleed-out row
    assert ledger.returned == 2


def test_unread_badges_get_only_the_count_the_teams_leave():
    from kfocr.domain.models import AliveCount

    ledger = Ledger(squad_size=4, teams=25)
    ledger.apply(ev("a1", 5, "x", 7))
    ledger.apply(ev("a2", 5, "y", 7))
    ledger.apply(ev("z", None, "w", 7))  # badge unread: likely team 5's third member
    ledger.observe_alive_count(AliveCount(4, 0, 0))
    drawn = ledger.draw()
    assert (drawn[5][0]["a1"], drawn[5][0]["a2"], drawn[5][1]) == ("alive", "alive", 2)
    assert drawn[None][0]["z"] == "unsure"


def test_the_hud_count_goes_to_knocked_then_recently_seen_players_first():
    from kfocr.domain.models import AliveCount

    ledger = Ledger(squad_size=4, teams=25)
    ledger.apply(ev("old", 7, "o1", 9))  # 'old' seen standing first
    ledger.apply(ev("new", 11, "n1", 12, K))  # then 'new' knocks 'n1'
    ledger.observe_alive_count(AliveCount(2, 0, 0))
    drawn = ledger.draw()
    # knocked n1 (the HUD counts knocked players) and the latest seen 'new' take both; the
    # stale 'old' is drawn unsure and no never-seen slot is assumed alive
    assert (drawn[12][0]["n1"], drawn[11][0]["new"], drawn[7][0]["old"]) == ("knocked", "alive", "unsure")
    assert not any(assumed for _, assumed in drawn.values())


def test_a_knock_that_runs_out_is_drawn_last_when_the_count_is_short():
    from kfocr.domain.models import AliveCount

    ledger = Ledger(squad_size=4, teams=25)
    ledger.apply(ev("a1", 7, "b1", 9, K))
    ledger.expire(ledger.last_ns + KNOCK_TIMEOUT_NS, 0.0)  # no news for the bleed-out time
    b1 = ledger.teams[9].players["b1"]
    assert b1.status is Status.ALIVE and not b1.sure  # most likely revived, maybe dead
    ledger.observe_alive_count(AliveCount(1, 0, 0))
    drawn = ledger.draw()
    assert (drawn[7][0]["a1"], drawn[9][0]["b1"]) == ("alive", "unsure")
