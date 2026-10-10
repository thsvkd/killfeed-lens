from __future__ import annotations

import httpx

from kfocr.telemetry.client import PubgApiClient
from kfocr.telemetry.mapping import map_telemetry


def test_mock_api_match_asset_to_canonical_telemetry_without_auth_leak():
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("Authorization")))
        if request.url.host == "api.pubg.com":
            return httpx.Response(
                200,
                json={
                    "data": {"id": "match-1", "relationships": {"assets": {"data": [{"id": "asset-1"}]}}},
                    "included": [
                        {
                            "type": "asset",
                            "id": "asset-1",
                            "attributes": {"URL": "https://telemetry-cdn.pubg.com/t.json"},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "_T": "LogPlayerMakeGroggy",
                    "_D": "2026-10-06T12:00:10Z",
                    "attacker": {"name": "A"},
                    "victim": {"name": "B"},
                }
            ],
        )

    client = PubgApiClient(
        api_key="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    match = client.get_match("steam", "match-1")
    telemetry_url = next(
        item["attributes"]["URL"]
        for item in match["included"]
        if item["id"] == match["data"]["relationships"]["assets"]["data"][0]["id"]
    )
    canonical = map_telemetry(client.get_telemetry(telemetry_url))

    assert canonical[0].actor_candidates == ["A"]
    assert seen == [("api.pubg.com", None), ("telemetry-cdn.pubg.com", None)]
