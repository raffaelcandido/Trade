"""Budget Manager for B3: contract and lot sizing in BRL for futures and stocks."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Tuple

from .config import load_config, load_json, save_json
from .instruments import get_instrument


def _floor_lot(qty: float, step: int) -> int:
    if qty < step:
        return 0
    return int(math.floor(qty / step) * step)


def size_position(
    plan: Dict[str, Any],
    config_path: Path = Path("trader/config.json"),
) -> Dict[str, Any]:
    """Compute whole B3 contracts or stock lots that fit all risk and margin limits."""
    cfg = load_config(config_path)
    symbol = plan["symbol"]
    side = plan["side"]
    entry = float(plan["entry_price"])
    stop = float(plan["stop_price"])
    target = float(plan["target_price"])
    spec = get_instrument(symbol)

    equity = float(cfg["account_equity_brl"])
    buffer_pct = float(cfg["cash_buffer_pct"])
    usable_cash = equity * (1.0 - buffer_pct)
    max_loss_brl = float(cfg["max_loss_per_trade_brl"])

    risk_per_unit = spec.risk_per_unit_brl(entry, stop, side)
    if risk_per_unit <= 0:
        raise ValueError("Risco por contrato/ação inválido (<= 0).")

    min_lot_risk = risk_per_unit * spec.lot_step
    if min_lot_risk > max_loss_brl:
        return {
            "status": "refused",
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "side": side,
            "reason": (
                f"Stop muito longo para o limite de risco: lote mínimo ({spec.lot_step}) "
                f"arrisca R$ {min_lot_risk:.2f}, acima do seu máximo de R$ {max_loss_brl:.2f} por trade."
            ),
            "risk_per_unit_brl": round(risk_per_unit, 2),
            "min_lot_risk_brl": round(min_lot_risk, 2),
            "max_loss_per_trade_brl": max_loss_brl,
        }

    limits: list[Tuple[int, str]] = []

    # 1. Max loss per trade limit
    qty_by_risk = _floor_lot(max_loss_brl / risk_per_unit, spec.lot_step)
    limits.append((qty_by_risk, f"risco máximo por trade (R$ {max_loss_brl:.2f})"))

    if spec.asset_class == "future":
        # 2. Margin per contract
        margin_map = cfg.get("margin_per_contract_brl", {})
        margin_per_contract = float(margin_map.get(spec.canonical, spec.default_margin_brl))
        if margin_per_contract > 0:
            qty_by_margin = _floor_lot(usable_cash / margin_per_contract, spec.lot_step)
            limits.append((qty_by_margin, f"margem disponível (R$ {usable_cash:.2f} / R$ {margin_per_contract:.2f})"))

        # 3. Max contracts cap
        cap_map = cfg.get("max_contracts", {})
        if spec.canonical in cap_map:
            qty_by_cap = _floor_lot(float(cap_map[spec.canonical]), spec.lot_step)
            limits.append((qty_by_cap, f"limite máximo de contratos ({cap_map[spec.canonical]})"))

        unit_margin_or_cost = margin_per_contract
    else:
        # 2. Stock position cap (% of equity)
        max_pos_brl = equity * float(cfg.get("max_position_pct_stocks", 0.35))
        qty_by_pos = _floor_lot(max_pos_brl / entry, spec.lot_step)
        limits.append((qty_by_pos, f"teto por posição ({cfg.get('max_position_pct_stocks', 0.35)*100:.0f}% do patrimônio)"))

        # 3. Usable cash after buffer
        qty_by_cash = _floor_lot(usable_cash / entry, spec.lot_step)
        limits.append((qty_by_cash, f"caixa livre após reserva (R$ {usable_cash:.2f})"))
        unit_margin_or_cost = entry

    qty, binding_limit = min(limits, key=lambda x: x[0])
    if qty < spec.lot_step:
        return {
            "status": "refused",
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "side": side,
            "reason": f"Quantidade resultante é 0 (limitado por: {binding_limit}).",
        }

    total_risk_brl = round(qty * risk_per_unit, 2)
    total_reward_brl = round(abs(target - entry) * spec.point_value_brl * qty, 2)
    required_capital_brl = round(qty * unit_margin_or_cost, 2)

    order = {
        "symbol": spec.symbol,
        "canonical": spec.canonical,
        "asset_class": spec.asset_class,
        "side": side,
        "qty": qty,
        "lot_step": spec.lot_step,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "entry_price": entry,
        "stop_loss_price": stop,
        "take_profit_price": target,
        "risk_pts": plan["risk_pts"],
        "reward_pts": plan["reward_pts"],
        "rr": plan["rr"],
        "max_loss_brl": total_risk_brl,
        "expected_profit_at_target_brl": total_reward_brl,
        "required_capital_or_margin_brl": required_capital_brl,
        "flat_by_brt": plan.get("flat_by_brt"),
        "paper": bool(cfg.get("paper", True)),
        "binding_limit": binding_limit,
        "reduce_only": False,
    }

    return {
        "status": "sized",
        "currency": "BRL",
        "orders": [order],
    }


def run_budget(
    plan_path: Path = Path("trader/plan.json"),
    config_path: Path = Path("trader/config.json"),
    out_path: Path = Path("trader/orders.json"),
    write: bool = True,
) -> Dict[str, Any]:
    plan = load_json(plan_path)
    result = size_position(plan, config_path=config_path)
    if write and result.get("status") == "sized":
        save_json(out_path, result)
    return result
