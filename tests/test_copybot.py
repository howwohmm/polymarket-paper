"""Offline tests for copybot.py — the Polymarket HTTP layer is mocked throughout.

The whole suite runs with no network and no API keys. DBs are created in tmp_path and
`copybot.DB` is re-pointed per test so no state leaks between tests.
"""
import json
import os
import sys

import pytest

import copybot


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Give every test its own empty DB in tmp_path and a clean CWD (docs/ writes
    must not touch the real repo). Restore DB/_endcache-related module globals."""
    monkeypatch.setattr(copybot, "DB", tmp_path / "copybot.db")
    # Only a couple of synthetic wallets so tick/resolve loops stay small/fast.
    monkeypatch.setattr(copybot, "WALLETS",
                        {"Alice": "0xaaaa", "Bob": "0xbbbb"})
    monkeypatch.chdir(tmp_path)
    yield
    copybot.E_SKIP_COUNTS.clear()


def _conn():
    return copybot.db()


# ---------------------------------------------------------------- categorize_market
@pytest.mark.parametrize("title,expected", [
    ("Will Bitcoin be up or down in 5m?", "crypto_5m"),
    ("Bitcoin up or down next 5 minutes", "crypto_5m"),
    ("Will ETH be above $4000 this hour?", "crypto_h"),
    ("Will Solana close green today?", "other"),
    ("NBA: will the Lakers win game 3?", "sports"),
    ("Next goal in the World Cup final?", "sports"),
    ("Who will win the 2028 US election?", "politics"),
    ("Will Trump win the presidency?", "politics"),
    ("Will T1 win the League of Legends Worlds?", "esports"),
    ("esports: which team wins?", "esports"),
    ("Will Real Madrid win the Champions League?", "sports"),
    ("Who wins the Premier League this season?", "sports"),
    ("Fetch a glass of water?", "other"),
])
def test_categorize_market(title, expected):
    assert copybot.categorize_market(title) == expected


@pytest.mark.parametrize("title,is_fast", [
    ("Bitcoin up or down 5m", True),
    ("Who wins next goal?", True),
    ("This drive result", True),
    ("Will the stock go up this quarter?", False),
    ("NBA winner", False),
])
def test_is_fast_market(title, is_fast):
    assert copybot._is_fast_market(title) == is_fast


# ---------------------------------------------------------------- _chart_entry_ok
def test_chart_entry_ok_passes_good_chart():
    sig = {"rsi": 55, "mom_30m": 0.01, "trend_ph": 0.02,
           "vol_ratio": 1.5, "buy_pressure": 0.62}
    ok, reason = copybot._chart_entry_ok(sig, 0.5)
    assert ok is True
    assert reason == ""


def test_chart_entry_ok_skips_overbought():
    ok, reason = copybot._chart_entry_ok({"rsi": 90, "mom_30m": 0, "trend_ph": 0}, 0.5)
    assert ok is False and "overbought" in reason


def test_chart_entry_ok_skips_chasing():
    ok, reason = copybot._chart_entry_ok({"rsi": 40, "mom_30m": 0.2, "trend_ph": 0.01}, 0.5)
    assert ok is False and "chasing" in reason


def test_chart_entry_ok_skips_downtrend():
    ok, reason = copybot._chart_entry_ok({"rsi": 40, "mom_30m": -0.1, "trend_ph": -0.08}, 0.5)
    assert ok is False and "downtrend" in reason


def test_chart_entry_ok_skips_vol_drying():
    sig = {"rsi": 55, "mom_30m": 0.0, "trend_ph": 0.0,
           "vol_ratio": 0.2, "buy_pressure": 0.30}
    ok, reason = copybot._chart_entry_ok(sig, 0.5)
    assert ok is False and "vol_drying" in reason


def test_chart_entry_ok_none_sig_passes():
    assert copybot._chart_entry_ok(None, 0.5) == (True, "")


# ---------------------------------------------------------------- chart_signal
class _FakeGet:
    """Dispatches copybot.get() by URL fragment for chart_signal tests."""

    def __init__(self, history, trades=None):
        self.history = history        # list of {"p": ...}
        self.trades = trades          # list of trade dicts or None

    def __call__(self, url):
        if "prices-history" in url:
            return {"history": self.history}
        if "trades" in url:
            return self.trades or []
        return {}


def test_chart_signal_computes_indicators(monkeypatch):
    # 40 minutes of steady 1-min bars: 0.50 -> 0.60 (uptrend, rising RSI)
    hist = [{"p": 0.50 + 0.0025 * i} for i in range(40)]
    import time as _t
    now = int(_t.time())
    trades = [
        {"size": "4", "price": "0.50", "timestamp": str(now - 120), "side": "BUY"},
        {"size": "6", "price": "0.52", "timestamp": str(now - 60), "side": "SELL"},
    ]
    monkeypatch.setattr(copybot, "get", _FakeGet(hist, trades))
    sig = copybot.chart_signal("tok123")
    assert sig is not None
    assert sig["bars"] == 40
    assert sig["rsi"] > 50            # steady uptrend -> RSI high
    assert sig["trend_ph"] > 0        # positive slope
    assert sig["mom_30m"] > 0
    assert sig["support"] < sig["resistance"]
    assert sig["current"] > sig["support"]
    assert sig["vol_1h"] == pytest.approx(5.0)          # round(5.12, 0)
    assert sig["buy_pressure"] == pytest.approx(2.0 / (2.0 + 3.12), abs=0.05)
    assert sig["vwap"] is not None


def test_chart_signal_too_short_returns_none(monkeypatch):
    monkeypatch.setattr(copybot, "get", _FakeGet([{"p": 0.5}] * 10))
    assert copybot.chart_signal("tok") is None


# ---------------------------------------------------------------- atomic write
def test_atomic_write_json_preserves_utf8_and_no_tmp(tmp_path):
    p = tmp_path / "docs" / "data.json"
    copybot._atomic_write_json(str(p), {"title": "Barcelona vs Real Madrid — 5m", "v": 1.5})
    data = json.load(open(p, encoding="utf-8"))
    assert data["title"] == "Barcelona vs Real Madrid — 5m"
    assert not list(p.parent.glob("*.tmp"))   # temp got replaced


# ---------------------------------------------------------------- bankroll
def test_cash_lazy_init_and_credit():
    c = _conn()
    assert copybot._cash(c, "hold", "Alice") == pytest.approx(copybot.BANKROLL_PER_MODEL)
    copybot._credit(c, "hold", "Alice", -copybot.STAKE)
    assert copybot._cash(c, "hold", "Alice") == pytest.approx(
        copybot.BANKROLL_PER_MODEL - copybot.STAKE)


# ---------------------------------------------------------------- tick
class _TickMocks:
    """Marshals copybot.get() activity+market lookups and a fake chart_signal."""

    def __init__(self, activity, market_by_cid, chart_sig=None):
        self.activity = activity          # list per wallet, in order
        self.markets = market_by_cid      # {cid: gamma dict}
        self.chart_sig = chart_sig        # fake return, or real function

    def fake_get(self, url):
        if "activity?user=" in url:
            for addr, acts in self.activity:
                if f"user={addr}" in url:
                    return acts
            return []
        if "markets?condition_ids=" in url:
            cid = url.split("condition_ids=")[1]
            return [self.markets[cid]] if cid in self.markets else []
        return []


def _mk_trade(asset, cid, ts, px=0.5, oidx=0, title="Will X happen?", side="BUY"):
    return {"type": "TRADE", "side": side, "asset": asset, "conditionId": cid,
            "outcomeIndex": oidx, "outcome": "Yes", "timestamp": str(ts),
            "price": str(px), "title": title, "transactionHash": f"0x{asset}{ts}"}


def _mk_market(cid, end_days_from_now, prices=(0.5, 0.5), closed=False):
    end = copybot.datetime.now(copybot.timezone.utc) + copybot.timedelta(days=end_days_from_now)
    return {"conditionId": cid, "outcomePrices": json.dumps(list(prices)),
            "closed": closed, "endDate": end.isoformat().replace("+00:00", "Z")}


def test_tick_inserts_and_dedups(monkeypatch, tmp_path):
    now = int(__import__("time").time())
    cid = "cid1"
    asset = "asset1"
    trade = _mk_trade(asset, cid, now - 60, px=0.5, title="Will ETH be up this hour?")
    mocks = _TickMocks([("0xaaaa", [trade])], {cid: _mk_market(cid, 1)})
    monkeypatch.setattr(copybot, "get", mocks.fake_get)
    monkeypatch.setattr(copybot, "chart_signal", lambda _a: None)   # no chart filtering

    copybot.tick()
    c = _conn()
    rows = c.execute("SELECT count(*) FROM copies").fetchone()[0]
    # models: hold(1d<3d ok), exit, cdecide, trail, chart => 5 copies for 1 trade
    assert rows == 5

    # re-running tick must NOT duplicate (dedup by key), even though cash now locked
    copybot.tick()
    assert c.execute("SELECT count(*) FROM copies").fetchone()[0] == 5

    # chart model should not have been filtered (sig None -> pass)
    assert not (tmp_path / "docs" / "e_skips.json").exists()


def test_tick_skips_hold_model_outside_resolve_window(monkeypatch):
    now = int(__import__("time").time())
    # far-out market (10d) => hold model skipped; only exit/cdecide/trail/chart remain
    cid = "cidA"
    trade = _mk_trade("a1", cid, now - 30, title="Will the Eagles win the NFC title?")
    mocks = _TickMocks([("0xaaaa", [trade])], {cid: _mk_market(cid, 10)})
    monkeypatch.setattr(copybot, "get", mocks.fake_get)
    monkeypatch.setattr(copybot, "chart_signal", lambda _a: None)
    copybot.tick()
    c = _conn()
    models = {r[0] for r in c.execute("SELECT DISTINCT model FROM copies")}
    assert "hold" not in models
    assert {"exit", "cdecide", "trail", "chart"} <= models


def test_tick_cash_gate_stops_when_wallet_exhausted(monkeypatch):
    """Exercise the `_cash < STAKE` guard: each trader's $20 wallet funds exactly 10
    $2 copies per model, so the 11th trade in a model must be dropped."""
    now = int(__import__("time").time())
    cid = "cg"
    # 11 trades in ONE market (all resolve within hold window) -> hold model included
    trades = [_mk_trade(f"asset{i}", cid, now - 30 * (i + 1),
                        title="Will the Eagles win the NFC title?") for i in range(11)]
    mocks = _TickMocks([("0xaaaa", trades)], {cid: _mk_market(cid, 1)})
    monkeypatch.setattr(copybot, "get", mocks.fake_get)
    monkeypatch.setattr(copybot, "chart_signal", lambda _a: None)
    copybot.tick()
    c = _conn()
    # Wallet funds 10 copies per model (hold/exit/cdecide/trail/chart) = 50 max,
    # but each of the 11 trades would also want to open 5 => cash gate drops the rest.
    # No key reuse (unique tx+ts per trade), so the cap is pure cash: <= 5*10 = 50.
    rows = c.execute("SELECT count(*) FROM copies").fetchone()[0]
    assert rows == 50  # 5 models x 10 funded copies; the 11th trade's copies all cash-gated
    # By model no model may exceed 10 copies (the $20 / $2 cap).
    per_model = dict(c.execute("SELECT model, count(*) FROM copies GROUP BY model"))
    assert all(v <= 10 for v in per_model.values())
    assert max(per_model.values()) == 10


def test_tick_counts_and_persists_e_skips(monkeypatch, tmp_path):
    now = int(__import__("time").time())
    cid = "cidE"
    trade = _mk_trade("assetE", cid, now - 20, px=0.5, title="Will ETH rise this hour?")
    mocks = _TickMocks([("0xaaaa", [trade])], {cid: _mk_market(cid, 1)})
    monkeypatch.setattr(copybot, "get", mocks.fake_get)
    # chart filter rejects: overbought RSI
    monkeypatch.setattr(copybot, "chart_signal",
                        lambda _a: {"rsi": 92, "mom_30m": 0.0, "trend_ph": 0.0,
                                    "vol_ratio": 1.0, "buy_pressure": 0.5})
    copybot.tick()
    c = _conn()
    models = {r[0] for r in c.execute("SELECT DISTINCT model FROM copies")}
    assert "chart" not in models          # the E copy was filtered out
    assert copybot.E_SKIP_COUNTS.get("overbought_rsi(92)") == 1
    eskips = json.load(open(tmp_path / "docs" / "e_skips.json", encoding="utf-8"))
    assert eskips["skipped"] == 1
    assert eskips["reasons"]["overbought_rsi(92)"] == 1


# ---------------------------------------------------------------- resolve
def _seed_copy(c, model, cid="cidR", oidx=0, entry=0.5, cur=None, set_prices=None,
               peak=None, status="open", age_h=1, title="Will X happen?"):
    now_ts = copybot.datetime.now(copybot.timezone.utc).timestamp()
    copied_ts = copybot.datetime.utcfromtimestamp(now_ts - age_h * 3600).isoformat() + "Z"
    shares = copybot.STAKE / entry
    c.execute("""INSERT INTO copies(key,copied_ts,wallet,their_ts,condition_id,asset,title,
                outcome,outcome_index,their_price,entry,stake,shares,status,model,peak)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (f"k:{cid}:{model}", copied_ts, "Alice", int(now_ts), cid, f"asset_{cid}",
               title, "Yes", oidx, entry, entry, copybot.STAKE, shares, status, model, peak))
    c.commit()


