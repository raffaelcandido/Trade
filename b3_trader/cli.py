"""Command-line interface for B3 Day Trader (WINFUT, WDOFUT, DOLFUT, Ações B3)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .budget import run_budget
from .calendar_b3 import market_status, now_brt
from .config import DEFAULT_CONFIG, save_json
from .entry import evaluate_entry, write_entry
from .exit_plan import build_plan, write_plan
from .instruments import get_instrument
from .providers import fetch_bars
from .risk import run_risk_check
from .timemachine import load_plan_and_run, write_timemachine


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_init(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config)
    if cfg_path.exists() and not args.force:
        print(f"Configuração já existe em {cfg_path}. Use --force para sobrescrever.")
        return 0
    save_json(cfg_path, DEFAULT_CONFIG)
    print(f"Configuração B3 criada em {cfg_path} (moeda BRL, horário America/Sao_Paulo, paper=true).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    spec = get_instrument(args.symbol)
    dt = now_brt(args.now)
    st = market_status(dt, spec.session_open, spec.session_close)
    st["symbol"] = spec.symbol
    st["canonical"] = spec.canonical
    st["asset_class"] = spec.asset_class
    st["tick_size"] = spec.tick_size
    st["point_value_brl"] = spec.point_value_brl
    st["tick_value_brl"] = spec.tick_value_brl
    st["lot_step"] = spec.lot_step
    if args.json:
        _print_json(st)
    else:
        print(f"{spec.symbol} ({spec.description})")
        print(f"  Agora: {st['now_brt']} — {st['reason']}")
        print(
            f"  Tick: {spec.tick_size} | Valor/ponto: R$ {spec.point_value_brl:.2f} "
            f"| Valor/tick: R$ {spec.tick_value_brl:.2f} | Lote mínimo: {spec.lot_step}"
        )
    return 0 if st["open"] else 1


def cmd_entry(args: argparse.Namespace) -> int:
    csv_path = Path(args.csv) if args.csv else None
    bars = fetch_bars(args.symbol, interval=args.interval, provider=args.provider, csv_path=csv_path)
    interval_min = int(args.interval.rstrip("m")) if args.interval.endswith("m") else 5
    res = evaluate_entry(
        symbol=args.symbol,
        bars=bars,
        setup=args.setup,
        side=args.side,
        or_minutes=args.or_minutes,
        interval_minutes=interval_min,
        level=args.level,
        now_override=args.now,
        config_path=Path(args.config),
    )
    if args.write:
        write_entry(res, Path(args.out))
    if args.json:
        _print_json(res)
    else:
        status = res["status"].upper()
        print(f"[{status}] {res['symbol']} ({res['as_of_brt']})")
        if res["status"] == "triggered":
            lado = "COMPRA" if res["side"] == "buy" else "VENDA"
            print(f"  Sinal: {lado} @ {res['entry_price']} às {res['entry_time_brt']} BRT (setup: {res['setup']})")
            if "or_high" in res:
                print(
                    f"  Range abertura ({res['or_minutes']}m): {res['or_low']} – {res['or_high']} "
                    f"(meio: {res['or_mid']}, amplitude: {res['or_range_pts']} pts)"
                )
        else:
            print(f"  {res.get('reason', '')}")
    return 0 if res["status"] == "triggered" else 1


def cmd_exit(args: argparse.Namespace) -> int:
    plan = build_plan(
        symbol=args.symbol,
        side=args.side,
        entry_price=args.entry,
        stop_rule=args.stop,
        target_rule=args.target,
        entry_path=Path(args.entry_file),
        config_path=Path(args.config),
    )
    if args.write:
        write_plan(plan, Path(args.out))
    if args.json:
        _print_json(plan)
    else:
        lado = "COMPRA" if plan["side"] == "buy" else "VENDA"
        print(f"PLANO DE SAÍDA — {plan['symbol']} ({lado})")
        print(f"  Entrada: {plan['entry_price']}")
        print(f"  Stop:    {plan['stop_price']} ({plan['risk_pts']} pts | R$ {plan['risk_per_unit_brl']:.2f}/unid) [{plan['stop_rule']}]")
        print(f"  Alvo:    {plan['target_price']} ({plan['reward_pts']} pts | R$ {plan['reward_per_unit_brl']:.2f}/unid) [{plan['target_rule']}]")
        print(f"  R:R:     {plan['rr']:.2f}R | Zeragem até: {plan['flat_by_brt']} BRT")
    return 0


def cmd_timemachine(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    csv_path = Path(args.csv) if args.csv else None
    bars = fetch_bars(plan_data["symbol"], interval="5m", provider=args.provider, csv_path=csv_path)
    res = load_plan_and_run(
        bars=bars,
        plan_path=plan_path,
        replays=args.replays,
        seed=args.seed,
        min_sessions=args.min_sessions,
    )
    if args.write:
        write_timemachine(res, Path(args.out))
    if args.json:
        _print_json(res)
    else:
        lado = "COMPRA" if res["side"] == "buy" else "VENDA"
        print(f"TIME MACHINE B3 — {res['symbol']} ({lado}) | {res['replays']:,} replays ({res['pool_sessions']} pregões)")
        print(f"  Entrada {res['entry_price']} | Stop {res['stop_price']} | Alvo {res['target_price']} ({res['rr']:.2f}R)")
        print(f"  Alvo primeiro:     {res['target_first_pct']:.1f}%")
        print(f"  Stop primeiro:     {res['stop_first_pct']:.1f}%")
        print(f"  Aberto às {res['flat_by_brt']}:  {res['open_at_flat_by_pct']:.1f}%")
        print(f"  R médio: {res['avg_r']:+.3f}R (expectativa: R$ {res['expected_per_unit_brl']:+.2f} por contrato/ação)")
        h = res["history"]
        if h["fired"] > 0:
            print(f"  Histórico real ORB ({h['sessions']} pregões): disparou {h['fired']}x -> {h['target']} alvo, {h['stop']} stop, {h['neither']} sem atingir")
        print(f"  Veredito: {res['verdict']}")
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    res = run_budget(
        plan_path=Path(args.plan),
        config_path=Path(args.config),
        out_path=Path(args.out),
        write=args.write,
    )
    if args.json:
        _print_json(res)
    elif res["status"] == "refused":
        print(f"[RECUSADO] {res['symbol']}: {res['reason']}")
        return 1
    else:
        o = res["orders"][0]
        lado = "COMPRA" if o["side"] == "buy" else "VENDA"
        unidade = "contrato(s)" if o["asset_class"] == "future" else "ação(ões)"
        print(f"DIMENSIONAMENTO — {o['symbol']} ({lado}) [{'PAPER' if o['paper'] else 'REAL'}]")
        print(f"  Quantidade:      {o['qty']} {unidade} (limitado por: {o['binding_limit']})")
        print(f"  Entrada/Stop/Alvo: {o['entry_price']} / {o['stop_loss_price']} / {o['take_profit_price']}")
        print(f"  Perda máx. stop: R$ {o['max_loss_brl']:.2f} | Ganho alvo: R$ {o['expected_profit_at_target_brl']:.2f}")
        print(f"  Margem/Capital:  R$ {o['required_capital_or_margin_brl']:.2f}")
    return 0


def cmd_risk(args: argparse.Namespace) -> int:
    report = run_risk_check(
        orders_path=Path(args.orders),
        config_path=Path(args.config),
        state_path=Path(args.state),
        now_override=args.now,
        used_daily_risk_brl=args.used_daily_risk,
        trades_today=args.trades_today,
    )
    if args.json:
        _print_json(report)
    else:
        for idx, v in enumerate(report["verdicts"], 1):
            print(f"ORDEM {idx}: [{v['verdict']}] {v['side'].upper()} {v['qty']} {v['symbol']} ({v['checked_at_brt']})")
            for r in v["reasons"]:
                print(f"  - {r}")
    return 0 if report["all_passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="b3-trader", description="Ferramentas de Day Trade para B3 (WINFUT, WDOFUT, DOLFUT e Ações)")
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Cria trader/config.json com padrões da B3 em BRL")
    p_init.add_argument("--config", default="trader/config.json")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_status = sub.add_parser("status", help="Mostra status do pregão B3 (BRT) e especificações do ativo")
    p_status.add_argument("symbol", nargs="?", default="WINFUT")
    p_status.add_argument("--now", default=None, help="Fixar relógio BRT: 'YYYY-MM-DD HH:MM'")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_entry = sub.add_parser("entry", help="Avalia rompimento de range de abertura (ORB) ou nível")
    p_entry.add_argument("symbol", help="Ex: WINFUT, WDOFUT, DOLFUT, WINZ26, PETR4")
    p_entry.add_argument("--setup", choices=("orb", "level"), default="orb")
    p_entry.add_argument("--side", choices=("buy", "sell", "both"), default="both")
    p_entry.add_argument("--or-minutes", type=int, default=15)
    p_entry.add_argument("--interval", default="5m")
    p_entry.add_argument("--level", type=float, default=None)
    p_entry.add_argument("--provider", default="auto", choices=("auto", "csv", "mt5", "yahoo"))
    p_entry.add_argument("--csv", default=None, help="Caminho do CSV (ProfitChart, MT5 ou Tryd)")
    p_entry.add_argument("--now", default=None)
    p_entry.add_argument("--config", default="trader/config.json")
    p_entry.add_argument("--out", default="trader/entry.json")
    p_entry.add_argument("--write", action="store_true")
    p_entry.add_argument("--json", action="store_true")
    p_entry.set_defaults(func=cmd_entry)

    p_exit = sub.add_parser("exit", help="Define stop-loss e alvo arredondados no tick da B3")
    p_exit.add_argument("--symbol", default=None)
    p_exit.add_argument("--side", choices=("buy", "sell"), default=None)
    p_exit.add_argument("--entry", type=float, default=None)
    p_exit.add_argument("--stop", default="or-mid", help="or-mid | or-edge | or-low | or-high | points:N | price:X | pct:X")
    p_exit.add_argument("--target", default="2R", help="1R | 2R | points:N | price:X")
    p_exit.add_argument("--entry-file", default="trader/entry.json")
    p_exit.add_argument("--config", default="trader/config.json")
    p_exit.add_argument("--out", default="trader/plan.json")
    p_exit.add_argument("--write", action="store_true")
    p_exit.add_argument("--json", action="store_true")
    p_exit.set_defaults(func=cmd_exit)

    p_tm = sub.add_parser("timemachine", help="Simula 10.000 sessões Monte Carlo + histórico real na B3")
    p_tm.add_argument("--plan", default="trader/plan.json")
    p_tm.add_argument("--provider", default="auto", choices=("auto", "csv", "mt5", "yahoo"))
    p_tm.add_argument("--csv", default=None)
    p_tm.add_argument("--replays", type=int, default=10000)
    p_tm.add_argument("--seed", type=int, default=42)
    p_tm.add_argument("--min-sessions", type=int, default=10)
    p_tm.add_argument("--out", default="trader/timemachine.json")
    p_tm.add_argument("--write", action="store_true")
    p_tm.add_argument("--json", action="store_true")
    p_tm.set_defaults(func=cmd_timemachine)

    p_bud = sub.add_parser("budget", help="Dimensiona contratos futuros ou lotes de ações em BRL")
    p_bud.add_argument("--plan", default="trader/plan.json")
    p_bud.add_argument("--config", default="trader/config.json")
    p_bud.add_argument("--out", default="trader/orders.json")
    p_bud.add_argument("--write", action="store_true")
    p_bud.add_argument("--json", action="store_true")
    p_bud.set_defaults(func=cmd_budget)

    p_risk = sub.add_parser("risk", help="Valida ordens contra regras de risco da B3 (PASS/BLOCK)")
    p_risk.add_argument("--orders", default="trader/orders.json")
    p_risk.add_argument("--config", default="trader/config.json")
    p_risk.add_argument("--state", default="trader/state/risk.json")
    p_risk.add_argument("--now", default=None)
    p_risk.add_argument("--used-daily-risk", type=float, default=0.0)
    p_risk.add_argument("--trades-today", type=int, default=0)
    p_risk.add_argument("--json", action="store_true")
    p_risk.set_defaults(func=cmd_risk)

    p_web = sub.add_parser("web", help="Abre o Painel Visual Web B3 (Simulador + MetaTrader 5)")
    p_web.add_argument("--host", default="127.0.0.1")
    p_web.add_argument("--port", type=int, default=8765)
    p_web.set_defaults(func=cmd_web)

    return p


def cmd_web(args: argparse.Namespace) -> int:
    from .web import run_server

    run_server(host=args.host, port=args.port)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
