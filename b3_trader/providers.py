"""Multi-source B3 market data providers: CSV (Profit/MT5/Tryd), MetaTrader 5, and Yahoo Finance."""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .calendar_b3 import BRT
from .instruments import get_instrument, yahoo_symbol_for


@dataclass(frozen=True)
class Bar:
    timestamp: datetime  # timezone-aware in America/Sao_Paulo (bar open time)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def _parse_br_float(raw: str) -> float:
    text = raw.strip().replace(" ", "")
    if not text:
        raise ValueError("Valor numérico vazio")
    # Handle Brazilian formatting like "128.450,00" or "5.432,50"
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    return float(text)


def _parse_timestamp(date_str: str, time_str: Optional[str] = None) -> datetime:
    raw = f"{date_str.strip()} {time_str.strip()}".strip() if time_str else date_str.strip()
    raw = raw.replace("T", " ").replace(".", "-")
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=BRT)
        except ValueError:
            continue
    raise ValueError(f"Não foi possível interpretar data/hora: {raw!r}")


def load_bars_csv(path: Path) -> List[Bar]:
    """Load OHLCV bars from CSV exported by ProfitChart (Nelogica), MetaTrader 5, or Tryd."""
    if not path.exists():
        raise FileNotFoundError(f"Arquivo CSV não encontrado: {path}")

    sample = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = [ln for ln in sample.splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"CSV vazio ou sem dados suficientes: {path}")

    first = lines[0]
    delimiter = ";" if first.count(";") >= first.count(",") else ("\t" if "\t" in first else ",")
    reader = csv.DictReader(lines, delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV sem cabeçalho.")

    # Normalize column lookup (supports Portuguese & English headers from Profit, MT5, Tryd)
    norm_map: Dict[str, str] = {}
    for col in reader.fieldnames:
        key = col.strip().lower().strip("<>")
        norm_map[key] = col

    def find_col(*candidates: str) -> Optional[str]:
        for c in candidates:
            if c in norm_map:
                return norm_map[c]
        return None

    c_datetime = find_col("datetime", "timestamp", "data_hora", "data/hora")
    c_date = find_col("date", "data")
    c_time = find_col("time", "hora")
    c_open = find_col("open", "abertura")
    c_high = find_col("high", "maxima", "máxima", "max")
    c_low = find_col("low", "minima", "mínima", "min")
    c_close = find_col("close", "fechamento", "ultimo", "último")
    c_vol = find_col("volume", "tickvol", "vol", "quantidade", "negocios", "negócios")

    if not (c_datetime or c_date) or not all((c_open, c_high, c_low, c_close)):
        raise ValueError(
            f"Colunas obrigatórias ausentes no CSV {path}. "
            f"Encontradas: {reader.fieldnames}"
        )

    bars: List[Bar] = []
    for row in reader:
        try:
            if c_datetime:
                ts = _parse_timestamp(row[c_datetime])
            else:
                ts = _parse_timestamp(row[c_date], row[c_time] if c_time else None)
            o = _parse_br_float(row[c_open])
            h = _parse_br_float(row[c_high])
            l = _parse_br_float(row[c_low])
            c = _parse_br_float(row[c_close])
            v = _parse_br_float(row[c_vol]) if c_vol and row.get(c_vol) else 0.0
            bars.append(Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=v))
        except (ValueError, KeyError):
            continue

    bars.sort(key=lambda b: b.timestamp)
    return bars


def resolve_mt5_symbol(mt5_mod: object, raw_symbol: str) -> str:
    """Resolve generic B3 tickers (WINFUT, WDOFUT, DOLFUT) to the live active MT5 symbol."""
    spec = get_instrument(raw_symbol)
    now_epoch = int(datetime.now(timezone.utc).timestamp())

    def _score_symbol(name: str) -> Optional[tuple[int, int, int, int, int]]:
        info = mt5_mod.symbol_info(name)  # type: ignore[attr-defined]
        if info is None:
            return None
        exp = int(getattr(info, "expiration_time", 0) or 0)
        is_not_expired = 1 if (exp == 0 or exp > now_epoch) else 0
        tick = mt5_mod.symbol_info_tick(name)  # type: ignore[attr-defined]
        tick_time = int(getattr(tick, "time", 0) or 0) if tick is not None else 0
        has_price = 1 if (tick is not None and (getattr(tick, "last", 0) > 0 or getattr(tick, "bid", 0) > 0)) else 0
        # Must have a recent tick (within last 7 days) to beat stale/expired contracts that report exp == 0
        is_fresh_tick = 1 if (tick_time > 0 and abs(now_epoch - tick_time) < 7 * 86400) else 0
        # Among fresh non-expired futures, prefer the NEAREST upcoming expiration (front-month, e.g. WINZ26 over WING27)
        if exp == 0:
            nearness = 0
        elif is_not_expired:
            nearness = -exp
        else:
            nearness = -10**12 + exp
        return (is_fresh_tick, is_not_expired, has_price, nearness, tick_time)

    # First try explicit continuous symbols if they have live data
    explicit_candidates: List[str] = []
    if spec.canonical == "WINFUT":
        explicit_candidates = ["WIN$N", "WIN$", "WINFUT", "WIN$D"]
    elif spec.canonical == "WDOFUT":
        explicit_candidates = ["WDO$N", "WDO$", "WDOFUT", "WDO$D"]
    elif spec.canonical == "DOLFUT":
        explicit_candidates = ["DOL$N", "DOL$", "DOLFUT", "DOL$D"]
    elif spec.canonical == "INDFUT":
        explicit_candidates = ["IND$N", "IND$", "INDFUT", "IND$D"]
    else:
        explicit_candidates = [spec.symbol]

    if spec.symbol not in explicit_candidates:
        explicit_candidates.insert(0, spec.symbol)

    scored: List[tuple[tuple[int, int, int, int, int], str]] = []
    seen = set()
    for cand in explicit_candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if mt5_mod.symbol_select(cand, True):  # type: ignore[attr-defined]
            sc = _score_symbol(cand)
            if sc is not None and sc[0] == 1 and sc[1] == 1 and sc[2] == 1:
                scored.append((sc, cand))

    # Also inspect all matching prefix symbols (e.g. WINZ26, WDOZ26) and prefer front-month non-expired with live price
    prefix = spec.canonical[:3] if spec.asset_class == "future" else spec.symbol
    matches = mt5_mod.symbols_get(f"{prefix}*") or []  # type: ignore[attr-defined]
    for m in matches:
        name = str(m.name)
        if name in seen:
            continue
        # Only consider standard contract/stock lengths (e.g. WINZ26, WDOZ26, PETR4)
        if spec.asset_class == "future" and len(name) > 7:
            continue
        seen.add(name)
        mt5_mod.symbol_select(name, True)  # type: ignore[attr-defined]
        sc = _score_symbol(name)
        if sc is not None:
            scored.append((sc, name))

    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        best = scored[0][1]
        mt5_mod.symbol_select(best, True)  # type: ignore[attr-defined]
        return best

    raise RuntimeError(
        f"Símbolo {raw_symbol!r} não encontrado no MT5. "
        f"Adicione o contrato vigente (ex: WINZ26 / WDOZ26) na Observação do Mercado do MT5."
    )


