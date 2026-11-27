"""Economic calendar & High-Impact (3-bull) news blackout filter for B3 Day Trader."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .calendar_b3 import BRT
from .config import load_json, save_json

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CACHE_TTL_SECONDS = 3600  # Refresh at most once per hour


def _parse_iso_to_brt(raw_date: str) -> Optional[datetime]:
    text = raw_date.strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BRT)
        return dt.astimezone(BRT)
    except ValueError:
        return None


def fetch_ff_calendar(
    cache_path: Path = Path("trader/state/news_cache.json"),
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Fetch weekly ForexFactory JSON calendar with 1-hour local cache."""
    now = datetime.now(BRT)
    if not force_refresh and cache_path.exists():
        try:
            cached = load_json(cache_path)
            fetched_at = _parse_iso_to_brt(str(cached.get("fetched_at", "")))
            if fetched_at and (now - fetched_at).total_seconds() < CACHE_TTL_SECONDS:
                return cached
        except Exception:
            pass

    req = urllib.request.Request(
        FF_CALENDAR_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; b3-trader/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
        if not raw.startswith("["):
            raise RuntimeError("Resposta inesperada do feed de calendário econômico.")
        items = json.loads(raw)
        events: List[Dict[str, Any]] = []
        for it in items:
            country = str(it.get("country", "")).upper()
            impact = str(it.get("impact", "")).strip()
            if country not in ("USD", "BRL"):
                continue
            if impact.lower() != "high":
                continue
            dt_brt = _parse_iso_to_brt(str(it.get("date", "")))
            if dt_brt is None:
                continue
            events.append(
                {
                    "title": str(it.get("title", "Evento Alto Impacto")),
                    "country": country,
                    "impact": "High",
                    "datetime_brt": dt_brt.isoformat(),
                    "date": dt_brt.strftime("%Y-%m-%d"),
                    "time_brt": dt_brt.strftime("%H:%M"),
                    "forecast": str(it.get("forecast", "") or ""),
                    "previous": str(it.get("previous", "") or ""),
                    "source": "ForexFactory",
                }
            )
        payload = {
            "fetched_at": now.isoformat(),
            "ok": True,
            "error": None,
            "events": events,
        }
        save_json(cache_path, payload)
        return payload
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
        # Fall back to stale cache if available
        if cache_path.exists():
            try:
                cached = load_json(cache_path)
                cached["error"] = f"Usando cache local (erro de rede: {exc})"
                return cached
            except Exception:
                pass
        return {
            "fetched_at": now.isoformat(),
            "ok": False,
            "error": str(exc),
            "events": [],
        }


def load_custom_events(custom_path: Path = Path("trader/custom_news.json")) -> List[Dict[str, Any]]:
    """Load user-added Brazilian/custom high-impact events (e.g. COPOM, IPCA, PIB)."""
    if not custom_path.exists():
        return []
    try:
        data = load_json(custom_path)
        items = data.get("events", []) if isinstance(data, dict) else data
        out: List[Dict[str, Any]] = []
        for it in items:
            date_s = str(it.get("date", "")).strip()
            time_s = str(it.get("time_brt", "")).strip()
            if not date_s or not time_s:
                continue
            dt_brt = _parse_iso_to_brt(f"{date_s}T{time_s}:00-03:00")
            if dt_brt is None:
                continue
            out.append(
                {
                    "title": str(it.get("title", "Evento BR")),
                    "country": str(it.get("country", "BRL")).upper(),
                    "impact": "High",
                    "datetime_brt": dt_brt.isoformat(),
                    "date": dt_brt.strftime("%Y-%m-%d"),
                    "time_brt": dt_brt.strftime("%H:%M"),
                    "forecast": "",
                    "previous": "",
                    "source": "Manual/BR",
                }
            )
        return out
    except Exception:
        return []


def add_custom_event(
    title: str,
    date_str: str,
    time_brt: str,
    country: str = "BRL",
    custom_path: Path = Path("trader/custom_news.json"),
) -> Dict[str, Any]:
    existing = load_custom_events(custom_path)
    ev = {
        "title": title.strip(),
        "country": country.strip().upper() or "BRL",
        "date": date_str.strip(),
        "time_brt": time_brt.strip(),
    }
    raw_list = [
        {"title": e["title"], "country": e["country"], "date": e["date"], "time_brt": e["time_brt"]}
        for e in existing
    ]
    raw_list.append(ev)
    save_json(custom_path, {"events": raw_list})
    return ev


def check_news_blackout(
    dt_now: datetime,
    before_minutes: int = 15,
    after_minutes: int = 15,
    enabled: bool = True,
    events_override: Optional[List[Dict[str, Any]]] = None,
    cache_path: Path = Path("trader/state/news_cache.json"),
    custom_path: Path = Path("trader/custom_news.json"),
) -> Dict[str, Any]:
    """Check whether dt_now falls inside a High-Impact news blackout window."""
    dt_brt = dt_now.astimezone(BRT)
    today_str = dt_brt.strftime("%Y-%m-%d")

    if events_override is not None:
        all_events = list(events_override)
        feed_error = None
    else:
        ff = fetch_ff_calendar(cache_path=cache_path)
        feed_error = ff.get("error")
        all_events = list(ff.get("events", [])) + load_custom_events(custom_path=custom_path)

    today_events: List[Dict[str, Any]] = []
    active_event: Optional[Dict[str, Any]] = None
    next_event: Optional[Dict[str, Any]] = None
    min_future_delta: Optional[float] = None

    for ev in all_events:
        if ev.get("date") != today_str:
            continue
        ev_dt = _parse_iso_to_brt(str(ev.get("datetime_brt", "")))
        if ev_dt is None:
            continue
        diff_min = (ev_dt - dt_brt).total_seconds() / 60.0
        enriched = dict(ev)
        enriched["minutes_to_event"] = round(diff_min, 1)
        in_window = (-after_minutes <= diff_min <= before_minutes)
        enriched["in_blackout"] = in_window
        today_events.append(enriched)

        if in_window and active_event is None:
            active_event = enriched
        if diff_min > 0 and (min_future_delta is None or diff_min < min_future_delta):
            min_future_delta = diff_min
            next_event = enriched

    today_events.sort(key=lambda e: e.get("time_brt", ""))
    blocked = bool(enabled and active_event is not None)

    if blocked and active_event is not None:
        mins = active_event["minutes_to_event"]
        when = f"em {abs(mins):.0f} min" if mins >= 0 else f"há {abs(mins):.0f} min"
        summary = (
            f"🚨 BLACKOUT DE NOTÍCIA ({active_event['country']}): "
            f"{active_event['title']} ({active_event['time_brt']} BRT, {when}) — novas entradas bloqueadas!"
        )
    elif next_event is not None:
        summary = (
            f"Próxima notícia 3 touros: [{next_event['country']}] {next_event['title']} "
            f"às {next_event['time_brt']} BRT (em {next_event['minutes_to_event']:.0f} min)."
        )
    else:
        summary = "Livre de notícias 3 touros (USD/BRL) no restante do pregão de hoje."

    return {
        "enabled": enabled,
        "blocked": blocked,
        "before_minutes": before_minutes,
        "after_minutes": after_minutes,
        "active_event": active_event,
        "next_event": next_event,
        "today_events": today_events,
        "summary": summary,
        "feed_error": feed_error,
    }
