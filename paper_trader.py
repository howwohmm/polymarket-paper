#!/usr/bin/env python3
"""
polymarket paper trader — the "fade the favorite" reversal strategy, money-free.

Translates the AskGina tweet strategy into deterministic code and PAPER-TRADES it:
no wallet, no deposits, no real orders. It just records what the strategy WOULD do
against live Polymarket prices, then scores it against real outcomes.

Strategy (verbatim from the tweet, encoded):
  - Check hourly BTC up/down first, then ETH.
  - Valid signal: one outcome (Up or Down) is between 88% and 98%.
  - If both valid -> take BTC. If neither -> stop.
  - Slippage check: simulate the buy on the LOWER-prob (underdog) side. Skip if > 4%.
  - Size by how extreme the leader is: 88-91% -> $10, 91-94% -> $17, 94-98% -> $25.
  - Buy the OTHER (underdog) side. Paper-fill at the real ask off the orderbook.
  - Budget guard: stop if cumulative paper losses exceed $500.

Commands:
  python3 paper_trader.py tick      # run the strategy once (place 1 paper bet or log a skip)
  python3 paper_trader.py resolve   # settle any open bets whose hour has ended
  python3 paper_trader.py report    # win-rate, staked, P&L, EV per $1
  python3 paper_trader.py run       # foreground daemon: resolve+tick at :46 and :53 forever
"""

import json
import sqlite3
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ---- strategy params (edit here to fork the strategy) ----------------------
SIGNAL_LOW = 0.88
SIGNAL_HIGH = 0.98
MAX_SLIPPAGE = 0.04          # 4%
RUN_MINUTES = [46, 53]       # minutes past the hour to act
COINS = ["bitcoin", "ethereum"]   # priority order: BTC first, then ETH
BUDGET_MAX_LOSS = 500.0      # stop trading once cumulative losses exceed this

def size_for(leading_price: float) -> float:
    if 0.88 <= leading_price < 0.91:
        return 10.0
    if 0.91 <= leading_price < 0.94:
        return 17.0
    if 0.94 <= leading_price <= 0.98:
        return 25.0
    return 0.0

# ---- infra -----------------------------------------------------------------
DB = Path(__file__).parent / "trades.db"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "paper-trader/1.0", "Accept": "application/json"}


