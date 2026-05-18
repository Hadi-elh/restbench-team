"""Operations rule layer for the RestBench 30-day Italian restaurant simulator.

This module implements deterministic, rule-based operational decisions that
sit beneath the LLM-driven team-lead orchestrator. It is a pure function:
given an observation dict, the current day, and the persisted notes_state,
it returns a list of tool-call action dicts and an updated scenario_flags
dict that the orchestrator persists via save_notes.

Contract
--------
    get_operations_actions(observation, day, notes_state,
                           llm_pricing_failed=False) -> (actions, updates)

`actions` is a list of dicts of the form
    {"tool": <tool_name>, "args": {...}}

The supported tools and arg shapes are:
    set_staff_level    {"level": int}
    run_happy_hour     {}
    offer_daily_special{"dish": str}
    set_menu           {"dishes": [str, ...]}
    set_price          {"dish": str, "price": float}

notes_state keys (READ)
-----------------------
    happy_hour_streak: int               (default 0; count of consecutive days
                                          HH ran, including yesterday. Cooldown
                                          is enforced by: if streak >= 1, we
                                          ran yesterday, so skip today.)
    last_staff_level: int                (default 8)
    scenario_flags: dict[str, bool]      (defaults all False; sticky once True)
    stockout_log: list[dict]             (Emre's schema. List of last 5
                                          {"day": int, "dish": str, "hour": int}
                                          stockout events. Derived per-ingredient
                                          streak is built from this every turn.)
    days_since_renovation: int           (default -1; -1 = never seen, 0 on
                                          first detect, +1 each day after.)

notes_state keys (WRITE)
------------------------
    Same keys EXCEPT stockout_log. The log is owned and maintained by the
    orchestrator (Emre) — operations.py is a read-only consumer of it.

updated_scenario_flags keys (WRITE)
-----------------------------------
Same keys as above; the orchestrator merges/persists them. Scenario flags
are sticky for the lifetime of the game: once a flag flips True, it stays
True.

Behaviour summary
-----------------
1. Staffing target derived from day_of_week, weather, reputation, walkouts,
   tourist_season and renovation flags. Only emits set_staff_level when
   the target differs from current observation['staff_level'].
2. Happy hour on Mon/Tue/Wed with a >=2 day cooldown, suppressed during
   supply_crisis when reputation is also weak.
3. One daily special per day, picked by yesterday's revenue or base_price
   fallback when no sales data is available.
4. Menu intervention only when an ingredient is stocked out two days in a
   row and removing dependent dishes still leaves >=5 active dishes.
5. Scenario flag detection by case-insensitive substring scan of alerts,
   plus a counter for days since the first renovation alert.
6. Passive pricing safety: resets any active dish whose current_price has
   drifted outside [0.8x, 1.2x] of base_price, with a full reset to base
   for every active dish when llm_pricing_failed is True.
"""

from __future__ import annotations

from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_BASE_STAFF_BY_DAY: dict[str, int] = {
    "Monday": 5,
    "Tuesday": 5,
    "Wednesday": 5,
    "Thursday": 6,
    "Friday": 7,
    "Saturday": 7,
    "Sunday": 7,
}

_STAFF_MIN = 3
_STAFF_MAX = 15
_RENOVATION_STAFF_CAP = 5
_MENU_MIN_DISHES = 5
_PRICE_SLACK = 0.001  # EUR tolerance for the 0.8x..1.2x band

_SCENARIO_KEYWORDS: dict[str, tuple[str, ...]] = {
    "supply_crisis": ("supplier", "halted", "outage", "supply"),
    "tourist_season": ("tourist", "surge", "festival"),
    "renovation": ("renovation", "capacity reduced", "reduced capacity", "table"),
    "inflation": ("inflation", "price increase", "cost increase", "cost rise"),
    "health_scare": (
        "health",
        "illness",
        "outbreak",
        "contamination",
        "inspector",
    ),
}

_DEFAULT_SCENARIO_FLAGS: dict[str, bool] = {k: False for k in _SCENARIO_KEYWORDS}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _detect_scenario_flags(
    alerts: Iterable[Any],
    previous_flags: dict[str, bool],
) -> dict[str, bool]:
    """Sticky scenario detection by case-insensitive substring scan."""
    merged: dict[str, bool] = dict(_DEFAULT_SCENARIO_FLAGS)
    # Carry forward any previously raised flag (sticky semantics).
    for key, val in (previous_flags or {}).items():
        if key in merged and val:
            merged[key] = True

    haystack = " ".join(str(a) for a in (alerts or [])).lower()
    if haystack:
        for flag, keywords in _SCENARIO_KEYWORDS.items():
            if merged[flag]:
                continue
            if any(kw in haystack for kw in keywords):
                merged[flag] = True
    return merged


