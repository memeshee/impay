"""The deploy's one irreplaceable artifact: the static ERC-8004 card.

`/agent.json` is the URL written into the on-chain registration, and the reason it
became a static file is that a quick-tunnel reconnect answers 530 for a few
seconds. That makes the build step load-bearing, so it gets tests: freeze exactly
what the agent serves, fall back to the in-process card when the origin is
unreachable, and refuse to ship anything that is not a card.
"""

import io
import json

import pytest

from scripts import build_site


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return None


def test_freezes_the_live_card(monkeypatch):
    live = {"name": "impay", "registrations": [{"agentId": "10005"}]}
    seen = {}

    def fake_urlopen(url, timeout=0):
        seen["url"] = url
        seen["timeout"] = timeout
        return _Resp(json.dumps(live).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    card = build_site.agent_card("https://celo.example")
    assert card == live
    assert seen["url"] == "https://celo.example/agent.json"
    assert seen["timeout"] == 10  # a deploy must not hang on a dead origin


def test_falls_back_to_the_in_process_card_when_the_origin_is_down(monkeypatch):
    def boom(*a, **k):
        raise OSError("tunnel down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    card = build_site.agent_card("https://celo.example")
    # Whatever the app serves is what a judge must see; assert the invariants the
    # on-chain registration depends on rather than the whole document.
    assert card["name"] == "impay"
    assert card["registrations"][0]["agentId"] == "10005"
    assert card["type"].startswith("https://eips.ethereum.org/EIPS/eip-8004")


def test_refuses_to_ship_a_broken_card(monkeypatch):
    def boom(*a, **k):
        raise OSError("tunnel down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setattr("bot.serve.agent_card", lambda: {"name": "impay"})
    with pytest.raises(SystemExit):
        build_site.agent_card("https://celo.example")


def test_vercel_json_serves_the_card_from_the_filesystem():
    cfg = build_site.vercel_json("https://origin.example")
    sources = [r["source"] for r in cfg["rewrites"]]
    # Vercel applies the filesystem before these rewrites, so the static agent.json
    # wins and the proxy entry is only the fallback for a checkout without a build.
    assert "/agent.json" in sources
    assert "/proof" in sources
    header = {h["source"]: h["headers"] for h in cfg["headers"]}
    cache = {x["key"]: x["value"] for x in header["/agent.json"]}
    # A republished card must not stay cached for the whole judging window.
    assert "max-age=300" in cache["Cache-Control"]