def test_resolve_settles_won_and_credits_wallet(monkeypatch):
    c = _conn()
    copybot._cash(c, "hold", "Alice")      # tick lazy-inits the bankroll row before copying
    _seed_copy(c, "hold", cid="c1", status="open")
    mkt = _mk_market("c1", 1, prices=(1.0, 0.0), closed=True)
    monkeypatch.setattr(copybot, "_market_by_condition", lambda cid: mkt)
    copybot.resolve()
    c = _conn()
    row = c.execute("SELECT status,pnl FROM copies").fetchone()
    assert row[0] == "won"
    # shares = 2/0.5 = 4; won payout = 4; pnl = 4 - 2 = 2
    assert row[1] == pytest.approx(2.0)
    # the seeded copy never locked its $2 stake, so a $20 wallet + $4 payout = 24
    assert copybot._cash(c, "hold", "Alice") == pytest.approx(24.0)


def test_resolve_model_b_take_profit(monkeypatch):
    c = _conn()
    _seed_copy(c, "exit", cid="c2", entry=0.5, age_h=1)
    # entry 0.5, cur 0.62 -> gain 0.24 >= 0.15 => exit_won
    mkt = _mk_market("c2", 1, prices=(0.62, 0.38), closed=False)
    monkeypatch.setattr(copybot, "_market_by_condition", lambda cid: mkt)
    copybot.resolve()
    c = _conn()
    row = c.execute("SELECT status,pnl FROM copies").fetchone()
    assert row[0] == "exit_won"
    # shares=4, cur=0.62 -> pnl = 4*0.62 - 2 = 2.48-2 = 0.48
    assert row[1] == pytest.approx(0.48)


