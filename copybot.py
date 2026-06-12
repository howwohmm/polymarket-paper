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
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
DB = Path(__file__).parent / "copybot.db"
# REAL $20 wallet per model: each model starts with $20, bets $2/trade (so up to ~10
# positions at once), and can only open a copy if it has free cash. When a position
# closes, the proceeds return to the wallet to be reused (recycling).
BANKROLL_PER_MODEL = 20.0
STAKE = 2.0
# Model B (take-profit exit): sell our copy when it's up enough to bank a margin, or
# force-close after MAX_HOLD so positions turn over fast (-> 1-3 day verdict, not weeks).
TAKE_PROFIT = 0.15        # exit when the position is +15% (enough margin to profit)
MAX_HOLD_HOURS = 36       # if it hasn't hit target in 36h, close it out at current price
# Model A only: skip copying markets that resolve further out than this (short test —
# no point holding a copy whose market settles in weeks/months). Model B is exempt.
MAX_HOLD_RESOLVE_DAYS = 3
# Model C: Claude decides HOLD/SELL per position (using market + price action), to cut
# losers smarter than a fixed rule. Bounded so the $500 credit lasts.
CLAUDE_MODEL = "claude-haiku-4-5"   # cheap; swap to claude-opus-4-6 for smarter, pricier calls
MAX_CLAUDE_CALLS_PER_RUN = 40       # hard cost cap per workflow run
CLAUDE_RECHECK_HOURS = 6            # don't re-ask about the same position within 6h
CDECIDE_MAX_HOLD_HOURS = 72         # backstop: force-close a Claude position after 72h

