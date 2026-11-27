"""Local web dashboard server for B3 Day Trader (zero external dependencies)."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .autopilot import analyze_intraday_context, run_autopilot_step
from .budget import size_position
from .calendar_b3 import market_status, now_brt
from .config import load_config, save_json
from .entry import evaluate_entry
from .exit_plan import build_plan
from .institutional import (
    analyze_channel_and_frequency,
    analyze_milhar_levels,
    compute_institutional_score,
    fetch_mt5_order_flow,
)
from .instruments import get_instrument
from .mt5_executor import BrokerBridge
from .news_calendar import add_custom_event, check_news_blackout
from .risk import check_order
from .timemachine import run_timemachine

STATIC_DIR = Path(__file__).parent / "static"


class B3DashboardApp:
    """Testable application controller backing the HTTP dashboard endpoints."""

    def __init__(
        self,
        config_path: Path = Path("trader/config.json"),
        state_path: Path = Path("trader/state/broker.json"),
    ) -> None:
        self.config_path = config_path
        self.bridge = BrokerBridge(config_path=config_path, state_path=state_path)

    def get_state(self, symbol: str = "WINFUT", mode: str = "sim", now_override: Optional[str] = None) -> Dict[str, Any]:
        spec = get_instrument(symbol)
        cfg = load_config(self.config_path)
        bars = self.bridge.get_bars(spec.symbol, mode=mode)
        closed_events = []
        if mode == "mt5_paper" and bars:
            closed_events = self.bridge.sync_positions_with_bar(spec.symbol, bars[-1])
        # In simulator mode, align clock to the latest simulated bar + 5m so ORB/risk work smoothly anytime
        if mode == "sim" and not now_override and bars:
            last_dt = bars[-1].timestamp
            from datetime import timedelta

            eff_dt = last_dt + timedelta(minutes=5)
            eff_now = eff_dt.strftime("%Y-%m-%d %H:%M")
        else:
            eff_now = now_override

        dt_now = now_brt(eff_now)
        mkt = market_status(dt_now, spec.session_open, spec.session_close)
        entry = evaluate_entry(
            spec.symbol,
            bars=bars,
            setup="orb",
            side="both",
            or_minutes=15,
            interval_minutes=5,
            now_override=eff_now,
            config_path=self.config_path,
        )
        context = analyze_intraday_context(spec.symbol, bars=bars, dt_now=dt_now, cfg=cfg)
        last_px = bars[-1].close if bars else 0.0
        flow = (
            fetch_mt5_order_flow(spec.symbol, lookback_minutes=5)
            if mode in ("mt5_paper", "mt5")
            else {"available": False, "flow_bias": "NEUTRO", "delta": 0.0, "delta_pct": 0.0, "big_buy_lots": 0, "big_sell_lots": 0, "book_imbalance_pct": 50.0, "note": "Modo Simulador Offline (fluxo real ativo no MT5)."}
        )
        milhar = analyze_milhar_levels(spec, last_px)
        channel = analyze_channel_and_frequency(bars, window=24)
        score_buy = compute_institutional_score("buy", context, flow, milhar, channel)
        score_sell = compute_institutional_score("sell", context, flow, milhar, channel)
        institutional = {
            "flow": flow,
            "milhar": milhar,
            "channel": channel,
            "score_buy": score_buy,
            "score_sell": score_sell,
        }
        news_status = check_news_blackout(
            dt_now=dt_now,
            before_minutes=int(cfg.get("news_blackout_before_min", 15)),
            after_minutes=int(cfg.get("news_blackout_after_min", 15)),
            enabled=bool(cfg.get("news_blackout_enabled", True)),
            events_override=[] if mode == "sim" else None,
        )
        autopilot = run_autopilot_step(
            bridge=self.bridge,
            symbol=spec.symbol,
            mode=mode,
            bars=bars,
            dt_now=dt_now,
            cfg=cfg,
            context=context,
            news_status=news_status,
            institutional=institutional,
        )
        acct = self.bridge.get_account_summary(mode=mode)
        chart_bars = [
            {
                "t": b.timestamp.strftime("%H:%M"),
                "date": b.timestamp.strftime("%Y-%m-%d"),
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "v": b.volume,
            }
            for b in bars[-45:]
        ]

        return {
            "symbol": spec.symbol,
            "canonical": spec.canonical,
            "description": spec.description,
            "asset_class": spec.asset_class,
            "tick_size": spec.tick_size,
            "point_value_brl": spec.point_value_brl,
            "tick_value_brl": spec.tick_value_brl,
            "lot_step": spec.lot_step,
            "last_price": last_px,
            "effective_now_brt": eff_now,
            "market": mkt,
            "entry": entry,
            "context": context,
            "institutional": institutional,
            "news": news_status,
            "autopilot": autopilot,
            "account": acct,
            "config": cfg,
            "chart_bars": chart_bars,
            "closed_events": closed_events,
        }

    def compute_plan(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(payload.get("symbol", "WINFUT"))
        mode = str(payload.get("mode", "sim"))
        side = str(payload.get("side", "buy"))
        entry_price = float(payload["entry_price"])
        stop_rule = str(payload.get("stop_rule", "points:200"))
        target_rule = str(payload.get("target_rule", "2R"))
        now_override = payload.get("now_override")

        cfg = load_config(self.config_path)
        if stop_rule == "auto_atr":
            bars = self.bridge.get_bars(symbol, mode=mode)
            ctx = analyze_intraday_context(symbol, bars=bars, dt_now=now_brt(now_override), cfg=cfg)
            stop_rule = f"points:{ctx['dynamic_stop_pts']}"

        plan = build_plan(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            stop_rule=stop_rule,
            target_rule=target_rule,
            config_path=self.config_path,
        )
        budget = size_position(plan, config_path=self.config_path)
        acct = self.bridge.get_account_summary(mode=mode)

        verdict = None
        if budget.get("status") == "sized":
            order = budget["orders"][0]
            verdict = check_order(
                order,
                cfg=cfg,
                dt_now=now_brt(now_override),
                used_daily_risk_brl=acct["used_daily_risk_brl"],
                trades_today=acct["trades_today"],
            )

        return {"ok": True, "plan": plan, "budget": budget, "verdict": verdict}

    def run_tm(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(payload.get("symbol", "WINFUT"))
        mode = str(payload.get("mode", "sim"))
        replays = int(payload.get("replays", 10000))
        plan = payload["plan"]
        bars = self.bridge.get_bars(symbol, mode=mode)
        res = run_timemachine(plan=plan, bars=bars, replays=replays, seed=42, min_sessions=10)
        return {"ok": True, "timemachine": res}

    def place_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.bridge.execute_order(
            order=payload["order"],
            mode=str(payload.get("mode", "sim")),
            confirmed=bool(payload.get("confirmed", False)),
            now_override=payload.get("now_override"),
        )

    def flatten(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.bridge.flatten_all(
            symbol=str(payload.get("symbol", "WINFUT")),
            mode=str(payload.get("mode", "sim")),
            current_price=payload.get("current_price"),
        )

    def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        cfg = load_config(self.config_path)
        for key in (
            "account_equity_brl",
            "max_loss_per_trade_brl",
            "daily_loss_limit_brl",
            "max_trades_per_day",
            "min_rr",
            "paper",
            "autopilot_enabled",
            "dynamic_atr_stop",
            "breakeven_at_1r",
            "autopilot_min_avg_r",
            "news_blackout_enabled",
            "adx_filter_enabled",
            "mtf_m15_filter_enabled",
            "rvol_filter_enabled",
        ):
            if key in payload:
                cfg[key] = payload[key]
        if "max_contracts_win" in payload:
            cfg.setdefault("max_contracts", {})["WINFUT"] = int(payload["max_contracts_win"])
        if "margin_win" in payload:
            cfg.setdefault("margin_per_contract_brl", {})["WINFUT"] = float(payload["margin_win"])
        save_json(self.config_path, cfg)
        return {"ok": True, "config": cfg}


def make_handler(app: B3DashboardApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                html_path = STATIC_DIR / "index.html"
                body = html_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/state":
                qs = parse_qs(parsed.query)
                symbol = qs.get("symbol", ["WINFUT"])[0]
                mode = qs.get("mode", ["sim"])[0]
                try:
                    self._send_json(app.get_state(symbol=symbol, mode=mode))
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return

            self._send_json({"ok": False, "error": "Rota não encontrada"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                payload = self._read_json()
                if parsed.path == "/api/plan":
                    self._send_json(app.compute_plan(payload))
                elif parsed.path == "/api/timemachine":
                    self._send_json(app.run_tm(payload))
                elif parsed.path == "/api/order":
                    self._send_json(app.place_order(payload))
                elif parsed.path == "/api/flatten":
                    self._send_json(app.flatten(payload))
                elif parsed.path == "/api/config":
                    self._send_json(app.update_config(payload))
                elif parsed.path == "/api/news/custom":
                    ev = add_custom_event(
                        title=str(payload["title"]),
                        date_str=str(payload["date"]),
                        time_brt=str(payload["time_brt"]),
                        country=str(payload.get("country", "BRL")),
                    )
                    self._send_json({"ok": True, "event": ev})
                elif parsed.path == "/api/sim/tick":
                    sym = str(payload.get("symbol", "WINFUT"))
                    self._send_json({"ok": True, **app.bridge.advance_sim_bar(sym)})
                else:
                    self._send_json({"ok": False, "error": "Rota não encontrada"}, status=HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    return Handler


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    app = B3DashboardApp()
    server = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"B3 Day Trader Dashboard rodando em http://{host}:{port}")
    print("Pressione Ctrl+C para encerrar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()


def main() -> int:
    p = argparse.ArgumentParser(description="Painel Visual Web B3 Day Trader (MT5 + Simulador)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    run_server(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
