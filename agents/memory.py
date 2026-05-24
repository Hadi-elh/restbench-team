"""
memory.py — Persistent notes memory system for RestBench agent.
Serializes/deserializes state via the save_notes tool (max 4000 chars).
"""

import json

DEFAULT_STATE = {
    "day_history": [],
    "stockout_log": [],
    "supplier_flags": {},
    "scenario_flags": {
        "supply_crisis": False,
        "tourist_season": False,
        "renovation": False,
        "inflation": False,
        "health_scare": False
    },
    "happy_hour_streak": 0,
    "staff_level": 8,
    "revenue_trend": "stable",
    "recent_consumption": [],
}

VALID_TRENDS = {"improving", "stable", "declining"}
VALID_FLAGS = {"ok", "unreliable", "blacklisted"}


def parse_notes(notes_str: str) -> dict:
    if not notes_str or not notes_str.strip():
        return _default()
    try:
        data = json.loads(notes_str)
        state = _default()
        state["day_history"] = data.get("day_history", [])[-5:]
        state["stockout_log"] = data.get("stockout_log", [])[-5:]
        state["supplier_flags"] = {
            k: v for k, v in data.get("supplier_flags", {}).items()
            if v in VALID_FLAGS
        }
        flags = data.get("scenario_flags", {})
        state["scenario_flags"] = {
            "supply_crisis": bool(flags.get("supply_crisis", False)),
            "tourist_season": bool(flags.get("tourist_season", False)),
            "renovation": bool(flags.get("renovation", False)),
            "inflation": bool(flags.get("inflation", False)),
            "health_scare": bool(flags.get("health_scare", False))
        }
        streak = data.get("happy_hour_streak", 0)
        state["happy_hour_streak"] = int(streak) if isinstance(streak, (int, float)) else 0
        level = data.get("staff_level", 8)
        state["staff_level"] = int(level) if isinstance(level, (int, float)) and 3 <= level <= 15 else 8
        trend = data.get("revenue_trend", "stable")
        state["revenue_trend"] = trend if trend in VALID_TRENDS else "stable"
        raw_rc = data.get("recent_consumption", [])
        if isinstance(raw_rc, list):
            state["recent_consumption"] = [
                e for e in raw_rc
                if isinstance(e, dict)
                and isinstance(e.get("day"), (int, float))
                and isinstance(e.get("consumption"), dict)
            ][-5:]
        else:
            state["recent_consumption"] = []
        return state
    except Exception:
        return _default()


def build_notes(state: dict) -> str:
    payload = {
        "day_history": state.get("day_history", [])[-5:],
        "stockout_log": state.get("stockout_log", [])[-5:],
        "supplier_flags": state.get("supplier_flags", {}),
        "scenario_flags": state.get("scenario_flags", _default()["scenario_flags"]),
        "happy_hour_streak": state.get("happy_hour_streak", 0),
        "staff_level": state.get("staff_level", 8),
        "revenue_trend": state.get("revenue_trend", "stable"),
        "recent_consumption": state.get("recent_consumption", [])[-5:],
    }
    result = json.dumps(payload, separators=(",", ":"))
    if len(result) <= 4000:
        return result
    payload["recent_consumption"] = payload["recent_consumption"][-3:]
    result = json.dumps(payload, separators=(",", ":"))
    if len(result) <= 4000:
        return result
    payload["recent_consumption"] = []
    payload["day_history"] = payload["day_history"][-3:]
    result = json.dumps(payload, separators=(",", ":"))
    if len(result) <= 4000:
        return result
    payload["day_history"] = payload["day_history"][-1:]
    result = json.dumps(payload, separators=(",", ":"))
    if len(result) <= 4000:
        return result
    payload["day_history"] = []
    return json.dumps(payload, separators=(",", ":"))


def compute_revenue_trend(day_history: list) -> str:
    revenues = [d["revenue"] for d in day_history if "revenue" in d]
    if len(revenues) < 2:
        return "stable"
    latest = revenues[-1]
    previous = revenues[-2]
    if previous == 0:
        return "stable"
    change = (latest - previous) / previous
    if change > 0.05:
        return "improving"
    if change < -0.05:
        return "declining"
    return "stable"


