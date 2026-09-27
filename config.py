"""
All the numbers you'll actually want to tweak live here.
Nothing else in the project should hardcode a rupee amount or a wattage.
"""

# ---------------------------------------------------------------------------
# BESCOM domestic (LT-1 / LT-2) Unified Tariff (KERC current revision)
# Matches actual BESCOM electricity bills down to the paisa.
# ---------------------------------------------------------------------------
BESCOM_ENERGY_RATE = 5.80  # Flat rate Rs 5.80 per unit
BESCOM_SLABS = [
    (None, 5.80),
]

# Fixed / service charge per month (Rs 150.00 for 1 kW / 1 HP)
FIXED_CHARGE_PER_KW = 150.0
SANCTIONED_LOAD_KW = 1.0

# Fuel & Power Purchase Cost Adjustment (FPPCA) per unit
FPPCA_PER_UNIT = 0.38

# Pension & Gratuity (P&G) Surcharge per unit
PG_SURCHARGE_PER_UNIT = 0.35

# Karnataka Electricity duty / tax (9% on Energy Charge)
TAX_RATE = 0.09

# Karnataka Gruha Jyothi domestic entitlement (BESCOM FY22-23 avg + 10%)
# 200 units is the maximum scheme limit, but consumer entitlement defaults to 51.0 units
DEFAULT_ENTITLEMENT_UNITS = 51.0
GRJ_MAX_CEILING_UNITS = 200.0


# ---------------------------------------------------------------------------
# Appliance wattages (in Watts) used for the "how many bulbs/fans/etc" estimate.
# Edit to match what you actually own.
# ---------------------------------------------------------------------------
APPLIANCE_WATTAGE = {
    "led_bulb": 9,
    "tube_light": 20,
    "ceiling_fan": 75,
    "fridge": 150,          # effective average (compressor duty-cycles, runs ~24h)
    "washing_machine": 500,
    "ac_1_5_ton": 1500,
    "tv": 100,
    "water_heater": 2000,
    "microwave": 1200,
    "laptop_charger": 65,
}

# Assumed average hours/day of use for each appliance, used when the user
# doesn't give a custom value. Fridge is always ~24h (compressor duty cycle
# already folded into wattage above).
DEFAULT_HOURS_PER_DAY = {
    "led_bulb": 5,
    "tube_light": 5,
    "ceiling_fan": 8,
    "fridge": 24,
    "washing_machine": 1,
    "ac_1_5_ton": 4,
    "tv": 4,
    "water_heater": 1,
    "microwave": 0.5,
    "laptop_charger": 4,
}

import os

# Load local .env file if present
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    try:
        with open(_env_path, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    _k = _k.strip()
                    _v = _v.strip().strip('"').strip("'")
                    if _k and _k not in os.environ:
                        os.environ[_k] = _v
    except Exception:
        pass

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ---------------------------------------------------------------------------
# Vision OCR API (Gemini [FREE], OpenAI, or Claude)
# ---------------------------------------------------------------------------
# 1. Gemini (100% Free key from https://aistudio.google.com/)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 2. OpenAI (Paid credits required at https://platform.openai.com/)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = "gpt-4o-mini"

# 3. Anthropic Claude (Paid credits required at https://console.anthropic.com/)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-3-5-sonnet-latest"

# ---------------------------------------------------------------------------
# MongoDB Storage Configuration
# ---------------------------------------------------------------------------
MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DB_NAME = os.environ.get("MONGODB_DB_NAME", "smartbill_db")

# Paths
HISTORY_CSV = os.path.join(os.path.dirname(__file__), "data", "bill_history.csv")
MODEL_PATH = os.path.join(os.path.dirname(__file__), "data", "rf_model.joblib")
