"""Risk Manager for B3: PASS / BLOCK checks in BRT and BRL before any order."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .calendar_b3 import BRT, is_trading_day, now_brt, parse_hhmm, session_bounds
from .config import load_config, load_json, save_json
from .instruments import get_instrument


def order_fingerprint(order: Dict[str, Any]) -> str:
    keys = ("symbol", "side", "qty", "entry_price", "stop_loss_price", "take_profit_price", "reduce_only")
    payload = {k: order.get(k) for k in keys}
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def check_order(
    order: Dict[str, Any],
    cfg: Dict[str, Any],
    dt_now: datetime,
    used_daily_risk_brl: float = 0.0,
    trades_today: int = 0,
    ignore_daily_limits: bool = False,
) -> Dict[str, Any]:
    """Validate a single B3 order against all risk rules. Returns PASS or BLOCK."""
    reasons: List[str] = []
    symbol = str(order["symbol"])
    side = str(order["side"]).lower()
    qty = int(order["qty"])
    reduce_only = bool(order.get("reduce_only", False))
    spec = get_instrument(symbol)

    # 1. Blocked tickers
    blocked = {t.upper() for t in cfg.get("blocked_tickers", [])}
    if spec.symbol in blocked or spec.canonical in blocked:
        reasons.append(f"Ativo bloqueado na configuração: {spec.symbol}.")

    # 2. Lot step check
    if qty <= 0 or qty % spec.lot_step != 0:
        reasons.append(f"Quantidade inválida ({qty}): precisa ser múltiplo de {spec.lot_step} para {spec.canonical}.")

    # Closing/flattening orders are never blocked by daily loss, entry window, or short rules
    if reduce_only:
        verdict = "PASS" if not reasons else "BLOCK"
        return {
            "verdict": verdict,
            "symbol": spec.symbol,
            "side": side,
            "qty": qty,
            "fingerprint": order_fingerprint(order),
            "checked_at_brt": dt_now.strftime("%Y-%m-%d %H:%M:%S BRT"),
            "reasons": reasons,
        }

    # 3. Short-selling permission
    if side == "sell":
        allowed = cfg.get("allow_short_futures", True) if spec.asset_class == "future" else cfg.get("allow_short_stocks", False)
        if not allowed:
            reasons.append(f"Venda a descoberto (short) desabilitada para {spec.asset_class}.")

    # 4. Market calendar and entry window (BRT)
    today = dt_now.date()
    trading_day, closed_reason = is_trading_day(today)
    if not trading_day:
        reasons.append(f"Mercado fechado: {closed_reason}.")
    else:
        open_dt, close_dt = session_bounds(today, spec.session_open, spec.session_close)
        no_entries_str = (
            cfg["futures_no_entries_after"] if spec.asset_class == "future" else cfg["stocks_no_entries_after"]
        )
        no_entries_dt = datetime.combine(today, parse_hhmm(no_entries_str), tzinfo=BRT)
        if dt_now < open_dt or dt_now > close_dt:
            reasons.append(
                f"Fora do horário de pregão B3 ({open_dt.strftime('%H:%M')}–{close_dt.strftime('%H:%M')} BRT; "
                f"agora: {dt_now.strftime('%H:%M')} BRT)."
            )
        elif dt_now > no_entries_dt:
            reasons.append(
                f"Fora da janela de novas entradas (limite: {no_entries_str} BRT; agora: {dt_now.strftime('%H:%M')} BRT)."
            )

    # 5. Stop-loss required and valid
    entry = float(order.get("entry_price", 0.0))
    stop = order.get("stop_loss_price")
    target = order.get("take_profit_price")
    if stop is None:
        reasons.append("Stop-loss obrigatório para novas entradas.")
    else:
        stop_f = float(stop)
        if side == "buy" and stop_f >= entry:
            reasons.append(f"Stop ({stop_f}) precisa estar abaixo da entrada ({entry}) na compra.")
        if side == "sell" and stop_f <= entry:
            reasons.append(f"Stop ({stop_f}) precisa estar acima da entrada ({entry}) na venda.")

        max_loss_brl = round(spec.risk_per_unit_brl(entry, stop_f, side) * qty, 2)
        if max_loss_brl > float(cfg["max_loss_per_trade_brl"]) + 1e-6:
            reasons.append(
                f"Perda no stop (R$ {max_loss_brl:.2f}) excede o limite por trade (R$ {cfg['max_loss_per_trade_brl']:.2f})."
            )
        if not ignore_daily_limits and (used_daily_risk_brl + max_loss_brl > float(cfg["daily_loss_limit_brl"]) + 1e-6):
            reasons.append(
                f"Risco acumulado do dia (R$ {used_daily_risk_brl + max_loss_brl:.2f}) "
                f"excede o limite diário (R$ {cfg['daily_loss_limit_brl']:.2f})."
            )

        if target is not None:
            target_f = float(target)
            risk_pts = abs(entry - stop_f)
            reward_pts = abs(target_f - entry)
            rr = (reward_pts / risk_pts) if risk_pts > 0 else 0.0
            if rr < float(cfg["min_rr"]) - 1e-6:
                reasons.append(f"Relação Retorno:Risco ({rr:.2f}R) abaixo do mínimo ({cfg['min_rr']}R).")

    # 6. Max trades per day
    if not ignore_daily_limits and trades_today >= int(cfg["max_trades_per_day"]):
        reasons.append(f"Limite diário de trades atingido ({trades_today}/{cfg['max_trades_per_day']}).")

    # 7. Margin / capital check
    usable_cash = float(cfg["account_equity_brl"]) * (1.0 - float(cfg["cash_buffer_pct"]))
    if spec.asset_class == "future":
        margin_per = float(cfg.get("margin_per_contract_brl", {}).get(spec.canonical, spec.default_margin_brl))
        req_margin = margin_per * qty
        if req_margin > usable_cash + 1e-6:
            reasons.append(f"Margem necessária (R$ {req_margin:.2f}) excede o caixa livre (R$ {usable_cash:.2f}).")
        max_c = cfg.get("max_contracts", {}).get(spec.canonical)
        if max_c is not None and qty > int(max_c):
            reasons.append(f"Contratos ({qty}) excedem o teto configurado ({max_c}) para {spec.canonical}.")
    else:
        cost = entry * qty
        if cost > usable_cash + 1e-6:
            reasons.append(f"Valor da ordem (R$ {cost:.2f}) excede o caixa livre (R$ {usable_cash:.2f}).")

    verdict = "PASS" if not reasons else "BLOCK"
    return {
        "verdict": verdict,
        "symbol": spec.symbol,
        "canonical": spec.canonical,
        "side": side,
        "qty": qty,
        "fingerprint": order_fingerprint(order),
        "checked_at_brt": dt_now.strftime("%Y-%m-%d %H:%M:%S BRT"),
        "reasons": reasons,
    }


def run_risk_check(
    orders_path: Path = Path("trader/orders.json"),
    config_path: Path = Path("trader/config.json"),
    state_path: Path = Path("trader/state/risk.json"),
    now_override: Optional[str] = None,
    used_daily_risk_brl: float = 0.0,
    trades_today: int = 0,
) -> Dict[str, Any]:
    cfg = load_config(config_path)
    orders_payload = load_json(orders_path)
    orders = orders_payload.get("orders", [])
    dt_now = now_brt(now_override)

    verdicts = [
        check_order(
            o,
            cfg=cfg,
            dt_now=dt_now,
            used_daily_risk_brl=used_daily_risk_brl,
            trades_today=trades_today,
        )
        for o in orders
    ]
    report = {
        "checked_at_brt": dt_now.strftime("%Y-%m-%d %H:%M:%S BRT"),
        "all_passed": all(v["verdict"] == "PASS" for v in verdicts) and len(verdicts) > 0,
        "verdicts": verdicts,
    }
    save_json(state_path, report)
    return report