def get(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS bets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, coin TEXT, market_id TEXT, slug TEXT, question TEXT,
        leading_outcome TEXT, leading_price REAL,
        underdog_outcome TEXT, underdog_token TEXT,
        entry_price REAL, slippage REAL, stake REAL, shares REAL,
        end_date TEXT, status TEXT, payout REAL, pnl REAL, resolve_ts TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS ticks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, action TEXT, detail TEXT)""")
    return c


def log_tick(c, action, detail):
    c.execute("INSERT INTO ticks(ts,action,detail) VALUES(?,?,?)",
              (now_utc().isoformat(), action, detail))
    c.commit()
    print(f"[{action}] {detail}")


# ---- market discovery ------------------------------------------------------
def find_hourly_market(coin: str):
    """Return the live hourly up/down market dict for `coin`, or None.

    Hourly markets have slugs like 'bitcoin-up-or-down-june-9-2026-12pm-et' and
    resolve at the top of the hour. We pick the active one with the soonest
    future endDate (= the current hour's market)."""
    events = get(f"{GAMMA}/events?closed=false&limit=400&order=startDate"
                 f"&ascending=false&tag_slug=crypto")
    best = None
    for e in events:
        s = e.get("slug", "")
        if not s.startswith(f"{coin}-up-or-down"):
            continue
        if "-et" not in s:            # the hourly variant carries an ET hour suffix
            continue
        if "updown-5m" in s or "updown-15m" in s:
            continue
        for m in e.get("markets", []):
            if m.get("closed") or not m.get("active"):
                continue
            try:
                end = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
            except Exception:
                continue
            if end <= now_utc():
                continue
            if best is None or end < best[0]:
                best = (end, e, m)
    if not best:
        return None
    _, e, m = best
    return {"slug": e["slug"], "market_id": str(m.get("id")),
            "question": m.get("question", ""),
            "outcomes": json.loads(m["outcomes"]),
            "prices": [float(p) for p in json.loads(m["outcomePrices"])],
            "tokens": json.loads(m["clobTokenIds"]),
            "end_date": m["endDate"]}


def simulate_buy(token_id: str, stake: float):
    """Walk the orderbook asks to fill `stake` USD. Returns (avg_price, shares, slippage)
    or None if the book can't fill it."""
    book = get(f"{CLOB}/book?token_id={token_id}")
    asks = sorted(({"p": float(a["price"]), "s": float(a["size"])} for a in book.get("asks", [])),
                  key=lambda x: x["p"])
    if not asks:
        return None
    best = asks[0]["p"]
    remaining, shares = stake, 0.0
    for lvl in asks:
        lvl_cost = lvl["p"] * lvl["s"]
        take = min(remaining, lvl_cost)
        shares += take / lvl["p"]
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or shares <= 0:
        return None  # book too thin to fill -> treat as unfillable / huge slippage
    avg = stake / shares
    slippage = (avg - best) / best
    return avg, shares, slippage


# ---- core commands ---------------------------------------------------------
def cumulative_pnl(c):
    row = c.execute("SELECT COALESCE(SUM(pnl),0) FROM bets WHERE status IN('won','lost')").fetchone()
    return row[0] or 0.0


def tick():
    c = db()
    # budget guard
    pnl = cumulative_pnl(c)
    if pnl < -BUDGET_MAX_LOSS:
        log_tick(c, "SKIP", f"Budget exceeded (cumulative P&L ${pnl:.2f} < -${BUDGET_MAX_LOSS})")
        return

    chosen = None
    for coin in COINS:
        mk = find_hourly_market(coin)
        if not mk:
            continue
        # find the leader (highest price) and check the 88-98% window
        lead_idx = max(range(len(mk["prices"])), key=lambda i: mk["prices"][i])
        lead_price = mk["prices"][lead_idx]
        if SIGNAL_LOW <= lead_price <= SIGNAL_HIGH:
            # dedupe: never bet the same market twice (hedge cron slots run >1x/window)
            already = c.execute("SELECT 1 FROM bets WHERE market_id=?",
                                (mk["market_id"],)).fetchone()
            if already:
                log_tick(c, "INFO", f"{coin}: already have a bet on {mk['slug']}, skipping")
                continue
            chosen = (coin, mk, lead_idx, lead_price)
            break  # BTC first; first valid wins
        else:
            log_tick(c, "INFO", f"{coin}: leader {mk['outcomes'][lead_idx]} @ "
                                f"{lead_price:.3f} outside [{SIGNAL_LOW},{SIGNAL_HIGH}]")

    if not chosen:
        log_tick(c, "SKIP", "No valid signal on BTC or ETH this tick")
        return

    coin, mk, lead_idx, lead_price = chosen
    under_idx = 1 - lead_idx
    under_token = mk["tokens"][under_idx]
    under_outcome = mk["outcomes"][under_idx]
    stake = size_for(lead_price)

    sim = simulate_buy(under_token, stake)
    if sim is None:
        log_tick(c, "SKIP", f"{coin}: orderbook too thin to fill ${stake} on {under_outcome}")
        return
    entry, shares, slippage = sim
    if slippage > MAX_SLIPPAGE:
        log_tick(c, "SKIP", f"{coin}: slippage {slippage*100:.2f}% > {MAX_SLIPPAGE*100:.0f}% "
                            f"on {under_outcome}")
        return

    c.execute("""INSERT INTO bets(ts,coin,market_id,slug,question,leading_outcome,leading_price,
        underdog_outcome,underdog_token,entry_price,slippage,stake,shares,end_date,status,payout,pnl)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now_utc().isoformat(), coin, mk["market_id"], mk["slug"], mk["question"],
         mk["outcomes"][lead_idx], lead_price, under_outcome, under_token,
         entry, slippage, stake, shares, mk["end_date"], "open", None, None))
    c.commit()
    log_tick(c, "BET", f"{coin} {mk['question']} | leader {mk['outcomes'][lead_idx]} @ {lead_price:.3f} "
                       f"-> ${stake:.0f} on {under_outcome} @ {entry:.3f} "
                       f"({shares:.1f} shares, slip {slippage*100:.2f}%)")


def resolve():
    c = db()
    open_bets = c.execute("SELECT id,market_id,slug,underdog_outcome,shares,stake,end_date "
                          "FROM bets WHERE status='open'").fetchall()
    for bid, mid, slug, under_outcome, shares, stake, end_date in open_bets:
        end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        if end > now_utc():
            continue  # hour not over yet
        try:
            m = get(f"{GAMMA}/markets/{mid}")
        except Exception as e:
            print(f"resolve fetch failed for bet {bid}: {e}")
            continue
        if not m.get("closed"):
            continue  # ended but not yet settled on-chain; try again next cycle
        outcomes = json.loads(m["outcomes"])
        prices = [float(p) for p in json.loads(m["outcomePrices"])]
        # resolved market: winning outcome price -> 1.0
        win_idx = max(range(len(prices)), key=lambda i: prices[i])
        win_outcome = outcomes[win_idx]
        if abs(prices[win_idx] - 0.5) < 0.1:   # ~50/50 -> void/refund
            status, payout = "void", stake
        elif win_outcome == under_outcome:
            status, payout = "won", shares * 1.0
        else:
            status, payout = "lost", 0.0
        pnl = payout - stake
        c.execute("UPDATE bets SET status=?,payout=?,pnl=?,resolve_ts=? WHERE id=?",
                  (status, payout, pnl, now_utc().isoformat(), bid))
        c.commit()
        print(f"[RESOLVE] bet {bid} {slug}: winner={win_outcome} -> {status.upper()} "
              f"(P&L ${pnl:+.2f})")


def report():
    c = db()
    rows = c.execute("SELECT status,stake,payout,pnl,coin,leading_price,entry_price "
                     "FROM bets").fetchall()
    settled = [r for r in rows if r[0] in ("won", "lost")]
    opens = [r for r in rows if r[0] == "open"]
    voids = [r for r in rows if r[0] == "void"]
    wins = [r for r in settled if r[0] == "won"]
    staked = sum(r[1] for r in settled)
    pnl = sum(r[3] for r in settled)
    print("\n" + "=" * 56)
    print("  POLYMARKET PAPER TRADER — fade-the-favorite report")
    print("=" * 56)
    print(f"  bets placed (settled): {len(settled)}   open: {len(opens)}   void: {len(voids)}")
    if settled:
        wr = len(wins) / len(settled) * 100
        print(f"  win rate:              {wr:.1f}%  ({len(wins)}/{len(settled)})")
        print(f"  total staked:          ${staked:.2f}")
        print(f"  total P&L:             ${pnl:+.2f}")
        print(f"  EV per $1 staked:      ${pnl/staked:+.3f}" if staked else "")
        roi = pnl / staked * 100 if staked else 0
        print(f"  ROI:                   {roi:+.1f}%")
        if wins:
            avg_win = sum(r[3] for r in wins) / len(wins)
            print(f"  avg win:               ${avg_win:+.2f}")
        verdict = "+EV so far (strategy may have edge)" if pnl > 0 else \
                  "-EV so far (fading favorites is losing money, as expected)"
        print(f"  verdict:               {verdict}")
    else:
        print("  no settled bets yet — let it run a few hours.")
    print("=" * 56 + "\n")


def run():
    print("paper trader daemon up. acting at minutes", RUN_MINUTES, "past each hour. ctrl-c to stop.")
    last_min = -1
    while True:
        n = now_utc()
        if n.minute in RUN_MINUTES and n.minute != last_min:
            last_min = n.minute
            print(f"\n--- cycle @ {n.isoformat()} ---")
            try:
                resolve()
                tick()
            except Exception as e:
                print("cycle error:", e)
        if n.minute not in RUN_MINUTES:
            last_min = -1
        time.sleep(20)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"tick": tick, "resolve": resolve, "report": report, "run": run}.get(cmd, report)()
