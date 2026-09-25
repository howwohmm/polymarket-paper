"""Offline tests for copytrade.py — mocks the Polymarket HTTP layer."""
import pytest

import copytrade


class _FakeGet:
    """Routes copytrade.get() by URL fragment."""

    def __init__(self, mapping):
        self.mapping = mapping   # {fragment: result}

    def __call__(self, url):
        for frag, result in self.mapping.items():
            if frag in url:
                return result
        pytest.fail(f"unexpected URL: {url}")


def test_leaderboard_survives_missing_proxy_key(capsys):
    # Regression: rows without a 'proxyWallet' key used to crash with KeyError,
    # even though the table had already printed.
    rows = [
        {"name": "alpha", "amount": 12000},
        {"amount": 500},                                   # no name, no proxyWallet
        {"pseudonym": "beta", "amount": 300, "proxyWallet": "0x123"},
    ]
    # call the wrapped function with the fake get
    orig_get = copytrade.get
    copytrade.get = _FakeGet({"/profit?": rows})
    try:
        out_rows = copytrade.leaderboard("30d")
    finally:
        copytrade.get = orig_get
    assert out_rows is rows
    out = capsys.readouterr().out
    assert "wallets:" in out


def test_price_at_picks_nearest_timestamp():
    hist = [{"t": 100, "p": 0.5}, {"t": 200, "p": 0.6}, {"t": 300, "p": 0.7}]
    assert copytrade.price_at(hist, 105) == 0.5
    assert copytrade.price_at(hist, 250) == 0.6
    assert copytrade.price_at(hist, 299) == 0.7


def test_copytest_full_flow(capsys):
    # One resolved market (won at settle=1.0). trader buys at 0.5 long ago.
    trades = [{"side": "BUY", "asset": "a1", "timestamp": 1000, "price": "0.5"}]
    mapping = {
        "trades?user=": trades,
        "fidelity=60": {"history": [{"p": 1.0, "t": 1e9}]},     # settlement tail
        "fidelity=1":  {"history": [{"p": 0.5, "t": 1020},      # lag window elsewhere
                                    {"p": 0.55, "t": 1600}],
                        },
    }

    orig_get = copytrade.get
    copytrade.get = _FakeGet(mapping)
    try:
        copytrade.copytest("0xwallet", lag_min=10, max_trades=80)
    finally:
        copytrade.get = orig_get
    out = capsys.readouterr().out
    assert "your win rate" in out
    # trader ROI = 1/0.5 - 1 = +100%; your ROI = 1/0.5(entry at lag, no move) - 1 = +100%
    assert "COPYABLE" in out
