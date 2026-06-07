# polymarket paper trader

Paper-trades the "fade the favorite" reversal strategy from the AskGina tweet against
**live** Polymarket prices — with **zero real money**. Records what the strategy would
bet, settles against real outcomes, and reports actual win-rate / EV so you can decide
whether the edge is real before risking a cent.

## the strategy (encoded in `paper_trader.py`)
At :46 and :53 past each hour, check hourly BTC up/down first, then ETH. If one side is
88–98%, buy the *other* (underdog) side — $10 / $17 / $25 scaled by how extreme the
leader is. Skip if orderbook slippage > 4%. Stop if cumulative losses exceed $500.

## usage
```bash
python3 paper_trader.py tick      # run once: place 1 paper bet or log a skip
python3 paper_trader.py resolve   # settle bets whose hour has ended
python3 paper_trader.py report    # win-rate, staked, P&L, EV per $1, ROI
python3 paper_trader.py run       # foreground daemon (alt to launchd)
```

## 24/7 (already installed)
launchd agent `com.ohm.polymarket-paper` runs `run-cycle.sh` (resolve + tick) at :46 and
:53 every hour. Survives sleep, runs on wake.

```bash
launchctl list | grep polymarket                                  # is it running?
tail -f cycle.log                                                  # watch live
launchctl unload ~/Library/LaunchAgents/com.ohm.polymarket-paper.plist   # stop it
```

## after a week
Run `report`. If P&L is positive across ~50+ settled bets, the edge might be real.
If negative (the academic prior — favorite-longshot bias — says it will be), you learned
that for $0 instead of out of your wallet.

## cost
- Polymarket gamma + CLOB APIs: free, public, no key.
- No LLM calls — the strategy is deterministic rules, not a prompt. $0 in API.
- Runs on your Mac. Electricity ≈ pennies for the week.
