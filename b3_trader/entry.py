"""Entry Timer for B3: Opening Range Breakout (long & short) and Level Breakout."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .calendar_b3 import BRT, is_trading_day, now_brt, parse_hhmm, session_bounds
from .config import load_config, save_json
from .instruments import get_instrument
from .providers import Bar


def evaluate_entry(
    symbol: str,
    bars: List[Bar],
    setup: str = "orb",
    side: str = "both",
    or_minutes: int = 15,
    interval_minutes: int = 5,
    level: Optional[float] = None,
    now_override: Optional[str] = None,
    config_path: Path = Path("trader/config.json"),
) -> Dict[str, Any]:
    """Evaluate whether an entry setup has triggered on completed B3 bars."""
    spec = get_instrument(symbol)
    cfg = load_config(config_path)
    dt_now = now_brt(now_override)
    today = dt_now.date()

    trading_day, closed_reason = is_trading_day(today)
    if not trading_day:
        return {
            "status": "closed",
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "reason": closed_reason,
            "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
        }

    open_dt, close_dt = session_bounds(today, spec.session_open, spec.session_close)
    no_entries_str = (
        cfg["futures_no_entries_after"] if spec.asset_class == "future" else cfg["stocks_no_entries_after"]
    )
    no_entries_dt = datetime.combine(today, parse_hhmm(no_entries_str), tzinfo=BRT)

    if dt_now < open_dt:
        return {
            "status": "not_open",
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "reason": f"Pregão ainda não abriu (abertura às {open_dt.strftime('%H:%M')} BRT)",
            "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
        }

    # Filter today's completed bars up to dt_now
    bar_delta = timedelta(minutes=interval_minutes)
    day_bars = [
        b for b in bars
        if b.timestamp.date() == today
        and b.timestamp >= open_dt
        and (b.timestamp + bar_delta) <= dt_now
    ]

    if not day_bars:
        return {
            "status": "waiting",
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "reason": "Nenhum candle fechado ainda na sessão de hoje.",
            "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
        }

    if setup == "orb":
        or_end = open_dt + timedelta(minutes=or_minutes)
        if dt_now < or_end:
            return {
                "status": "waiting",
                "symbol": spec.symbol,
                "canonical": spec.canonical,
                "setup": "orb",
                "reason": f"Range de abertura ({or_minutes} min) ainda em formação até {or_end.strftime('%H:%M')} BRT.",
                "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
            }

        or_bars = [b for b in day_bars if b.timestamp < or_end]
        expected_or = max(1, or_minutes // interval_minutes)
        if len(or_bars) < expected_or:
            return {
                "status": "waiting",
                "symbol": spec.symbol,
                "canonical": spec.canonical,
                "setup": "orb",
                "reason": f"Range de abertura incompleto ({len(or_bars)}/{expected_or} candles).",
                "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
            }

        or_high = max(b.high for b in or_bars)
        or_low = min(b.low for b in or_bars)
        or_mid = spec.round_to_tick((or_high + or_low) / 2.0, "nearest")
        post_or_bars = [b for b in day_bars if b.timestamp >= or_end and (b.timestamp + bar_delta) <= no_entries_dt]

        allow_buy = side in ("buy", "both")
        allow_sell = side in ("sell", "both") and (
            cfg["allow_short_futures"] if spec.asset_class == "future" else cfg["allow_short_stocks"]
        )

        for b in post_or_bars:
            close_time = b.timestamp + bar_delta
            if allow_buy and b.close > or_high:
                return {
                    "status": "triggered",
                    "symbol": spec.symbol,
                    "canonical": spec.canonical,
                    "asset_class": spec.asset_class,
                    "setup": "orb",
                    "side": "buy",
                    "entry_price": b.close,
                    "entry_bar_open_brt": b.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "entry_time_brt": close_time.strftime("%Y-%m-%d %H:%M"),
                    "or_minutes": or_minutes,
                    "or_high": or_high,
                    "or_low": or_low,
                    "or_mid": or_mid,
                    "or_range_pts": round(or_high - or_low, 4),
                    "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
                }
            if allow_sell and b.close < or_low:
                return {
                    "status": "triggered",
                    "symbol": spec.symbol,
                    "canonical": spec.canonical,
                    "asset_class": spec.asset_class,
                    "setup": "orb",
                    "side": "sell",
                    "entry_price": b.close,
                    "entry_bar_open_brt": b.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "entry_time_brt": close_time.strftime("%Y-%m-%d %H:%M"),
                    "or_minutes": or_minutes,
                    "or_high": or_high,
                    "or_low": or_low,
                    "or_mid": or_mid,
                    "or_range_pts": round(or_high - or_low, 4),
                    "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
                }

        last_close = day_bars[-1].close
        expired = dt_now > no_entries_dt
        return {
            "status": "expired" if expired else "waiting",
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "setup": "orb",
            "side": side,
            "last_close": last_close,
            "or_high": or_high,
            "or_low": or_low,
            "or_mid": or_mid,
            "dist_to_high_pts": round(or_high - last_close, 4),
            "dist_to_low_pts": round(last_close - or_low, 4),
            "reason": (
                f"Janela de entrada encerrada às {no_entries_str} BRT sem rompimento."
                if expired
                else f"Aguardando fechamento acima de {or_high} ou abaixo de {or_low} (último: {last_close})."
            ),
            "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
        }

    if setup == "level":
        if level is None:
            raise ValueError("Para --setup level, informe --level <PREÇO>.")
        if side not in ("buy", "sell"):
            raise ValueError("Para --setup level, informe --side buy ou --side sell.")
        for b in day_bars:
            close_time = b.timestamp + bar_delta
            if close_time > no_entries_dt:
                break
            hit = (b.close > level) if side == "buy" else (b.close < level)
            if hit:
                return {
                    "status": "triggered",
                    "symbol": spec.symbol,
                    "canonical": spec.canonical,
                    "asset_class": spec.asset_class,
                    "setup": "level",
                    "side": side,
                    "level": level,
                    "entry_price": b.close,
                    "entry_bar_open_brt": b.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "entry_time_brt": close_time.strftime("%Y-%m-%d %H:%M"),
                    "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
                }
        last_close = day_bars[-1].close
        expired = dt_now > no_entries_dt
        return {
            "status": "expired" if expired else "waiting",
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "setup": "level",
            "side": side,
            "level": level,
            "last_close": last_close,
            "reason": f"Aguardando fechamento {'acima' if side == 'buy' else 'abaixo'} de {level} (último: {last_close}).",
            "as_of_brt": dt_now.strftime("%Y-%m-%d %H:%M BRT"),
        }

    raise ValueError(f"Setup desconhecido: {setup!r} (use 'orb' ou 'level')")


def write_entry(result: Dict[str, Any], out_path: Path = Path("trader/entry.json")) -> None:
    save_json(out_path, result)
