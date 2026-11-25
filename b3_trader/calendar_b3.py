"""B3 trading calendar and Brasília timezone (America/Sao_Paulo) helpers."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")

# Official B3 holidays for 2026 and 2027 (no trading session)
B3_HOLIDAYS: Dict[date, str] = {
    # 2026
    date(2026, 1, 1): "Confraternização Universal",
    date(2026, 2, 16): "Carnaval (Segunda-feira)",
    date(2026, 2, 17): "Carnaval (Terça-feira)",
    date(2026, 4, 3): "Sexta-feira Santa",
    date(2026, 4, 21): "Tiradentes",
    date(2026, 5, 1): "Dia do Trabalho",
    date(2026, 6, 4): "Corpus Christi",
    date(2026, 9, 7): "Independência do Brasil",
    date(2026, 10, 12): "Nossa Senhora Aparecida",
    date(2026, 11, 2): "Finados",
    date(2026, 11, 15): "Proclamação da República",
    date(2026, 11, 20): "Dia da Consciência Negra",
    date(2026, 12, 25): "Natal",
    date(2026, 12, 31): "Último dia útil do ano (sem pregão na B3)",
    # 2027
    date(2027, 1, 1): "Confraternização Universal",
    date(2027, 2, 8): "Carnaval (Segunda-feira)",
    date(2027, 2, 9): "Carnaval (Terça-feira)",
    date(2027, 3, 26): "Sexta-feira Santa",
    date(2027, 4, 21): "Tiradentes",
    date(2027, 5, 1): "Dia do Trabalho",
    date(2027, 5, 27): "Corpus Christi",
    date(2027, 9, 7): "Independência do Brasil",
    date(2027, 10, 12): "Nossa Senhora Aparecida",
    date(2027, 11, 2): "Finados",
    date(2027, 11, 15): "Proclamação da República",
    date(2027, 11, 20): "Dia da Consciência Negra",
    date(2027, 12, 25): "Natal",
    date(2027, 12, 31): "Último dia útil do ano (sem pregão na B3)",
}

# Ash Wednesday: B3 opens at 13:00 BRT
B3_LATE_OPENS: Dict[date, time] = {
    date(2026, 2, 18): time(13, 0),
    date(2027, 2, 10): time(13, 0),
}


def now_brt(override: Optional[str] = None) -> datetime:
    """Return current datetime in America/Sao_Paulo, or parse 'YYYY-MM-DD HH:MM'."""
    if not override:
        return datetime.now(BRT)
    text = override.strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=BRT)
        except ValueError:
            continue
    raise ValueError(f"Formato de data/hora inválido: {override!r} (use 'YYYY-MM-DD HH:MM')")


def parse_hhmm(hhmm: str) -> time:
    parts = hhmm.strip().split(":")
    return time(int(parts[0]), int(parts[1]))


def is_trading_day(d: date) -> Tuple[bool, Optional[str]]:
    if d.weekday() >= 5:
        return False, "Fim de semana (B3 fechada)"
    if d in B3_HOLIDAYS:
        return False, f"Feriado B3: {B3_HOLIDAYS[d]}"
    return True, None


def session_bounds(d: date, default_open: str, default_close: str) -> Tuple[datetime, datetime]:
    open_t = B3_LATE_OPENS.get(d, parse_hhmm(default_open))
    close_t = parse_hhmm(default_close)
    return (
        datetime.combine(d, open_t, tzinfo=BRT),
        datetime.combine(d, close_t, tzinfo=BRT),
    )


def market_status(
    dt: datetime,
    default_open: str = "09:00",
    default_close: str = "18:25",
) -> Dict[str, object]:
    """Return structured status of the B3 session at dt (BRT)."""
    dt_brt = dt.astimezone(BRT)
    d = dt_brt.date()
    trading_day, reason = is_trading_day(d)
    if not trading_day:
        return {
            "open": False,
            "state": "closed",
            "reason": reason,
            "now_brt": dt_brt.strftime("%Y-%m-%d %H:%M:%S BRT"),
            "session_open": None,
            "session_close": None,
        }

    open_dt, close_dt = session_bounds(d, default_open, default_close)
    if dt_brt < open_dt:
        state = "pre_market"
        is_open = False
        reason = f"Pré-abertura (pregão abre às {open_dt.strftime('%H:%M')} BRT)"
    elif dt_brt > close_dt:
        state = "after_hours"
        is_open = False
        reason = f"Pregão encerrado às {close_dt.strftime('%H:%M')} BRT"
    else:
        state = "open"
        is_open = True
        reason = f"Pregão aberto ({open_dt.strftime('%H:%M')}–{close_dt.strftime('%H:%M')} BRT)"

    return {
        "open": is_open,
        "state": state,
        "reason": reason,
        "now_brt": dt_brt.strftime("%Y-%m-%d %H:%M:%S BRT"),
        "session_open": open_dt.strftime("%H:%M"),
        "session_close": close_dt.strftime("%H:%M"),
    }
