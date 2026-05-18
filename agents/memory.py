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
    "revenue_trend": "stable"
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
        "revenue_trend": state.get("revenue_trend", "stable")
    }
    result = json.dumps(payload, separators=(",", ":"))
    if len(result) <= 4000:
        return result
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
        last = deliveries_sorted[-1]
        ordered = last.get("ordered_kg", 0)
        delivered = last.get("delivered_kg", 0)
        perfect = ordered > 0 and delivered >= ordered
        if perfect:
            flags[supplier] = "ok"
        else:
            current = flags.get(supplier, "ok")
            if current == "unreliable":
                flags[supplier] = "blacklisted"
            elif current != "blacklisted":
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
    ss = observation.get("service_summary", {})
    return {
        "day": day,
        "revenue": observation.get("yesterday_revenue", 0),
        "covers": ss.get("total_covers", 0),
        "walkouts": ss.get("walkout_band", "None"),
        "cash": round(observation.get("cash", 0), 0)
    }


def build_stockout_entries(observation: dict, day: int) -> list:
    ss = observation.get("service_summary", {})
    unavailable = ss.get("dishes_unavailable_at", {})
    entries = []
    menu_book = {d["name"]: d for d in observation.get("menu_book", [])}
    for dish, hour in unavailable.items():
        recipe = menu_book.get(dish, {})
        for ing in recipe.get("ingredients", []):
            entries.append({
                "day": day,
                "ingredient": ing["ingredient"],
                "hour_ran_out": hour
            })
    return entries


def get_best_daily_special(observation: dict) -> str:
    """Pick daily special by highest estimated revenue (count * current_price) from yesterday."""
    ss = observation.get("service_summary", {})
    dishes_sold = ss.get("dishes_sold", {})
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
    ss = observation.get("service_summary", {})
    walkout_band = ss.get("walkout_band", "None")
    reputation_band = observation.get("reputation_band", "Very Good")
    menu_book = {d["name"]: d for d in observation.get("menu_book", [])}
    active_menu = observation.get("active_menu", [])

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
        "revenue_trend": "stable"
    }