def test_resolve_model_d_stop_loss(monkeypatch):
    c = _conn()
    _seed_copy(c, "trail", cid="c3", entry=0.5, age_h=1)
    mkt = _mk_market("c3", 1, prices=(0.32, 0.68), closed=False)   # -36% => stop
    monkeypatch.setattr(copybot, "_market_by_condition", lambda cid: mkt)
    copybot.resolve()
    assert c.execute("SELECT status FROM copies").fetchone()[0] == "d_stop"


def test_resolve_model_d_trailing_stop(monkeypatch):
    c = _conn()
    # peak already armed at 0.70 (>=0.5*1.25), cur falls to 0.55 (<=0.70*0.8=0.56)
    _seed_copy(c, "trail", cid="c4", entry=0.5, peak=0.70, age_h=1)
    mkt = _mk_market("c4", 1, prices=(0.55, 0.45), closed=False)
    monkeypatch.setattr(copybot, "_market_by_condition", lambda cid: mkt)
    copybot.resolve()
    assert c.execute("SELECT status FROM copies").fetchone()[0] == "d_trail"


# ---------------------------------------------------------------- report
def test_report_includes_all_five_models(tmp_path, monkeypatch, capsys):
    for m in ("hold", "exit", "cdecide", "trail", "chart"):
        _seed_copy(_conn(), m, cid=f"r{m}")
    monkeypatch.chdir(tmp_path)
    copybot.report()
    out = capsys.readouterr().out
    for lbl in ("MODEL A", "MODEL B", "MODEL C", "MODEL D", "MODEL E"):
        assert lbl in out


