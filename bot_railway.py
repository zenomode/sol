#!/usr/bin/env python3
"""
Sweep + MSB Bot — Railway-tuned variant.

Same strategy as bot.py, with cloud-hosting hardening:
  - Retry logic (3 attempts, 1s backoff) around every Binance call
  - Startup message does NOT fetch levels (Railway cold-start rejections were causing
    fallback warnings). Levels appear in Setup Armed / Sweep / Entry messages,
    where fetches happen at HTF/LTF close (never at boot).
  - Verbose retry logs to stdout, visible in Railway's log stream

Everything else identical to bot.py.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

TELEGRAM_TOKEN = "8970547302:AAFsgPdAuLETzMvEoT841HizSjLC8xDqq3I"
TELEGRAM_CHAT_ID = 6056114263

FUT_KLINES = "https://fapi.binance.com/fapi/v1/klines"

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

TF_MINUTES = {
    "1d": 24 * 60, "4h": 4 * 60, "1h": 60,
    "30m": 30, "15m": 15, "5m": 5,
}
TF_BINANCE = {
    "1d": "1d", "4h": "4h", "1h": "1h",
    "30m": "30m", "15m": "15m", "5m": "5m",
}

HTF_LIST = ("1d", "4h", "1h")
LTF_LIST = ("30m", "15m", "5m")

HTF_TO_LTF = {"1d": "30m", "4h": "15m", "1h": "5m"}

LEVELS_FOR_TF = {
    "1d": {"long": ["PWL", "PML"], "short": ["PWH", "PMH"]},
    "4h": {"long": ["PDL", "PWL"], "short": ["PDH", "PWH"]},
    "1h": {"long": ["PDL"],        "short": ["PDH"]},
}

DERIVED_INTERVAL = {"PD": "1d", "PW": "1w", "PM": "1M"}

SETUP_TTL_HOURS = {"1d": 24 * 7, "4h": 48, "1h": 8}
MSB_LOOKBACK_BARS = 50

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"


def now_utc() -> datetime:
    return datetime.now(UTC)


def ist_str(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def iso_ist(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat()


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    with path.open("w") as f:
        json.dump(data, f, indent=2, default=str)


def next_close(tf: str, after: datetime) -> datetime:
    mins = TF_MINUTES[tf]
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta_min = int((after - epoch).total_seconds() // 60)
    next_bucket = (delta_min // mins + 1) * mins
    return epoch + timedelta(minutes=next_bucket)


def parse_candle_row(row) -> dict:
    return {
        "open_time": datetime.fromtimestamp(row[0] / 1000, tz=UTC),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "close_time": datetime.fromtimestamp(row[6] / 1000, tz=UTC),
    }


RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
STARTUP_DELAY_SECONDS = 8


def _binance_get(params: dict, tag: str):
    """GET the klines endpoint with retry. Returns parsed JSON or None."""
    last_err = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = requests.get(FUT_KLINES, params=params, timeout=8)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            print(f"[{ist_str(now_utc())}] {tag} attempt {attempt}/{RETRY_ATTEMPTS} failed: {e}")
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    print(f"[{ist_str(now_utc())}] {tag} gave up after {RETRY_ATTEMPTS} attempts: {last_err}")
    return None


def fetch_last_closed(symbol: str, interval: str) -> dict | None:
    rows = _binance_get(
        {"symbol": symbol, "interval": interval, "limit": 2},
        f"fetch_last_closed {symbol} {interval}",
    )
    if rows is None or len(rows) < 2:
        return None
    return parse_candle_row(rows[-2])


def fetch_recent_closed(symbol: str, interval: str, limit: int) -> list[dict]:
    rows = _binance_get(
        {"symbol": symbol, "interval": interval, "limit": limit + 1},
        f"fetch_recent_closed {symbol} {interval}",
    )
    if rows is None:
        return []
    return [parse_candle_row(row) for row in rows[:-1]]


def derive_levels(symbol: str) -> dict[str, float]:
    """Return PDL/PWL/PML + PDH/PWH/PMH from last closed daily/weekly/monthly candles."""
    out: dict[str, float] = {}
    for prefix, interval in DERIVED_INTERVAL.items():
        c = fetch_last_closed(symbol, interval)
        if c is not None:
            out[f"{prefix}L"] = c["low"]
            out[f"{prefix}H"] = c["high"]
    return out


def find_last_up_close_before(candles: list[dict], sweep_open_time: datetime) -> dict | None:
    for c in reversed(candles):
        if c["open_time"] >= sweep_open_time:
            continue
        if c["close"] > c["open"]:
            return c
    return None


def find_last_down_close_before(candles: list[dict], sweep_open_time: datetime) -> dict | None:
    for c in reversed(candles):
        if c["open_time"] >= sweep_open_time:
            continue
        if c["close"] < c["open"]:
            return c
    return None


def send_telegram(text: str, enabled: bool) -> None:
    if not enabled:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=8,
        )
    except Exception as e:
        print(f"[{ist_str(now_utc())}] telegram send failed: {e}")


def kv_block(rows: list[tuple[str, str]]) -> str:
    """Aligned key-value monospace block for Telegram."""
    if not rows:
        return ""
    width = max(len(k) for k, _ in rows)
    body = "\n".join(f"{k:<{width}}  {v}" for k, v in rows)
    return f"```\n{body}\n```"


def short_ist(dt_iso: str) -> str:
    """Trim ISO IST datetime to 'YYYY-MM-DD HH:MM'."""
    return dt_iso.replace("T", " ")[:16]


def get_setups(state: dict, symbol: str) -> list[dict]:
    return state.setdefault(symbol, {}).setdefault("setups", [])


def setup_exists(state: dict, symbol: str, setup_id: str) -> bool:
    return any(s["id"] == setup_id for s in get_setups(state, symbol))


def arm_setup(state: dict, symbol: str, htf: str, side: str, level_name: str,
              level_value: float, candle: dict, tg: bool) -> None:
    candle_open_ist = iso_ist(candle["open_time"])
    setup_id = f"{symbol}_{htf}_{level_name}_{side}_{candle_open_ist}"
    if setup_exists(state, symbol, setup_id):
        return

    if side == "long":
        swing_price = candle["low"]
        reaction = "RECLAIM" if candle["close"] > level_value else "BREAKDOWN"
    else:
        swing_price = candle["high"]
        reaction = "REJECTION" if candle["close"] < level_value else "BREAKOUT"

    ltf = HTF_TO_LTF[htf]
    expires_at = candle["close_time"] + timedelta(hours=SETUP_TTL_HOURS[htf])

    setup = {
        "id": setup_id,
        "side": side,
        "htf": htf,
        "ltf": ltf,
        "level_name": level_name,
        "level_value": level_value,
        "swing_price": swing_price,
        "swing_candle_open_ist": candle_open_ist,
        "swing_candle_close_utc": candle["close_time"].isoformat(),
        "reaction": reaction,
        "status": "armed",
        "sweep": None,
        "msb": None,
        "entry": None,
        "expires_at_utc": expires_at.isoformat(),
        "logged_at_ist": iso_ist(now_utc()),
    }
    get_setups(state, symbol).append(setup)

    swing_label = "Swing low" if side == "long" else "Swing high"
    icon = "🟢" if side == "long" else "🔴"
    title = "Long" if side == "long" else "Short"

    rows = [
        ("Symbol",    symbol),
        ("HTF",       htf.upper()),
        ("Level",     f"{level_name} @ {level_value}"),
        ("Reaction",  reaction),
        (swing_label, str(swing_price)),
        ("Candle",    short_ist(candle_open_ist)),
        ("LTF watch", ltf.upper()),
        ("Expires",   short_ist(iso_ist(expires_at))),
    ]
    msg = f"{icon} *{title} Setup Armed*\n" + kv_block(rows)
    print(f"[{ist_str(now_utc())}] ARMED {setup_id}")
    send_telegram(msg, tg)


def process_htf_close(htf: str, cfg: dict, state: dict) -> None:
    tg = cfg.get("telegram_enabled", True)
    for symbol in cfg["symbols"]:
        candle = fetch_last_closed(symbol, TF_BINANCE[htf])
        if candle is None:
            continue

        levels = derive_levels(symbol)

        for level_name in LEVELS_FOR_TF[htf]["long"]:
            v = levels.get(level_name)
            if v is None or candle["low"] > v:
                continue
            arm_setup(state, symbol, htf, "long", level_name, v, candle, tg)

        for level_name in LEVELS_FOR_TF[htf]["short"]:
            v = levels.get(level_name)
            if v is None or candle["high"] < v:
                continue
            arm_setup(state, symbol, htf, "short", level_name, v, candle, tg)

    save_json(STATE_PATH, state)


def process_ltf_close(ltf: str, cfg: dict, state: dict) -> None:
    tg = cfg.get("telegram_enabled", True)
    now = now_utc()

    for symbol in cfg["symbols"]:
        setups = get_setups(state, symbol)
        active = [s for s in setups if s["ltf"] == ltf and s["status"] in ("armed", "swept")]
        if not active:
            continue

        candle = fetch_last_closed(symbol, TF_BINANCE[ltf])
        if candle is None:
            continue

        for setup in active:
            expires_at = datetime.fromisoformat(setup["expires_at_utc"])
            if now > expires_at:
                setup["status"] = "expired"
                print(f"[{ist_str(now)}] EXPIRED {setup['id']}")
                rows = [
                    ("Symbol", symbol),
                    ("HTF",    f"{setup['htf'].upper()} · {setup['level_name']}"),
                    ("Side",   setup["side"].upper()),
                    ("Reason", "TTL elapsed, no entry"),
                ]
                send_telegram("⚫ *Setup Expired*\n" + kv_block(rows), tg)
                continue

            swing_close = datetime.fromisoformat(setup["swing_candle_close_utc"])
            if candle["close_time"] <= swing_close:
                continue

            side = setup["side"]

            if setup["status"] == "armed":
                swept = False
                if side == "long" and candle["low"] < setup["swing_price"]:
                    swept = True
                elif side == "short" and candle["high"] > setup["swing_price"]:
                    swept = True

                if swept:
                    recent = fetch_recent_closed(symbol, TF_BINANCE[ltf], MSB_LOOKBACK_BARS)
                    if side == "long":
                        anchor = find_last_up_close_before(recent, candle["open_time"])
                        msb_value = anchor["high"] if anchor else None
                    else:
                        anchor = find_last_down_close_before(recent, candle["open_time"])
                        msb_value = anchor["low"] if anchor else None

                    if anchor is None:
                        setup["status"] = "expired"
                        print(f"[{ist_str(now)}] MSB_NOT_FOUND {setup['id']}")
                        rows = [
                            ("Symbol", symbol),
                            ("LTF",    ltf.upper()),
                            ("Side",   side.upper()),
                            ("Reason", f"No {'up' if side == 'long' else 'down'}-close in last {MSB_LOOKBACK_BARS} bars"),
                        ]
                        send_telegram("⚠️ *MSB not found — setup discarded*\n" + kv_block(rows), tg)
                        continue

                    setup["sweep"] = {
                        "candle_open_ist": iso_ist(candle["open_time"]),
                        "extreme": candle["low"] if side == "long" else candle["high"],
                        "close": candle["close"],
                    }
                    setup["msb"] = {
                        "value": msb_value,
                        "candle_open_ist": iso_ist(anchor["open_time"]),
                    }
                    setup["status"] = "swept"

                    swing_label = "Swing low" if side == "long" else "Swing high"
                    sweep_label = "Wick low" if side == "long" else "Spike high"
                    wait_dir = "above" if side == "long" else "below"
                    icon = "🟢" if side == "long" else "🔴"

                    rows = [
                        ("Symbol",     symbol),
                        ("HTF",        f"{setup['htf'].upper()} · {setup['level_name']}"),
                        ("LTF",        ltf.upper()),
                        (swing_label,  str(setup["swing_price"])),
                        (sweep_label,  str(setup["sweep"]["extreme"])),
                        ("MSB level",  str(msb_value)),
                    ]
                    msg = (
                        f"{icon} *Sweep Detected*\n"
                        + kv_block(rows)
                        + f"\nWaiting for `{ltf.upper()}` close *{wait_dir}* MSB..."
                    )
                    print(f"[{ist_str(now)}] SWEPT {setup['id']} msb={msb_value}")
                    send_telegram(msg, tg)
                    continue

            if setup["status"] == "swept":
                msb_val = setup["msb"]["value"]
                broken = (side == "long" and candle["close"] > msb_val) or \
                         (side == "short" and candle["close"] < msb_val)
                if broken:
                    setup["entry"] = {
                        "price": candle["close"],
                        "candle_open_ist": iso_ist(candle["open_time"]),
                    }
                    setup["status"] = "fired"

                    icon = "🟢" if side == "long" else "🔴"
                    direction = "LONG" if side == "long" else "SHORT"
                    swing_label = "Swing low" if side == "long" else "Swing high"
                    sweep_label = "Sweep low" if side == "long" else "Sweep high"

                    rows = [
                        ("Symbol",      symbol),
                        ("HTF",         f"{setup['htf'].upper()} · {setup['level_name']} @ {setup['level_value']}"),
                        ("LTF",         ltf.upper()),
                        (swing_label,   str(setup["swing_price"])),
                        (sweep_label,   str(setup["sweep"]["extreme"])),
                        ("MSB level",   str(msb_val)),
                        ("Entry price", str(candle["close"])),
                        ("Time",        short_ist(iso_ist(candle["open_time"]))),
                    ]
                    msg = f"{icon} *{direction} ENTRY*\n" + kv_block(rows)
                    print(f"[{ist_str(now)}] FIRED {setup['id']} entry={candle['close']}")
                    send_telegram(msg, tg)

    save_json(STATE_PATH, state)


def main():
    if not CONFIG_PATH.exists():
        print(f"config not found: {CONFIG_PATH}")
        sys.exit(1)

    cfg = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {})
    buffer_s = cfg.get("poll_buffer_seconds", 15)
    tg = cfg.get("telegram_enabled", True)

    pair_rows = [("1D", "30M"), ("4H", "15M"), ("1H", "5M")]
    watch_rows = [
        ("1D", "PWL, PML", "PWH, PMH"),
        ("4H", "PDL, PWL", "PDH, PWH"),
        ("1H", "PDL",      "PDH"),
    ]

    parts = [
        "🤖 *Sweep+MSB Bot online*",
        f"Symbols: `{', '.join(cfg['symbols'])}`",
        "",
        "*Pairs (HTF ↔ LTF)*",
        "```\n" + "\n".join(f"{h}  ↔  {l}" for h, l in pair_rows) + "\n```",
        "*Level watchlist*",
        "```\n"
        + f"{'HTF':<4}  {'LONG':<10}  {'SHORT':<10}\n"
        + "\n".join(f"{h:<4}  {lo:<10}  {sh:<10}" for h, lo, sh in watch_rows)
        + "\n```",
    ]

    startup = "\n".join(parts)
    print(f"[{ist_str(now_utc())}] Sweep+MSB Bot online")
    send_telegram(startup, tg)

    all_tfs = list(TF_MINUTES.keys())

    while True:
        now = now_utc()
        upcoming = {tf: next_close(tf, now) for tf in all_tfs}
        earliest = min(upcoming.values())
        wait_s = max((earliest - now).total_seconds() + buffer_s, 1)

        preview = ", ".join(f"{tf}={ist_str(t)}" for tf, t in sorted(upcoming.items(), key=lambda x: x[1])[:3])
        print(f"[{ist_str(now)}] next: {preview} | sleep {int(wait_s)}s")
        time.sleep(wait_s)

        fire = now_utc()
        for tf, close_t in upcoming.items():
            if fire >= close_t:
                if tf in HTF_LIST:
                    process_htf_close(tf, cfg, state)
                elif tf in LTF_LIST:
                    process_ltf_close(tf, cfg, state)


if __name__ == "__main__":
    main()
