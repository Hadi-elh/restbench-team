"""Rule-based inventory ordering — cheapest eligible supplier, no duplicates."""

from __future__ import annotations

REORDER_POINT: dict[str, float] = {
    "Flour": 6.0,
    "Tomato Sauce": 3.0,
    "Mozzarella": 3.0,
    "Fresh Pasta": 4.0,
    "Cream": 2.0,
    "Mushrooms": 2.0,
    "Chicken": 3.0,
    "Lettuce": 2.0,
    "Pepperoni": 2.0,
    "Salmon": 2.0,
}

ORDER_QTY: dict[str, float] = {
    "Flour": 8.0,
    "Tomato Sauce": 5.0,
    "Mozzarella": 5.0,
    "Fresh Pasta": 8.0,
    "Cream": 5.0,
    "Mushrooms": 5.0,
    "Chicken": 5.0,
    "Lettuce": 5.0,
    "Pepperoni": 5.0,
    "Salmon": 5.0,
}

CASH_RESERVE = 1500.0


def get_ordering_actions(
    observation: dict,
    day: int,
    notes_state: dict | None = None,
) -> tuple[list[dict], dict]:
    actions: list[dict] = []
    notes_state = notes_state or {}
    supplier_flags = notes_state.get("supplier_flags", {})

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

    # ── Build supplier index: ingredient → (supplier_name, price, min_order) ──
    # Among suppliers that carry the ingredient, pick cheapest non-blacklisted.
    cheapest: dict[str, tuple[str, float, float]] = {}
    for sup in observation.get("supplier_catalog", []):
        name = sup["name"]
        if supplier_flags.get(name) == "blacklisted":
            continue
        min_order = float(sup.get("min_order_kg", 0))
        for ingredient, price in sup.get("ingredients", {}).items():
            price = float(price)
            if ingredient not in cheapest or price < cheapest[ingredient][1]:
                cheapest[ingredient] = (name, price, min_order)

    # ── Budget ────────────────────────────────────────────────────────────────
    cash = float(observation.get("cash", 0))
    budget = cash - CASH_RESERVE
    if budget <= 0:
        return actions, {}
    spent = 0.0

    # ── Build need list, cheapest-first ───────────────────────────────────────
    needs: list[tuple[float, str, str, float]] = []
    for ingredient, reorder_point in REORDER_POINT.items():
        usable = usable_stock.get(ingredient, 0.0)
        pending = pending_qty.get(ingredient, 0.0)
        effective = usable + pending

        if effective >= reorder_point:
            continue
        if ingredient not in cheapest:
            continue

        supplier_name, price, min_order = cheapest[ingredient]
        qty = max(ORDER_QTY.get(ingredient, 5.0), min_order)
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

    return actions, {}