def _compute_staff_target(
    day_of_week: str,
    weather_today: str,
    reputation_band: str,
    walkout_band: str,
    scenario_flags: dict[str, bool],
) -> int:
    """Derive the desired staff level from contextual signals."""
    target = _BASE_STAFF_BY_DAY.get(day_of_week, 6)

    if scenario_flags.get("tourist_season") and day_of_week in (
        "Friday",
        "Saturday",
        "Sunday",
    ):
        target += 1

    if weather_today == "sunny":
        target += 1
    elif weather_today == "stormy":
        target -= 1

    if reputation_band in ("Poor", "Fair"):
        target += 1

    # Renovation reduces covers, so cap the upper end before the walkout
    # override (walkout override may still push above this cap).
    if scenario_flags.get("renovation"):
        target = min(target, _RENOVATION_STAFF_CAP)

    # Walkout override: force +2 regardless of other modifiers.
    if walkout_band in ("Some", "Many"):
        target += 2

    # Final clip to engine-supported range.
    if target < _STAFF_MIN:
        target = _STAFF_MIN
    if target > _STAFF_MAX:
        target = _STAFF_MAX
    return target


def _select_daily_special(
    active_menu: list[str],
    menu_book: list[dict],
    dishes_sold: dict[str, int],
) -> str | None:
    """Pick today's special: highest revenue yesterday, fallback to base_price."""
    if not active_menu:
        return None

    active_set = set(active_menu)
    price_by_name: dict[str, float] = {}
    base_price_by_name: dict[str, float] = {}
    for entry in menu_book or []:
        name = entry.get("name")
        if not name:
            continue
        price_by_name[name] = float(entry.get("current_price", 0.0) or 0.0)
        base_price_by_name[name] = float(entry.get("base_price", 0.0) or 0.0)

    # Try revenue ranking first.
    best_dish: str | None = None
    best_revenue = -1.0
    for dish, count in (dishes_sold or {}).items():
        if dish not in active_set:
            continue
        try:
            revenue = float(count) * price_by_name.get(dish, 0.0)
        except (TypeError, ValueError):
            continue
        if revenue > best_revenue:
            best_revenue = revenue
            best_dish = dish

    if best_dish is not None and best_revenue > 0:
        return best_dish

    # Fallback: active dish with highest base_price.
    fallback: str | None = None
    fallback_price = -1.0
    for dish in active_menu:
        bp = base_price_by_name.get(dish, 0.0)
        if bp > fallback_price:
            fallback_price = bp
            fallback = dish
    return fallback


def _dish_to_ingredients(menu_book: list[dict]) -> dict[str, set[str]]:
    """Build dish_name -> {ingredient_names} from menu_book.

    Handles all three observed shapes for the `ingredients` field:
      - list[{"ingredient": str, "quantity_kg": float}] (canonical schema)
      - dict[str, float]
      - list[str]
    """
    out: dict[str, set[str]] = {}
    for entry in menu_book or []:
        name = entry.get("name")
        if not name:
            continue
        ings = entry.get("ingredients") or []
        names: set[str] = set()
        if isinstance(ings, dict):
            names = {str(k) for k in ings.keys()}
        elif isinstance(ings, list):
            for item in ings:
                if isinstance(item, dict):
                    n = item.get("ingredient") or item.get("name")
                    if n:
                        names.add(str(n))
                elif isinstance(item, str):
                    names.add(item)
        out[name] = names
    return out


def _derive_stockout_streak(
    stockout_log: list,
    dish_ingredients: dict[str, set[str]],
) -> dict[str, int]:
    """Build ingredient -> consecutive-trailing-days from Emre's stockout_log.

    The log is a list of {"day": int, "dish": str, "hour": int} events,
    capped at the last 5 entries. We map each entry through dish_ingredients
    to collect the set of days each ingredient appears, then compute the
    length of the consecutive run ending at the most recent day for that
    ingredient.

    Caveat: with only 5 log entries, an ingredient that's chronically out
    but masked by other dish events may underestimate. Use this in
    combination with current inventory as a safety check.
    """
    if not stockout_log:
        return {}

    ing_days: dict[str, set[int]] = {}
    for entry in stockout_log:
        if not isinstance(entry, dict):
            continue
        try:
            d = int(entry.get("day"))
        except (TypeError, ValueError):
            continue
        dish = entry.get("dish")
        if not dish:
            continue
        for ing in dish_ingredients.get(str(dish), set()):
            ing_days.setdefault(ing, set()).add(d)

    streak: dict[str, int] = {}
    for ing, days in ing_days.items():
        if not days:
            continue
        latest = max(days)
        run = 0
        cursor = latest
        while cursor in days:
            run += 1
            cursor -= 1
        streak[ing] = run
    return streak


