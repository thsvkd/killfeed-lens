from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from kfocr.domain.models import ConfirmedEvent, EventType, Session
from kfocr.telemetry.client import PubgApiClient
from kfocr.telemetry.mapping import TelemetryCanonicalEvent, map_telemetry
from kfocr.telemetry.match_finder import find_match
from kfocr.telemetry.validator import validate_events


def _character(name: str | None) -> dict | None:
    return None if name is None else {"name": name}


def _session(**overrides: object) -> Session:
    values = {
        "session_id": uuid4(),
        "source_type": "video",
        "started_at_utc": datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
        "ended_at_utc": datetime(2026, 10, 6, 12, 30, tzinfo=UTC),
        "source_resolution": (1920, 1080),
        "roi_profile_id": "test",
        "app_version": "0.1.0",
        "git_commit": "abc123",
        "config_hash": "config-hash",
        "model_manifest_hash": "model-hash",
        "status": "completed",
        "timeline_origin_ns": 0,
        "metadata": {"ocr_engine": "fake/1", "platform": "steam"},
    }
    values.update(overrides)
    return Session(**values)


def _observed(
    session: Session,
    seconds: float,
    killer: str,
    victim: str,
    event_type: EventType = EventType.KILL,
    confidence: float = 0.95,
    dedup_key: str | None = None,
) -> ConfirmedEvent:
    return ConfirmedEvent(
        observed_at_ns=int(seconds * 1_000_000_000),
        frame_seq=int(seconds * 10),
        killer_name_raw=killer,
        victim_name_raw=victim,
        killer_name=killer,
        victim_name=victim,
        killer_team=None,
        victim_team=None,
        event_type=event_type,
        weapon_label=None,
        headshot=None,
        confidence_row=confidence,
        confidence_killer=confidence,
        confidence_victim=confidence,
        confidence_event_type=confidence,
        confidence_total=confidence,
        source_bbox=(0, 0, 100, 20),
        source_crop_ref=None,
        event_id=uuid4(),
        session_id=session.session_id,
        confirmed_at_ns=int(seconds * 1_000_000_000),
        dedup_key=dedup_key or f"{killer}:{victim}:{seconds}",
        tracker_evidence_count=2,
    )


def _canonical(
    session: Session,
    seconds: float,
    actor: str,
    victim: str,
    event_type: EventType = EventType.KILL,
    raw_index: int = 0,
) -> TelemetryCanonicalEvent:
    return TelemetryCanonicalEvent(
        occurred_at_utc=session.started_at_utc + timedelta(seconds=seconds),
        event_type=event_type,
        actor_candidates=[actor],
        victim=victim,
        dBNO_maker=actor if event_type == EventType.KILL else None,
        finisher=actor if event_type == EventType.KILL else None,
        killer=actor,
        raw_type="LogPlayerKillV2" if event_type == EventType.KILL else "LogPlayerMakeGroggy",
        raw_index=raw_index,
    )


def test_client_retries_429_and_never_sends_auth_to_telemetry(monkeypatch: pytest.MonkeyPatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[{"_T": "LogMatchStart"}])

    sleeps: list[float] = []
    transport = httpx.MockTransport(handler)
    client = PubgApiClient(
        api_key="super-secret",
        client=httpx.Client(transport=transport),
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    assert client.get_telemetry("https://telemetry-cdn.pubg.com/example.json") == [
        {"_T": "LogMatchStart"}
    ]
    assert sleeps == [2.0]
    assert all("Authorization" not in request.headers for request in requests)
    assert "super-secret" not in repr(requests)


def test_client_uses_exact_player_endpoint_and_keeps_auth_off_match():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"id": "x"}})

    client = PubgApiClient(
        api_key="key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client.get_player("steam", "A B")
    client.get_match("steam", "match-1")

    assert requests[0].url.path == "/shards/steam/players"
    assert requests[0].url.params["filter[playerNames]"] == "A B"
    assert requests[0].headers["Authorization"] == "Bearer key"
    assert requests[1].url.path == "/shards/steam/matches/match-1"
    assert "Authorization" not in requests[1].headers


def test_client_rejects_non_pubg_telemetry_host_before_request():
    client = PubgApiClient(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: pytest.fail(f"unexpected request to {request.url}")
            )
        )
    )

    with pytest.raises(ValueError, match="approved PUBG telemetry host"):
        client.get_telemetry("https://example.invalid/telemetry.json")


def test_client_retry_budget_is_bounded():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, request=request)

    sleeps: list[float] = []
    client = PubgApiClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=2,
        sleep=sleeps.append,
        jitter=lambda: 0.0,
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.get_match("steam", "match-1")
    assert attempts == 3
    assert sleeps == [0.25, 0.5]


