"""Rule-based inventory ordering — forward-looking, weekend-aware.

Changes vs v1 (static reorder points):
  A. Consumption-rate reorder: threshold = max(STATIC[ing], burn × (lead_gap + 2))
     where lead_gap = supplier_lead_days + 7 (worst-case weekly delivery schedule).
     Falls back to static threshold when no consumption history exists.
  B. Weekend buffer: on Tue/Wed/Thu, raise effective threshold so projected stock
     at Friday start covers max_5d_consumption × 1.5 (weekend multiplier).
  C. Dynamic order qty: max(min_order, min(30 kg, burn_rate × 4 days)).
     Falls back to ORDER_QTY static table when no burn data.
"""

from __future__ import annotations

from agents.memory import update_supplier_flags

REORDER_POINT: dict[str, float] = {
    "Flour": 12.0,
    "Tomato Sauce": 6.0,
    "Mozzarella": 5.0,
    "Fresh Pasta": 8.0,
    "Cream": 4.0,
    "Mushrooms": 4.0,
    "Chicken": 5.0,
    "Lettuce": 3.0,
    "Pepperoni": 3.0,
    "Salmon": 3.0,
}

ORDER_QTY: dict[str, float] = {
    "Flour": 20.0,
    "Tomato Sauce": 12.0,
    "Mozzarella": 10.0,
    "Fresh Pasta": 15.0,
    "Cream": 8.0,
    "Mushrooms": 8.0,
    "Chicken": 10.0,
    "Lettuce": 6.0,
    "Pepperoni": 6.0,
    "Salmon": 6.0,
}

CASH_RESERVE = 1500.0
SUPPLY_CRISIS_BUFFER = 1.5
_MAX_ORDER_KG = 30.0
_WEEKEND_MULTIPLIER = 1.5
# Days where we pre-check weekend coverage: name → days until Friday
_PRE_WEEKEND_DAYS: dict[str, int] = {"Tuesday": 3, "Wednesday": 2, "Thursday": 1}


def _build_consumption_stats(recent_consumption: list) -> tuple[dict[str, float], dict[str, float]]:
    """Return (avg_daily, max_daily) per ingredient from last ≤5 consumption entries.

    Entries are {day: int, consumption: {ingredient: kg}}. Returns empty dicts
    when the list is empty (day 1 or first run).
    """
    if not recent_consumption:
        return {}, {}
    all_ings: set[str] = set()
    for entry in recent_consumption:
        c = entry.get("consumption") or {}
        all_ings.update(c.keys())
    n = len(recent_consumption)
    avg: dict[str, float] = {}
    max_: dict[str, float] = {}
    for ing in all_ings:
        values = [
            float(e.get("consumption", {}).get(ing, 0) or 0)
            for e in recent_consumption
        ]
        avg[ing] = sum(values) / n
        max_[ing] = max(values)
    return avg, max_