def _ingredients_out_today(inventory: list[dict] | dict) -> set[str]:
    """Return the set of ingredients whose total_kg is currently 0.

    Tolerates both the canonical list[dict] shape and a dict[name->info]
    shape defensively.
    """
    out: set[str] = set()
    if isinstance(inventory, list):
        for entry in inventory:
            if not isinstance(entry, dict):
                continue
            name = entry.get("ingredient")
            if not name:
                continue
            try:
                total = float(entry.get("total_kg", 0.0) or 0.0)
            except (TypeError, ValueError):
                total = 0.0
            if total == 0.0:
                out.add(str(name))
    elif isinstance(inventory, dict):
        for name, info in inventory.items():
            if not isinstance(info, dict):
                continue
            try:
                total = float(info.get("total_kg", 0.0) or 0.0)
            except (TypeError, ValueError):
                total = 0.0
            if total == 0.0:
                out.add(str(name))
    return out


def _menu_after_stockout_drop(
    active_menu: list[str],
    dish_ingredients: dict[str, set[str]],
    bad_ingredients: set[str],
) -> list[str] | None:
    """Return a new active menu with dishes using bad_ingredients removed.

    Returns None when no drops are needed, or when dropping would push the
    menu below the minimum dish count.
    """
    if not bad_ingredients:
        return None

    kept: list[str] = []
    dropped_any = False
    for dish in active_menu:
        if dish_ingredients.get(dish, set()) & bad_ingredients:
            dropped_any = True
            continue
        kept.append(dish)

    if not dropped_any:
        return None
    if len(kept) < _MENU_MIN_DISHES:
        return None
    return kept


