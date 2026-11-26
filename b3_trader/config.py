"""Configuration and JSON state helpers for B3 Day Trader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "currency": "BRL",
    "timezone": "America/Sao_Paulo",
    "paper": True,
    "account_equity_brl": 10000.0,
    "cash_buffer_pct": 0.10,
    "max_loss_per_trade_brl": 150.0,
    "daily_loss_limit_brl": 450.0,
    "max_trades_per_day": 4,
    "min_rr": 1.0,
    "max_contracts": {
        "WINFUT": 10,
        "WDOFUT": 5,
        "DOLFUT": 5,
        "INDFUT": 5,
    },
    "margin_per_contract_brl": {
        "WINFUT": 150.0,
        "WDOFUT": 200.0,
        "DOLFUT": 1000.0,
        "INDFUT": 750.0,
    },
    "max_position_pct_stocks": 0.35,
    "allow_short_futures": True,
    "allow_short_stocks": False,
    "futures_no_entries_after": "17:30",
    "futures_flat_by": "17:55",
    "stocks_no_entries_after": "16:45",
    "stocks_flat_by": "17:45",
    "blocked_tickers": [],
    "data_provider": "auto",  # auto | mt5 | csv | yahoo | brapi
    "brapi_token": "",
    "autopilot_enabled": False,
    "dynamic_atr_stop": True,
    "breakeven_at_1r": True,
    "autopilot_min_avg_r": 0.0,
    "allow_live_autopilot": False,
}


def load_config(path: Path = Path("trader/config.json")) -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        user_cfg = json.loads(path.read_text(encoding="utf-8"))
        for k, v in user_cfg.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
