#!/usr/bin/env python3
"""
Polymarket COPYBOT — paper forward-test of mirroring vetted traders.

We can't backtest these traders (their bets are still open), so this forward-tests:
every time a tracked wallet BUYs, we record a paper copy at a fixed $10 notional at the
price available, then track it. As markets resolve we book realized P&L; open positions
are marked to current price. ROI (bankroll-independent) is the metric that matters.

Self-healing/idempotent: each tick re-reads recent trades and only inserts ones we
haven't copied yet (deduped by tx+asset), so missed/throttled runs lose nothing.

Tracked wallets were vetted for copyability: small clip ($5-15, a $10 copy can ride),
taker (not a maker/rebate farmer), diversified, slow/lag-tolerant markets.

Commands:
  python3 copybot.py tick      # detect tracked wallets' new buys, record paper copies
  python3 copybot.py resolve   # settle resolved copies; mark open ones to current price
  python3 copybot.py report    # realized + unrealized copy P&L, per wallet and total
"""
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DB = Path(__file__).parent / "copybot.db"
STAKE = 10.0

# vetted copyable traders (small clip, taker, diversified, slow markets)
WALLETS = {
    "GoalLineGhost": "0x0346afae2603313d2bbee96b628536c8cbe352a5",
    "alwayslate":    "0xb687f00464e33934f5d591f224e71c3559ecaee5",
    "mooseborzoi":   "0x84cfffc3f16dcc353094de30d4a45226eccd2f63",
    "afkpnl":        "0x55eca3687ea7d69632ffe0f297ea3d5158bb8c7d",
    "RN1":           "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
    "domahhh":       "0x9d84ce0306f8551e02efef1680475fc0f1dc1344",
    "aenews2":       "0x44c1dfe43260c94ed4f1d00de2e1f80fb113ebc1",
}


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS copies(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        key TEXT UNIQUE, copied_ts TEXT, wallet TEXT, their_ts INTEGER,
        condition_id TEXT, asset TEXT, title TEXT, outcome TEXT, outcome_index INTEGER,
        their_price REAL, entry REAL, stake REAL, shares REAL,
        status TEXT, mark_price REAL, pnl REAL, resolved_ts TEXT)""")
    return c


# only copy trades fresh enough that the current price ~= their fill (true forward test,
# no look-ahead on their already-moved older positions). Hourly ticks catch everything.
MAX_TRADE_AGE_SEC = 4 * 3600


def tick():
    c = db()
    now = int(time.time())
    added = 0
    for name, addr in WALLETS.items():
        try:
            act = get(f"{DATA}/activity?user={addr}&limit=100")
        except Exception as e:
            print(f"  {name}: fetch failed ({e})")
            continue
        for a in act:
            if a.get("type") != "TRADE" or a.get("side") != "BUY":
                continue
            if now - int(a.get("timestamp") or 0) > MAX_TRADE_AGE_SEC:
                continue  # too old — would be look-ahead, skip
            asset = a.get("asset")
            tx = a.get("transactionHash") or ""
            their_price = float(a.get("price") or 0)
            if not asset or their_price <= 0 or their_price >= 1:
                continue
            key = f"{tx}:{asset}:{a.get('timestamp')}"
            if c.execute("SELECT 1 FROM copies WHERE key=?", (key,)).fetchone():
                continue
            entry = their_price            # slow markets: copy fill ~= their fill
            shares = STAKE / entry
            c.execute("""INSERT OR IGNORE INTO copies(key,copied_ts,wallet,their_ts,condition_id,
                asset,title,outcome,outcome_index,their_price,entry,stake,shares,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, now_iso(), name, a.get("timestamp"), a.get("conditionId"), asset,
                 (a.get("title") or "")[:80], a.get("outcome"), a.get("outcomeIndex"),
                 their_price, entry, STAKE, shares, "open"))
            if c.total_changes:
                added += 1
        c.commit()
    print(f"tick: recorded {added} new paper copies across {len(WALLETS)} wallets")


def _market_by_condition(cid):
    if not cid:
        return None
    try:
        r = get(f"{GAMMA}/markets?condition_ids={cid}")
        return r[0] if r else None
    except Exception:
        return None