def _pricing_fallback_actions(
    menu_book: list[dict],
    active_menu: list[str],
    llm_pricing_failed: bool,
) -> list[dict]:
    """Mode B (crash override) is a superset of mode A; never run both."""
    actions: list[dict] = []
    base_price_by_name: dict[str, float] = {}
    for entry in menu_book or []:
        name = entry.get("name")
        if name:
            try:
                base_price_by_name[name] = float(entry.get("base_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                base_price_by_name[name] = 0.0

    if llm_pricing_failed:
        for dish in active_menu or []:
            bp = base_price_by_name.get(dish)
            if bp is None or bp <= 0:
                continue
            actions.append({"tool": "set_price", "args": {"dish": dish, "price": bp}})
        return actions

    # Mode A: out-of-bounds reset for active dishes.
    for entry in menu_book or []:
        if not entry.get("is_active"):
            continue
        name = entry.get("name")
        if not name:
            continue
        try:
            base = float(entry.get("base_price", 0.0) or 0.0)
            current = float(entry.get("current_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if base <= 0:
            continue
        lower = 0.8 * base - _PRICE_SLACK
        upper = 1.2 * base + _PRICE_SLACK
        if current < lower or current > upper:
            actions.append({"tool": "set_price", "args": {"dish": name, "price": base}})
    return actions


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def get_operations_actions(
    observation: dict,
    day: int,
    notes_state: dict,
    llm_pricing_failed: bool = False,
) -> tuple[list[dict], dict]:
    """Returns (actions, updated_scenario_flags)."""
    observation = observation or {}
    notes_state = notes_state or {}

    # --- Read observation fields defensively. ---
    day_of_week: str = observation.get("day_of_week", "Monday")
    reputation_band: str = observation.get("reputation_band", "Good")
    weather_today: str = observation.get("weather_today", "cloudy")
    alerts: list = observation.get("alerts", []) or []
    active_menu: list[str] = list(observation.get("active_menu", []) or [])
    menu_book: list[dict] = list(observation.get("menu_book", []) or [])
    service_summary: dict = observation.get("service_summary", {}) or {}
    walkout_band: str = service_summary.get("walkout_band", "None")
    dishes_sold: dict = service_summary.get("dishes_sold", {}) or {}
    # Inventory is a list of dicts per the AGENT_CONTRACT schema; the helper
    # tolerates both shapes defensively.
    inventory = observation.get("inventory", []) or []
    current_staff: int = int(observation.get("staff_level", 8) or 8)

    # --- Read notes_state defensively. ---
    happy_hour_streak: int = int(notes_state.get("happy_hour_streak", 0))
    last_staff_level: int = int(notes_state.get("last_staff_level", 8))
    previous_scenario_flags: dict[str, bool] = dict(
        notes_state.get("scenario_flags", {}) or {}
    )
    stockout_log: list = list(notes_state.get("stockout_log", []) or [])
    days_since_renovation: int = int(
        notes_state.get("days_since_renovation", -1)
    )

    # --- Scenario detection (sticky merge). ---
    scenario_flags = _detect_scenario_flags(alerts, previous_scenario_flags)

    # Renovation counter: 0 on the day it first flips True, then +1 daily.
    renovation_was_seen = previous_scenario_flags.get("renovation", False)
    renovation_now = scenario_flags.get("renovation", False)
    if renovation_now and not renovation_was_seen:
        days_since_renovation = 0
    elif renovation_now and days_since_renovation >= 0:
        days_since_renovation += 1
    # If renovation has never been seen, leave at -1.

    # --- Stockout: derive per-ingredient streak from Emre's stockout_log,
    # then filter by current inventory as a safety net so we don't drop
    # dishes based on stale log signal after a restock. ---
    dish_ingredients = _dish_to_ingredients(menu_book)
    derived_streak = _derive_stockout_streak(stockout_log, dish_ingredients)
    out_today = _ingredients_out_today(inventory)
    bad_ingredients = {
        ing for ing, streak in derived_streak.items()
        if streak >= 2 and ing in out_today
    }

    staff_actions: list[dict] = []
    menu_actions: list[dict] = []
    price_actions: list[dict] = []
    special_actions: list[dict] = []
    happy_hour_actions: list[dict] = []

    # --- Staffing. ---
    staff_target = _compute_staff_target(
        day_of_week=day_of_week,
        weather_today=weather_today,
        reputation_band=reputation_band,
        walkout_band=walkout_band,
        scenario_flags=scenario_flags,
    )
    if staff_target != current_staff:
        staff_actions.append(
            {"tool": "set_staff_level", "args": {"level": int(staff_target)}}
        )
        last_staff_level = staff_target
    else:
        last_staff_level = current_staff

    # --- Menu intervention from stockouts. ---
    new_menu = _menu_after_stockout_drop(active_menu, dish_ingredients, bad_ingredients)
    effective_active_menu = active_menu
    if new_menu is not None:
        # Dedup while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for d in new_menu:
            if d not in seen:
                seen.add(d)
                deduped.append(d)
        if len(deduped) >= _MENU_MIN_DISHES:
            menu_actions.append(
                {"tool": "set_menu", "args": {"dishes": deduped}}
            )
            effective_active_menu = deduped

    # --- Pricing safety layer. ---
    price_actions = _pricing_fallback_actions(
        menu_book=menu_book,
        active_menu=effective_active_menu,
        llm_pricing_failed=llm_pricing_failed,
    )

    # --- Daily special. ---
    special_dish = _select_daily_special(
        active_menu=effective_active_menu,
        menu_book=menu_book,
        dishes_sold=dishes_sold,
    )
    if special_dish is not None:
        special_actions.append(
            {"tool": "offer_daily_special", "args": {"dish": special_dish}}
        )

    # --- Happy hour. ---
    eligible_day = day_of_week in ("Monday", "Tuesday", "Wednesday")
    # Streak >= 1 means we ran HH yesterday; skip today to enforce no-back-to-back.
    cooldown_ok = happy_hour_streak < 1
    suppressed = scenario_flags.get("supply_crisis", False) and reputation_band in (
        "Poor",
        "Fair",
    )
    if eligible_day and cooldown_ok and not suppressed:
        happy_hour_actions.append({"tool": "run_happy_hour", "args": {}})
        happy_hour_streak = happy_hour_streak + 1  # bumped to 1+ for tomorrow's check
    else:
        happy_hour_streak = 0  # no run today -> reset streak

    # --- Assemble actions in debug-friendly order. ---
    actions: list[dict] = []
    actions.extend(staff_actions)
    actions.extend(menu_actions)
    actions.extend(price_actions)
    actions.extend(special_actions)
    actions.extend(happy_hour_actions)

    # --- Build updated state (operations-owned keys only). The orchestrator
    # merges these into the master notes dict. stockout_log is owned by the
    # orchestrator and is NOT returned here. ---
    updated_scenario_flags: dict = {
        "happy_hour_streak": int(happy_hour_streak),
        "last_staff_level": int(last_staff_level),
        "scenario_flags": scenario_flags,
        "days_since_renovation": int(days_since_renovation),
    }

    return actions, updated_scenario_flags
