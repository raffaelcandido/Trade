"""Time Machine for B3: 10,000 Monte Carlo session replays + historical setup replay."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .calendar_b3 import parse_hhmm
from .config import load_json, save_json
from .instruments import get_instrument
from .providers import Bar


def _group_sessions(bars: List[Bar]) -> Dict[date, List[Bar]]:
    sessions: Dict[date, List[Bar]] = defaultdict(list)
    for b in bars:
        sessions[b.timestamp.date()].append(b)
    for d in sessions:
        sessions[d].sort(key=lambda x: x.timestamp)
    return dict(sorted(sessions.items()))


def _bar_hits_level(bar_low: float, bar_high: float, stop: float, target: float, side: str) -> Optional[str]:
    """Conservative hit check: if both stop and target fall inside the same bar, stop wins."""
    if side == "buy":
        hit_stop = bar_low <= stop
        hit_target = bar_high >= target
    else:
        hit_stop = bar_high >= stop
        hit_target = bar_low <= target

    if hit_stop and hit_target:
        return "stop"
    if hit_stop:
        return "stop"
    if hit_target:
        return "target"
    return None


def _verdict(target_pct: float, stop_pct: float, avg_r: float, rr: float) -> str:
    breakeven_pct = 100.0 / (1.0 + rr)
    if avg_r >= 0.15 and target_pct > breakeven_pct + 3.0:
        return f"Vantagem estatística positiva nos últimos pregões (R médio {avg_r:+.2f}, acima do break-even de {breakeven_pct:.1f}%)."
    if avg_r <= -0.10 or target_pct < breakeven_pct - 3.0:
        return f"Desfavorável nos últimos pregões (R médio {avg_r:+.2f}, abaixo do break-even de {breakeven_pct:.1f}%)."
    return f"Próximo de cara ou coroa (R médio {avg_r:+.2f}; break-even teórico para {rr:.2f}R é {breakeven_pct:.1f}%)."


def run_timemachine(
    plan: Dict[str, Any],
    bars: List[Bar],
    replays: int = 10000,
    seed: int = 42,
    min_sessions: int = 10,
) -> Dict[str, Any]:
    """Run Monte Carlo intraday replay + historical ORB replay on B3 5-min bars."""
    symbol = plan["symbol"]
    side = plan["side"]
    entry = float(plan["entry_price"])
    stop = float(plan["stop_price"])
    target = float(plan["target_price"])
    risk_pts = float(plan["risk_pts"])
    rr = float(plan["rr"])
    flat_by_t = parse_hhmm(plan.get("flat_by_brt", "17:55"))
    spec = get_instrument(symbol)

    entry_time_str = plan.get("entry_time_brt")
    if entry_time_str and " " in entry_time_str:
        entry_date_str, entry_hhmm = entry_time_str.split(" ", 1)
        entry_t = parse_hhmm(entry_hhmm)
        exclude_date: Optional[date] = date.fromisoformat(entry_date_str)
    else:
        entry_t = parse_hhmm("09:15" if spec.asset_class == "future" else "10:15")
        exclude_date = None

    sessions = _group_sessions(bars)
    pool_sessions = {d: sb for d, sb in sessions.items() if d != exclude_date and len(sb) >= 10}
    if len(pool_sessions) < min_sessions:
        raise ValueError(
            f"Histórico insuficiente para o Time Machine: {len(pool_sessions)} pregões encontrados "
            f"(mínimo exigido: {min_sessions}). Exporte pelo menos {min_sessions} dias de candles 5m."
        )

    # Build time-slot buckets of relative (high_ret, low_ret, close_ret) after entry_t up to flat_by_t
    slot_pool: Dict[time, List[Tuple[float, float, float]]] = defaultdict(list)
    for sb in pool_sessions.values():
        prev_close: Optional[float] = None
        for b in sb:
            t = b.timestamp.time()
            ref = prev_close if prev_close is not None and prev_close > 0 else b.open
            if entry_t <= t <= flat_by_t and ref > 0:
                slot_pool[t].append(
                    (
                        (b.high - ref) / ref,
                        (b.low - ref) / ref,
                        (b.close - ref) / ref,
                    )
                )
            prev_close = b.close

    slots = sorted(slot_pool.keys())
    if not slots:
        raise ValueError(
            f"Nenhum candle encontrado entre {entry_t.strftime('%H:%M')} e {flat_by_t.strftime('%H:%M')} BRT."
        )

    rng = random.Random(seed)
    hits_target = 0
    hits_stop = 0
    open_at_close = 0
    r_outcomes: List[float] = []
    close_prices: List[float] = []

    for _ in range(replays):
        price = entry
        outcome = "open"
        for slot in slots:
            h_ret, l_ret, c_ret = rng.choice(slot_pool[slot])
            bar_high = price * (1.0 + h_ret)
            bar_low = price * (1.0 + l_ret)
            bar_close = price * (1.0 + c_ret)
            hit = _bar_hits_level(bar_low, bar_high, stop, target, side)
            if hit == "target":
                hits_target += 1
                r_outcomes.append(rr)
                outcome = "target"
                break
            if hit == "stop":
                hits_stop += 1
                r_outcomes.append(-1.0)
                outcome = "stop"
                break
            price = bar_close

        if outcome == "open":
            open_at_close += 1
            close_prices.append(price)
            diff = (price - entry) if side == "buy" else (entry - price)
            r_outcomes.append(diff / risk_pts)

    target_pct = round(100.0 * hits_target / replays, 1)
    stop_pct = round(100.0 * hits_stop / replays, 1)
    open_pct = round(100.0 * open_at_close / replays, 1)
    avg_r = round(sum(r_outcomes) / len(r_outcomes), 3)
    expected_unit_brl = round(avg_r * float(plan["risk_per_unit_brl"]), 2)

    # Historical replay of ORB setup on past sessions (when plan came from ORB)
    history = {"fired": 0, "target": 0, "stop": 0, "neither": 0, "sessions": len(pool_sessions)}
    if plan.get("setup") == "orb":
        or_minutes = int(plan.get("or_minutes", 15))
        open_t = parse_hhmm(spec.session_open)
        or_end_total = open_t.hour * 60 + open_t.minute + or_minutes
        or_end_t = time(or_end_total // 60, or_end_total % 60)

        for sb in pool_sessions.values():
            or_bars = [b for b in sb if open_t <= b.timestamp.time() < or_end_t]
            post_bars = [b for b in sb if or_end_t <= b.timestamp.time() <= flat_by_t]
            if not or_bars or not post_bars:
                continue
            or_hi = max(b.high for b in or_bars)
            or_lo = min(b.low for b in or_bars)
            or_mid = spec.round_to_tick((or_hi + or_lo) / 2.0, "nearest")

            triggered_idx: Optional[int] = None
            trig_price = 0.0
            for idx, b in enumerate(post_bars):
                if (side == "buy" and b.close > or_hi) or (side == "sell" and b.close < or_lo):
                    triggered_idx = idx
                    trig_price = b.close
                    break
            if triggered_idx is None:
                continue

            # Apply same stop/target proportion
            h_stop = or_mid if plan.get("stop_rule") == "or-mid" else (
                (trig_price - risk_pts) if side == "buy" else (trig_price + risk_pts)
            )
            h_risk = abs(trig_price - h_stop)
            if h_risk < spec.tick_size:
                continue
            h_target = (trig_price + h_risk * rr) if side == "buy" else (trig_price - h_risk * rr)

            history["fired"] += 1
            res = "neither"
            for b in post_bars[triggered_idx + 1:]:
                hit = _bar_hits_level(b.low, b.high, h_stop, h_target, side)
                if hit:
                    res = hit
                    break
            history[res] += 1

    return {
        "symbol": symbol,
        "canonical": spec.canonical,
        "side": side,
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "rr": rr,
        "replays": replays,
        "seed": seed,
        "pool_sessions": len(pool_sessions),
        "target_first_pct": target_pct,
        "stop_first_pct": stop_pct,
        "open_at_flat_by_pct": open_pct,
        "avg_r": avg_r,
        "expected_per_unit_brl": expected_unit_brl,
        "flat_by_brt": flat_by_t.strftime("%H:%M"),
        "history": history,
        "verdict": _verdict(target_pct, stop_pct, avg_r, rr),
    }


def load_plan_and_run(
    bars: List[Bar],
    plan_path: Path = Path("trader/plan.json"),
    replays: int = 10000,
    seed: int = 42,
    min_sessions: int = 10,
) -> Dict[str, Any]:
    plan = load_json(plan_path)
    return run_timemachine(plan=plan, bars=bars, replays=replays, seed=seed, min_sessions=min_sessions)


def write_timemachine(result: Dict[str, Any], out_path: Path = Path("trader/timemachine.json")) -> None:
    save_json(out_path, result)