def resolve():
    c = db()
    # oldest-open first (likeliest resolved); bounded per run to keep cloud runtime sane
    rows = c.execute("SELECT id,condition_id,outcome_index,shares,stake,entry FROM copies "
                     "WHERE status='open' ORDER BY their_ts ASC LIMIT 200").fetchall()
    settled = marked = 0
    for cid_row in rows:
        cid_id, cid, oidx, shares, stake, entry = cid_row
        m = _market_by_condition(cid)
        if not m:
            continue
        try:
            prices = [float(p) for p in json.loads(m["outcomePrices"])]
        except Exception:
            continue
        oidx = oidx if oidx is not None else 0
        if m.get("closed"):
            win_idx = max(range(len(prices)), key=lambda i: prices[i])
            won = (win_idx == oidx) and prices[win_idx] > 0.5
            payout = shares if won else 0.0
            c.execute("UPDATE copies SET status=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                      ("won" if won else "lost", 1.0 if won else 0.0, payout - stake, now_iso(), cid_id))
            settled += 1
        else:
            cur = prices[oidx] if oidx < len(prices) else entry
            c.execute("UPDATE copies SET mark_price=?,pnl=? WHERE id=?",
                      (cur, shares * cur - stake, cid_id))
            marked += 1
    c.commit()
    print(f"resolve: settled {settled}, marked-to-market {marked} open")


def report():
    c = db()
    rows = c.execute("SELECT wallet,status,stake,pnl,mark_price FROM copies").fetchall()
    print("\n" + "=" * 66)
    print("  POLYMARKET COPYBOT — paper forward-test ($10/copy)")
    print("=" * 66)
    if not rows:
        print("  no copies yet — run tick.\n"); return
    settled = [r for r in rows if r[1] in ("won", "lost")]
    open_ = [r for r in rows if r[1] == "open"]
    by = {}
    for w, st, stake, pnl, mark in rows:
        d = by.setdefault(w, {"n": 0, "real": 0.0, "unreal": 0.0, "settled": 0, "wins": 0})
        d["n"] += 1
        if st in ("won", "lost"):
            d["settled"] += 1
            d["real"] += pnl or 0
            d["wins"] += 1 if st == "won" else 0
        elif pnl is not None:
            d["unreal"] += pnl
    print(f"  copies: {len(rows)}   settled: {len(settled)}   open: {len(open_)}")
    print(f"  {'wallet':<14}{'copies':>7}{'settled':>8}{'realized':>11}{'unreal(MTM)':>13}")
    print("  " + "-" * 60)
    tot_real = tot_unreal = 0.0
    for w, d in sorted(by.items(), key=lambda x: -(x[1]["real"] + x[1]["unreal"])):
        tot_real += d["real"]; tot_unreal += d["unreal"]
        print(f"  {w:<14}{d['n']:>7}{d['settled']:>8}{('$%+.2f' % d['real']):>11}{('$%+.2f' % d['unreal']):>13}")
    print("  " + "-" * 60)
    print(f"  {'TOTAL':<14}{len(rows):>7}{len(settled):>8}{('$%+.2f' % tot_real):>11}{('$%+.2f' % tot_unreal):>13}")
    staked_settled = sum(r[2] for r in settled)
    if staked_settled:
        print(f"\n  realized ROI (resolved only): {tot_real/staked_settled*100:+.1f}%  "
              f"({sum(1 for r in settled if r[1]=='won')}/{len(settled)} won)")
    # how late do we SEE their trades? (detection lag = when-we-copied minus when-they-traded)
    lags = []
    for ct, tt in c.execute("SELECT copied_ts, their_ts FROM copies WHERE their_ts IS NOT NULL"):
        try:
            cu = datetime.fromisoformat(ct).timestamp()
            lags.append(cu - int(tt))
        except Exception:
            pass
    if lags:
        lags.sort()
        med = lags[len(lags)//2] / 60
        fast = lags[0] / 60
        print(f"\n  DETECTION LAG (how late we catch their trades): median {med:.1f} min, "
              f"fastest {fast:.1f} min  (over {len(lags)} trades)")
    print("  note: most copies stay OPEN (slow markets); unreal(MTM) = current mark, not final.\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"tick": tick, "resolve": resolve, "report": report}.get(cmd, report)()
