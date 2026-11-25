"""B3 instrument specifications: WINFUT, WDOFUT, DOLFUT, INDFUT, and B3 equities."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    canonical: str
    asset_class: str  # "future" | "stock"
    description: str
    tick_size: float
    point_value_brl: float  # BRL per 1.0 price point per 1 contract/share
    lot_step: int
    default_margin_brl: float  # Approximate intraday margin per contract (0 for stocks)
    session_open: str  # HH:MM BRT
    session_close: str  # HH:MM BRT
    default_no_entries_after: str  # HH:MM BRT
    default_flat_by: str  # HH:MM BRT
    allow_short_default: bool

    @property
    def tick_value_brl(self) -> float:
        return round(self.tick_size * self.point_value_brl, 6)

    def round_to_tick(self, price: float, mode: str = "nearest") -> float:
        """Round price to valid B3 tick size.

        mode:
          - "nearest": nearest valid tick
          - "down": floor to valid tick (conservative stop for buy/long)
          - "up": ceil to valid tick (conservative stop for sell/short)
        """
        ratio = price / self.tick_size
        eps = 1e-9
        if mode == "down":
            ticks = math.floor(ratio + eps)
        elif mode == "up":
            ticks = math.ceil(ratio - eps)
        else:
            ticks = round(ratio)
        value = ticks * self.tick_size
        decimals = 0 if self.tick_size >= 1 else max(0, len(f"{self.tick_size:.6f}".rstrip("0").split(".")[1]))
        return round(value, decimals)

    def pnl_brl(self, entry: float, exit_price: float, qty: int, side: str) -> float:
        """Compute gross P&L in BRL for a given side ('buy' or 'sell')."""
        diff = (exit_price - entry) if side == "buy" else (entry - exit_price)
        return round(diff * self.point_value_brl * qty, 2)

    def risk_per_unit_brl(self, entry: float, stop: float, side: str) -> float:
        """Positive loss in BRL per 1 contract/share if stop is hit."""
        diff = (entry - stop) if side == "buy" else (stop - entry)
        return round(diff * self.point_value_brl, 4)


# Regexes for B3 futures tickers (continuous or contract months)
_WIN_RE = re.compile(r"^WIN(FUT|\$N?|\$D?|[GJMQVZ]\d{2})$")
_IND_RE = re.compile(r"^IND(FUT|\$N?|\$D?|[GJMQVZ]\d{2})$")
_WDO_RE = re.compile(r"^WDO(FUT|\$N?|\$D?|[FGHJKMNQUVXZ]\d{2})$")
_DOL_RE = re.compile(r"^DOL(FUT|\$N?|\$D?|[FGHJKMNQUVXZ]\d{2})$")
_STOCK_FRAC_RE = re.compile(r"^[A-Z]{4}\d{1,2}F$")
_STOCK_ETF_FII_BDR_RE = re.compile(r"^[A-Z]{4}(11|34|35|39)$")


def normalize_symbol(raw: str) -> str:
    s = raw.strip().upper()
    if s.endswith(".SA"):
        s = s[:-3]
    return s


def get_instrument(raw_symbol: str, lot_step_override: Optional[int] = None) -> InstrumentSpec:
    """Resolve a B3 ticker into its contract/stock specification."""
    sym = normalize_symbol(raw_symbol)

    if _WIN_RE.match(sym) or sym == "WIN":
        return InstrumentSpec(
            symbol=sym,
            canonical="WINFUT",
            asset_class="future",
            description="Mini Índice Bovespa Futuro (B3)",
            tick_size=5.0,
            point_value_brl=0.20,
            lot_step=lot_step_override or 1,
            default_margin_brl=150.0,
            session_open="09:00",
            session_close="18:25",
            default_no_entries_after="17:30",
            default_flat_by="17:55",
            allow_short_default=True,
        )

    if _IND_RE.match(sym) or sym == "IND":
        return InstrumentSpec(
            symbol=sym,
            canonical="INDFUT",
            asset_class="future",
            description="Índice Bovespa Futuro Cheio (B3)",
            tick_size=5.0,
            point_value_brl=1.00,
            lot_step=lot_step_override or 5,
            default_margin_brl=750.0,
            session_open="09:00",
            session_close="18:25",
            default_no_entries_after="17:30",
            default_flat_by="17:55",
            allow_short_default=True,
        )

    if _WDO_RE.match(sym) or sym == "WDO":
        return InstrumentSpec(
            symbol=sym,
            canonical="WDOFUT",
            asset_class="future",
            description="Mini Dólar Comercial Futuro (B3)",
            tick_size=0.5,
            point_value_brl=10.00,
            lot_step=lot_step_override or 1,
            default_margin_brl=200.0,
            session_open="09:00",
            session_close="18:30",
            default_no_entries_after="17:30",
            default_flat_by="17:55",
            allow_short_default=True,
        )

    if _DOL_RE.match(sym) or sym == "DOL":
        return InstrumentSpec(
            symbol=sym,
            canonical="DOLFUT",
            asset_class="future",
            description="Dólar Comercial Futuro Cheio (B3)",
            tick_size=0.5,
            point_value_brl=50.00,
            lot_step=lot_step_override or 5,
            default_margin_brl=1000.0,
            session_open="09:00",
            session_close="18:30",
            default_no_entries_after="17:30",
            default_flat_by="17:55",
            allow_short_default=True,
        )

    # B3 Equities, ETFs, FIIs, BDRs
    if lot_step_override is not None:
        lot_step = lot_step_override
    elif _STOCK_FRAC_RE.match(sym) or _STOCK_ETF_FII_BDR_RE.match(sym):
        lot_step = 1
    else:
        lot_step = 100

    return InstrumentSpec(
        symbol=sym,
        canonical=sym,
        asset_class="stock",
        description=f"Ação / ETF B3 ({sym})",
        tick_size=0.01,
        point_value_brl=1.00,
        lot_step=lot_step,
        default_margin_brl=0.0,
        session_open="10:00",
        session_close="17:55",
        default_no_entries_after="16:45",
        default_flat_by="17:45",
        allow_short_default=False,
    )


def yahoo_symbol_for(raw_symbol: str) -> str:
    """Convert a B3 ticker into its Yahoo Finance symbol."""
    sym = normalize_symbol(raw_symbol)
    if sym in ("IBOV", "^BVSP"):
        return "^BVSP"
    if sym.startswith("^") or sym.endswith(".SA"):
        return sym
    if _STOCK_FRAC_RE.match(sym):
        # Yahoo Finance quotes standard lot ticker without trailing F
        return f"{sym[:-1]}.SA"
    return f"{sym}.SA"