# ---------------------------------------------------------------- export
def test_export_writes_dashboard_and_quality(monkeypatch, tmp_path):
    c = _conn()
    _seed_copy(c, "hold", cid="e1", status="won")
    # also give Bob a cdecide decision so decisions feed has content
    c.execute("INSERT INTO decisions(ts,copy_id,wallet,title,entry,current,gain,decision,why) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (copybot.now_iso(), 1, "Alice", "Will X?", 0.5, 0.6, 0.2, "HOLD", "riding"))
    c.commit()
    # write an e_skips payload for tick's skip history (would normally come from tick)
    copybot._atomic_write_json("docs/e_skips.json",
                               {"updated": copybot.now_iso(), "skipped": 3,
                                "reasons": {"overbought_rsi(92)": 2, "chasing_30m": 1}})
    monkeypatch.chdir(tmp_path)

    copybot.export()
    data = json.load(open(tmp_path / "docs" / "data.json", encoding="utf-8"))
    assert {"models", "traders", "decisions", "meta"} <= set(data.keys())
    assert {"A", "B", "C", "D", "E"} == set(data["models"].keys())
    assert data["meta"]["e_skips"]["overbought_rsi(92)"] == 2
    assert data["traders"] and data["traders"][0]["A"]["value"] > 0

    quality = json.load(open(tmp_path / "docs" / "quality.json", encoding="utf-8"))
    cats = {k for t in quality["quality"] for k in t.keys()}
    assert "esports" in cats            # category that categorize_market can return
