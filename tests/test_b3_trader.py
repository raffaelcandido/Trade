"""Unit and end-to-end tests for b3_trader (WINFUT, WDOFUT, DOLFUT, and B3 stocks)."""

from __future__ import annotations

import random
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from b3_trader.budget import size_position
from b3_trader.calendar_b3 import BRT, market_status, now_brt
from b3_trader.cli import main as cli_main
from b3_trader.config import DEFAULT_CONFIG, save_json
from b3_trader.entry import evaluate_entry
from b3_trader.exit_plan import build_plan
from b3_trader.instruments import get_instrument, yahoo_symbol_for
from b3_trader.providers import Bar, load_bars_csv
from b3_trader.risk import check_order
from b3_trader.timemachine import run_timemachine


def _make_synthetic_sessions(
    start_price: float,
    tick: float,
    n_days: int = 25,
    seed: int = 7,
    open_hour: int = 9,
    final_day_breakout: str = "buy",
) -> tuple[list[Bar], date]:
    """Generate deterministic 5-minute B3 sessions (09:00-18:00 BRT) for testing."""
    rng = random.Random(seed)
    bars: list[Bar] = []
    cur_date = date(2026, 9, 1)
    generated = 0
    last_date = cur_date

    while generated < n_days:
        if cur_date.weekday() < 5 and cur_date != date(2026, 9, 7):
            is_last = generated == n_days - 1
            price = start_price
            dt = datetime(cur_date.year, cur_date.month, cur_date.day, open_hour, 0, tzinfo=BRT)
            for i in range(108):  # 9 hours of 5m bars
                if is_last and i < 3:
                    # Clean opening range
                    o = start_price
                    h = start_price + 10 * tick
                    l = start_price - 10 * tick
                    c = start_price
                elif is_last and i == 3:
                    # Breakout candle right after 15m OR
                    if final_day_breakout == "buy":
                        o = start_price + 5 * tick
                        h = start_price + 20 * tick
                        l = start_price + 2 * tick
                        c = start_price + 15 * tick
                    else:
                        o = start_price - 5 * tick
                        h = start_price - 2 * tick
                        l = start_price - 20 * tick
                        c = start_price - 15 * tick
                else:
                    move = rng.choice([-4, -3, -2, -1, 0, 1, 2, 3, 4]) * tick
                    o = price
                    c = max(tick, round((o + move) / tick) * tick)
                    h = max(o, c) + rng.randint(1, 3) * tick
                    l = max(tick, min(o, c) - rng.randint(1, 3) * tick)
                bars.append(Bar(timestamp=dt, open=o, high=h, low=l, close=c, volume=1000.0))
                price = c
                dt += timedelta(minutes=5)
            last_date = cur_date
            generated += 1
        cur_date += timedelta(days=1)

    return bars, last_date


class TestInstruments(unittest.TestCase):
    def test_winfut_and_contract_codes(self) -> None:
        win = get_instrument("WINFUT")
        self.assertEqual(win.canonical, "WINFUT")
        self.assertEqual(win.tick_size, 5.0)
        self.assertEqual(win.point_value_brl, 0.20)
        self.assertEqual(win.tick_value_brl, 1.0)
        self.assertEqual(win.lot_step, 1)
        self.assertEqual(win.round_to_tick(128432.0, "down"), 128430.0)
        self.assertEqual(win.round_to_tick(128432.0, "up"), 128435.0)
        self.assertEqual(get_instrument("WINZ26").canonical, "WINFUT")
        self.assertEqual(get_instrument("WIN$N").canonical, "WINFUT")

    def test_dolfut_and_wdofut(self) -> None:
        wdo = get_instrument("WDOFUT")
        self.assertEqual(wdo.tick_size, 0.5)
        self.assertEqual(wdo.point_value_brl, 10.0)
        self.assertEqual(wdo.lot_step, 1)

        dol = get_instrument("DOLFUT")
        self.assertEqual(dol.tick_size, 0.5)
        self.assertEqual(dol.point_value_brl, 50.0)
        self.assertEqual(dol.lot_step, 5)
        self.assertEqual(dol.round_to_tick(5432.3, "up"), 5432.5)
        self.assertEqual(dol.round_to_tick(5432.3, "down"), 5432.0)

    def test_b3_stocks_and_yahoo_mapping(self) -> None:
        petr = get_instrument("PETR4")
        self.assertEqual(petr.asset_class, "stock")
        self.assertEqual(petr.lot_step, 100)
        self.assertEqual(yahoo_symbol_for("PETR4"), "PETR4.SA")
        self.assertEqual(get_instrument("PETR4F").lot_step, 1)
        self.assertEqual(yahoo_symbol_for("PETR4F"), "PETR4.SA")
        self.assertEqual(get_instrument("BOVA11").lot_step, 1)
        self.assertEqual(yahoo_symbol_for("IBOV"), "^BVSP")


class TestCalendarB3(unittest.TestCase):
    def test_holidays_and_ash_wednesday(self) -> None:
        # Dia da Consciência Negra (feriado nacional / B3)
        st_hol = market_status(now_brt("2026-11-20 11:00"), "09:00", "18:25")
        self.assertFalse(st_hol["open"])
        self.assertIn("Consciência Negra", str(st_hol["reason"]))

        # Quarta-feira de Cinzas: abre às 13:00 BRT
        st_pre = market_status(now_brt("2026-02-18 11:30"), "09:00", "18:25")
        self.assertFalse(st_pre["open"])
        self.assertEqual(st_pre["session_open"], "13:00")

        st_open = market_status(now_brt("2026-02-18 13:05"), "09:00", "18:25")
        self.assertTrue(st_open["open"])