# vetted copyable traders (small clip, taker, diversified, slow markets)
WALLETS = {
    "GoalLineGhost": "0x0346afae2603313d2bbee96b628536c8cbe352a5",
    "alwayslate":    "0xb687f00464e33934f5d591f224e71c3559ecaee5",
    "mooseborzoi":   "0x84cfffc3f16dcc353094de30d4a45226eccd2f63",
    "afkpnl":        "0x55eca3687ea7d69632ffe0f297ea3d5158bb8c7d",
    "RN1":           "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
    "domahhh":       "0x9d84ce0306f8551e02efef1680475fc0f1dc1344",
    "aenews2":       "0x44c1dfe43260c94ed4f1d00de2e1f80fb113ebc1",
    "gopfan2":       "0xf2f6af4f27ec2dcf4072095ab804016e14cd5817",
    "HolyMoses7":    "0xa4b366ad22fc0d06f1e934ff468e8922431a87b8",  # lottery acct ($1->$1M goal)
    # --- Dune-sweep quiet winners (vetted from 1,617 active wallets, 2026-06-12) ---
    "AJSV":          "0xad5353afe30c2da57709e2704ef3ccdcf67eef24",  # +$7.9k, 500 mkts, $18 clip
    "AshHaykArs":    "0xd0ac6e6b585c00042a0552a6cf35f2f056ef7dee",  # +$1.9k, 38 mkts, $15 clip
    "zerowander01":  "0xa30de80ba9d6cc3c2d79955b6dca38307bf941af",  # +$224, 152 mkts, $4 clip (micro)
    "ElMagoCS2":     "0xf67948563be54a8a6aa0ab7b976d80e8f7e92d3d",  # +$404, 30 mkts, $14 clip
    "fox54498":      "0x54498e4c40b17261479aeaeeddf9cc37bd46992b",  # +$456, 362 mkts, $20 clip
    "belaba":        "0xb1556d9d1a734508178d15ba822e579901a7ca84",  # +$295, 21 mkts, $38 clip
    "molodoyy":      "0x564f22744b7941ade18d5e0e4f347c30e3057026",  # +$3.0k, 256 mkts, $57 clip
    "Tee1000":       "0x5edc7bba7516cd28dc886ad576ba197bb114bbad",  # +$23k, 494 mkts (experimental)
    "Polkadot-Frog": "0x9d57c42e847173d06841703825d3fe2299e456ea",  # +$20k, 497 mkts (experimental)
    "MILKinDenial":  "0xb02188c268290bb758d6c261bdf4e552c6e1ebd9",  # +$8.7k, 498 mkts (experimental)
    "Eatpraylove":   "0xc02147dee42356b7a4edbb1c35ac4ffa95f61fa8",  # +$136k whale (experimental)
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
    try:
        c.execute("ALTER TABLE copies ADD COLUMN model TEXT DEFAULT 'hold'")  # A=hold, B=exit
    except Exception:
        pass
    c.execute("CREATE TABLE IF NOT EXISTS market_end(condition_id TEXT PRIMARY KEY, end_date TEXT)")
    for col in ("last_check TEXT", "last_reason TEXT"):
        try:
            c.execute(f"ALTER TABLE copies ADD COLUMN {col}")
        except Exception:
            pass
    c.execute("""CREATE TABLE IF NOT EXISTS decisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, copy_id INTEGER, wallet TEXT,
        title TEXT, entry REAL, current REAL, gain REAL, decision TEXT, why TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS bankroll(model TEXT PRIMARY KEY, cash REAL)")
    for m in ("hold", "exit", "cdecide"):
        c.execute("INSERT OR IGNORE INTO bankroll(model,cash) VALUES(?,?)", (m, BANKROLL_PER_MODEL))
    return c


def _cash(c, model):
    r = c.execute("SELECT cash FROM bankroll WHERE model=?", (model,)).fetchone()
    return r[0] if r else 0.0


def _credit(c, model, amount):
    c.execute("UPDATE bankroll SET cash=cash+? WHERE model=?", (amount, model))


_endcache = {}


def _resolves_soon(cid, c):
    """True if this market resolves within MAX_HOLD_RESOLVE_DAYS. Caches end dates
    (in-memory + db) so we only look up each market once. Unknown/unfetchable -> False
    (skip, since a short test can't wait on an unknown resolution)."""
    if not cid:
        return False
    if cid in _endcache:
        ed = _endcache[cid]
    else:
        row = c.execute("SELECT end_date FROM market_end WHERE condition_id=?", (cid,)).fetchone()
        if row:
            ed = row[0]
        else:
            m = _market_by_condition(cid)
            ed = m.get("endDate") if m else None
            c.execute("INSERT OR REPLACE INTO market_end(condition_id,end_date) VALUES(?,?)", (cid, ed))
        _endcache[cid] = ed
    if not ed:
        return False
    try:
        end = datetime.fromisoformat(ed.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return now < end <= now + timedelta(days=MAX_HOLD_RESOLVE_DAYS)
    except Exception:
        return False


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
            base = f"{tx}:{asset}:{a.get('timestamp')}"
            entry = their_price            # slow markets: copy fill ~= their fill
            shares = STAKE / entry
            # Model A only copies markets resolving soon; Model B always copies (exits early)
            hold_ok = _resolves_soon(a.get("conditionId"), c)
            for model in ("hold", "exit", "cdecide"):   # A=hold, B=take-profit, C=Claude
                if model == "hold" and not hold_ok:
                    continue
                key = f"{base}:{model}"
                if c.execute("SELECT 1 FROM copies WHERE key=?", (key,)).fetchone():
                    continue
                if _cash(c, model) < STAKE:     # wallet's out of cash -> can't copy (realistic)
                    continue
                c.execute("""INSERT OR IGNORE INTO copies(key,copied_ts,wallet,their_ts,condition_id,
                    asset,title,outcome,outcome_index,their_price,entry,stake,shares,status,model)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, now_iso(), name, a.get("timestamp"), a.get("conditionId"), asset,
                     (a.get("title") or "")[:80], a.get("outcome"), a.get("outcomeIndex"),
                     their_price, entry, STAKE, shares, "open", model))
                _credit(c, model, -STAKE)       # lock the stake out of the wallet
                added += 1
        c.commit()
    print(f"tick: recorded {added} new paper copies (x2 models) across {len(WALLETS)} wallets")


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
    # newest-open prioritized for Model B (exit needs frequent price checks); bounded by
    # row count + wall-clock so a slow gamma API can't blow the job timeout.
    rows = c.execute("SELECT id,condition_id,outcome_index,shares,stake,entry,"
                     "COALESCE(model,'hold'),copied_ts FROM copies "
                     "WHERE status='open' ORDER BY id DESC LIMIT 150").fetchall()
    deadline = time.time() + 300
    now = time.time()
    settled = marked = exited = 0
    for cid_id, cid, oidx, shares, stake, entry, model, copied_ts in rows:
        if time.time() > deadline:
            print("resolve: hit 5-min deadline, stopping early (rest next run)")
            break
        m = _market_by_condition(cid)
        if not m:
            continue
        try:
            prices = [float(p) for p in json.loads(m["outcomePrices"])]
        except Exception:
            continue
        oidx = oidx if oidx is not None else 0
        cur = prices[oidx] if oidx < len(prices) else entry
        if m.get("closed"):                       # market resolved -> settle either model
            win_idx = max(range(len(prices)), key=lambda i: prices[i])
            won = (win_idx == oidx) and prices[win_idx] > 0.5
            payout = shares if won else 0.0
            c.execute("UPDATE copies SET status=?,mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                      ("won" if won else "lost", 1.0 if won else 0.0, payout - stake, now_iso(), cid_id))
            _credit(c, model, payout)             # proceeds back to the wallet
            settled += 1
        elif model == "exit":                     # Model B: take-profit / timeout exit
            gain = (cur - entry) / entry if entry > 0 else 0
            try:
                age_h = (now - datetime.fromisoformat(copied_ts).timestamp()) / 3600
            except Exception:
                age_h = 0
            if gain >= TAKE_PROFIT:
                c.execute("UPDATE copies SET status='exit_won',mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "exit", shares * cur); exited += 1
            elif age_h >= MAX_HOLD_HOURS:
                c.execute("UPDATE copies SET status='exit_closed',mark_price=?,pnl=?,resolved_ts=? WHERE id=?",
                          (cur, shares * cur - stake, now_iso(), cid_id)); _credit(c, "exit", shares * cur); exited += 1
            else:
                c.execute("UPDATE copies SET mark_price=?,pnl=? WHERE id=?",
                          (cur, shares * cur - stake, cid_id)); marked += 1
        else:                                     # Model A: hold, just mark-to-market
            c.execute("UPDATE copies SET mark_price=?,pnl=? WHERE id=?",
                      (cur, shares * cur - stake, cid_id)); marked += 1
    c.commit()
    print(f"resolve: settled {settled}, Model-B exits {exited}, marked {marked}")


def _ask_claude(key, title, outcome, entry, cur, gain, age_h, wallet):
    """Ask Claude HOLD or SELL for one copied position. Returns (decision, why) or (None, err)."""
    prompt = (
        f"You manage a Polymarket COPY position (mirroring trader {wallet}).\n"
        f'Market: "{title}"\n'
        f"Bought outcome: {outcome} at {entry:.3f}, now {cur:.3f} ({gain*100:+.1f}%), held {age_h:.0f}h.\n"
        f"Your job: maximize profit, cut losers early, don't give back gains. "
        f"Decide whether to HOLD or SELL now.\n"
        f'Reply with ONLY compact JSON: {{"decision":"HOLD"|"SELL","why":"<=10 words"}}.')
    body = {"model": CLAUDE_MODEL, "max_tokens": 120,
            "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        txt = r["content"][0]["text"]
        d = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
        return d.get("decision", "HOLD").upper(), (d.get("why", "") or "")[:120]
    except Exception as e:
        return None, str(e)[:80]


def claude_decide():
    """Model C: for moved Model-C positions, let Claude decide HOLD/SELL. Bounded by
    MAX_CLAUDE_CALLS_PER_RUN + a 6h re-check throttle. Logs every decision + the why."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("claude_decide: no ANTHROPIC_API_KEY, skipping"); return
    c = db()
    now = time.time()
    calls = sold = held = closed = 0
    rows = c.execute("SELECT id,shares,stake,entry,title,outcome,copied_ts,last_check,mark_price,wallet "
                     "FROM copies WHERE model='cdecide' AND status='open' AND mark_price IS NOT NULL "
                     "ORDER BY id DESC LIMIT 500").fetchall()
    for cid_id, shares, stake, entry, title, outcome, copied_ts, last_check, cur, wallet in rows:
        if calls >= MAX_CLAUDE_CALLS_PER_RUN:
            break
        try:
            age_h = (now - datetime.fromisoformat(copied_ts).timestamp()) / 3600
        except Exception:
            age_h = 0
        gain = (cur - entry) / entry if entry else 0
        if age_h >= CDECIDE_MAX_HOLD_HOURS:    # backstop force-close
            c.execute("UPDATE copies SET status='c_closed',pnl=?,resolved_ts=?,last_reason=? WHERE id=?",
                      (shares * cur - stake, now_iso(), "auto: 72h cap", cid_id))
            _credit(c, "cdecide", shares * cur)
            c.execute("INSERT INTO decisions(ts,copy_id,wallet,title,entry,current,gain,decision,why) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (now_iso(), cid_id, wallet, title, entry, cur, round(gain, 3), "SELL", "auto: 72h cap"))
            closed += 1
            c.commit(); continue
        moved = gain >= 0.05 or gain <= -0.10
        recently = False
        if last_check:
            try:
                recently = (now - datetime.fromisoformat(last_check).timestamp()) < CLAUDE_RECHECK_HOURS * 3600
            except Exception:
                recently = False
        if not moved or recently:
            continue
        decision, why = _ask_claude(key, title, outcome, entry, cur, gain, age_h, wallet)
        calls += 1
        if decision is None:
            continue
        c.execute("INSERT INTO decisions(ts,copy_id,wallet,title,entry,current,gain,decision,why) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (now_iso(), cid_id, wallet, title, entry, cur, round(gain, 3), decision, why))
        c.execute("UPDATE copies SET last_check=?,last_reason=? WHERE id=?", (now_iso(), why, cid_id))
        if decision == "SELL":
            c.execute("UPDATE copies SET status='c_sold',pnl=?,resolved_ts=? WHERE id=?",
                      (shares * cur - stake, now_iso(), cid_id)); _credit(c, "cdecide", shares * cur); sold += 1
        else:
            held += 1
        c.commit()
    print(f"claude_decide: {calls} calls -> {sold} sold, {held} held, {closed} auto-closed")


def _model_summary(c, model):
    rows = c.execute("SELECT wallet,title,outcome,entry,mark_price,pnl,status,last_reason "
                     "FROM copies WHERE model=? ORDER BY id DESC LIMIT 100", (model,)).fetchall()
    positions = []
    for w, t, o, e, mk, pnl, st, why in rows:
        gain = ((mk - e) / e * 100) if (mk and e) else 0
        positions.append(dict(wallet=w, title=(t or "")[:80], outcome=o, entry=e, current=mk,
                              gain=round(gain, 1), status=st, pnl=round(pnl, 2) if pnl is not None else None,
                              why=why))
    allr = c.execute("SELECT status,pnl,stake FROM copies WHERE model=?", (model,)).fetchall()
    closed = [r for r in allr if r[0] in CLOSED]
    wins = [r for r in closed if (r[1] or 0) > 0]
    openr = [r for r in allr if r[0] == "open"]
    cash = _cash(c, model)
    at_risk = round(sum(r[2] or 0 for r in openr), 2)
    unreal = round(sum(r[1] or 0 for r in openr), 2)
    return dict(open=len(openr), closed=len(closed), wins=len(wins),
                win_rate=round(len(wins) / len(closed) * 100, 1) if closed else 0,
                realized=round(sum(r[1] or 0 for r in closed), 2),
                unreal=unreal,
                started=BANKROLL_PER_MODEL,
                cash=round(cash, 2),                                  # free cash in the $20 wallet
                at_risk=at_risk,                                      # $ tied up in open positions
                value=round(cash + at_risk + unreal, 2),             # total wallet worth right now
                churned=round(sum(r[2] or 0 for r in allr), 2),      # total $ volume ever deployed
                positions=positions[:60])


def export():
    """Write docs/data.json for the live dashboard — all three models A/B/C + C's decisions."""
    c = db()
    models = {"A": _model_summary(c, "hold"),
              "B": _model_summary(c, "exit"),
              "C": _model_summary(c, "cdecide")}
    decs = c.execute("SELECT ts,wallet,title,entry,current,gain,decision,why FROM decisions "
                     "ORDER BY id DESC LIMIT 200").fetchall()
    decisions = [dict(ts=d[0], wallet=d[1], title=(d[2] or "")[:80], entry=d[3],
                      current=d[4], gain=round((d[5] or 0) * 100, 1), decision=d[6], why=d[7]) for d in decs]
    os.makedirs("docs", exist_ok=True)
    out = dict(updated=now_iso(), models=models, decisions=decisions)
    json.dump(out, open("docs/data.json", "w"))
    print("export: wrote docs/data.json ->", {k: (v["closed"], v["realized"]) for k, v in models.items()})


CLOSED = ("won", "lost", "exit_won", "exit_closed", "c_sold", "c_closed")


def _model_block(label, rows, note):
    # rows: (status, stake, pnl)
    closed = [r for r in rows if r[0] in CLOSED]
    open_ = [r for r in rows if r[0] == "open"]
    wins = [r for r in closed if r[2] and r[2] > 0]
    real = sum(r[2] or 0 for r in closed)
    unreal = sum(r[2] or 0 for r in open_)
    staked = sum(r[1] for r in closed)
    print(f"  -- {label} --   {note}")
    print(f"     copies: {len(rows)}   closed: {len(closed)}   open: {len(open_)}")
    if closed:
        wr = len(wins) / len(closed) * 100
        print(f"     win rate:   {wr:.0f}%  ({len(wins)}/{len(closed)})")
        print(f"     realized:   ${real:+.2f}" + (f"   ROI {real/staked*100:+.1f}%" if staked else ""))
    print(f"     open (mark-to-market): ${unreal:+.2f}")


def report():
    c = db()
    rows = c.execute("SELECT COALESCE(model,'hold'),status,stake,pnl FROM copies").fetchall()
    print("\n" + "=" * 64)
    print("  POLYMARKET COPYBOT — two parallel models ($10/copy)")
    print("=" * 64)
    if not rows:
        print("  no copies yet — run tick.\n"); return
    hold = [(r[1], r[2], r[3]) for r in rows if r[0] == "hold"]
    exit_ = [(r[1], r[2], r[3]) for r in rows if r[0] == "exit"]
    cdec = [(r[1], r[2], r[3]) for r in rows if r[0] == "cdecide"]
    _model_block("MODEL A: copy & HOLD to resolution", hold, "(slow — markets resolve weeks out)")
    print()
    _model_block("MODEL B: copy & SELL at +%d%% / %dh cap" % (TAKE_PROFIT * 100, MAX_HOLD_HOURS),
                 exit_, "(fast — closes in 1-3 days)")
    print()
    _model_block("MODEL C: Claude decides HOLD/SELL", cdec, "(AI exits — see dashboard for whys)")
    # detection lag
    lags = []
    for ct, tt in c.execute("SELECT copied_ts, their_ts FROM copies WHERE their_ts IS NOT NULL"):
        try:
            lags.append(datetime.fromisoformat(ct).timestamp() - int(tt))
        except Exception:
            pass
    if lags:
        lags.sort()
        print(f"\n  detection lag: median {lags[len(lags)//2]/60:.1f} min over {len(lags)} trades")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"tick": tick, "resolve": resolve, "report": report,
     "claude_decide": claude_decide, "export": export}.get(cmd, report)()