def load_bars_mt5(symbol: str, interval: str = "5m", count: int = 5000) -> List[Bar]:
    """Load bars directly from a running MetaTrader 5 terminal (Windows)."""
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Pacote 'MetaTrader5' não instalado (ou ambiente Linux). "
            "No Windows com MT5 aberto, instale com 'pip install MetaTrader5' ou use --csv."
        ) from exc

    tf_map = {
        "1m": mt5.TIMEFRAME_M1,
        "5m": mt5.TIMEFRAME_M5,
        "15m": mt5.TIMEFRAME_M15,
        "1d": mt5.TIMEFRAME_D1,
    }
    if interval not in tf_map:
        raise ValueError(f"Intervalo não suportado no MT5: {interval}")

    if not mt5.initialize():
        raise RuntimeError(f"Falha ao conectar no MetaTrader 5: {mt5.last_error()}")

    try:
        resolved = resolve_mt5_symbol(mt5, symbol)
        rates = mt5.copy_rates_from_pos(resolved, tf_map[interval], 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"Sem barras retornadas pelo MT5 para {resolved}.")
        bars: List[Bar] = []
        for r in rates:
            # MT5 encodes B3 broker server time (BRT) directly in the epoch timestamp
            ts = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).replace(tzinfo=BRT)
            bars.append(
                Bar(
                    timestamp=ts,
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["real_volume"] or r["tick_volume"]),
                )
            )
        bars.sort(key=lambda b: b.timestamp)
        return bars
    finally:
        mt5.shutdown()


def load_bars_yahoo(symbol: str, interval: str = "5m", range_str: str = "60d") -> List[Bar]:
    """Load bars from Yahoo Finance v8 chart API (works for B3 stocks like PETR4.SA, ^BVSP)."""
    spec = get_instrument(symbol)
    if spec.asset_class == "future":
        raise RuntimeError(
            f"Yahoo Finance não fornece barras intraday confiáveis para futuros da B3 ({spec.symbol}). "
            "Use --csv (exportado do ProfitChart/MT5/Tryd) ou --provider mt5 no Windows."
        )

    ysym = yahoo_symbol_for(symbol)
    q = urllib.parse.urlencode({"interval": interval, "range": range_str})
    hosts = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
    last_err: Optional[Exception] = None

    for host in hosts:
        url = f"https://{host}/v8/finance/chart/{urllib.parse.quote(ysym)}?{q}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; b3-trader/1.0)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            result = payload.get("chart", {}).get("result")
            if not result:
                raise RuntimeError(f"Sem dados no Yahoo Finance para {ysym}")
            data = result[0]
            timestamps = data.get("timestamp") or []
            quote = (data.get("indicators", {}).get("quote") or [{}])[0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            vols = quote.get("volume") or []

            bars: List[Bar] = []
            for i, ts_epoch in enumerate(timestamps):
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                if None in (o, h, l, c):
                    continue
                v = vols[i] if i < len(vols) and vols[i] is not None else 0.0
                dt = datetime.fromtimestamp(int(ts_epoch), tz=timezone.utc).astimezone(BRT)
                bars.append(Bar(timestamp=dt, open=float(o), high=float(h), low=float(l), close=float(c), volume=float(v)))
            bars.sort(key=lambda b: b.timestamp)
            return bars
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
            continue

    raise RuntimeError(f"Erro ao buscar {ysym} no Yahoo Finance: {last_err}")


def fetch_bars(
    symbol: str,
    interval: str = "5m",
    provider: str = "auto",
    csv_path: Optional[Path] = None,
    range_str: str = "60d",
) -> List[Bar]:
    """Dispatch bar loading based on provider or CSV path."""
    if csv_path is not None:
        return load_bars_csv(csv_path)

    prov = provider.lower()
    if prov == "mt5":
        return load_bars_mt5(symbol, interval=interval)
    if prov == "yahoo":
        return load_bars_yahoo(symbol, interval=interval, range_str=range_str)
    if prov == "auto":
        spec = get_instrument(symbol)
        if spec.asset_class == "future":
            return load_bars_mt5(symbol, interval=interval)
        return load_bars_yahoo(symbol, interval=interval, range_str=range_str)

    raise ValueError(f"Provider desconhecido: {provider!r} (use auto, csv, mt5 ou yahoo)")