class TestCsvProvider(unittest.TestCase):
    def test_profitchart_brazilian_csv(self) -> None:
        content = (
            "Data;Hora;Abertura;Máxima;Mínima;Fechamento;Volume\n"
            "24/11/2026;09:00;128.400,00;128.500,00;128.350,00;128.450,00;15000\n"
            "24/11/2026;09:05;128.450,00;128.550,00;128.420,00;128.525,00;12000\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "win.csv"
            p.write_text(content, encoding="utf-8")
            bars = load_bars_csv(p)
            self.assertEqual(len(bars), 2)
            self.assertEqual(bars[0].open, 128400.0)
            self.assertEqual(bars[1].close, 128525.0)
            self.assertEqual(bars[0].timestamp.tzinfo, BRT)


class TestEndToEndPipelines(unittest.TestCase):
    def test_winfut_long_full_pipeline(self) -> None:
        bars, last_day = _make_synthetic_sessions(128000.0, tick=5.0, n_days=22, final_day_breakout="buy")
        now_str = f"{last_day.isoformat()} 09:25"

        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            entry_path = Path(td) / "entry.json"
            save_json(cfg_path, DEFAULT_CONFIG)

            entry = evaluate_entry("WINFUT", bars, setup="orb", side="both", now_override=now_str, config_path=cfg_path)
            self.assertEqual(entry["status"], "triggered")
            self.assertEqual(entry["side"], "buy")
            self.assertEqual(entry["entry_price"], 128075.0)
            save_json(entry_path, entry)

            plan = build_plan(stop_rule="or-mid", target_rule="2R", entry_path=entry_path, config_path=cfg_path)
            self.assertEqual(plan["stop_price"], 128000.0)
            self.assertEqual(plan["risk_pts"], 75.0)
            self.assertEqual(plan["risk_per_unit_brl"], 15.0)  # 75 * R$0.20
            self.assertEqual(plan["target_price"], 128225.0)
            self.assertEqual(plan["rr"], 2.0)

            tm = run_timemachine(plan, bars, replays=1000, seed=42, min_sessions=10)
            self.assertEqual(tm["replays"], 1000)
            self.assertAlmostEqual(
                tm["target_first_pct"] + tm["stop_first_pct"] + tm["open_at_flat_by_pct"], 100.0, delta=0.3
            )

            sized = size_position(plan, config_path=cfg_path)
            self.assertEqual(sized["status"], "sized")
            order = sized["orders"][0]
            # max_loss = 150 / 15 = 10 contracts (also equals max_contracts=10)
            self.assertEqual(order["qty"], 10)
            self.assertEqual(order["max_loss_brl"], 150.0)

            verdict = check_order(order, DEFAULT_CONFIG, now_brt(now_str))
            self.assertEqual(verdict["verdict"], "PASS")

    def test_dolfut_short_and_risk_blocks(self) -> None:
        bars, last_day = _make_synthetic_sessions(5500.0, tick=0.5, n_days=20, final_day_breakout="sell")
        now_str = f"{last_day.isoformat()} 09:25"

        with tempfile.TemporaryDirectory() as td:
            cfg = dict(DEFAULT_CONFIG)
            cfg["account_equity_brl"] = 50000.0
            cfg["max_loss_per_trade_brl"] = 2500.0
            cfg["daily_loss_limit_brl"] = 5000.0
            cfg_path = Path(td) / "config.json"
            entry_path = Path(td) / "entry.json"
            save_json(cfg_path, cfg)

            entry = evaluate_entry("DOLFUT", bars, setup="orb", side="both", now_override=now_str, config_path=cfg_path)
            self.assertEqual(entry["status"], "triggered")
            self.assertEqual(entry["side"], "sell")
            save_json(entry_path, entry)

            plan = build_plan(stop_rule="or-mid", target_rule="2R", entry_path=entry_path, config_path=cfg_path)
            # Entry 5492.5, stop 5500.0 -> 7.5 pts * R$50 = R$375/contract -> 5 contracts = R$1875
            self.assertEqual(plan["side"], "sell")
            self.assertGreater(plan["stop_price"], plan["entry_price"])
            self.assertLess(plan["target_price"], plan["entry_price"])

            sized = size_position(plan, config_path=cfg_path)
            self.assertEqual(sized["status"], "sized")
            order = sized["orders"][0]
            self.assertEqual(order["qty"], 5)  # DOLFUT minimum lot_step is 5

            # PASS inside window
            v_pass = check_order(order, cfg, now_brt(now_str))
            self.assertEqual(v_pass["verdict"], "PASS")

            # BLOCK after entry window (17:45 BRT)
            v_late = check_order(order, cfg, now_brt(f"{last_day.isoformat()} 17:45"))
            self.assertEqual(v_late["verdict"], "BLOCK")

            # Reduce-only flatten order is NEVER blocked by late window
            flatten_order = dict(order, side="buy", reduce_only=True)
            v_flat = check_order(flatten_order, cfg, now_brt(f"{last_day.isoformat()} 17:50"))
            self.assertEqual(v_flat["verdict"], "PASS")

    def test_cli_status_smoke(self) -> None:
        rc = cli_main(["status", "WINFUT", "--now", "2026-11-24 10:30"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
