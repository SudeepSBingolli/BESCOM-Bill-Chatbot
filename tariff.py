"""
Accurate BESCOM LT-1 / LT-2 domestic electricity tariff engine.
Matches official Karnataka Electricity Regulatory Commission (KERC) and BESCOM billing rules:
- Flat Energy Charge: ₹5.80 / unit
- Fixed Charges: ₹150.00 / kW (or HP) prorated across billing duration
- FPPCA: Fuel and Power Purchase Cost Adjustment
- Pension & Gratuity (P&G) Surcharge: ₹0.35 / unit
- Karnataka Electricity Tax: 9% on Energy Charges
- Gruha Jyothi (GRJ) Scheme: Subsidizes eligible units based on billing period:
  GRJ Eligible Units = Monthly Entitlement × (Billing Days / 30)
  Chargeable Units = Total Consumption - GRJ Eligible Units
- Multi-month billing: Entitlement and ceilings scale with billing duration.
"""
from config import (
    BESCOM_SLABS,
    FIXED_CHARGE_PER_KW,
    SANCTIONED_LOAD_KW,
    TAX_RATE,
    FPPCA_PER_UNIT,
    PG_SURCHARGE_PER_UNIT,
    DEFAULT_ENTITLEMENT_UNITS,
)


def energy_charge(units: float) -> float:
    """Calculates Energy Charge across KERC tariff slabs (flat ₹5.80/unit)."""
    remaining = max(0.0, float(units))
    lower = 0
    total = 0.0
    for upper, rate in BESCOM_SLABS:
        if upper is None:
            total += remaining * rate
            break
        slab_units = max(0.0, min(remaining, upper - lower))
        total += slab_units * rate
        remaining -= slab_units
        lower = upper
        if remaining <= 0:
            break
    return round(total, 2)


