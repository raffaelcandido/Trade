"""MetaTrader 5 & Paper Simulator executor for B3 Day Trader (BTG / B3)."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .calendar_b3 import BRT, now_brt
from .config import load_config, load_json, save_json
from .instruments import get_instrument
from .providers import Bar, load_bars_mt5
from .risk import check_order


def _mt5_available() -> bool:
    try:
        import MetaTrader5  # type: ignore  # noqa: F401
        return True
    except ImportError:
        return False


def _default_sim_state() -> Dict[str, Any]:
    return {
        "mode": "sim",
        "date": now_brt().date().isoformat(),
        "trades_today": 0,
        "used_daily_risk_brl": 0.0,
        "realized_pnl_brl": 0.0,
        "positions": [],
        "orders_log": [],
        "sim_bars": {},  # symbol -> list of serialized bars
    }


def _bar_to_dict(b: Bar) -> Dict[str, Any]:
    return {
        "t": b.timestamp.strftime("%Y-%m-%d %H:%M"),
        "o": b.open,
        "h": b.high,
        "l": b.low,
        "c": b.close,
        "v": b.volume,
    }


def _dict_to_bar(d: Dict[str, Any]) -> Bar:
    dt = datetime.strptime(d["t"], "%Y-%m-%d %H:%M").replace(tzinfo=BRT)
    return Bar(timestamp=dt, open=float(d["o"]), high=float(d["h"]), low=float(d["l"]), close=float(d["c"]), volume=float(d.get("v", 0)))


def _seed_sim_history(symbol: str, n_days: int = 20) -> List[Bar]:
    """Generate deterministic 20-day B3 5m history for simulator / training mode."""
    spec = get_instrument(symbol)
    base_map = {
        "WINFUT": 128400.0,
        "INDFUT": 128400.0,
        "WDOFUT": 5480.0,
        "DOLFUT": 5480.0,
    }
    start_price = base_map.get(spec.canonical, 38.50)
    tick = spec.tick_size
    rng = random.Random(hash(spec.canonical) & 0xFFFF)

    today = now_brt().date()
    # Collect n_days weekdays ending on today (or last weekday)
    trading_dates = []
    cur = today
    while len(trading_dates) < n_days:
        if cur.weekday() < 5:
            trading_dates.append(cur)
        cur -= timedelta(days=1)
    trading_dates.reverse()

    open_h = 9 if spec.asset_class == "future" else 10
    bars: List[Bar] = []
    for idx_d, d in enumerate(trading_dates):
        is_today = idx_d == len(trading_dates) - 1
        price = spec.round_to_tick(start_price + rng.randint(-20, 20) * tick)
        if is_today:
            price = start_price
        n_bars = 12 if is_today else 108  # Today starts at 10:00 (after 15m OR + breakout)
        dt = datetime(d.year, d.month, d.day, open_h, 0, tzinfo=BRT)
        for i in range(n_bars):
            if is_today and i < 3:
                o = start_price
                h = spec.round_to_tick(start_price + 12 * tick)
                l = spec.round_to_tick(start_price - 12 * tick)
                c = spec.round_to_tick(start_price + rng.randint(-4, 4) * tick)
            elif is_today and i == 3:
                # Clean breakout above OR high so user sees a real triggered signal
                o = spec.round_to_tick(start_price + 6 * tick)
                h = spec.round_to_tick(start_price + 24 * tick)
                l = spec.round_to_tick(start_price + 4 * tick)
                c = spec.round_to_tick(start_price + 18 * tick)
            else:
                move = rng.choice([-8, -5, -3, -2, -1, 0, 1, 2, 3, 5, 8]) * tick
                o = price
                c = max(tick, spec.round_to_tick(o + move))
                h = spec.round_to_tick(max(o, c) + rng.randint(1, 5) * tick)
                l = max(tick, spec.round_to_tick(min(o, c) - rng.randint(1, 5) * tick))
            bars.append(Bar(timestamp=dt, open=o, high=h, low=l, close=c, volume=float(rng.randint(5000, 25000))))
            price = c
            dt += timedelta(minutes=5)
    return bars


class BrokerBridge:
    """Unified bridge supporting MetaTrader 5 (live/demo on Windows) and built-in Simulator."""

    def __init__(
        self,
        config_path: Path = Path("trader/config.json"),
        state_path: Path = Path("trader/state/broker.json"),
    ) -> None:
        self.config_path = config_path
        self.state_path = state_path

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            st = load_json(self.state_path)
            if st.get("date") != now_brt().date().isoformat():
                st["date"] = now_brt().date().isoformat()
                st["trades_today"] = 0
                st["used_daily_risk_brl"] = 0.0
                st["realized_pnl_brl"] = 0.0
            return st
        return _default_sim_state()

    def _save_state(self, st: Dict[str, Any]) -> None:
        save_json(self.state_path, st)

    def connection_info(self, mode: str = "sim") -> Dict[str, Any]:
        cfg = load_config(self.config_path)
        st = self._load_state()
        if mode in ("mt5", "mt5_paper"):
            if not _mt5_available():
                return {
                    "mode": mode,
                    "connected": False,
                    "account_type": "INDISPONÍVEL",
                    "equity_brl": round(float(cfg["account_equity_brl"]) + float(st["realized_pnl_brl"]), 2),
                    "message": "MetaTrader 5 não instalado neste sistema (use no Windows com BTG MT5 aberto ou alterne para Simulador Offline).",
                }
            import MetaTrader5 as mt5  # type: ignore

            if not mt5.initialize():
                return {
                    "mode": mode,
                    "connected": False,
                    "account_type": "DESCONECTADO",
                    "equity_brl": round(float(cfg["account_equity_brl"]) + float(st["realized_pnl_brl"]), 2),
                    "message": f"MT5 instalado, mas não conectado: {mt5.last_error()}",
                }
            try:
                info = mt5.account_info()
                if info is None:
                    return {
                        "mode": mode,
                        "connected": False,
                        "account_type": "SEM CONTA",
                        "equity_brl": round(float(cfg["account_equity_brl"]) + float(st["realized_pnl_brl"]), 2),
                        "message": "Nenhuma conta logada no MetaTrader 5.",
                    }
                is_demo = info.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO
                if mode == "mt5_paper":
                    return {
                        "mode": "mt5_paper",
                        "connected": True,
                        "account_type": "MT5 AO VIVO + PAPER",
                        "login": info.login,
                        "server": info.server,
                        "equity_brl": round(float(cfg["account_equity_brl"]) + float(st["realized_pnl_brl"]), 2),
                        "balance_brl": float(cfg["account_equity_brl"]),
                        "message": f"Cotações AO VIVO via {info.server} | Ordens 100% simuladas (Paper local — zero risco real).",
                    }
                return {
                    "mode": "mt5",
                    "connected": True,
                    "account_type": "DEMO" if is_demo else "REAL",
                    "login": info.login,
                    "server": info.server,
                    "equity_brl": round(float(info.equity), 2),
                    "balance_brl": round(float(info.balance), 2),
                    "message": f"Conectado ao MT5 ({'DEMO' if is_demo else 'REAL'}) — {info.server}",
                }
            finally:
                mt5.shutdown()

        return {
            "mode": "sim",
            "connected": True,
            "account_type": "SIMULADOR",
            "equity_brl": round(float(cfg["account_equity_brl"]) + float(st["realized_pnl_brl"]), 2),
            "balance_brl": float(cfg["account_equity_brl"]),
            "message": "Modo Simulador Offline ativo (dados de treino — nenhuma ordem real enviada).",
        }

    def get_bars(self, symbol: str, mode: str = "sim", count: int = 2500) -> List[Bar]:
        spec = get_instrument(symbol)
        if mode in ("mt5", "mt5_paper"):
            return load_bars_mt5(spec.symbol, interval="5m", count=count)

        st = self._load_state()
        cached = st.get("sim_bars", {}).get(spec.canonical)
        if not cached:
            bars = _seed_sim_history(spec.symbol)
            st.setdefault("sim_bars", {})[spec.canonical] = [_bar_to_dict(b) for b in bars]
            self._save_state(st)
            return bars
        return [_dict_to_bar(d) for d in cached]

    def sync_positions_with_bar(self, symbol: str, bar: Bar) -> List[Dict[str, Any]]:
        """Update floating P&L and trigger Paper SL/TP against a live or simulated bar."""
        spec = get_instrument(symbol)
        st = self._load_state()
        if not st.get("positions"):
            return []

        remaining = []
        closed_events = []
        for pos in st.get("positions", []):
            if pos["canonical"] != spec.canonical:
                remaining.append(pos)
                continue
            sl = float(pos["stop_loss_price"])
            tp = float(pos["take_profit_price"])
            side = pos["side"]
            hit_sl = (bar.low <= sl) if side == "buy" else (bar.high >= sl)
            hit_tp = (bar.high >= tp) if side == "buy" else (bar.low <= tp)
            if hit_sl or hit_tp:
                exit_px = sl if hit_sl else tp
                reason = "STOP-LOSS" if hit_sl else "TAKE-PROFIT"
                pnl = spec.pnl_brl(float(pos["entry_price"]), exit_px, int(pos["qty"]), side)
                st["realized_pnl_brl"] = round(float(st["realized_pnl_brl"]) + pnl, 2)
                evt = {
                    "id": str(uuid.uuid4())[:8],
                    "time_brt": bar.timestamp.strftime("%Y-%m-%d %H:%M BRT"),
                    "symbol": pos["symbol"],
                    "side": "sell" if side == "buy" else "buy",
                    "qty": pos["qty"],
                    "price": exit_px,
                    "status": f"FECHADO ({reason})",
                    "pnl_brl": pnl,
                }
                st.setdefault("orders_log", []).insert(0, evt)
                closed_events.append(evt)
            else:
                pos["last_price"] = bar.close
                pos["floating_pnl_brl"] = spec.pnl_brl(float(pos["entry_price"]), bar.close, int(pos["qty"]), side)
                remaining.append(pos)

        st["positions"] = remaining
        self._save_state(st)
        return closed_events

    def advance_sim_bar(self, symbol: str) -> Dict[str, Any]:
        """Advance one 5-minute simulated candle and evaluate open SL/TP triggers."""
        spec = get_instrument(symbol)
        st = self._load_state()
        bars = self.get_bars(spec.symbol, mode="sim")
        last = bars[-1]
        rng = random.Random()
        move = rng.choice([-8, -5, -4, -2, -1, 0, 1, 2, 4, 5, 8]) * spec.tick_size
        o = last.close
        c = max(spec.tick_size, spec.round_to_tick(o + move))
        h = spec.round_to_tick(max(o, c) + rng.randint(1, 5) * spec.tick_size)
        l = max(spec.tick_size, spec.round_to_tick(min(o, c) - rng.randint(1, 5) * spec.tick_size))
        new_bar = Bar(
            timestamp=last.timestamp + timedelta(minutes=5),
            open=o,
            high=h,
            low=l,
            close=c,
            volume=float(rng.randint(6000, 28000)),
        )
        bars.append(new_bar)
        st.setdefault("sim_bars", {})[spec.canonical] = [_bar_to_dict(b) for b in bars]
        self._save_state(st)
        closed_events = self.sync_positions_with_bar(spec.symbol, new_bar)
        return {"bar": _bar_to_dict(new_bar), "closed_events": closed_events}

    def get_account_summary(self, mode: str = "sim") -> Dict[str, Any]:
        cfg = load_config(self.config_path)
        st = self._load_state()
        conn = self.connection_info(mode)
        daily_limit = float(cfg["daily_loss_limit_brl"])
        used_risk = float(st["used_daily_risk_brl"])
        return {
            "connection": conn,
            "trades_today": int(st["trades_today"]),
            "max_trades_per_day": int(cfg["max_trades_per_day"]),
            "used_daily_risk_brl": round(used_risk, 2),
            "daily_loss_limit_brl": daily_limit,
            "remaining_daily_risk_brl": round(max(0.0, daily_limit - used_risk), 2),
            "realized_pnl_brl": round(float(st["realized_pnl_brl"]), 2),
            "positions": st.get("positions", []),
            "orders_log": st.get("orders_log", [])[:20],
        }

    def execute_order(
        self,
        order: Dict[str, Any],
        mode: str = "sim",
        confirmed: bool = False,
        now_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate via risk manager and execute on MT5 (Demo/Real) or Simulator."""
        if not confirmed:
            return {"ok": False, "error": "Confirmação explícita obrigatória antes de enviar qualquer ordem."}

        cfg = load_config(self.config_path)
        st = self._load_state()
        dt_now = now_brt(now_override)

        verdict = check_order(
            order,
            cfg=cfg,
            dt_now=dt_now,
            used_daily_risk_brl=float(st["used_daily_risk_brl"]),
            trades_today=int(st["trades_today"]),
        )
        if verdict["verdict"] != "PASS":
            return {
                "ok": False,
                "verdict": verdict,
                "error": "Ordem bloqueada pela gestão de risco: " + " | ".join(verdict["reasons"]),
            }

        spec = get_instrument(order["symbol"])
        side = order["side"]
        qty = int(order["qty"])
        entry_px = float(order["entry_price"])
        sl = float(order["stop_loss_price"])
        tp = float(order["take_profit_price"])
        max_loss_brl = round(spec.risk_per_unit_brl(entry_px, sl, side) * qty, 2)

        if mode == "mt5":
            conn = self.connection_info("mt5")
            if not conn["connected"]:
                return {"ok": False, "error": conn["message"]}
            if cfg.get("paper", True) and conn["account_type"] == "REAL":
                return {
                    "ok": False,
                    "error": "SEGURANÇA: Conta REAL detectada no MT5, mas 'paper: true' está ativo em trader/config.json.",
                }
            import MetaTrader5 as mt5  # type: ignore

            if not mt5.initialize():
                return {"ok": False, "error": f"Falha MT5: {mt5.last_error()}"}
            try:
                from .providers import resolve_mt5_symbol

                mt5_sym = resolve_mt5_symbol(mt5, spec.symbol)
                tick = mt5.symbol_info_tick(mt5_sym)
                if tick is None:
                    return {"ok": False, "error": f"Sem cotação em tempo real no MT5 para {mt5_sym}."}
                price = tick.ask if side == "buy" else tick.bid
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": mt5_sym,
                    "volume": float(qty),
                    "type": mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL,
                    "price": price,
                    "sl": sl,
                    "tp": tp,
                    "deviation": 20,
                    "magic": 202603,
                    "comment": "b3_trader",
                    "type_time": mt5.ORDER_TIME_DAY,
                    "type_filling": mt5.ORDER_FILLING_RETURN,
                }
                res = mt5.order_send(req)
                if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
                    return {"ok": False, "error": f"MT5 recusou a ordem: {getattr(res, 'comment', mt5.last_error())}"}
                broker_id = str(res.order)
                fill_price = float(res.price or price)
            finally:
                mt5.shutdown()
        else:
            prefix = "PAPER" if mode == "mt5_paper" else "SIM"
            broker_id = f"{prefix}-{str(uuid.uuid4())[:6].upper()}"
            fill_price = entry_px

        st["trades_today"] = int(st["trades_today"]) + 1
        st["used_daily_risk_brl"] = round(float(st["used_daily_risk_brl"]) + max_loss_brl, 2)
        pos = {
            "id": broker_id,
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "side": side,
            "qty": qty,
            "entry_price": fill_price,
            "last_price": fill_price,
            "stop_loss_price": sl,
            "take_profit_price": tp,
            "max_loss_brl": max_loss_brl,
            "floating_pnl_brl": 0.0,
            "opened_at_brt": dt_now.strftime("%Y-%m-%d %H:%M:%S BRT"),
        }
        st.setdefault("positions", []).append(pos)
        status_label = (
            "EXECUTADA (MT5 REAL)"
            if mode == "mt5"
            else ("EXECUTADA (PAPER AO VIVO)" if mode == "mt5_paper" else "EXECUTADA (SIMULADOR)")
        )
        st.setdefault("orders_log", []).insert(
            0,
            {
                "id": broker_id,
                "time_brt": dt_now.strftime("%Y-%m-%d %H:%M:%S BRT"),
                "symbol": spec.symbol,
                "side": side,
                "qty": qty,
                "price": fill_price,
                "sl": sl,
                "tp": tp,
                "status": status_label,
                "pnl_brl": 0.0,
            },
        )
        self._save_state(st)
        return {"ok": True, "order_id": broker_id, "position": pos, "verdict": verdict}

    def flatten_all(self, symbol: str, mode: str = "sim", current_price: Optional[float] = None) -> Dict[str, Any]:
        """Close all open positions for symbol (never blocked by daily limits)."""
        spec = get_instrument(symbol)
        st = self._load_state()
        dt_now = now_brt()
        closed = []
        remaining = []

        for pos in st.get("positions", []):
            if pos["canonical"] != spec.canonical:
                remaining.append(pos)
                continue
            exit_px = float(current_price if current_price is not None else pos.get("last_price", pos["entry_price"]))
            pnl = spec.pnl_brl(float(pos["entry_price"]), exit_px, int(pos["qty"]), pos["side"])
            st["realized_pnl_brl"] = round(float(st["realized_pnl_brl"]) + pnl, 2)
            evt = {
                "id": f"FLAT-{str(uuid.uuid4())[:6].upper()}",
                "time_brt": dt_now.strftime("%Y-%m-%d %H:%M:%S BRT"),
                "symbol": pos["symbol"],
                "side": "sell" if pos["side"] == "buy" else "buy",
                "qty": pos["qty"],
                "price": exit_px,
                "status": "ZERADA MANUALMENTE",
                "pnl_brl": pnl,
            }
            st.setdefault("orders_log", []).insert(0, evt)
            closed.append(evt)

        st["positions"] = remaining
        self._save_state(st)
        return {"ok": True, "closed": closed, "count": len(closed)}
