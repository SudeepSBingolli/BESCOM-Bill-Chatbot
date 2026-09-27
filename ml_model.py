"""
Random Forest regressor trained on YOUR historical bills.

Why Random Forest here specifically: monthly electricity use is noisy and
non-linear (e.g. AC usage jumps hard once temperature crosses a threshold;
a festival month with guests behaves differently from a normal month) — a
handful of decision trees voting together handles that better than a
straight-line formula, as long as you feed it enough past months (12+ is
a reasonable minimum, more is better).

Expects data/bill_history.csv with columns:
    month, bulbs, tube_lights, fans, fridges, ac_units, washing_machines,
    tvs, water_heaters, avg_temp_c, units, bill_amount

`avg_temp_c` is optional (monthly average temperature) - it helps a lot
because AC/fan load is temperature-driven. Leave it blank/0 if you don't
want to track it.
"""
import os
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error
import joblib

from config import HISTORY_CSV, MODEL_PATH

FEATURE_COLS = [
    "bulbs", "tube_lights", "fans", "fridges", "ac_units",
    "washing_machines", "tvs", "water_heaters", "avg_temp_c", "month_num",
]


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # month column like "2026-03" -> extract month number (captures seasonality)
    df["month_num"] = pd.to_datetime(df["month"], format="%Y-%m").dt.month
    for col in FEATURE_COLS:
        if col not in df.columns:
            df[col] = 0
    df[FEATURE_COLS] = df[FEATURE_COLS].fillna(0)
    return df


def train(target: str = "bill_amount"):
    """target: 'bill_amount' or 'units'. Returns (model, mae, n_rows_used)."""
    if not os.path.exists(HISTORY_CSV):
        raise FileNotFoundError(
            f"No history file at {HISTORY_CSV}. Copy sample_bill_history.csv there "
            "and fill it with your real past bills first."
        )
    df = pd.read_csv(HISTORY_CSV)
    if len(df) < 6:
        raise ValueError(
            f"Only {len(df)} rows of history found. Random Forest needs at least "
            "~6-12 months of real past bills to be worth anything; add more rows."
        )
    df = _prep(df)
    X, y = df[FEATURE_COLS], df[target]

    if len(df) >= 10:
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    else:
        X_train, y_train = X, y
        X_test, y_test = X, y  # too little data to hold out a real test set

    model = RandomForestRegressor(n_estimators=200, max_depth=6, random_state=42)
    model.fit(X_train, y_train)
    mae = mean_absolute_error(y_test, model.predict(X_test))

    os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
    joblib.dump({"model": model, "target": target, "features": FEATURE_COLS}, MODEL_PATH)
    return model, mae, len(df)


def predict(appliance_counts: dict, avg_temp_c: float = 0, month_num: int | None = None) -> float:
    """Load the trained model and predict for a given set of appliance counts (auto-trains if needed)."""
    if not os.path.exists(MODEL_PATH):
        try:
            train()
        except Exception as e:
            raise ValueError(f"Could not prepare AI model: {e}")

    bundle = joblib.load(MODEL_PATH)
    model, features = bundle["model"], bundle["features"]

    import datetime
    month_num = month_num or datetime.date.today().month

    row = {
        "bulbs": appliance_counts.get("led_bulb", 0) + appliance_counts.get("tube_light", 0),
        "tube_lights": appliance_counts.get("tube_light", 0),
        "fans": appliance_counts.get("ceiling_fan", 0),
        "fridges": appliance_counts.get("fridge", 0),
        "ac_units": appliance_counts.get("ac_1_5_ton", 0),
        "washing_machines": appliance_counts.get("washing_machine", 0),
        "tvs": appliance_counts.get("tv", 0),
        "water_heaters": appliance_counts.get("water_heater", 0),
        "avg_temp_c": avg_temp_c,
        "month_num": month_num,
    }
    X = pd.DataFrame([[row[f] for f in features]], columns=features)
    return round(float(model.predict(X)[0]), 2)


if __name__ == "__main__":
    model, mae, n = train()
    print(f"Trained on {n} rows. Mean absolute error: {mae:.2f}")
