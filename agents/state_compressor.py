"""Compress the raw observation dict into a ~500-token text summary for the LLM."""

from __future__ import annotations


def compress_observation(observation: dict, day: int, notes_state: dict) -> str:
    lines: list[str] = []

    # ── FINANCIALS ──────────────────────────────────────────────────────────
    cash = observation.get("cash", 0)
    staff_level = observation.get("staff_level", 8)
    daily_burn = 300 + 120 * staff_level
    days_remaining = observation.get("days_remaining", 30 - day)
    yesterday_revenue = observation.get("yesterday_revenue", 0)
    yesterday_costs = observation.get("yesterday_total_costs", 0)
    yesterday_net = yesterday_revenue - yesterday_costs
    projected_cash = cash + yesterday_net * days_remaining

    lines.append("=== FINANCIALS ===")
    lines.append(f"Cash: €{cash:.0f} | Daily burn: €{daily_burn} | Days remaining: {days_remaining}")
    lines.append(f"Yesterday: revenue=€{yesterday_revenue:.0f}  costs=€{yesterday_costs:.0f}  net=€{yesterday_net:.0f}")
    cost_bd = observation.get("cost_breakdown", {})
    if cost_bd:
        lines.append(
            f"  Cost breakdown: staff=€{cost_bd.get('staff',0):.0f}  fixed=€{cost_bd.get('fixed',0):.0f}"
            f"  marketing=€{cost_bd.get('marketing',0):.0f}  waste=€{cost_bd.get('waste',0):.0f}"
        )
    lines.append(f"Projected cash at day 30 (if trend holds): €{projected_cash:.0f}")

    # ── INVENTORY RISK ───────────────────────────────────────────────────────
    lines.append("\n=== INVENTORY RISK ===")
    pending_qty: dict[str, float] = {}
    for po in observation.get("pending_orders", []):
        k = po["ingredient"]
        pending_qty[k] = pending_qty.get(k, 0) + po["quantity_kg"]

    for inv in observation.get("inventory", []):
        ingredient = inv["ingredient"]
        usable_kg = sum(
            b["quantity_kg"] for b in inv.get("batches", [])
            if b["expires_in_days"] > 1
        )
        expiring_soon_kg = sum(
            b["quantity_kg"] for b in inv.get("batches", [])
            if b["expires_in_days"] <= 2
        )
        pending_kg = pending_qty.get(ingredient, 0)
        effective = usable_kg + pending_kg

        if effective < 2:
            flag = "  !! CRITICAL"
        elif effective < 4:
            flag = "  ! WARNING"
        else:
            flag = ""

        parts = [f"{ingredient}: usable={usable_kg:.1f}kg  pending={pending_kg:.1f}kg"]
        if expiring_soon_kg > 0:
            parts.append(f"expiring_soon={expiring_soon_kg:.1f}kg")
        lines.append("  " + "  ".join(parts) + flag)

    ss = observation.get("service_summary", {}) or {}
    unavailable = ss.get("dishes_unavailable_at", {})
    if unavailable:
        lines.append(f"  STOCKOUTS yesterday: {', '.join(f'{d} @{h}h' for d, h in unavailable.items())}")

    # ── OPERATIONS ───────────────────────────────────────────────────────────
    lines.append("\n=== OPERATIONS ===")
    dow = observation.get("day_of_week", "?")
    weather_today = observation.get("weather_today", "?")
    weather_forecast = observation.get("weather_forecast", [])
    forecast_str = ", ".join(weather_forecast) if weather_forecast else "?"
    reputation = observation.get("reputation_band", "?")
    customer_trend = observation.get("customer_trend", "?")
    walkout_band = ss.get("walkout_band", "?")
    total_covers = ss.get("total_covers", 0)

    lines.append(f"Day {day}  {dow}  Staff: {staff_level}")
    lines.append(f"Weather: {weather_today} | Forecast (3d): {forecast_str}")
    lines.append(f"Reputation: {reputation} | Customer trend: {customer_trend} | Walkouts yesterday: {walkout_band}")
    lines.append(f"Yesterday: {total_covers} covers  €{yesterday_revenue:.0f} revenue")

    active_menu = observation.get("active_menu", [])
    lines.append(f"Active menu ({len(active_menu)} dishes): {', '.join(active_menu)}")

    menu_book = observation.get("menu_book", [])
    if menu_book:
        lines.append("Menu prices (base → current):")
        for dish in menu_book:
            if dish.get("is_active"):
                lines.append(f"  {dish['name']}: base=€{dish['base_price']}  current=€{dish['current_price']}")

    dishes_sold = ss.get("dishes_sold", {})
    if dishes_sold:
        top = sorted(dishes_sold.items(), key=lambda x: x[1], reverse=True)[:5]
        lines.append(f"Top dishes sold: {', '.join(f'{d}={n}' for d, n in top)}")

    # ── SUPPLIERS ────────────────────────────────────────────────────────────
    lines.append("\n=== SUPPLIERS ===")
    alert_text = " ".join(observation.get("alerts", []))
    for sup in observation.get("supplier_catalog", []):
        name = sup["name"]
        lead = sup.get("lead_time_days", "?")
        delivery_days = ", ".join(sup.get("delivery_days", []))
        ingredients = list(sup.get("ingredients", {}).keys())
        flag = " [IN ALERTS]" if name in alert_text else ""
        lines.append(
            f"  {name}{flag}: lead={lead}d  deliveries={delivery_days}  "
            f"stocks={', '.join(ingredients)}"
        )

    pending_orders = observation.get("pending_orders", [])
    if pending_orders:
        lines.append("Pending orders:")
        for po in pending_orders:
            lines.append(f"  {po['ingredient']} {po['quantity_kg']}kg from {po['supplier']} → day {po['delivery_day']}")

    # ── ALERTS ───────────────────────────────────────────────────────────────
    alerts = observation.get("alerts", [])
    if alerts:
        lines.append("\n=== ALERTS ===")
        for a in alerts:
            lines.append(f"  {a}")

    # ── MEMORY ───────────────────────────────────────────────────────────────
    if notes_state:
        lines.append("\n=== MEMORY ===")
        if "day_history" in notes_state:
            hist = notes_state["day_history"]
            if hist:
                trend = "  ".join(
                    f"d{h['day']}:rev=€{h.get('revenue',0):.0f}" for h in hist[-3:]
                )
                lines.append(f"Revenue trend (last 3): {trend}")
        if "stockout_log" in notes_state:
            recent_so = notes_state["stockout_log"][-3:]
            if recent_so:
                lines.append(f"Stockout log (last 3): {recent_so}")
        if "supplier_flags" in notes_state and notes_state["supplier_flags"]:
            lines.append(f"Supplier flags: {notes_state['supplier_flags']}")
        if "scenario_flags" in notes_state and notes_state["scenario_flags"]:
            lines.append(f"Scenario flags: {notes_state['scenario_flags']}")
        if "happy_hour_streak" in notes_state:
            lines.append(f"Happy hour streak: {notes_state['happy_hour_streak']} days")

    return "\n".join(lines)
