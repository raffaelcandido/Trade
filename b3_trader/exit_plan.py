"""Exit Planner for B3: stop-loss and take-profit aligned to B3 ticks and BRL risk."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from .config import load_config, load_json, save_json
from .instruments import get_instrument


def build_plan(
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    entry_price: Optional[float] = None,
    stop_rule: str = "or-mid",
    target_rule: str = "2R",
    entry_path: Path = Path("trader/entry.json"),
    config_path: Path = Path("trader/config.json"),
) -> Dict[str, Any]:
    """Compute a complete exit plan (stop, target, R in points and BRL)."""
    cfg = load_config(config_path)
    entry_data: Dict[str, Any] = {}
    if entry_price is None or symbol is None or side is None:
        entry_data = load_json(entry_path)
        if entry_data.get("status") != "triggered":
            raise ValueError(
                f"trader/entry.json não está com status 'triggered' (atual: {entry_data.get('status')})."
            )
        symbol = symbol or entry_data["symbol"]
        side = side or entry_data["side"]
        entry_price = float(entry_price if entry_price is not None else entry_data["entry_price"])

    assert symbol is not None and side is not None and entry_price is not None
    if side not in ("buy", "sell"):
        raise ValueError(f"Lado inválido: {side!r} (use 'buy' ou 'sell')")

    spec = get_instrument(symbol)
    entry_ticked = spec.round_to_tick(entry_price, "nearest")

    # Resolve stop price
    sr = stop_rule.strip().lower()
    if sr == "or-mid":
        if "or_mid" not in entry_data:
            raise ValueError("Stop 'or-mid' exige or_mid em trader/entry.json.")
        raw_stop = float(entry_data["or_mid"])
    elif sr == "or-edge":
        key = "or_low" if side == "buy" else "or_high"
        if key not in entry_data:
            raise ValueError(f"Stop 'or-edge' exige {key} em trader/entry.json.")
        raw_stop = float(entry_data[key])
    elif sr == "or-low":
        if "or_low" not in entry_data:
            raise ValueError("Stop 'or-low' exige or_low em trader/entry.json.")
        raw_stop = float(entry_data["or_low"])
    elif sr == "or-high":
        if "or_high" not in entry_data:
            raise ValueError("Stop 'or-high' exige or_high em trader/entry.json.")
        raw_stop = float(entry_data["or_high"])
    elif sr.startswith("points:"):
        pts = float(sr.split(":", 1)[1])
        if pts <= 0:
            raise ValueError("Stop em pontos deve ser positivo.")
        raw_stop = (entry_ticked - pts) if side == "buy" else (entry_ticked + pts)
    elif sr.startswith("price:"):
        raw_stop = float(sr.split(":", 1)[1])
    elif sr.startswith("pct:"):
        pct = float(sr.split(":", 1)[1]) / 100.0
        if pct <= 0:
            raise ValueError("Stop percentual deve ser positivo.")
        raw_stop = entry_ticked * (1.0 - pct) if side == "buy" else entry_ticked * (1.0 + pct)
    else:
        raise ValueError(
            f"Regra de stop inválida: {stop_rule!r} "
            "(use or-mid, or-edge, or-low, or-high, points:N, price:X ou pct:X)"
        )

    # Round stop conservatively away from entry (down for buy, up for sell)
    stop_price = spec.round_to_tick(raw_stop, "down" if side == "buy" else "up")

    if side == "buy" and stop_price >= entry_ticked:
        raise ValueError(f"Stop ({stop_price}) precisa estar ABAIXO da entrada ({entry_ticked}) na compra.")
    if side == "sell" and stop_price <= entry_ticked:
        raise ValueError(f"Stop ({stop_price}) precisa estar ACIMA da entrada ({entry_ticked}) na venda.")

    risk_pts = round(abs(entry_ticked - stop_price), 6)
    if risk_pts < spec.tick_size:
        raise ValueError(f"Stop muito curto ({risk_pts} pts < 1 tick de {spec.tick_size}).")

    # Resolve target price
    tr = target_rule.strip().lower()
    if tr.endswith("r"):
        mult = float(tr[:-1])
        if mult <= 0:
            raise ValueError("Multiplicador R do alvo deve ser positivo.")
        raw_target = (entry_ticked + risk_pts * mult) if side == "buy" else (entry_ticked - risk_pts * mult)
    elif tr.startswith("points:"):
        pts = float(tr.split(":", 1)[1])
        if pts <= 0:
            raise ValueError("Alvo em pontos deve ser positivo.")
        raw_target = (entry_ticked + pts) if side == "buy" else (entry_ticked - pts)
    elif tr.startswith("price:"):
        raw_target = float(tr.split(":", 1)[1])
    else:
        raise ValueError(f"Regra de alvo inválida: {target_rule!r} (use 1R, 2R, points:N ou price:X)")

    target_price = spec.round_to_tick(raw_target, "nearest")
    if side == "buy" and target_price <= entry_ticked:
        raise ValueError(f"Alvo ({target_price}) precisa estar ACIMA da entrada ({entry_ticked}) na compra.")
    if side == "sell" and target_price >= entry_ticked:
        raise ValueError(f"Alvo ({target_price}) precisa estar ABAIXO da entrada ({entry_ticked}) na venda.")

    reward_pts = round(abs(target_price - entry_ticked), 6)
    rr = round(reward_pts / risk_pts, 2)
    risk_unit_brl = round(risk_pts * spec.point_value_brl, 2)
    reward_unit_brl = round(reward_pts * spec.point_value_brl, 2)
    flat_by = cfg["futures_flat_by"] if spec.asset_class == "future" else cfg["stocks_flat_by"]

    return {
        "symbol": spec.symbol,
        "canonical": spec.canonical,
        "asset_class": spec.asset_class,
        "side": side,
        "setup": entry_data.get("setup", "manual"),
        "entry_price": entry_ticked,
        "entry_time_brt": entry_data.get("entry_time_brt"),
        "stop_price": stop_price,
        "stop_rule": stop_rule,
        "target_price": target_price,
        "target_rule": target_rule,
        "tick_size": spec.tick_size,
        "point_value_brl": spec.point_value_brl,
        "risk_pts": risk_pts,
        "reward_pts": reward_pts,
        "risk_per_unit_brl": risk_unit_brl,
        "reward_per_unit_brl": reward_unit_brl,
        "rr": rr,
        "flat_by_brt": flat_by,
        "or_high": entry_data.get("or_high"),
        "or_low": entry_data.get("or_low"),
        "or_minutes": entry_data.get("or_minutes", 15),
    }


def write_plan(plan: Dict[str, Any], out_path: Path = Path("trader/plan.json")) -> None:
    save_json(out_path, plan)
