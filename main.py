#!/usr/bin/env python3
"""Dip-buy BTC bot for the make.fun btc-race-24h competition (bars-replay/v1).

Strategy, in plain terms: Bitcoin's 1-minute closes are noisy, so this bot does
not chase every wiggle. It waits until the close trades a clear step below its
own 4-hour average, buys the dip with all available cash, then lets the
position run: it only sells when the close falls 1% below the best close seen
since entry (a trailing stop), when it falls 2% below the price the buy
filled at (a hard stop), or after 8 hours in the trade. One position at a
time, long only, no leverage, no shorts.

Why this shape: a full-size round trip costs about 26 bps in fees, slippage
and pool impact, so a trade must catch a move well above that. Buying a dip
against a slow average and riding it with a wide trailing stop takes a few
trades a day instead of dozens, and the wide stops survive ordinary 1-minute
noise (about 6 bps per minute) instead of being shaken out by it.

Wire protocol: the bot reads newline-delimited JSON on stdin (hello, then one
batch per minute, then done) and answers every batch with exactly one reply
line echoing its seq. All arithmetic is integer math on the wire's scaled
units, so two runs on the same data produce identical orders. Standard
library only; no network, no clock, no environment, no files.
"""

import json
import sys

SYMBOL = "CBBTC"

# Strategy parameters (bars are 1 minute).
MEAN_BARS = 240          # 4-hour average of 1-minute closes
DIP_BPS = 40             # buy when the close is 40 bps below that average
TRAIL_BPS = 100          # sell when the close is 100 bps below the peak
HARD_STOP_BPS = 200      # sell when the close is 200 bps below the fill price
MAX_HOLD_BARS = 480      # sell after 8 hours in a position
NO_ENTRY_AFTER_SEQ = 1380  # no new entries in the final hour of the window
MAX_SLIPPAGE_BPS = 200   # an order fills only within 2% of the last close

BPS = 10000


def buy_order(notional, client_id):
    return {
        "side": "buy",
        "client_id": client_id,
        "symbol": SYMBOL,
        "notional_quote_units": str(notional),
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }


def sell_order(qty, client_id):
    return {
        "side": "sell",
        "client_id": client_id,
        "symbol": SYMBOL,
        "qty": str(qty),
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }


def main():
    warmup_bars = 0
    cash = 0
    qty = 0
    closes = []            # closes of bars already seen, oldest first
    entry_price = 0        # price the open buy filled at (price scale units)
    entry_seq = -1         # batch seq whose fills reported the open buy
    peak_close = 0         # best close seen since entry

    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")

        if kind == "hello":
            warmup_bars = int(msg.get("warmup_bars") or 0)
            cash = int(msg["capital"]["quote_units"])
            continue
        if kind == "done":
            break
        if kind != "batch":
            continue

        seq = int(msg["seq"])

        # Fills of the orders decided on the previous batch.
        for fill in msg.get("fills") or []:
            if fill.get("symbol") != SYMBOL or fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                entry_price = int(fill["price"])
                entry_seq = seq
                # The peak starts at the close before the fill bar, which is
                # the latest close we have seen so far.
                peak_close = closes[-1] if closes else entry_price

        # The account snapshot is authoritative for cash and position.
        account = msg.get("account") or {}
        cash = int(account.get("cash_quote_units", cash))
        qty = 0
        for pos in account.get("positions") or []:
            if pos.get("symbol") == SYMBOL:
                qty = int(pos["qty"])
        if qty == 0:
            entry_price = 0
            entry_seq = -1
            peak_close = 0

        orders = []
        bar = None
        for b in msg.get("bars") or []:
            if b.get("symbol") == SYMBOL:
                bar = b
                break
        if bar is not None:
            close = int(bar["c"])
            if qty == 0:
                # Flat: buy the dip, once we have a full average to judge by.
                if (
                    seq >= warmup_bars
                    and seq <= NO_ENTRY_AFTER_SEQ
                    and len(closes) >= MEAN_BARS
                    and cash > 0
                ):
                    mean_sum = sum(closes[-MEAN_BARS:])
                    # close < mean * (1 - DIP_BPS / 10000), in integers.
                    if close * MEAN_BARS * BPS < mean_sum * (BPS - DIP_BPS):
                        orders.append(buy_order(cash, "o%d" % seq))
            else:
                # Holding: ride the move, exit on the trailing stop, the
                # hard stop, or the maximum hold.
                if close > peak_close:
                    peak_close = close
                if close * BPS < peak_close * (BPS - TRAIL_BPS):
                    orders.append(sell_order(qty, "o%d" % seq))
                elif close * BPS < entry_price * (BPS - HARD_STOP_BPS):
                    orders.append(sell_order(qty, "o%d" % seq))
                elif seq - entry_seq >= MAX_HOLD_BARS:
                    orders.append(sell_order(qty, "o%d" % seq))
            closes.append(close)

        out.write(json.dumps({"type": "reply", "seq": seq, "orders": orders}))
        out.write("\n")
        out.flush()


if __name__ == "__main__":
    main()
