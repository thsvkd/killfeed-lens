# Board snapshot contract (v1)

`kfocr engine` (src/kfocr/ui/engine.py) owns all match state (`kfocr.ui.teams.Ledger`) and the
capture. The Electron app only draws what it is sent and sends back a few commands.

## Transport

- **engine → UI**: stdout, UTF-8, one JSON object per line (`\n`). A full snapshot goes out
  **whenever anything on the board changes**, right away; events that arrive together go out as
  one. Lines that are not JSON or lack `"v": 1` are ignored, so stdout must carry nothing else
  (logs go to stderr and `<data>/logs/killfeed-lens.log`).
- **UI → engine**: stdin, the same JSON-lines format. **stdin EOF = shut down cleanly** (capture
  stops and its session is closed). The UI waits 20 s (the engine's own capture stop waits up to
  15 s), then kills the engine's process tree.
- Full snapshots, not diffs: about 6.5 KB with 25 teams and 20 events. A UI that starts late or
  restarts needs only the latest line.

## Time

`now` and every `at` / `back` / `last_at` value are the engine's `time.monotonic()` in **ms**, and
`0` means "never". The UI keeps its own offset to the engine clock, so ages keep counting between
snapshots. No text carries a live age (the engine strips Ledger.basis' "(N초 전 관측)"): text that
changes every second would make the engine emit every second.

## Snapshot

```jsonc
{
  "v": 1, "seq": 42, "now": 812345,           // seq: +1 per emitted snapshot
  "mode": "live",                             // "live" (screen capture) | "demo" (kfocr engine --demo)
  "monitor": 0,                               // capture.monitor; ROIs below are normalized to it
  "tracking": true,                           // false = capture stopped by the user
  "status": "캡처 중",                         // shown as is (capture health, ROI needed, paused...)
  "error": false,                             // true = status is an error (drawn as a warning)
  "match_over": false,                        // true from "match ended" (HUD gone 45 s) until the
                                              //   next match or a capture restart: overlay hides
  "players_left": 61, "teams_left": 18,       // Ledger.players_left / teams_left
  "teams_left_min": 15,                       // Ledger.teams_left_min: the truth most likely lies
                                              //   in [min, max] (with a HUD count the max is a
                                              //   likelihood, not a bound)
  "start_teams": 25, "squad": 4,              // given start teams, else the highest confirmed badge
                                              //   (what teams_left counts down from); null = unknown
  "hud": {"count": 61, "at": 811900},         // last HUD alive count; null = never read
  "basis": "시작 25팀 x 4명 가정",              // Ledger.basis without the live age
  "kills": 34, "knocked": 3,                  // total_kills, knocked_now
  "last_at": 812100,                          // when the newest event was applied
  "placed": 41,                               // Ledger.placed: players the feed shows alive or
                                              //   knocked in badged teams still in (> hud.count:
                                              //   the feed missed deaths)
  "teams": [                                  // Ledger.ranked() order (the overlay relies on it):
                                              //   fewest never-seen slots first, then maybe_out
                                              //   teams, the "??" bucket, eliminated teams last
    {"id": 12,                                // badge; null = "??" bucket for unreadable badges
     "kills": 3, "knocks": 2,                 // kills credited, knocks dealt
     "out": false, "left": 3,                 // eliminated, standing marks drawn (maybe_out: not seen dead)
     "maybe_out": false,                      // every seen member down and none knocked after the
                                              //   last death seen (a knock means a teammate stands),
                                              //   fewer than a squad seen: may be wiped
     "slots": 4,                              // Team.total; UI pads players with never-seen slots
     "assumed": 2,                            // Ledger.draw: how many of them to draw assumed alive
                                              //   (the HUD count has room for), the rest "?" unsure
     "unseen": "assumed",                     // older UIs: all of them "assumed" | "unsure"
     "at": 812100,                            // Team.touched (change highlight)
     "atk": 812100, "hit": 0,                 // last kill/knock it dealt / took (UI: blue / red)
     "players": [{"name": "SnowFox", "st": "alive", "at": 812100, "back": 0,
                  "sure": true, "downs": 0, "knock_seconds": 90}]}
                                              // st: alive | knocked | dead | unsure (Ledger.draw:
                                              //   the HUD count, which counts knocked players, has
                                              //   no room for it; a missed death or bleed-out);
                                              //   back: last observed comeback (not expiry)
                                              //   sure: false after a knock expires without a death
                                              //   downs: observed knocks taken this life; recall resets,
                                              //     ordinary revive keeps it (earlier knocks may be missed)
                                              //   knock_seconds: per-stack inference threshold:
                                              //     90,30,30,10,7,5,4,3,2 (9+); based on update 15.2.
                                              //     NOT a death bound: update 23.2 changed stationary
                                              //     bleeding; the feed does not expose movement.
  ],
  "events": [                                 // newest first, at most 20 (Ledger.recent)
    {"kind": "kill",                          // EventType value
     "clock": "03:12",                        // feed time since the first event
     "killer": "SnowFox", "kt": 12,           // kt/vt: Ledger.team_of() (badge resolved by name)
     "victim": "MadWolf", "vt": 7,
     "weapon": "M416", "hs": true}            // may be null
  ],
  "rois": {"killfeed": [0.68, 0.0, 0.31, 0.2],// normalized x, y, w, h; null = not set yet
           "alive": [0.92, 0.01, 0.04, 0.04]},// (live + killfeed null = first run: ROI guide)
  "settings": {"save_event_crops": false}     // debug.save_event_crops of <data>/config.yaml; never null
}
```

`teams_left_min`, `maybe_out`, `unseen`, `assumed`, `atk`, `hit`, `placed` and `match_over` are additive in
v1; a UI that does not know them shows the plain `teams_left` and draws never-seen slots by
`maybe_out`. The UI shows "min~max" while they differ, `0~left` remaining for a `maybe_out` team
(with "전멸 가능"), and never-seen slots as `assumed` says. With a HUD count the board draws no more
alive marks than it shows: knocked players first, then players by how recently the feed showed them
standing, then never-seen slots of the teams seen standing, less the players a recall brought back
that the feed has not shown yet; a team with no room left at all is sent `maybe_out`. The range comes from it
(Ledger.teams_left_min: at least count / squad teams; teams_left: the teams seen standing plus as
many others as the count's unplaced players can make, after they fill 90% of those teams'
never-seen slots), not from the teams the feed shows standing alone, since some of those are deaths
the reader missed. While `start_teams` is null, `teams_left` and (without `hud`)
`players_left` count only the teams seen so far, so the UI shows "—" instead.

What the UI derives by itself: players/teams labels (화면 / 추정 / 마지막 관측 after 5 s without a
HUD reading), the HUD age, coarse "방금 / N초 전" labels, change highlights (4 s after `at`), the 2 px
gauge (`players_left / (start_teams * squad)`), whether the overlay has anything to show (any
team, event or HUD reading, like `gui.overlay_has_content`, and not `match_over`), the highlight
colour (blue when `atk` is the newest change, red for `hit`, yellow otherwise), the board's sort
and filter (kept per viewer), and its feed-confirmed / missed-death note (`placed` vs a fresh
`hud.count`). The legacy Tk app (`kfocr gui`) does not follow `match_over` or `unseen`.

## Commands (UI → engine)

```jsonc
{"cmd": "tracking", "on": false}             // stop / restart capture; the board is kept
{"cmd": "calibrate"}                         // the UI opened the ROI editor: pause capture
{"cmd": "roi", "killfeed": [x, y, w, h],     // editor confirmed: save <data>/roi.yaml (save_roi_profile)
 "alive": [x, y, w, h] | null,               //   null = no alive-count ROI
 "screen": [2560, 1440]}                     //   the monitor in physical pixels (profile resolution)
{"cmd": "cancel"}                            // editor cancelled: resume with the old ROIs
{"cmd": "settings", "save_event_crops": true}// merge debug.save_event_crops into <data>/config.yaml
```

After `roi` or `cancel` capture resumes if tracking is on. An invalid or unsavable `roi` keeps the
old ROIs and shows `ROI를 저장하지 않았습니다: …` as an error status. The demo keeps ROIs in memory
only. `settings` keeps every other key of an existing `config.yaml` (comments are lost), creates
the file if missing and restarts capture like `roi`; a non-boolean value or an unreadable
`config.yaml` is ignored with `설정을 저장하지 않았습니다: …` as an error status. The demo keeps
settings in memory only. Unknown commands and non-JSON lines are ignored.
