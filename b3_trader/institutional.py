"""Institutional Order Flow (MT5 Ticks/DOM), Round-Number (Milhar) walls, and Regression Channel analysis for B3."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .instruments import InstrumentSpec, get_instrument
from .providers import Bar


def fetch_mt5_order_flow(symbol: str, lookback_minutes: int = 5) -> Dict[str, Any]:
    """Read live Times & Trades aggression (BUY vs SELL volume, big lots) and Book (DOM) from MT5."""
    spec = get_instrument(symbol)
    big_lot_threshold = 50 if spec.canonical == "WINFUT" else (20 if spec.canonical in ("WDOFUT", "DOLFUT") else 5000)

    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError:
        return {
            "available": False,
            "source": "unavailable",
            "buy_vol": 0.0,
            "sell_vol": 0.0,
            "delta": 0.0,
            "delta_pct": 0.0,
            "big_buy_lots": 0,
            "big_sell_lots": 0,
            "book_bid_vol": 0.0,
            "book_ask_vol": 0.0,
            "book_imbalance_pct": 50.0,
            "flow_bias": "NEUTRO",
            "note": "Fluxo real disponível apenas no Windows conectado ao MT5.",
        }

    if not mt5.initialize():
        return {
            "available": False,
            "source": "disconnected",
            "buy_vol": 0.0,
            "sell_vol": 0.0,
            "delta": 0.0,
            "delta_pct": 0.0,
            "big_buy_lots": 0,
            "big_sell_lots": 0,
            "book_bid_vol": 0.0,
            "book_ask_vol": 0.0,
            "book_imbalance_pct": 50.0,
            "flow_bias": "NEUTRO",
            "note": "MT5 desconectado.",
        }

    try:
        from .providers import resolve_mt5_symbol

        mt5_sym = resolve_mt5_symbol(mt5, spec.symbol)
        tick_info = mt5.symbol_info_tick(mt5_sym)
        if tick_info is None:
            return {"available": False, "flow_bias": "NEUTRO", "delta": 0.0, "delta_pct": 0.0, "big_buy_lots": 0, "big_sell_lots": 0, "book_imbalance_pct": 50.0, "note": "Sem tick no MT5."}

        # Use MT5 broker server time from latest tick
        latest_ts = datetime.fromtimestamp(int(tick_info.time), tz=timezone.utc)
        from_ts = latest_ts - timedelta(minutes=lookback_minutes)
        ticks = mt5.copy_ticks_range(mt5_sym, from_ts, latest_ts, mt5.COPY_TICKS_TRADE)

        buy_vol = 0.0
        sell_vol = 0.0
        big_buy = 0
        big_sell = 0
        if ticks is not None and len(ticks) > 0:
            for t in ticks:
                flags = int(t["flags"])
                vol = float(t["volume_real"] or t["volume"] or 0.0)
                if vol <= 0:
                    continue
                is_buy = bool(flags & mt5.TICK_FLAG_BUY)
                is_sell = bool(flags & mt5.TICK_FLAG_SELL)
                if is_buy and not is_sell:
                    buy_vol += vol
                    if vol >= big_lot_threshold:
                        big_buy += 1
                elif is_sell and not is_buy:
                    sell_vol += vol
                    if vol >= big_lot_threshold:
                        big_sell += 1

        total_vol = buy_vol + sell_vol
        delta = round(buy_vol - sell_vol, 1)
        delta_pct = round(100.0 * delta / total_vol, 1) if total_vol > 0 else 0.0

        # Depth of Market (Book de Ofertas)
        book_bid = 0.0
        book_ask = 0.0
        if mt5.market_book_add(mt5_sym):
            book = mt5.market_book_get(mt5_sym)
            if book:
                for lvl in book:
                    v = float(getattr(lvl, "volume_real", 0) or getattr(lvl, "volume", 0) or 0)
                    if lvl.type == mt5.BOOK_TYPE_BUY:
                        book_bid += v
                    elif lvl.type == mt5.BOOK_TYPE_SELL:
                        book_ask += v
        total_book = book_bid + book_ask
        book_imb = round(100.0 * book_bid / total_book, 1) if total_book > 0 else 50.0

        if delta_pct >= 12.0 or (delta_pct >= 5.0 and book_imb >= 58.0):
            bias = "COMPRADOR"
        elif delta_pct <= -12.0 or (delta_pct <= -5.0 and book_imb <= 42.0):
            bias = "VENDEDOR"
        else:
            bias = "NEUTRO"

        return {
            "available": True,
            "source": f"MT5 ({mt5_sym})",
            "buy_vol": round(buy_vol, 1),
            "sell_vol": round(sell_vol, 1),
            "delta": delta,
            "delta_pct": delta_pct,
            "big_buy_lots": big_buy,
            "big_sell_lots": big_sell,
            "big_lot_threshold": big_lot_threshold,
            "book_bid_vol": round(book_bid, 1),
            "book_ask_vol": round(book_ask, 1),
            "book_imbalance_pct": book_imb,
            "flow_bias": bias,
            "note": f"Fluxo {lookback_minutes}m ({mt5_sym}): Delta {delta:+.0f} ({delta_pct:+.1f}%) | Book {book_imb:.0f}% Compra | Lotes >= {big_lot_threshold}: {big_buy}C / {big_sell}V",
        }
    except Exception as exc:
        return {
            "available": False,
            "source": "error",
            "buy_vol": 0.0,
            "sell_vol": 0.0,
            "delta": 0.0,
            "delta_pct": 0.0,
            "big_buy_lots": 0,
            "big_sell_lots": 0,
            "book_bid_vol": 0.0,
            "book_ask_vol": 0.0,
            "book_imbalance_pct": 50.0,
            "flow_bias": "NEUTRO",
            "note": f"Erro ao ler fluxo/book MT5: {exc}",
        }
    finally:
        mt5.shutdown()


def analyze_milhar_levels(spec: InstrumentSpec, price: float) -> Dict[str, Any]:
    """Identify nearest psychological round-number walls (e.g. 1000-pt Milhar on WINFUT, 50-pt on WDO/DOL)."""
    if spec.canonical in ("WINFUT", "INDFUT"):
        step = 1000.0
        danger_zone = 120.0
    elif spec.canonical in ("WDOFUT", "DOLFUT"):
        step = 50.0
        danger_zone = 6.0
    else:
        step = 1.0
        danger_zone = 0.12

    below = math.floor(price / step) * step
    above = below + step
    dist_above = round(above - price, 2)
    dist_below = round(price - below, 2)

    return {
        "step": step,
        "support_milhar": round(below, 2),
        "resistance_milhar": round(above, 2),
        "dist_to_resistance": dist_above,
        "dist_to_support": dist_below,
        "danger_zone": danger_zone,
        "blocked_buy_wall": dist_above <= danger_zone,
        "blocked_sell_wall": dist_below <= danger_zone,
    }


def analyze_channel_and_frequency(bars: List[Bar], window: int = 24) -> Dict[str, Any]:
    """Fit a linear regression trend channel and estimate intraday market frequency (avg swing)."""
    sample = bars[-window:] if len(bars) >= window else bars
    n = len(sample)
    if n < 6:
        return {
            "slope": 0.0,
            "channel_bias": "LATERAL",
            "upper": 0.0,
            "mid": 0.0,
            "lower": 0.0,
            "position_pct": 50.0,
            "frequency_pts": 0.0,
            "overbought": False,
            "oversold": False,
        }

    xs = list(range(n))
    ys = [b.close for b in sample]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs) or 1.0
    slope = num / den
    intercept = mean_y - slope * mean_x

    residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
    std = math.sqrt(sum(r * r for r in residuals) / n) or 1.0

    mid_now = slope * (n - 1) + intercept
    upper = mid_now + 1.6 * std
    lower = mid_now - 1.6 * std
    last_c = ys[-1]
    pos_pct = round(100.0 * (last_c - lower) / max(1e-6, upper - lower), 1)

    # Market frequency: average 3-bar swing amplitude
    swings: List[float] = []
    for i in range(0, n - 2, 3):
        chunk = sample[i:i + 3]
        swings.append(max(b.high for b in chunk) - min(b.low for b in chunk))
    freq_pts = round(sum(swings) / len(swings), 1) if swings else 0.0

    if slope > 0.5:
        bias = "CANAL DE ALTA"
    elif slope < -0.5:
        bias = "CANAL DE BAIXA"
    else:
        bias = "CANAL LATERAL"

    return {
        "slope": round(slope, 2),
        "channel_bias": bias,
        "upper": round(upper, 2),
        "mid": round(mid_now, 2),
        "lower": round(lower, 2),
        "position_pct": max(0.0, min(100.0, pos_pct)),
        "frequency_pts": freq_pts,
        "overbought": pos_pct >= 88.0,
        "oversold": pos_pct <= 12.0,
    }


def compute_institutional_score(
    side: str,
    context: Dict[str, Any],
    flow: Dict[str, Any],
    milhar: Dict[str, Any],
    channel: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute a 0–100 institutional confluence score with clear component breakdown."""
    score = 0
    reasons: List[str] = []
    want_trend = "ALTA" if side == "buy" else "BAIXA"
    want_flow = "COMPRADOR" if side == "buy" else "VENDEDOR"

    # 1. Trend M5 + M15 (20 pts)
    if context.get("trend") == want_trend:
        score += 10
        reasons.append(f"M5 {want_trend} (+10)")
    if context.get("m15_trend") == want_trend:
        score += 10
        reasons.append(f"M15 {want_trend} (+10)")

    # 2. ADX trend strength (15 pts)
    adx = float(context.get("adx14", 0.0))
    if adx >= 25.0:
        score += 15
        reasons.append(f"ADX forte {adx:.1f} (+15)")
    elif adx >= 20.0:
        score += 10
        reasons.append(f"ADX bom {adx:.1f} (+10)")

    # 3. Order flow aggression & Book DOM (35 pts)
    if flow.get("available"):
        if flow.get("flow_bias") == want_flow:
            score += 20
            reasons.append(f"Fluxo {want_flow} {flow.get('delta_pct', 0):+.1f}% (+20)")
        imb = float(flow.get("book_imbalance_pct", 50.0))
        if (side == "buy" and imb >= 56.0) or (side == "sell" and imb <= 44.0):
            score += 10
            reasons.append(f"Book favorável {imb:.0f}%C (+10)")
        big_fav = int(flow.get("big_buy_lots", 0)) if side == "buy" else int(flow.get("big_sell_lots", 0))
        big_opp = int(flow.get("big_sell_lots", 0)) if side == "buy" else int(flow.get("big_buy_lots", 0))
        if big_fav > big_opp and big_fav >= 2:
            score += 5
            reasons.append(f"Lotes grandes a favor {big_fav}x{big_opp} (+5)")
    else:
        # Neutral baseline when offline/sim so simulator remains usable
        score += 20
        reasons.append("Fluxo offline/sim (+20 base)")

    # 4. Relative Volume (10 pts)
    rvol = float(context.get("rvol", 1.0))
    if rvol >= 1.2:
        score += 10
        reasons.append(f"Volume {rvol:.2f}x (+10)")

    # 5. Milhar wall & Channel room (20 pts)
    wall_blocked = milhar["blocked_buy_wall"] if side == "buy" else milhar["blocked_sell_wall"]
    if not wall_blocked:
        score += 10
        reasons.append("Livre de parede de Milhar (+10)")
    else:
        wall_px = milhar["resistance_milhar"] if side == "buy" else milhar["support_milhar"]
        reasons.append(f"ATENÇÃO: parede de Milhar {wall_px} próxima (0)")

    stretched = channel["overbought"] if side == "buy" else channel["oversold"]
    if not stretched:
        score += 10
        reasons.append(f"Espaço no {channel['channel_bias']} (+10)")
    else:
        reasons.append("ATENÇÃO: preço esticado na borda do canal (0)")

    return {
        "side": side,
        "score": min(100, score),
        "wall_blocked": wall_blocked,
        "stretched": stretched,
        "reasons": reasons,
    }
