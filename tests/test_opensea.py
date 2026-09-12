import autopilot


class FakeReply:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def test_top_offer_picks_the_best_weth_offer(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers)
        return FakeReply({"offers": [
            {"price": {"currency": "WETH", "decimals": 18, "value": "69300000000000000"}},
            {"price": {"currency": "WETH", "decimals": 18, "value": "70100000000000000"}},
            {"price": {"currency": "USDC", "decimals": 6, "value": "999999999999"}},
        ]})

    monkeypatch.setattr(autopilot.requests, "get", fake_get)
    assert autopilot.fetch_top_offer_eth("k") == 0.0701
    assert seen["url"].endswith("/offers/collection/hash-cats")
    assert seen["headers"]["x-api-key"] == "k"


def test_top_offer_is_zero_when_none_listed(monkeypatch):
    monkeypatch.setattr(autopilot.requests, "get", lambda *a, **k: FakeReply({"offers": []}))
    assert autopilot.fetch_top_offer_eth("k") == 0.0
