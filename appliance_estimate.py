"""
Turns 'how many bulbs / fans / fridges ... and how many hours/day' into
an estimated monthly kWh figure. This is the feature-engineering step that
feeds the ML model (and also works standalone as a quick estimate).
"""
from config import APPLIANCE_WATTAGE, DEFAULT_HOURS_PER_DAY

DAYS_IN_MONTH = 30


def estimate_monthly_units(appliance_counts: dict, hours_override: dict | None = None) -> float:
    """
    appliance_counts: {"led_bulb": 6, "ceiling_fan": 3, "fridge": 1, ...}
    hours_override: optional {"ac_1_5_ton": 6} to override DEFAULT_HOURS_PER_DAY
    Returns estimated units (kWh) for the month.
    """
    hours_override = hours_override or {}
    total_wh = 0.0
    for appliance, count in appliance_counts.items():
        if count <= 0 or appliance not in APPLIANCE_WATTAGE:
            continue
        watts = APPLIANCE_WATTAGE[appliance]
        hours = hours_override.get(appliance, DEFAULT_HOURS_PER_DAY.get(appliance, 2))
        total_wh += watts * hours * count * DAYS_IN_MONTH
    return round(total_wh / 1000.0, 2)  # Wh -> kWh (units)


def appliance_breakdown(appliance_counts: dict, hours_override: dict | None = None) -> list:
    """Same as above but returns a per-appliance breakdown, useful for showing
    the user *why* their bill is what it is (e.g. 'your AC is 60% of your bill')."""
    hours_override = hours_override or {}
    rows = []
    for appliance, count in appliance_counts.items():
        if count <= 0 or appliance not in APPLIANCE_WATTAGE:
            continue
        watts = APPLIANCE_WATTAGE[appliance]
        hours = hours_override.get(appliance, DEFAULT_HOURS_PER_DAY.get(appliance, 2))
        units = round((watts * hours * count * DAYS_IN_MONTH) / 1000.0, 2)
        rows.append({"appliance": appliance, "count": count, "hours_per_day": hours, "units": units})
    rows.sort(key=lambda r: -r["units"])
    return rows