def update_supplier_flags(supplier_flags: dict, delivery_history: list) -> dict:
    flags = dict(supplier_flags)
    recent_by_supplier: dict = {}
    for d in delivery_history:
        name = d.get("supplier")
        if not name:
            continue
        recent_by_supplier.setdefault(name, []).append(d)

    for supplier, deliveries in recent_by_supplier.items():
        deliveries_sorted = sorted(deliveries, key=lambda x: x.get("delivery_day", 0))
        window = deliveries_sorted[-5:]

        def _is_partial(d: dict) -> bool:
            ordered = d.get("ordered_kg", 0) or 0
            delivered = d.get("delivered_kg", 0) or 0
            return ordered > 0 and delivered < ordered

        partials = [d for d in window if _is_partial(d)]

        # Blacklist only when 3+ shortfalls in last 5 AND avg ratio < 0.5.
        if len(partials) >= 3:
            avg_ratio = sum(
                (d.get("delivered_kg", 0) or 0) / (d.get("ordered_kg", 1) or 1)
                for d in partials
            ) / len(partials)
            if avg_ratio < 0.5:
                flags[supplier] = "blacklisted"
                continue

        # Mark ok when last delivery is perfect AND last 3 have no shortfalls.
        last = window[-1]
        last_3 = window[-3:]
        last_perfect = (
            (last.get("ordered_kg", 0) or 0) > 0
            and (last.get("delivered_kg", 0) or 0) >= (last.get("ordered_kg", 0) or 0)
        )
        last_3_clean = not any(_is_partial(d) for d in last_3)
        if last_perfect and last_3_clean:
            flags[supplier] = "ok"
            continue

        # Otherwise: mark unreliable on any shortfall (keep blacklisted if already set).
        if partials and flags.get(supplier) != "blacklisted":
            flags[supplier] = "unreliable"

    return flags


def update_scenario_flags(scenario_flags: dict, alerts: list) -> dict:
    """Flags are sticky — once True, never reset."""
    flags = dict(scenario_flags)
    alerts_lower = " ".join(alerts).lower()
    if any(w in alerts_lower for w in ["outage", "disruption", "halted", "supply crisis", "supplier"]):
        flags["supply_crisis"] = True
    if any(w in alerts_lower for w in ["surge", "tourist", "demand spike", "influx", "peak season"]):
        flags["tourist_season"] = True
    if any(w in alerts_lower for w in ["renovation", "reduced capacity", "tables unavailable"]):
        flags["renovation"] = True
    if any(w in alerts_lower for w in ["inflation", "price increase", "cost increase", "supplier raised", "rising costs"]):
        flags["inflation"] = True
    if any(w in alerts_lower for w in ["health", "inspection", "outbreak", "hygiene", "closure warning", "food safety"]):
        flags["health_scare"] = True
    return flags


def build_day_entry(observation: dict, day: int) -> dict:
    # NOTE: the live API can return service_summary=None on day 1 (no service yet),
    # so we use `or {}` to coerce None into an empty dict. `dict.get(key, default)`
    # only returns the default when the key is missing — not when the value is None.
    ss = observation.get("service_summary") or {}
    return {
        "day": day,
        "revenue": observation.get("yesterday_revenue", 0),
        "covers": ss.get("total_covers", 0),
        "walkouts": ss.get("walkout_band", "None"),
        "cash": round(observation.get("cash", 0), 0)
    }


