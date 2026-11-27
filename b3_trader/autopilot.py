"""Intraday technical indicators, dynamic ATR stop, Breakeven 1R, and Autopilot engine for B3."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .budget import size_position
from .calendar_b3 import BRT, is_trading_day, parse_hhmm, session_bounds
from .exit_plan import build_plan
from .instruments import InstrumentSpec, get_instrument
from .providers import Bar
from .risk import check_order
from .timemachine import run_timemachine


def calc_ema(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def calc_atr(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(bars)):
        b = bars[i]
        prev = bars[i - 1]
        if b.timestamp.date() != prev.timestamp.date():
            # Do not let overnight B3 gaps artificially inflate intraday ATR
            tr = b.high - b.low
        else:
            tr = max(b.high - b.low, abs(b.high - prev.close), abs(b.low - prev.close))
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window)


def calc_vwap(day_bars: List[Bar]) -> List[float]:
    out: List[float] = []
    cum_pv = 0.0
    cum_v = 0.0
    for b in day_bars:
        typical = (b.high + b.low + b.close) / 3.0
        vol = b.volume if b.volume > 0 else 1.0
        cum_pv += typical * vol
        cum_v += vol
        out.append(cum_pv / cum_v)
    return out


def calc_adx(bars: List[Bar], period: int = 14) -> float:
    """Compute Wilder's ADX(14) trend-strength indicator ignoring overnight gaps."""
    if len(bars) < period + 2:
        return 0.0
    trs: List[float] = []
    plus_dms: List[float] = []
    minus_dms: List[float] = []
    for i in range(1, len(bars)):
        cur, prev = bars[i], bars[i - 1]
        if cur.timestamp.date() != prev.timestamp.date():
            # Ignore overnight B3 gap in directional movement and true range
            tr = cur.high - cur.low
            plus_dm = 0.0
            minus_dm = 0.0
        else:
            up_move = cur.high - prev.high
            down_move = prev.low - cur.low
            plus_dm = up_move if (up_move > down_move and up_move > 0) else 0.0
            minus_dm = down_move if (down_move > up_move and down_move > 0) else 0.0
            tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    if len(trs) < period:
        return 0.0

    # Wilder smoothing
    sm_tr = sum(trs[:period])
    sm_pdm = sum(plus_dms[:period])
    sm_mdm = sum(minus_dms[:period])
    dxs: List[float] = []

    for i in range(period, len(trs)):
        sm_tr = sm_tr - (sm_tr / period) + trs[i]
        sm_pdm = sm_pdm - (sm_pdm / period) + plus_dms[i]
        sm_mdm = sm_mdm - (sm_mdm / period) + minus_dms[i]
        if sm_tr <= 0:
            continue
        pdi = 100.0 * (sm_pdm / sm_tr)
        mdi = 100.0 * (sm_mdm / sm_tr)
        denom = pdi + mdi
        if denom > 0:
            dxs.append(100.0 * abs(pdi - mdi) / denom)

    if not dxs:
        return 0.0
    adx = sum(dxs[:period]) / min(period, len(dxs))
    for dx in dxs[period:]:
        adx = ((adx * (period - 1)) + dx) / period
    return round(adx, 2)