def test_client_refuses_retry_after_larger_than_bounded_wait_budget():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, request=request, headers={"Retry-After": "120"})

    sleeps: list[float] = []
    client = PubgApiClient(
        api_key="key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )

    with pytest.raises(RuntimeError, match="Retry-After exceeds"):
        client.get_player("steam", "Player")
    assert attempts == 1
    assert sleeps == []


def test_map_telemetry_refuses_ambiguous_kill_actor():
    mapped = map_telemetry(
        [
            {
                "_T": "LogPlayerKillV2",
                "_D": "2026-10-06T12:00:10.000Z",
                "victim": _character("Victim"),
                "dBNOMaker": _character("Knocker"),
                "finisher": _character("Finisher"),
                "killer": _character("Killer"),
                "isSuicide": False,
            },
            {
                "_T": "LogPlayerMakeGroggy",
                "_D": "2026-10-06T12:00:11.000Z",
                "attacker": _character("Attacker"),
                "victim": _character("OtherVictim"),
            },
        ]
    )

    assert mapped[0].actor_candidates == []
    assert mapped[0].dBNO_maker == "Knocker"
    assert mapped[0].finisher == "Finisher"
    assert mapped[0].killer == "Killer"
    assert mapped[1].actor_candidates == ["Attacker"]
    assert mapped[1].event_type == EventType.KNOCK


def test_map_telemetry_resolves_kill_only_when_actor_fields_agree():
    mapped = map_telemetry(
        [
            {
                "_T": "LogPlayerKillV2",
                "_D": "2026-10-06T12:00:10Z",
                "victim": _character("Victim"),
                "dBNOMaker": _character("Actor"),
                "finisher": _character("Actor"),
                "killer": _character("Actor"),
                "isSuicide": False,
            }
        ]
    )

    assert mapped[0].actor_candidates == ["Actor"]


def test_map_telemetry_drops_knocks_out_of_the_viewers_feed_range():
    def at(name: str, team: int, x: float) -> dict:
        return {"name": name, "teamId": team, "location": {"x": x, "y": 0.0}}

    def knock(second: int, victim_x: float) -> dict:
        return {"_T": "LogPlayerMakeGroggy", "_D": f"2026-10-06T12:00:{second:02d}Z",
                "attacker": _character("A"), "victim": at(f"V{second}", 2, victim_x)}

    events = [
        {"_T": "LogPlayerPosition", "_D": "2026-10-06T12:00:00Z", "character": at("Me", 1, 0.0)},
        {"_T": "LogPlayerPosition", "_D": "2026-10-06T12:00:00Z",
         "character": at("Mate", 1, 300_000.0)},
        knock(10, 90_000.0),  # 900 m from Me
        knock(11, 200_000.0),  # 1,000 m from Mate
        knock(12, 150_000.0),  # 1,500 m from both
        {"_T": "LogPlayerKillV2", "_D": "2026-10-06T12:00:13Z", "victim": at("Far", 2, 150_000.0),
         "killer": _character("A"), "dBNOMaker": None, "finisher": _character("A")},
    ]

    assert [e.victim for e in map_telemetry(events, viewer="Me")] == ["V10", "V11", "Far"]
    assert len(map_telemetry(events)) == 4
    assert len(map_telemetry(events, viewer="Unknown")) == 4


def test_team_state_report_scores_the_board_against_telemetry_by_player_names():
    from dataclasses import replace

    from kfocr.telemetry.team_state import team_state_report

    session = _session(ended_at_utc=datetime(2026, 10, 6, 12, 2, 10, tzinfo=UTC))
    roster = [("Alpha", 1), ("Bravo", 1), ("Echo", 1), ("Fox", 1),
              ("Hunter", 2), ("Ivy", 2), ("Jade", 2), ("Kilo", 2)]
    raw = [{"_T": "LogMatchStart", "_D": "2026-10-06T12:00:00Z",
            "characters": [{"character": {"name": n, "teamId": t}} for n, t in roster]}]
    raw += [{"_T": "LogPlayerKillV2", "_D": f"2026-10-06T12:00:{s}Z", "killer": {"name": "Hunter"},
             "victim": {"name": v}} for s, v in ((10, "Alpha"), (20, "Bravo"), (30, "Echo"), (40, "Fox"))]
    # Badges 7/5 are on screen; the glued "7Alpha" still maps to Alpha. The board misses Fox.
    observed = [replace(_observed(session, s, "Hunter", v), killer_team=5, victim_team=7)
                for s, v in ((10, "7Alpha"), (20, "Bravo"), (30, "Echo"))]

    report = team_state_report(session, observed, raw, map_telemetry(raw), offset_seconds=0.0)

    half = {"dead": 0.5, "kills": 0.5, "eliminated": 0.5}
    assert report["summary"] == {
        "checkpoints": 3, "team_checks": 6, "vs_truth": half, "vs_oracle": half,
        "oracle_vs_truth": {"dead": 1.0, "kills": 1.0, "eliminated": 1.0},
        "teams_left_in_range": 0.0, "teams_left_unknown": 0,
        "oracle_teams_left_in_range": 1.0}
    last = report["checkpoints"][-1]
    # Teams up to the highest badge (7); badge 7 may be an underfilled wiped squad and two of the
    # never-seen badges may be lobby leavers.
    assert last["teams_left"] == [[4, 7], 1] and last["oracle_teams_left"] == [1, 1]
    assert last["mismatched_teams"] == [
        {"telemetry_team": 1, "board_badges": [7], "dead": [3, 4], "kills": [0, 0],
         "eliminated": [False, True], "oracle": [4, 0, True]},
        {"telemetry_team": 2, "board_badges": [5], "dead": [0, 0], "kills": [3, 4],
         "eliminated": [False, False], "oracle": [0, 4, False]}]


def test_team_state_counts_a_recalled_player_as_alive_again():
    from kfocr.telemetry.team_state import _timeline, _truth_at

    raw = [{"_T": "LogPlayerKillV2", "_D": "2026-10-06T12:00:10Z", "victim": {"name": "A"},
            "killer": {"name": "B"}},
           {"_T": "LogPlayerCreate", "_D": "2026-10-06T12:00:40Z", "character": {"name": "A"}}]
    timeline = _timeline(raw)
    status, *_ = _truth_at(timeline, set(), {"A": 1, "B": 2},
                           datetime(2026, 10, 6, 12, 0, 20, tzinfo=UTC))
    assert status["A"].value == "dead"
    status, *_ = _truth_at(timeline, set(), {"A": 1, "B": 2},
                           datetime(2026, 10, 6, 12, 0, 50, tzinfo=UTC))
    assert status["A"].value == "alive"


def test_map_telemetry_rejects_supported_event_with_malformed_timestamp():
    with pytest.raises(ValueError, match=r"index 0.*_D"):
        map_telemetry(
            [
                {
                    "_T": "LogPlayerMakeGroggy",
                    "_D": "not-a-timestamp",
                    "attacker": _character("A"),
                    "victim": _character("B"),
                }
            ]
        )


def test_find_match_uses_documented_included_participants_and_refuses_tie():
    session = _session()

    def match(match_id: str) -> dict:
        return {
            "data": {
                "type": "match",
                "id": match_id,
                "attributes": {"createdAt": "2026-10-06T12:00:00Z", "duration": 1800},
            },
            "included": [
                {"type": "participant", "attributes": {"stats": {"name": "PlayerOne"}}}
            ],
        }

    decision = find_match(session, [match("a"), match("b")], "PlayerOne")
    assert decision.status == "ambiguous"
    assert decision.match is None

    decision = find_match(session, [match("a")], "PlayerOne")
    assert decision.status == "matched"
    assert decision.match_id == "a"
    assert decision.confidence >= 0.9


def test_validate_events_uses_multiple_pairs_for_offset_and_one_to_one_assignment():
    session = _session()
    observed = [
        _observed(session, 10, "A", "B"),
        _observed(session, 20, "C", "D"),
        _observed(session, 30, "E", "F"),
        _observed(session, 30.1, "E", "F"),
    ]
    canonical = [
        _canonical(session, 15, "A", "B", raw_index=0),
        _canonical(session, 25, "C", "D", raw_index=1),
        _canonical(session, 95, "E", "F", raw_index=2),  # offset outlier
        _canonical(session, 35, "E", "F", raw_index=3),
    ]

    report = validate_events(session, observed, canonical)

    assert report["alignment"]["offset_sample_count"] == 4
    assert report["alignment"]["offset_inlier_count"] == 3
    assert report["alignment"]["offset_seconds"] == pytest.approx(5.0)
    assert report["event_metrics"] == {
        "tp": 3,
        "fp": 1,
        "fn": 1,
        "precision": pytest.approx(0.75),
        "recall": pytest.approx(0.75),
        "f1": pytest.approx(0.75),
    }
    telemetry_indexes = [match["telemetry_index"] for match in report["matches"]]
    assert len(telemetry_indexes) == len(set(telemetry_indexes))
    assert report["tracking"]["false_event_count"] == 1


def test_validate_events_reports_unresolved_and_confidence_buckets_json_safe():
    session = _session()
    observed = [_observed(session, 10, "A", "B", confidence=0.93)]
    canonical = [
        TelemetryCanonicalEvent(
            occurred_at_utc=session.started_at_utc + timedelta(seconds=10),
            event_type=EventType.KILL,
            actor_candidates=[],
            victim="B",
            dBNO_maker="A",
            finisher="C",
            killer="C",
            raw_type="LogPlayerKillV2",
            raw_index=7,
        )
    ]

    report = validate_events(session, observed, canonical)

    assert report["event_metrics"]["fp"] == 0
    assert report["event_metrics"]["fn"] == 0
    assert report["unresolved_semantics"][0]["telemetry_index"] == 7
    assert report["unresolved_observed"][0]["observed_index"] == 0
    assert report["evaluation_coverage"] == {
        "observed_evaluated": 0,
        "observed_total": 1,
        "observed_ratio": 0.0,
        "telemetry_evaluated": 0,
        "telemetry_total": 1,
        "telemetry_ratio": 0.0,
    }
    assert report["confidence_buckets"]["0.90-1.00"]["count"] == 1
    assert report["confidence_buckets"]["0.90-1.00"]["evaluated_count"] == 0
    assert report["tracking"]["merge_error_rate"]["available"] is False
    assert report["tracking"]["split_error_rate"]["available"] is False
    assert report["reproducibility"]["config_hash"] == "config-hash"

    import json

    json.dumps(report)


def test_validate_events_does_not_establish_offset_from_one_pair():
    session = _session()
    report = validate_events(
        session,
        [_observed(session, 10, "A", "B")],
        [_canonical(session, 16, "A", "B")],
    )

    assert report["alignment"] == {
        "offset_seconds": 0.0,
        "offset_sample_count": 1,
        "offset_inlier_count": 0,
        "time_tolerance_seconds": 5.0,
    }
    assert report["event_metrics"]["tp"] == 0


def test_validate_events_estimates_offset_from_multiple_unknown_type_pairs():
    session = _session()
    observed = [
        _observed(session, 10, "A", "B", event_type=EventType.UNKNOWN),
        _observed(session, 20, "C", "D", event_type=EventType.UNKNOWN),
    ]
    canonical = [
        _canonical(session, 16, "A", "B", EventType.KILL, raw_index=0),
        _canonical(session, 26, "C", "D", EventType.KILL, raw_index=1),
    ]

    report = validate_events(session, observed, canonical)

    assert report["alignment"]["offset_seconds"] == pytest.approx(6.0)
    assert report["alignment"]["offset_sample_count"] == 2
    assert report["alignment"]["offset_inlier_count"] == 2
    assert report["event_metrics"]["tp"] == 2
    assert report["field_accuracy"]["event_type"] == {
        "correct": 0,
        "total": 2,
        "accuracy": 0.0,
    }


def test_validate_events_excludes_telemetry_outside_shifted_session_interval():
    session = _session(ended_at_utc=datetime(2026, 10, 6, 12, 0, 30, tzinfo=UTC))
    observed = [_observed(session, 10, "A", "B")]
    canonical = [
        _canonical(session, 10, "A", "B", raw_index=0),
        _canonical(session, 600, "C", "D", raw_index=1),
    ]

    report = validate_events(session, observed, canonical)

    assert report["event_metrics"]["recall"] == 1.0
    assert report["sample_counts"]["telemetry"] == 2
    assert report["sample_counts"]["temporal_telemetry"] == 1
    assert report["sample_counts"]["excluded_outside_session"] == 1
    assert report["temporal_coverage"]["included"] == 1
    assert report["temporal_coverage"]["excluded"] == 1
    assert report["temporal_coverage"]["excluded_telemetry"][0]["telemetry_index"] == 1
    assert report["evaluation_coverage"]["telemetry_ratio"] == 0.5


def test_validate_events_requires_known_session_end():
    with pytest.raises(ValueError, match="end time"):
        validate_events(_session(ended_at_utc=None), [], [])


def test_validate_events_does_not_match_known_incompatible_event_types():
    session = _session()
    observed = [_observed(session, 10, "A", "B", event_type=EventType.KNOCK)]
    canonical = [_canonical(session, 10, "A", "B", event_type=EventType.KILL)]

    report = validate_events(session, observed, canonical)

    assert report["event_metrics"]["tp"] == 0
    assert report["event_metrics"]["fp"] == 1
    assert report["event_metrics"]["fn"] == 1