def get_ordering_actions(
    observation: dict,
    day: int,
    notes_state: dict | None = None,
) -> tuple[list[dict], dict]:
    actions: list[dict] = []
    notes_state = notes_state or {}
    supplier_flags = notes_state.get("supplier_flags", {})

    # Build effective static reorder points — bumped during supply_crisis.
    scenario_flags = notes_state.get("scenario_flags", {}) or {}
    in_supply_crisis = bool(scenario_flags.get("supply_crisis"))
    in_renovation = bool(scenario_flags.get("renovation"))
    if in_supply_crisis:
        effective_static = {k: v * SUPPLY_CRISIS_BUFFER for k, v in REORDER_POINT.items()}
    elif in_renovation:
        effective_static = {k: v * 0.6 for k, v in REORDER_POINT.items()}
    else:
        effective_static = dict(REORDER_POINT)
    renovation_qty_mult = 0.7 if in_renovation else 1.0

    # ── Usable stock (batches expiring > 1 day from now) ──────────────────────
    usable_stock: dict[str, float] = {}
    for inv in observation.get("inventory", []):
        ingredient = inv["ingredient"]
        usable_stock[ingredient] = sum(
            b["quantity_kg"] for b in inv.get("batches", [])
            if b["expires_in_days"] > 1
        )

    # ── Pending orders — count as future stock to avoid double-ordering ───────
    pending_qty: dict[str, float] = {}
    for po in observation.get("pending_orders", []):
        k = po["ingredient"]
        pending_qty[k] = pending_qty.get(k, 0) + po["quantity_kg"]

    # ── Build supplier index: ingredient → (name, price, min_order, lead_days) ─
    # Pass 1: prefer non-blacklisted suppliers.
    cheapest: dict[str, tuple[str, float, float, int]] = {}
    for sup in observation.get("supplier_catalog", []):
        name = sup["name"]
        if supplier_flags.get(name) == "blacklisted":
            continue
        min_order = float(sup.get("min_order_kg", 0))
        lead_days = int(sup.get("lead_time_days", 1))
        for ingredient, price in sup.get("ingredients", {}).items():
            price = float(price)
            if ingredient not in cheapest or price < cheapest[ingredient][1]:
                cheapest[ingredient] = (name, price, min_order, lead_days)
    # Pass 2: blacklisted last-resort when sole source.
    for sup in observation.get("supplier_catalog", []):
        if supplier_flags.get(sup["name"]) != "blacklisted":
            continue
        min_order = float(sup.get("min_order_kg", 0))
        lead_days = int(sup.get("lead_time_days", 1))
        for ingredient, price in sup.get("ingredients", {}).items():
            if ingredient not in cheapest:
                cheapest[ingredient] = (sup["name"], float(price), min_order, lead_days)

    # ── Consumption stats from persisted notes ────────────────────────────────
    recent_consumption: list = notes_state.get("recent_consumption", [])
    avg_daily, max_daily = _build_consumption_stats(recent_consumption)

    # Weekend pre-order context.
    day_of_week: str = observation.get("day_of_week", "")
    days_until_friday: int = _PRE_WEEKEND_DAYS.get(day_of_week, 0)

    # ── Budget ────────────────────────────────────────────────────────────────
    cash = float(observation.get("cash", 0))
    budget = cash - CASH_RESERVE
    if budget <= 0:
        return actions, {}
    spent = 0.0

    # ── Build need list, cheapest-first ───────────────────────────────────────
    needs: list[tuple[float, str, str, float]] = []
    # Track ingredients already covered to avoid duplicates between main and
    # weekend-only paths (there's no duplication risk in this single-pass design,
    # but the explicit set makes the logic clear).
    for ingredient, static_threshold in effective_static.items():
        usable = usable_stock.get(ingredient, 0.0)
        pending = pending_qty.get(ingredient, 0.0)
        effective = usable + pending

        if ingredient not in cheapest:
            continue

        supplier_name, price, min_order, lead_days = cheapest[ingredient]
        burn = avg_daily.get(ingredient, 0.0)
        daily_max = max_daily.get(ingredient, 0.0)

        # Change A: dynamic reorder threshold based on burn rate.
        if burn > 0:
            # worst-case gap: lead time + up to 7 days until next delivery slot
            lead_gap = lead_days + 7
            dynamic_threshold = burn * (lead_gap + 2)
            threshold = max(static_threshold, dynamic_threshold)
        else:
            threshold = static_threshold  # fall back when no history

        # Change B: weekend buffer — raise threshold on Tue/Wed/Thu.
        if days_until_friday > 0 and daily_max > 0:
            # We want projected_stock_at_friday >= weekend_burn.
            # projected = effective - (days_until_friday × avg_daily)
            # => minimum effective stock now = days_until_friday*avg + weekend_burn
            weekend_burn = daily_max * _WEEKEND_MULTIPLIER
            weekend_min = days_until_friday * burn + weekend_burn
            threshold = max(threshold, weekend_min)

        if effective >= threshold:
            continue

        # Change C: dynamic order quantity.
        if burn > 0:
            base_qty = min(_MAX_ORDER_KG, burn * 4)
        else:
            base_qty = ORDER_QTY.get(ingredient, 5.0)

        base_qty *= renovation_qty_mult
        if in_supply_crisis:
            base_qty *= SUPPLY_CRISIS_BUFFER

        qty = max(base_qty, min_order)
        cost = qty * price
        needs.append((cost, ingredient, supplier_name, qty))

    needs.sort()  # cheapest orders first

    for cost, ingredient, supplier_name, qty in needs:
        if spent + cost > budget:
            continue
        actions.append({
            "tool": "place_order",
            "args": {
                "supplier": supplier_name,
                "ingredient": ingredient,
                "quantity_kg": round(qty, 1),
            },
        })
        spent += cost

    # Update supplier reliability flags based on recent delivery history.
    updated_supplier_flags = update_supplier_flags(
        supplier_flags,
        observation.get("delivery_history", []) or [],
    )
    return actions, {"supplier_flags": updated_supplier_flags}