def compute_consumption(observation: dict) -> dict[str, float]:
    """Compute yesterday's ingredient consumption from dishes_sold × recipe quantities.

    Returns ingredient -> kg consumed. Handles all three ingredient list shapes
    documented in AGENT_CONTRACT (list[dict], dict[str, float], list[str]).
    Returns {} when no dishes_sold data is present (day 1 / zero-cover days).
    """
    ss = observation.get("service_summary") or {}
    dishes_sold = ss.get("dishes_sold", {}) or {}
    if not dishes_sold:
        return {}
    menu_lookup: dict[str, dict] = {
        d["name"]: d for d in (observation.get("menu_book", []) or []) if d.get("name")
    }
    consumption: dict[str, float] = {}
    for dish, count in dishes_sold.items():
        entry = menu_lookup.get(dish)
        if not entry:
            continue
        ings = entry.get("ingredients") or []
        if isinstance(ings, list):
            for item in ings:
                if isinstance(item, dict):
                    ingredient = item.get("ingredient") or item.get("name")
                    qty = float(item.get("quantity_kg", 0) or 0)
                    if ingredient and qty > 0:
                        consumption[ingredient] = consumption.get(ingredient, 0.0) + count * qty
        elif isinstance(ings, dict):
            for ingredient, qty in ings.items():
                qty = float(qty or 0)
                if qty > 0:
                    consumption[ingredient] = consumption.get(ingredient, 0.0) + count * qty
    return consumption


def build_stockout_entries(observation: dict, day: int) -> list:
    # Schema must match what operations._derive_stockout_streak reads:
    # {"day": int, "dish": str, "hour": int}
    ss = observation.get("service_summary", {}) or {}
    unavailable = ss.get("dishes_unavailable_at", {}) or {}
    return [{"day": day, "dish": dish, "hour": hour} for dish, hour in unavailable.items()]


def get_best_daily_special(observation: dict) -> str:
    """Pick daily special by highest estimated revenue (count * current_price) from yesterday."""
    ss = observation.get("service_summary") or {}
    dishes_sold = ss.get("dishes_sold", {}) or {}
    menu_book = {d["name"]: d for d in observation.get("menu_book", [])}
    active_menu = observation.get("active_menu", [])
    best_dish = None
    best_revenue = -1
    for dish in active_menu:
        count = dishes_sold.get(dish, 0)
        price = menu_book.get(dish, {}).get("current_price", 0)
        estimated_revenue = count * price
        if estimated_revenue > best_revenue:
            best_revenue = estimated_revenue
            best_dish = dish
    return best_dish or (active_menu[0] if active_menu else None)


def get_price_actions(observation: dict, scenario_flags: dict) -> list:
    """
    Hard-rule pricing. Returns set_price actions when a rule triggers.
    Returns empty list if no rule applies — let LLM decide.
    Priority: reputation recovery > walkout control > tourist season > inflation.
    """
    ss = observation.get("service_summary") or {}
    walkout_band = ss.get("walkout_band", "None")
    reputation_band = observation.get("reputation_band", "Very Good")
    menu_book = {d["name"]: d for d in observation.get("menu_book", []) or []}
    active_menu = observation.get("active_menu", []) or []

    multiplier = None
    if reputation_band in ("Poor", "Fair"):
        multiplier = 0.90
    elif walkout_band == "Many":
        multiplier = 0.95
    elif scenario_flags.get("tourist_season"):
        multiplier = 1.10
    elif scenario_flags.get("inflation"):
        multiplier = 1.12

    if multiplier is None:
        return []

    actions = []
    for dish in active_menu:
        info = menu_book.get(dish, {})
        base_price = info.get("base_price", 0)
        current_price = info.get("current_price", 0)
        if base_price <= 0:
            continue
        new_price = round(base_price * multiplier, 2)
        new_price = max(base_price * 0.80, min(base_price * 1.20, new_price))
        if abs(new_price - current_price) > 0.01:
            actions.append({"tool": "set_price", "args": {"dish": dish, "price": new_price}})
    return actions


def _default() -> dict:
    return {
        "day_history": [],
        "stockout_log": [],
        "supplier_flags": {},
        "scenario_flags": {
            "supply_crisis": False,
            "tourist_season": False,
            "renovation": False,
            "inflation": False,
            "health_scare": False
        },
        "happy_hour_streak": 0,
        "staff_level": 8,
        "revenue_trend": "stable",
        "recent_consumption": [],
    }