def calculate_bill(
    units: float,
    gruha_jyothi: bool = False,
    entitlement_units: float = DEFAULT_ENTITLEMENT_UNITS,
    billing_days: int = 30,
    sanctioned_load_kw: float = SANCTIONED_LOAD_KW,
    md_penalty: float = 0.0,
    interest: float = 0.0,
    credit: float = 0.0,
    true_up_ec: float = 0.0,
    true_up_tax: float = 0.0,
    true_up_gj_sub: float = 0.0,
    true_up_gj_tax: float = 0.0,
    arrears: float = 0.0,
    fppca_override: float = None,
    fppca_charge_override: float = None,
    fppca_sub_override: float = None,
    fixed_charge_override: float = None,
) -> dict:
    """
    Calculates domestic electricity bill matching actual BESCOM bill slips down to the paisa.
    
    Correctly accounts for:
    - Billing duration (prorates fixed charges and GRJ entitlement: entitlement × days / 30)
    - Gruha Jyothi monthly ceiling (200 units/month scaled by billing days)
    - Sub-total 1 (Gross Charges) & Sub-total 2 (GRJ Subsidy)
    - Current demand and net payable after penalties, adjustments, true-ups, and credits.
    """
    units = max(0.0, float(units))
    entitlement = max(0.0, float(entitlement_units))
    billing_days = max(1, int(billing_days))
    billing_months = billing_days / 30.0

    # 1. Normal Electricity Charges (Before Subsidy)
    load_basis = round(sanctioned_load_kw * billing_months, 2)
    regular_fixed = round(fixed_charge_override, 2) if fixed_charge_override is not None else round(load_basis * FIXED_CHARGE_PER_KW, 2)
    regular_energy = round(units * 5.80, 2)
    effective_fppca = fppca_charge_override if fppca_charge_override is not None else fppca_override
    regular_fppca = round(effective_fppca, 2) if effective_fppca is not None else round(units * FPPCA_PER_UNIT, 2)
    regular_pg = round(units * PG_SURCHARGE_PER_UNIT, 2)
    regular_tax = round(regular_energy * TAX_RATE, 2)
    sub_total_1 = round(regular_fixed + regular_energy + regular_fppca + regular_pg + regular_tax, 2)

    # 2. Gruha Jyothi Calculation
    monthly_equivalent = units / billing_months
    monthly_cap = 200.0 * billing_months

    if gruha_jyothi:
        # Check if monthly equivalent consumption exceeds the 200-unit ceiling
        if monthly_equivalent > 200.0 and units > monthly_cap:
            return {
                "units": round(units, 2),
                "billing_days": billing_days,
                "billing_months": round(billing_months, 2),
                "energy_charge": regular_energy,
                "fppca": regular_fppca,
                "pg_surcharge": regular_pg,
                "fixed_charge": regular_fixed,
                "tax": regular_tax,
                "sub_total_1": sub_total_1,
                "sub_total_2": 0.0,
                "amount_after_subsidy": sub_total_1,
                "md_penalty": round(md_penalty, 2),
                "interest": round(interest, 2),
                "current_demand": round(sub_total_1 + md_penalty + interest, 2),
                "credit": round(credit, 2),
                "true_up": round(true_up_ec + true_up_tax + true_up_gj_sub + true_up_gj_tax, 2),
                "arrears": round(arrears, 2),
                "total": round(sub_total_1 + md_penalty + interest - credit + true_up_ec + true_up_tax + true_up_gj_sub + true_up_gj_tax + arrears, 2),
                "gruha_jyothi": True,
                "is_free": False,
                "disqualified": True,
                "disqualified_reason": f"Consumption exceeded 200 units/month limit ({monthly_equivalent:.1f} u/month)",
                "monthly_entitlement": round(entitlement, 2),
                "entitlement_units": round(entitlement, 2),
                "grj_eligible_units": 0.0,
                "free_units": 0.0,
                "chargeable_units": round(units, 2),
                "excess_units": round(units, 2),
                "govt_subsidy": 0.0,
                "regular_total": sub_total_1,
            }

        # Calculate billing-period eligible units: entitlement × days / 30
        grj_eligible = round(entitlement * billing_months, 1)
        if units <= grj_eligible:
            # Zero Bill
            return {
                "units": round(units, 2),
                "billing_days": billing_days,
                "billing_months": round(billing_months, 2),
                "energy_charge": 0.0,
                "fppca": 0.0,
                "pg_surcharge": 0.0,
                "fixed_charge": 0.0,
                "tax": 0.0,
                "sub_total_1": sub_total_1,
                "sub_total_2": sub_total_1,
                "amount_after_subsidy": 0.0,
                "md_penalty": round(md_penalty, 2),
                "interest": round(interest, 2),
                "current_demand": round(md_penalty + interest, 2),
                "credit": round(credit, 2),
                "true_up": round(true_up_ec + true_up_tax + true_up_gj_sub + true_up_gj_tax, 2),
                "arrears": round(arrears, 2),
                "total": round(max(0.0, md_penalty + interest - credit + true_up_ec + true_up_tax + true_up_gj_sub + true_up_gj_tax + arrears), 2),
                "gruha_jyothi": True,
                "is_free": True,
                "disqualified": False,
                "monthly_entitlement": round(entitlement, 2),
                "entitlement_units": round(entitlement, 2),
                "grj_eligible_units": round(units, 2),
                "free_units": round(units, 2),
                "chargeable_units": 0.0,
                "excess_units": 0.0,
                "govt_subsidy": sub_total_1,
                "regular_total": sub_total_1,
            }
        else:
            chargeable_units = round(units - grj_eligible, 1)

            # GRJ Subsidy (Sub-total 2)
            sub_fixed = regular_fixed
            sub_energy = round(grj_eligible * 5.80, 2)
            sub_fppca = round(fppca_sub_override, 2) if fppca_sub_override is not None else round(grj_eligible * FPPCA_PER_UNIT, 2)
            sub_pg = round(grj_eligible * PG_SURCHARGE_PER_UNIT, 2)
            sub_tax = round(sub_energy * TAX_RATE, 2)
            sub_total_2 = round(sub_fixed + sub_energy + sub_fppca + sub_pg + sub_tax, 2)

            amount_after_subsidy = round(sub_total_1 - sub_total_2, 2)
            current_demand = round(amount_after_subsidy + md_penalty + interest, 2)
            true_up_net = round(true_up_ec + true_up_tax + true_up_gj_sub + true_up_gj_tax, 2)
            final_net = round(current_demand - credit + true_up_net + arrears, 2)

            return {
                "units": round(units, 2),
                "billing_days": billing_days,
                "billing_months": round(billing_months, 2),
                "energy_charge": round(chargeable_units * 5.80, 2),
                "fppca": round(regular_fppca - sub_fppca, 2),
                "pg_surcharge": round(chargeable_units * PG_SURCHARGE_PER_UNIT, 2),
                "fixed_charge": 0.0,  # 100% subsidized
                "tax": round(round(chargeable_units * 5.80, 2) * TAX_RATE, 2),
                "sub_total_1": sub_total_1,
                "sub_total_2": sub_total_2,
                "sub_fixed": sub_fixed,
                "sub_energy": sub_energy,
                "sub_fppca": sub_fppca,
                "sub_pg": sub_pg,
                "sub_tax": sub_tax,
                "amount_after_subsidy": amount_after_subsidy,
                "md_penalty": round(md_penalty, 2),
                "interest": round(interest, 2),
                "current_demand": current_demand,
                "credit": round(credit, 2),
                "true_up": true_up_net,
                "arrears": round(arrears, 2),
                "total": final_net,
                "gruha_jyothi": True,
                "is_free": False,
                "disqualified": False,
                "monthly_entitlement": round(entitlement, 2),
                "entitlement_units": round(entitlement, 2),
                "grj_eligible_units": grj_eligible,
                "free_units": grj_eligible,
                "chargeable_units": chargeable_units,
                "excess_units": chargeable_units,
                "govt_subsidy": sub_total_2,
                "regular_total": sub_total_1,
            }

    # Regular Non-Gruha Jyothi
    current_demand = round(sub_total_1 + md_penalty + interest, 2)
    true_up_net = round(true_up_ec + true_up_tax + true_up_gj_sub + true_up_gj_tax, 2)
    final_net = round(current_demand - credit + true_up_net + arrears, 2)

    return {
        "units": round(units, 2),
        "billing_days": billing_days,
        "billing_months": round(billing_months, 2),
        "energy_charge": regular_energy,
        "fppca": regular_fppca,
        "pg_surcharge": regular_pg,
        "fixed_charge": regular_fixed,
        "tax": regular_tax,
        "sub_total_1": sub_total_1,
        "sub_total_2": 0.0,
        "amount_after_subsidy": sub_total_1,
        "md_penalty": round(md_penalty, 2),
        "interest": round(interest, 2),
        "current_demand": current_demand,
        "credit": round(credit, 2),
        "true_up": true_up_net,
        "arrears": round(arrears, 2),
        "total": final_net,
        "gruha_jyothi": False,
        "is_free": False,
        "disqualified": False,
        "monthly_entitlement": 0.0,
        "entitlement_units": 0.0,
        "grj_eligible_units": 0.0,
        "free_units": 0.0,
        "chargeable_units": round(units, 2),
        "excess_units": round(units, 2),
        "govt_subsidy": 0.0,
        "regular_total": sub_total_1,
    }