def analyze_m15_trend(bars: List[Bar]) -> str:
    """Aggregate 5m bars into 15-minute candles and classify M15 trend (ALTA/BAIXA/LATERAL)."""
    if len(bars) < 30:
        return "LATERAL"
    buckets: Dict[Tuple[int, int, int, int, int], List[Bar]] = {}
    for b in bars[-180:]:
        slot_min = (b.timestamp.minute // 15) * 15
        key = (b.timestamp.year, b.timestamp.month, b.timestamp.day, b.timestamp.hour, slot_min)
        buckets.setdefault(key, []).append(b)
    m15_closes = [grp[-1].close for _, grp in sorted(buckets.items())]
    if len(m15_closes) < 10:
        return "LATERAL"
    ema9 = calc_ema(m15_closes, 9)[-1]
    ema21 = calc_ema(m15_closes, 21)[-1]
    last_c = m15_closes[-1]
    if ema9 > ema21 and last_c >= ema9:
        return "ALTA"
    if ema9 < ema21 and last_c <= ema9:
        return "BAIXA"
    return "LATERAL"


def calc_rvol(bars: List[Bar], period: int = 20) -> float:
    """Compute relative volume of the latest completed candle vs 20-bar average."""
    if len(bars) < 5:
        return 1.0
    completed = bars[:-1] if len(bars) >= 6 else bars
    last_vol = completed[-1].volume
    window = [b.volume for b in completed[-(period + 1):-1] if b.volume > 0]
    if not window or last_vol <= 0:
        return 1.0
    avg_vol = sum(window) / len(window)
    return round(last_vol / avg_vol, 2) if avg_vol > 0 else 1.0


def choose_dynamic_stop_pts(
    spec: InstrumentSpec,
    bars: List[Bar],
    max_loss_per_trade_brl: float,
    atr_mult: float = 1.5,
) -> float:
    """Compute a volatility-adapted stop in points, bounded by the user's BRL risk limit."""
    atr = calc_atr(bars, period=14)
    raw_pts = atr * atr_mult if atr > 0 else (200.0 if spec.canonical == "WINFUT" else 8.0)

    # Hard risk ceiling for 1 minimum lot so the trade never gets refused for wide stop
    max_pts_by_risk = max_loss_per_trade_brl / (spec.point_value_brl * spec.lot_step)
    min_pts = max(spec.tick_size * 4, 100.0 if spec.canonical == "WINFUT" else spec.tick_size * 6)
    clamped = max(min_pts, min(raw_pts, max_pts_by_risk))
    return max(spec.tick_size, spec.round_to_tick(clamped, "down"))


def analyze_intraday_context(
    symbol: str,
    bars: List[Bar],
    dt_now: datetime,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute live indicators (EMA9, EMA21, VWAP, ATR14, ADX14, M15, RVOL) and fresh signals."""
    spec = get_instrument(symbol)
    today = dt_now.date()
    day_bars = [b for b in bars if b.timestamp.date() == today]
    recent = bars[-60:] if len(bars) >= 60 else bars
    closes = [b.close for b in recent]

    ema9_series = calc_ema(closes, 9)
    ema21_series = calc_ema(closes, 21)
    vwap_series = calc_vwap(day_bars) if day_bars else []
    atr14 = round(calc_atr(recent, 14), 2)
    adx14 = calc_adx(bars[-90:] if len(bars) >= 90 else bars, 14)
    m15_trend = analyze_m15_trend(bars)
    rvol = calc_rvol(bars, 20)
    dyn_stop_pts = choose_dynamic_stop_pts(spec, recent, float(cfg["max_loss_per_trade_brl"]))

    ema9 = round(ema9_series[-1], 2) if ema9_series else 0.0
    ema21 = round(ema21_series[-1], 2) if ema21_series else 0.0
    vwap = round(vwap_series[-1], 2) if vwap_series else (closes[-1] if closes else 0.0)
    last_close = closes[-1] if closes else 0.0

    if ema9 > ema21 and last_close > vwap:
        trend = "ALTA"
    elif ema9 < ema21 and last_close < vwap:
        trend = "BAIXA"
    else:
        trend = "LATERAL"

    # Detect fresh intraday signal on the last completed 5m candle
    # We use day_bars[:-1] as completed bars when the last bar is still forming
    signal: Optional[Dict[str, Any]] = None
    completed = day_bars[:-1] if len(day_bars) >= 5 else day_bars
    if len(completed) >= 5:
        last_done = completed[-1]
        prev3 = completed[-4:-1]
        prev3_high = max(b.high for b in prev3)
        prev3_low = min(b.low for b in prev3)
        allow_short = cfg["allow_short_futures"] if spec.asset_class == "future" else cfg["allow_short_stocks"]

        # Signal 1: Fresh ORB 15m breakout (first 3 bars of the session)
        open_dt, _ = session_bounds(today, spec.session_open, spec.session_close)
        or_end = open_dt + timedelta(minutes=15)
        or_bars = [b for b in completed if open_dt <= b.timestamp < or_end]
        if len(or_bars) >= 3 and last_done.timestamp >= or_end:
            or_hi = max(b.high for b in or_bars)
            or_lo = min(b.low for b in or_bars)
            # Only treat ORB as fresh if it broke on the latest completed candle
            prev_done = completed[-2]
            if last_done.close > or_hi and prev_done.close <= or_hi:
                signal = {
                    "strategy": "ORB 15m",
                    "side": "buy",
                    "price": last_done.close,
                    "bar_time": last_done.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "reason": f"Rompimento fresco da máxima de abertura ({or_hi}).",
                }
            elif allow_short and last_done.close < or_lo and prev_done.close >= or_lo:
                signal = {
                    "strategy": "ORB 15m",
                    "side": "sell",
                    "price": last_done.close,
                    "bar_time": last_done.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "reason": f"Rompimento fresco da mínima de abertura ({or_lo}).",
                }

        # Signal 2: Trend + VWAP continuation breakout (works all day long!)
        if signal is None:
            if trend == "ALTA" and last_done.close > prev3_high and last_done.close > last_done.open:
                signal = {
                    "strategy": "Tendência + VWAP",
                    "side": "buy",
                    "price": last_done.close,
                    "bar_time": last_done.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "reason": f"Tendência de ALTA (EMA9 > EMA21 > VWAP) com rompimento de {prev3_high}.",
                }
            elif allow_short and trend == "BAIXA" and last_done.close < prev3_low and last_done.close < last_done.open:
                signal = {
                    "strategy": "Tendência + VWAP",
                    "side": "sell",
                    "price": last_done.close,
                    "bar_time": last_done.timestamp.strftime("%Y-%m-%d %H:%M"),
                    "reason": f"Tendência de BAIXA (EMA9 < EMA21 < VWAP) com perda de {prev3_low}.",
                }

    return {
        "trend": trend,
        "ema9": ema9,
        "ema21": ema21,
        "vwap": vwap,
        "atr14": atr14,
        "adx14": adx14,
        "m15_trend": m15_trend,
        "rvol": rvol,
        "dynamic_stop_pts": dyn_stop_pts,
        "signal": signal,
        "ema9_tail": [round(x, 2) for x in ema9_series[-45:]],
        "ema21_tail": [round(x, 2) for x in ema21_series[-45:]],
    }


def apply_breakeven_1r(
    bridge: Any,
    symbol: str,
    last_price: float,
    enabled: bool = True,
) -> Optional[Dict[str, Any]]:
    """Move open position's stop-loss to entry price (0x0) once price advances >= 1R."""
    if not enabled:
        return None
    spec = get_instrument(symbol)
    st = bridge._load_state()
    changed = False
    event: Optional[Dict[str, Any]] = None

    for pos in st.get("positions", []):
        if pos["canonical"] != spec.canonical or pos.get("breakeven_moved"):
            continue
        entry = float(pos["entry_price"])
        sl = float(pos["stop_loss_price"])
        side = pos["side"]
        risk_pts = abs(entry - sl)
        if risk_pts <= 0:
            continue
        moved_pts = (last_price - entry) if side == "buy" else (entry - last_price)
        if moved_pts >= risk_pts:
            pos["stop_loss_price"] = entry
            pos["breakeven_moved"] = True
            changed = True
            event = {
                "symbol": pos["symbol"],
                "new_stop": entry,
                "message": f"Breakeven (1R) ativado em {pos['symbol']}: Stop movido para {entry} (risco R$ 0,00).",
            }
            st.setdefault("orders_log", []).insert(
                0,
                {
                    "id": f"BE-{pos['id'][-4:]}",
                    "time_brt": datetime.now(BRT).strftime("%Y-%m-%d %H:%M:%S BRT"),
                    "symbol": pos["symbol"],
                    "side": side,
                    "qty": pos["qty"],
                    "price": entry,
                    "status": "BREAKEVEN 1R (STOP NO 0x0)",
                    "pnl_brl": 0.0,
                },
            )
    if changed:
        bridge._save_state(st)
    return event


def run_autopilot_step(
    bridge: Any,
    symbol: str,
    mode: str,
    bars: List[Bar],
    dt_now: datetime,
    cfg: Dict[str, Any],
    context: Dict[str, Any],
    news_status: Optional[Dict[str, Any]] = None,
    institutional: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one autonomous robot evaluation cycle (Breakeven, EOD flatten, news/ADX/M15/RVOL filters, or new entry)."""
    spec = get_instrument(symbol)
    st = bridge._load_state()
    open_pos = [p for p in st.get("positions", []) if p["canonical"] == spec.canonical]

    # 1. Always manage open positions (Breakeven 1R + flat_by auto-close)
    if open_pos and bars:
        be_evt = apply_breakeven_1r(
            bridge,
            symbol,
            last_price=bars[-1].close,
            enabled=bool(cfg.get("breakeven_at_1r", True)),
        )
        flat_str = cfg["futures_flat_by"] if spec.asset_class == "future" else cfg["stocks_flat_by"]
        if dt_now.time() >= parse_hhmm(flat_str):
            bridge.flatten_all(symbol, mode=mode, current_price=bars[-1].close)
            return {"active": bool(cfg.get("autopilot_enabled", False)), "action": "flattened_eod", "message": f"Zeragem automática às {flat_str} BRT executada."}
        if be_evt:
            return {"active": bool(cfg.get("autopilot_enabled", False)), "action": "breakeven", "message": be_evt["message"]}
        return {
            "active": bool(cfg.get("autopilot_enabled", False)),
            "action": "managing",
            "message": f"Monitorando operação aberta (Breakeven 1R {'ATIVADO' if open_pos[0].get('breakeven_moved') else 'armado'}).",
        }

    # 2. If Autopilot is OFF, just report context
    if not cfg.get("autopilot_enabled", False):
        return {
            "active": False,
            "action": "idle",
            "message": f"Robô em pausa. Tendência M5: {context['trend']} | M15: {context['m15_trend']} | ADX: {context['adx14']} | Stop ATR: {context['dynamic_stop_pts']} pts.",
        }

    # 3. Safety gate for live real-money autopilot
    if mode == "mt5" and (cfg.get("paper", True) or not cfg.get("allow_live_autopilot", False)):
        return {
            "active": False,
            "action": "blocked_live",
            "message": "Robô bloqueado em Conta REAL por segurança (use 'MT5 Ao Vivo + Paper Local' ou libere allow_live_autopilot no config.json).",
        }

    # 4. News blackout filter (High Impact USD/BRL ±15m)
    if news_status and news_status.get("blocked"):
        return {
            "active": True,
            "action": "blocked_news",
            "message": str(news_status.get("summary", "Blackout de notícia de alto impacto ativo.")),
        }

    # 5. Check trading day & entry window
    trading_day, reason = is_trading_day(dt_now.date())
    if not trading_day:
        return {"active": True, "action": "closed", "message": f"Robô aguardando pregão ({reason})."}

    no_entries = cfg["futures_no_entries_after"] if spec.asset_class == "future" else cfg["stocks_no_entries_after"]
    if dt_now.time() > parse_hhmm(no_entries):
        return {"active": True, "action": "after_window", "message": f"Janela de entradas encerrada ({no_entries} BRT)."}

    sig = context.get("signal")
    if not sig:
        return {
            "active": True,
            "action": "scanning",
            "message": f"Robô ativo monitorando {spec.symbol} — M5: {context['trend']} | M15: {context['m15_trend']} | ADX: {context['adx14']} | RVOL: {context['rvol']}x. Aguardando sinal.",
        }

    # 6. Anti-chop professional filters: ADX(14), Multi-Timeframe M15, and RVOL
    min_adx = float(cfg.get("min_adx", 20.0))
    if cfg.get("adx_filter_enabled", True) and context["adx14"] < min_adx:
        return {
            "active": True,
            "action": "filtered_adx",
            "message": f"Sinal {sig['strategy']} ignorado: mercado lateral (ADX {context['adx14']:.1f} < {min_adx:.0f}).",
        }

    if cfg.get("mtf_m15_filter_enabled", True):
        expected_m15 = "ALTA" if sig["side"] == "buy" else "BAIXA"
        if context["m15_trend"] != expected_m15:
            return {
                "active": True,
                "action": "filtered_m15",
                "message": f"Sinal {sig['strategy']} ({sig['side'].upper()}) ignorado: M15 está em {context['m15_trend']} (precisa {expected_m15}).",
            }

    min_rvol = float(cfg.get("min_rvol", 1.2))
    if cfg.get("rvol_filter_enabled", True) and context["rvol"] < min_rvol:
        return {
            "active": True,
            "action": "filtered_rvol",
            "message": f"Sinal {sig['strategy']} ignorado: volume fraco (RVOL {context['rvol']:.2f}x < {min_rvol:.1f}x).",
        }

    # 7. Institutional confluence filters: Milhar wall, live MT5 Order Flow, and Score >= 70
    if institutional and cfg.get("institutional_filter_enabled", True):
        side_score = institutional["score_buy"] if sig["side"] == "buy" else institutional["score_sell"]
        flow = institutional.get("flow", {})
        if cfg.get("milhar_wall_filter_enabled", True) and side_score.get("wall_blocked"):
            return {
                "active": True,
                "action": "filtered_milhar",
                "message": f"Sinal {sig['strategy']} ({sig['side'].upper()}) ignorado: colado em parede de Milhar ({institutional['milhar']['resistance_milhar'] if sig['side'] == 'buy' else institutional['milhar']['support_milhar']}).",
            }
        if cfg.get("flow_filter_enabled", True) and flow.get("available"):
            opp_flow = "VENDEDOR" if sig["side"] == "buy" else "COMPRADOR"
            if flow.get("flow_bias") == opp_flow:
                return {
                    "active": True,
                    "action": "filtered_flow",
                    "message": f"Sinal {sig['strategy']} ({sig['side'].upper()}) ignorado: fluxo institucional contrário ({opp_flow}, Delta {flow.get('delta_pct', 0):+.1f}%).",
                }
        min_score = int(cfg.get("min_institutional_score", 70))
        if int(side_score.get("score", 0)) < min_score:
            return {
                "active": True,
                "action": "filtered_score",
                "message": f"Sinal {sig['strategy']} ({sig['side'].upper()}) ignorado: Score Institucional {side_score['score']}/100 abaixo do mínimo ({min_score}/100).",
            }

    # Prevent duplicate entry on the same candle
    sig_key = f"{spec.canonical}:{sig['bar_time']}:{sig['side']}"
    if st.get("last_autopilot_signal") == sig_key:
        return {
            "active": True,
            "action": "waiting_next",
            "message": f"Sinal {sig['strategy']} ({sig['side'].upper()}) já avaliado neste candle. Aguardando próximo.",
        }

    # 5. Build plan with dynamic ATR stop (or 200 pts default) and 2R target
    stop_pts = context["dynamic_stop_pts"] if cfg.get("dynamic_atr_stop", True) else 200.0
    target_mult = max(float(cfg.get("min_rr", 2.0)), 2.0)
    plan = build_plan(
        symbol=spec.symbol,
        side=sig["side"],
        entry_price=bars[-1].close,
        stop_rule=f"points:{stop_pts}",
        target_rule=f"{target_mult:g}R",
        config_path=bridge.config_path,
    )

    # 6. Time Machine statistical filter (3,000 fast Monte Carlo replays)
    try:
        tm = run_timemachine(plan=plan, bars=bars, replays=3000, seed=42, min_sessions=10)
        min_avg_r = float(cfg.get("autopilot_min_avg_r", 0.0))
        if tm["avg_r"] <= min_avg_r:
            st["last_autopilot_signal"] = sig_key
            bridge._save_state(st)
            return {
                "active": True,
                "action": "filtered_tm",
                "message": f"Sinal {sig['strategy']} ({sig['side'].upper()}) filtrado pelo Time Machine (R médio {tm['avg_r']:+.3f}R <= {min_avg_r:.2f}R).",
            }
    except ValueError:
        pass  # If <10 sessions in history, proceed using risk rules

    # 7. Size + Risk check + Execute automatically
    sized = size_position(plan, config_path=bridge.config_path)
    if sized.get("status") != "sized":
        return {"active": True, "action": "refused_budget", "message": f"Robô recusou entrada: {sized.get('reason')}"}

    order = sized["orders"][0]
    verdict = check_order(
        order,
        cfg=cfg,
        dt_now=dt_now,
        used_daily_risk_brl=bridge.effective_daily_risk_brl(st),
        trades_today=int(st["trades_today"]),
        ignore_daily_limits=(mode != "mt5" and bool(cfg.get("paper_unlimited_daily", True))),
    )
    if verdict["verdict"] != "PASS":
        return {"active": True, "action": "blocked_risk", "message": "Robô bloqueado pelo risco: " + " | ".join(verdict["reasons"])}

    res = bridge.execute_order(order, mode=mode, confirmed=True, now_override=dt_now.strftime("%Y-%m-%d %H:%M"))
    st2 = bridge._load_state()
    st2["last_autopilot_signal"] = sig_key
    bridge._save_state(st2)

    if res.get("ok"):
        return {
            "active": True,
            "action": "executed",
            "order_id": res["order_id"],
            "message": f"🤖 ROBÔ EXECUTOU {order['side'].upper()} {order['qty']} {spec.symbol} @ {order['entry_price']} (Stop {order['stop_loss_price']} / Alvo {order['take_profit_price']}) [{sig['strategy']}]!",
        }
    return {"active": True, "action": "error", "message": f"Falha na execução automática: {res.get('error')}"}
