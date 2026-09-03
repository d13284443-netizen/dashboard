"""
alerts.py — Telegram notification for detected spikes.

ALERT VOLUME IS THE REAL DESIGN CONSTRAINT. With four drift windows
across hundreds of strikes and two sides, a naive one-message-per-event
notifier produces a flood, and a flood is functionally the same as no
alerts at all — you stop reading them. Three controls:

  1. Grouping. Adjacent strikes on the same side and rule moving
     together are one event, not twelve. That is what a vol move
     actually looks like, so it is how it should be reported.
  2. Severity ordering with a hard per-cycle cap. If more fires than
     the cap, the most significant go out and the rest are recorded
     silently in the database, with a one-line note that they exist.
  3. Shadow-mode annotation. During the test week, a group the smile
     filter would have dropped is still sent, marked. That is the whole
     point of the week: seeing which alerts the filter would have taken
     away, before trusting it to take them away.
"""
import requests

import config
import db

STRIKE_GROUP_DISTANCE = 30


def group_events(events):
    """Collapses adjacent strikes on the same (side, rule) into one
    group. All events in a pass share a snapshot, so unlike the original
    there is no need for a wall-clock proximity test."""
    buckets = {}
    for e in events:
        buckets.setdefault((e["side"], e["rule"]), []).append(e)

    groups = []
    for (side, rule), items in buckets.items():
        items.sort(key=lambda e: e["strike"])
        current = [items[0]]
        for e in items[1:]:
            if e["strike"] - current[-1]["strike"] <= STRIKE_GROUP_DISTANCE:
                current.append(e)
            else:
                groups.append(_summarise(side, rule, current))
                current = [e]
        groups.append(_summarise(side, rule, current))
    return groups


def _summarise(side, rule, items):
    strikes = [e["strike"] for e in items]
    adj = [e["adj_pct_change"] for e in items if e["adj_pct_change"] is not None]
    return {
        "side": side, "rule": rule, "count": len(items),
        "strike_range": f"{strikes[0]:g}" if len(strikes) == 1
                        else f"{strikes[0]:g}-{strikes[-1]:g}",
        "max_raw": max(e["raw_pct_change"] for e in items),
        "max_adj": max(adj) if adj else None,
        "severity": max((e["severity"] for e in items),
                        key=lambda s: ("info", "warn", "high").index(s)),
        # A group only counts as filtered if EVERY member would be
        # dropped. One survivor means the move was real somewhere in
        # that strike range.
        "all_suppressed": all(e["would_suppress"] for e in items),
        "spot_move": items[0].get("spot_move_pct"),
        "atm_change": items[0].get("atm_iv_change_pct"),
        "items": items,
    }


ICON = {"high": "\U0001F534", "warn": "\U0001F7E0", "info": "\U0001F535"}


def format_group(symbol, g):
    side = "CALL" if g["side"] == "c" else "PUT"
    head = (f"{ICON[g['severity']]} IV {g['rule']} — {symbol} {side} "
            f"{g['strike_range']}" + (f" (x{g['count']})" if g["count"] > 1 else ""))
    lines = [head, f"Raw change: +{g['max_raw']*100:.1f}%"]

    if g["max_adj"] is not None:
        lines.append(f"Smile-adjusted: {g['max_adj']*100:+.1f}%")
    else:
        lines.append("Smile-adjusted: n/a (outside baseline strike range)")

    if g["spot_move"] is not None:
        lines.append(f"Spot moved {g['spot_move']*100:+.2f}% over the window")
    if g["atm_change"] is not None:
        lines.append(f"ATM IV {g['atm_change']*100:+.1f}%")

    if g["all_suppressed"]:
        # The single most useful line during the test week: this is the
        # detector telling you it thinks the move was spot, not vol.
        lines.append("\u2139\ufe0f Consistent with smile roll — "
                     "would be filtered once shadow mode ends.")
    return "\n".join(lines)


def send(message):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        print(f"[alerts] Telegram not configured — would have sent:\n{message}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        return resp.status_code < 300
    except Exception as e:
        db.log("alerts", f"telegram send failed: {e}", level="warn")
        return False


def notify(symbol, events):
    if not events:
        return
    groups = group_events(events)
    if not config.ALERT_ON_RAW:
        groups = [g for g in groups if not g["all_suppressed"]]

    order = {"high": 0, "warn": 1, "info": 2}
    groups.sort(key=lambda g: (order[g["severity"]], -(g["max_adj"] or g["max_raw"])))

    to_send = groups[:config.MAX_ALERTS_PER_CYCLE]
    for g in to_send:
        send(format_group(symbol, g))
    if len(groups) > len(to_send):
        send(f"...and {len(groups) - len(to_send)} more {symbol} groups this cycle "
             f"(see the database; suppressed to keep the feed readable).")
