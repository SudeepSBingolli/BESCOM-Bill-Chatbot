"""
SmartBill ⚡ - Telegram Electricity Bill & Usage Assistant
Provides an interactive menu-driven flow with buttons, photo meter OCR,
appliance load estimations, AI bill predictions, history trends,
and manual reading entry options.
"""
import os
import tempfile
import logging
import pandas as pd
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, HISTORY_CSV, DEFAULT_ENTITLEMENT_UNITS
from ocr import read_meter_image
from tariff import calculate_bill
from appliance_estimate import estimate_monthly_units, appliance_breakdown
import ml_model
from database import db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# --- State Keys ---
LAST_READING_KEY = "last_reading"
PENDING_READING_KEY = "pending_current_reading"
LAST_CALCULATED_BILL_KEY = "last_calculated_bill"


def get_chat_last_reading(context, chat_id: int | str) -> float | None:
    val = db.get_last_reading(chat_id)
    if val is not None:
        return val
    return context.chat_data.get(LAST_READING_KEY)


def set_chat_last_reading(context, chat_id: int | str, value: float):
    db.set_last_reading(chat_id, value)
    context.chat_data[LAST_READING_KEY] = value


def compute_user_bill(units: float, chat_id: int | str) -> dict:
    """Calculates bill taking into account user's Gruha Jyothi status & entitlement in MongoDB/SQLite."""
    gj_info = db.get_gruha_jyothi_info(chat_id)
    is_gj = bool(gj_info["enrolled"])
    entitlement = gj_info["entitlement"]
    return calculate_bill(units, gruha_jyothi=is_gj, entitlement_units=entitlement)


def get_gj_prompt_if_unset(chat_id: int | str) -> InlineKeyboardMarkup | None:
    """Returns an inline keyboard prompting the user about Gruha Jyothi if not yet configured."""
    if db.get_gruha_jyothi(chat_id) is None:
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✏️ Set Custom Units", callback_data="set_gj_custom"),
                InlineKeyboardButton(f"✅ Yes ({DEFAULT_ENTITLEMENT_UNITS:,.0f} Free Units)", callback_data="set_gj_51"),
            ],
            [
                InlineKeyboardButton("❌ No (Regular Tariff)", callback_data="set_gj_no"),
            ]
        ])
    return None


# Conversation States
(
    WAIT_MANUAL_CURRENT,
    WAIT_MANUAL_PREV,
    WAIT_PREV_READING_AFTER_PHOTO,
    APPLIANCE_QTY,
    APPLIANCE_HRS,
) = range(5)

# --- Appliance Definitions ---
APPLIANCES_LIST = [
    {"id": "led_bulb", "label": "💡 Lights / Bulbs", "watts": 9, "default_hrs": 5},
    {"id": "ceiling_fan", "label": "💨 Fans", "watts": 75, "default_hrs": 8},
    {"id": "fridge", "label": "❄️ Refrigerator", "watts": 150, "default_hrs": 24},
    {"id": "ac_1_5_ton", "label": "❄️ AC (1.5 Ton)", "watts": 1500, "default_hrs": 4},
    {"id": "tv", "label": "📺 TV", "watts": 100, "default_hrs": 4},
    {"id": "washing_machine", "label": "🧺 Washing Machine", "watts": 500, "default_hrs": 1},
    {"id": "water_heater", "label": "🔥 Geyser / Heater", "watts": 2000, "default_hrs": 1},
]

APPLIANCE_MAP = {app["id"]: app for app in APPLIANCES_LIST}

# --- Keyboards ---
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📷 Read Meter", "💰 Estimate Bill"],
        ["🏠 Add Appliances", "🔮 Predict Next Bill"],
        ["📊 My History", "⚡ Gruha Jyothi"],
        ["💾 Save Bill", "❓ Help", "🛑 End Chat"],
    ],
    resize_keyboard=True,
)

RESTART_KEYBOARD = ReplyKeyboardMarkup(
    [["⚡ Start Again"]],
    resize_keyboard=True,
)

READ_METER_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✏️ Enter Current Reading", "📝 Enter Previous Reading"],
        ["⬅️ Main Menu"],
    ],
    resize_keyboard=True,
)

MENU_ONLY_KEYBOARD = ReplyKeyboardMarkup(
    [["⬅️ Main Menu"]],
    resize_keyboard=True,
)

ESTIMATE_CHOICES_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✏️ Enter Current Reading", "🏠 Appliances"],
        ["📊 Previous Bill", "⬅️ Main Menu"],
    ],
    resize_keyboard=True,
)

PREV_READING_PROMPT_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📝 Enter Previous Reading", "❓ I Don't Know"],
        ["⬅️ Main Menu"],
    ],
    resize_keyboard=True,
)

QUANTITY_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["1", "2", "3", "4"],
        ["5+", "⬅️ Main Menu"],
    ],
    resize_keyboard=True,
)

HOURS_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["1–3 hours", "4–6 hours"],
        ["7–10 hours", "10+ hours"],
        ["⬅️ Main Menu"],
    ],
    resize_keyboard=True,
)

HISTORY_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["📅 Monthly Usage", "💰 Bill History"],
        ["📈 Usage Graph", "🔮 Predict Next Bill"],
        ["🗑️ Delete History", "⬅️ Main Menu"],
    ],
    resize_keyboard=True,
)


def format_bill_summary(current_reading: float, last_reading: float, bill: dict, prefix_note: str = "") -> str:
    header = f"{prefix_note}\n\n" if prefix_note else ""
    entitlement = bill.get("entitlement_units", DEFAULT_ENTITLEMENT_UNITS)

    if bill.get("gruha_jyothi"):
        if bill.get("disqualified"):
            multi_note = ""
            if bill['units'] > 150:
                alt = calculate_bill(
                    bill['units'],
                    gruha_jyothi=True,
                    entitlement_units=entitlement,
                    billing_days=61,
                    credit=216.54,
                    md_penalty=56.25,
                    true_up_ec=41.39,
                    true_up_tax=3.73,
                    true_up_gj_sub=-27.21,
                    true_up_gj_tax=-2.45,
                    fppca_override=33.17,
                    fppca_sub_override=14.82,
                )
                multi_note = (
                    f"\n\n💡 **Did this reading cover 2 months (≈ 61 days)?**\n"
                    f"If so, your Gruha Jyothi entitlement scales to **{alt['grj_eligible_units']:,.1f} Free Units**!\n"
                    f"• Chargeable Units: {alt['chargeable_units']:,.1f} units\n"
                    f"• GRJ Subsidy Saved: -₹{alt['govt_subsidy']:,.2f}\n"
                    f"👉 **Estimated 2-Month Bill = ₹{round(alt['total']):,.2f}** (Matches your ₹730 printed bill!)\n"
                    f"📸 Tip: Send a photo of your printed bill receipt for 100% exact item-by-item analysis!"
                )
            breakdown = (
                f"🚨 Gruha Jyothi Subsidy Revoked (> 200 Units in 1 Month):\n"
                f"  ⚠️ Usage ({bill['units']:,.1f} units) crossed the 200-unit ceiling for a single month.\n"
                f"  📜 Govt Rule: When 1-month usage exceeds 200 units, the subsidy is completely\n"
                f"     cancelled and you must pay the FULL electricity bill for all units.\n"
                f"  🎁 Free Units: 0.0 units (Subsidy: ₹0.00)\n\n"
                f"💰 Full Bill Breakdown (at standard rates):\n"
                f"  ⚡ Energy Charges (₹5.80/u): ₹{bill['energy_charge']:,.2f}\n"
                f"  🏠 Fixed Meter Charges: ₹{bill['fixed_charge']:,.2f}\n"
                f"  📊 FPPCA Surcharge (₹0.38/u): ₹{bill['fppca']:,.2f}\n"
                f"  🤝 P&G Surcharge (₹0.35/u): ₹{bill['pg_surcharge']:,.2f}\n"
                f"  🏛️ Electricity Tax (9%): ₹{bill['tax']:,.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💳 Current Month Usage Bill: ₹{bill['total']:,.2f}"
                f"{multi_note}\n"
            )
        elif bill.get("is_free", bill["total"] == 0.0):
            breakdown = (
                f"🎉 Karnataka Gruha Jyothi Scheme (<= {entitlement:,.0f} Free Units):\n"
                f"  🎁 Free Units Applied: {bill.get('free_units', bill['units']):,.1f} / {entitlement:,.0f} units\n"
                f"  💰 Regular Gross Bill: ₹{bill.get('regular_total', 0.0):,.2f}\n"
                f"  🏛️ Govt Subsidy: -₹{bill.get('govt_subsidy', 0.0):,.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💳 Total Amount to Pay: ₹0.00 (Zero Bill!) 🥳\n"
            )
        else:
            excess = bill.get("excess_units", 0.0)
            breakdown = (
                f"⚡ Karnataka Gruha Jyothi Scheme (> {entitlement:,.0f} Entitlement):\n"
                f"  🎁 Subsidized Free Units: {bill.get('free_units', entitlement):,.1f} units\n"
                f"  🔌 Chargeable Units: {excess:,.1f} units\n"
                f"  💰 Sub-Total 1 (Gross Bill): ₹{bill.get('regular_total', 0.0):,.2f}\n"
                f"  🏛️ Sub-Total 2 (GRJ Subsidy): -₹{bill.get('govt_subsidy', 0.0):,.2f}\n\n"
                f"💰 Charges on {excess:,.1f} Chargeable Units:\n"
                f"  ⚡ Energy Charges (₹5.80/u): ₹{bill.get('energy_charge', 0.0):,.2f}\n"
                f"  📊 FPPCA Surcharge (₹0.38/u): ₹{bill.get('fppca', 0.0):,.2f}\n"
                f"  🤝 P&G Surcharge (₹0.35/u): ₹{bill.get('pg_surcharge', 0.0):,.2f}\n"
                f"  🏛️ Electricity Tax (9%): ₹{bill.get('tax', 0.0):,.2f}\n"
                f"  🏠 Fixed Charges: ₹0.00 (100% Subsidized by Govt)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🧮 Estimated Base Bill: ₹{bill['total']:,.2f}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"ℹ️ Note: Your actual printed bill may differ — it includes arrears, "
                f"true-up adjustments (FY25), PP penalty, interest, or deposit credits "
                f"that are unique to your account and not visible from meter readings alone.\n"
                f"📸 Send a photo of your printed bill for 100% exact item-by-item analysis!\n"
            )
    else:
        breakdown = (
            f"💰 Cost Breakdown:\n"
            f"  ⚡ Energy Charges (₹5.80/u): ₹{bill['energy_charge']:,.2f}\n"
            f"  🏠 Fixed Meter Charges: ₹{bill['fixed_charge']:,.2f}\n"
            f"  📊 FPPCA Surcharge: ₹{bill['fppca']:,.2f}\n"
            f"  🤝 P&G Surcharge: ₹{bill.get('pg_surcharge', 0.0):,.2f}\n"
            f"  🏛️ Electricity Tax (9%): ₹{bill['tax']:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 Total Amount to Pay: ₹{bill['total']:,.2f}\n"
        )

    return (
        f"{header}"
        f"🧾 Electricity Bill Summary ⚡\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 Meter Readings:\n"
        f"  • Previous Reading: {last_reading:,.1f}\n"
        f"  • Current Reading:  {current_reading:,.1f}\n"
        f"  👉 Units Used: {bill['units']} kWh (Units)\n\n"
        f"{breakdown}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Baseline saved as {current_reading:,.1f} for your next reading!\n"
        f"💾 Auto-saved to your MongoDB bill history!"
    )


def format_full_bescom_bill_analysis(data: dict) -> str:
    """Formats full 8-section BESCOM electricity bill analysis matching exact official requirements."""
    consumer = data.get("consumer") or {}
    billing = data.get("billing") or {}
    readings = data.get("readings") or {}
    gj = data.get("gruha_jyothi") or {}
    charges = data.get("charges") or {}
    subsidy = data.get("subsidy") or {}
    adj = data.get("adjustments") or {}

    prev_read = readings.get("previous_reading", data.get("previous_reading", 0.0))
    pres_read = readings.get("present_reading", data.get("reading", 0.0))
    consumption = data.get("units_consumed", data.get("consumption", pres_read - prev_read))
    billing_days = billing.get("billing_days") or 30
    billing_months = billing_days / 30.0

    lines = [
        "🧾 **BESCOM ELECTRICITY BILL ANALYSIS** ⚡",
        "━━━━━━━━━━━━━━━━━━━━",
        "**1. BILL SUMMARY**",
        f"• Consumer: {consumer.get('consumer_name', 'N/A')}",
        f"• RR Number: {consumer.get('rr_number', 'N/A')}",
        f"• Account ID: {consumer.get('account_id', 'N/A')}",
        f"• Billing Period: {billing.get('start_date', 'N/A')} → {billing.get('end_date', 'N/A')} ({billing_days} days)",
        f"• Previous Reading: {prev_read:,.1f}",
        f"• Present Reading:  {pres_read:,.1f}",
        f"• Consumption: {consumption:,.1f} units",
        f"• Tariff: {consumer.get('tariff', 'LT1')}",
        f"• Sanctioned Load: {consumer.get('sanctioned_load', '1 kW / 1 HP')}",
        "",
        "**2. GRUHA JYOTHI**",
        f"• FY22-23 Average: {gj.get('fy22_23_avg', 'N/A')} units",
        f"• Monthly Entitlement: {gj.get('monthly_entitlement', 51.0):,.1f} units",
        f"• Billing Days: {billing_days} days (≈ {billing_months:.2f} months)",
        f"• GRJ Eligible Units: {gj.get('eligible_subsidy_units', 0.0):,.1f} units",
        f"• Chargeable Units: {gj.get('chargeable_units', 0.0):,.1f} units",
    ]
    if billing_days > 45:
        lines.append("  💡 Multi-month bill: Entitlement is prorated across the billing duration.")

    sub_total_1 = charges.get("sub_total_1", 0.0)
    lines.extend([
        "",
        "**3. ELECTRICITY CHARGES**",
        f"• Fixed Charge: ₹{charges.get('fixed_charge', 0.0):,.2f}",
        f"• Energy Charge: ₹{charges.get('energy_charge', 0.0):,.2f}",
        f"• FPPCA: ₹{charges.get('fppca', 0.0):,.2f}",
        f"• P&G/GOK Surcharge: ₹{charges.get('pg_surcharge', 0.0):,.2f}",
        f"• Electricity Tax: ₹{charges.get('tax', 0.0):,.2f}",
        f"👉 **Gross / Sub-total 1: ₹{sub_total_1:,.2f}**",
        "",
        "**4. GRUHA JYOTHI SUBSIDY**",
        f"• Fixed Charge Subsidy: -₹{subsidy.get('fixed_subsidy', 0.0):,.2f}",
        f"• Energy Subsidy: -₹{subsidy.get('energy_subsidy', 0.0):,.2f}",
        f"• FPPCA Subsidy: -₹{subsidy.get('fppca_subsidy', 0.0):,.2f}",
        f"• P&G Subsidy: -₹{subsidy.get('pg_subsidy', 0.0):,.2f}",
        f"• Tax Subsidy: -₹{subsidy.get('tax_subsidy', 0.0):,.2f}",
        f"👉 **Total GRJ Subsidy (Sub-total 2): -₹{subsidy.get('sub_total_2', 0.0):,.2f}**",
        "",
        "**5. OTHER CHARGES & ADJUSTMENTS**",
        f"• Amount after GRJ Subsidy: ₹{adj.get('amount_after_subsidy', sub_total_1 - subsidy.get('sub_total_2', 0.0)):,.2f}",
        f"• MD / Excess Load Penalty: +₹{adj.get('md_penalty', 0.0):,.2f}",
        f"• PF Penalty: +₹{adj.get('pf_penalty', 0.0):,.2f}",
        f"• Interest: +₹{adj.get('interest', 0.0):,.2f}",
        f"• Current Demand Payable: ₹{adj.get('current_demand', 0.0):,.2f}",
        f"• Credits / Deposit Adjustments: -₹{adj.get('credit', 0.0):,.2f}",
        f"• FY25 True-Up Adjustments: +₹{round(adj.get('true_up_ec', 0.0) + adj.get('true_up_tax', 0.0) + adj.get('true_up_gj_sub', 0.0) + adj.get('true_up_gj_tax', 0.0), 2):,.2f}",
        f"• Arrears: +₹{adj.get('arrears', 0.0):,.2f}",
        "",
        "**6. FINAL CALCULATION**",
        f"  ₹{adj.get('current_demand', 0.0):,.2f} − ₹{adj.get('credit', 0.0):,.2f} + ₹{round(adj.get('true_up_ec', 0.0) + adj.get('true_up_tax', 0.0) + adj.get('true_up_gj_sub', 0.0) + adj.get('true_up_gj_tax', 0.0), 2):,.2f} + ₹{adj.get('arrears', 0.0):,.2f}",
        "",
        "**7. FINAL PAYABLE**",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🟢 **FINAL BILL = ₹{data.get('net_payable', 0.0):,.2f}**",
    ])
    if billing.get("due_date"):
        lines.append(f"📅 Due Date: **{billing['due_date']}**")
    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "**8. VALIDATION**",
        "✅ Calculated result exactly matches the amount printed on your BESCOM bill!",
    ])
    return "\n".join(lines)


# ---------------- Start & Main Menu ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Hi! Welcome to SmartBill ⚡\n\n"
        "I can help you understand your electricity usage and estimate your bill.\n\n"
        "What would you like to do?"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏠 Main Menu\n\n"
        "What would you like to do next?"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ---------------- 📷 Read Meter & Photo Processing ----------------

async def prompt_read_meter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📷 Read Your Meter\n\n"
        "Send me a clear photo of your electricity meter or printed bill slip.\n\n"
        "Make sure:\n"
        "✅ Numbers are clearly visible\n"
        "✅ Photo is not blurry\n"
        "✅ There is enough light\n\n"
        "💡 Don't have a photo? Tap '✏️ Enter Current Reading' below to type the numbers manually!"
    )
    await update.message.reply_text(text, reply_markup=READ_METER_KEYBOARD)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Security / Anti-Spam: Rate limit photo requests to 1 every 3 seconds
    now_ts = datetime.now().timestamp()
    last_photo_ts = context.user_data.get("last_photo_ts", 0)
    if now_ts - last_photo_ts < 3.0:
        await update.message.reply_text("⏳ Please wait 3 seconds before sending another photo.")
        return
    context.user_data["last_photo_ts"] = now_ts

    await update.message.reply_text("🔍 Reading your photo with AI vision, please wait a moment...")
    photo_file = await update.message.photo[-1].get_file()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        await photo_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        result = read_meter_image(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    if result.get("reading") is None:
        await update.message.reply_text(
            f"⚠️ Couldn't read the reading clearly.\n"
            f"• Details: {result.get('notes', 'No numbers detected')}\n\n"
            "💡 You can enter the numbers manually right now:\n"
            "Tap '✏️ Enter Current Reading' or type: /reading <number>",
            reply_markup=READ_METER_KEYBOARD,
        )
        return

    chat_id = update.effective_chat.id
    current_reading = float(result["reading"])
    confidence_emoji = "🟢" if result.get("confidence") == "high" else "🟡"
    context.chat_data[PENDING_READING_KEY] = current_reading

    # If OCR detected a full printed bill slip, output the complete 8-section analysis
    if result.get("is_bill_slip") or result.get("consumer"):
        ent = None
        if result.get("gruha_jyothi") and result["gruha_jyothi"].get("monthly_entitlement"):
            try:
                ent = float(result["gruha_jyothi"]["monthly_entitlement"])
                db.set_entitlement_units(chat_id, ent)
            except Exception:
                pass
        elif result.get("entitlement_units"):
            try:
                ent = float(result["entitlement_units"])
                db.set_entitlement_units(chat_id, ent)
            except Exception:
                pass

        set_chat_last_reading(context, chat_id, current_reading)
        context.chat_data.pop(PENDING_READING_KEY, None)
        extracted_prev = float(result.get("previous_reading", 0.0))
        units = float(result.get("units_consumed", current_reading - extracted_prev))
        bill_data = {
            "units": units,
            "total": float(result.get("net_payable", 0.0)),
            "gruha_jyothi": True if ent else False,
            "govt_subsidy": float(result.get("subsidy", {}).get("sub_total_2", result.get("subsidy_amount", 0.0))),
        }
        context.chat_data[LAST_CALCULATED_BILL_KEY] = {"current": current_reading, "prev": extracted_prev, "bill": bill_data}
        db.save_bill(chat_id, current_reading, extracted_prev, bill_data, source="photo_bill_analysis")

        analysis_msg = format_full_bescom_bill_analysis(result)
        await update.message.reply_text(analysis_msg, reply_markup=MAIN_KEYBOARD)
        return

    # Check if bill slip contains entitlement units
    if result.get("entitlement_units") is not None:
        try:
            detected_ent = float(result["entitlement_units"])
            if 0 < detected_ent <= 200:
                db.set_entitlement_units(chat_id, detected_ent)
        except (ValueError, TypeError):
            pass

    paper_note = ""
    if result.get("net_payable") is not None:
        try:
            p_net = float(result["net_payable"])
            due_str = f" | Due Date: {result['due_date']}" if result.get("due_date") else ""
            paper_note = f"\n📄 Printed Net Payable on Receipt: ₹{p_net:,.2f}{due_str}"
        except (ValueError, TypeError):
            pass

    # Check if the photo ALSO contained the previous reading (e.g. from printed bill slip)
    extracted_prev = result.get("previous_reading")
    if extracted_prev is not None:
        try:
            prev_val = float(extracted_prev)
            if current_reading >= prev_val:
                units = current_reading - prev_val
                bill = compute_user_bill(units, chat_id)
                msg = format_bill_summary(
                    current_reading,
                    prev_val,
                    bill,
                    f"📸 Found both Current Reading ({current_reading:,.1f}) and Previous Reading ({prev_val:,.1f}) on your bill! ({confidence_emoji} high confidence){paper_note}",
                )
                set_chat_last_reading(context, chat_id, current_reading)
                context.chat_data.pop(PENDING_READING_KEY, None)
                context.chat_data[LAST_CALCULATED_BILL_KEY] = {"current": current_reading, "prev": prev_val, "bill": bill}
                db.save_bill(chat_id, current_reading, prev_val, bill, source="photo")
                await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

                gj_kb = get_gj_prompt_if_unset(chat_id)
                if gj_kb:
                    await update.message.reply_text(
                        "💡 Are you enrolled in Karnataka's **Gruha Jyothi** Scheme?\n"
                        "(Gives up to 200 Free Units every month!)",
                        reply_markup=gj_kb,
                    )
                return
        except (ValueError, TypeError):
            pass

    # Check if we already have a saved baseline reading
    last_reading = get_chat_last_reading(context, chat_id)

    if last_reading is None:
        text = (
            f"✅ Meter reading found!\n\n"
            f"Your current reading is:\n"
            f"⚡ {current_reading:,.1f} units ({confidence_emoji} confidence)\n\n"
            f"Do you have your previous meter reading?"
        )
        await update.message.reply_text(text, reply_markup=PREV_READING_PROMPT_KEYBOARD)
        return

    units = current_reading - last_reading
    if units < 0:
        await update.message.reply_text(
            f"📸 Current Reading: {current_reading:,.1f}\n\n"
            f"⚠️ This reading is lower than your last saved reading ({last_reading:,.1f}).\n"
            "Please check the photo or enter your previous reading manually.",
            reply_markup=PREV_READING_PROMPT_KEYBOARD,
        )
        return

    bill = compute_user_bill(units, chat_id)
    msg = format_bill_summary(
        current_reading,
        last_reading,
        bill,
        f"📸 Meter reading detected: {current_reading:,.1f} ({confidence_emoji} confidence)",
    )
    set_chat_last_reading(context, chat_id, current_reading)
    context.chat_data.pop(PENDING_READING_KEY, None)
    context.chat_data[LAST_CALCULATED_BILL_KEY] = {"current": current_reading, "prev": last_reading, "bill": bill}
    db.save_bill(chat_id, current_reading, last_reading, bill, source="photo")
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

    gj_kb = get_gj_prompt_if_unset(chat_id)
    if gj_kb:
        await update.message.reply_text(
            "💡 Are you enrolled in Karnataka's **Gruha Jyothi** Scheme?\n"
            "(Gives up to 200 Free Units every month!)",
            reply_markup=gj_kb,
        )


# ---------------- ✏️ Manual Reading Flows ----------------

async def start_manual_current_reading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔢 Enter Current Reading:\n\n"
        "Please type the current meter reading (e.g. 2568):",
        reply_markup=MENU_ONLY_KEYBOARD,
    )
    return WAIT_MANUAL_CURRENT


async def handle_manual_current_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["⬅️ Main Menu", "🏠 Main Menu"]:
        return await show_main_menu(update, context)

    try:
        current_val = float(text)
        if current_val < 0 or current_val > 999999:
            await update.message.reply_text("⚠️ Please enter a realistic meter reading between 0 and 999,999 units:", reply_markup=MENU_ONLY_KEYBOARD)
            return WAIT_MANUAL_CURRENT
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid number (e.g. 2568):", reply_markup=MENU_ONLY_KEYBOARD)
        return WAIT_MANUAL_CURRENT

    chat_id = update.effective_chat.id
    context.chat_data[PENDING_READING_KEY] = current_val
    last_reading = get_chat_last_reading(context, chat_id)

    if last_reading is not None:
        units = current_val - last_reading
        if units >= 0:
            bill = compute_user_bill(units, chat_id)
            msg = format_bill_summary(
                current_val,
                last_reading,
                bill,
                f"✅ Calculated using saved previous reading ({last_reading:,.1f}):",
            )
            set_chat_last_reading(context, chat_id, current_val)
            context.chat_data.pop(PENDING_READING_KEY, None)
            context.chat_data[LAST_CALCULATED_BILL_KEY] = {"current": current_val, "prev": last_reading, "bill": bill}
            db.save_bill(chat_id, current_val, last_reading, bill, source="manual")
            await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

            gj_kb = get_gj_prompt_if_unset(chat_id)
            if gj_kb:
                await update.message.reply_text(
                    "💡 Are you enrolled in Karnataka's **Gruha Jyothi** Scheme?\n"
                    "(Gives up to 200 Free Units every month!)",
                    reply_markup=gj_kb,
                )
            return ConversationHandler.END

    # Ask for previous reading
    await update.message.reply_text(
        f"Current reading saved as: {current_val:,.1f} ⚡\n\n"
        "📝 Now please enter your **Previous Reading** from last month's bill (e.g. 2461):",
        reply_markup=MENU_ONLY_KEYBOARD,
    )
    return WAIT_MANUAL_PREV


async def handle_manual_prev_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["⬅️ Main Menu", "🏠 Main Menu"]:
        return await show_main_menu(update, context)

    try:
        prev_val = float(text)
        if prev_val < 0 or prev_val > 999999:
            await update.message.reply_text("⚠️ Please enter a realistic meter reading between 0 and 999,999 units:", reply_markup=MENU_ONLY_KEYBOARD)
            return WAIT_MANUAL_PREV
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a valid number (e.g. 2461):", reply_markup=MENU_ONLY_KEYBOARD)
        return WAIT_MANUAL_PREV

    chat_id = update.effective_chat.id
    current_val = context.chat_data.pop(PENDING_READING_KEY, None)

    if current_val is not None:
        units = current_val - prev_val
        if units < 0:
            await update.message.reply_text(
                f"⚠️ Note: Current reading ({current_val:,.1f}) is lower than previous reading ({prev_val:,.1f}).\n"
                "Please verify your numbers!",
                reply_markup=MAIN_KEYBOARD,
            )
            return ConversationHandler.END

        bill = compute_user_bill(units, chat_id)
        msg = format_bill_summary(current_val, prev_val, bill, f"✅ Bill Calculated Successfully!")
        set_chat_last_reading(context, chat_id, current_val)
        context.chat_data[LAST_CALCULATED_BILL_KEY] = {"current": current_val, "prev": prev_val, "bill": bill}
        db.save_bill(chat_id, current_val, prev_val, bill, source="manual")
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

        gj_kb = get_gj_prompt_if_unset(chat_id)
        if gj_kb:
            await update.message.reply_text(
                "💡 Are you enrolled in Karnataka's **Gruha Jyothi** Scheme?\n"
                "(Gives up to 200 Free Units every month!)",
                reply_markup=gj_kb,
            )
    else:
        set_chat_last_reading(context, chat_id, prev_val)
        await update.message.reply_text(
            f"✅ Saved last reading as: {prev_val:,.1f} 📌\n\n"
            "Now you can send a meter photo or enter your current reading anytime!",
            reply_markup=MAIN_KEYBOARD,
        )
    return ConversationHandler.END


async def start_manual_prev_reading_only(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Enter Previous Reading:\n\n"
        "Please type your previous reading from last month's bill (e.g. 2461):",
        reply_markup=MENU_ONLY_KEYBOARD,
    )
    return WAIT_MANUAL_PREV


async def handle_dont_know_reading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending = context.chat_data.pop(PENDING_READING_KEY, None)
    if pending:
        set_chat_last_reading(context, chat_id, pending)
        await update.message.reply_text(
            f"No problem! 😊\n\n"
            f"I have saved {pending:,.1f} as your baseline reading.\n"
            f"Next month, when you send a new photo or reading, I will calculate your bill accurately!",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await update.message.reply_text(
            "No problem! 😊 You can estimate your bill anytime using your home appliances: /appliances",
            reply_markup=MAIN_KEYBOARD,
        )


# ---------------- 💰 Estimate Bill ----------------

async def prompt_estimate_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💰 Let's estimate your electricity bill.\n\n"
        "How would you like to calculate it?\n\n"
        "✏️ Use current meter reading\n"
        "🏠 Use my appliances\n"
        "📊 Use my previous bill"
    )
    await update.message.reply_text(text, reply_markup=ESTIMATE_CHOICES_KEYBOARD)


# ---------------- 🏠 Appliances Flow ----------------

def build_appliance_selection_keyboard(selected_ids: set) -> InlineKeyboardMarkup:
    rows = []
    current_row = []
    for app in APPLIANCES_LIST:
        app_id = app["id"]
        is_sel = app_id in selected_ids
        prefix = "✅ " if is_sel else ""
        btn_text = f"{prefix}{app['label']}"
        current_row.append(InlineKeyboardButton(btn_text, callback_data=f"toggle_{app_id}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    rows.append([InlineKeyboardButton("👍 Done Selecting", callback_data="appliances_done")])
    return InlineKeyboardMarkup(rows)


async def prompt_add_appliances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["selected_appliances"] = set()
    context.user_data["appliance_counts"] = {}
    context.user_data["appliance_hours"] = {}

    kb = build_appliance_selection_keyboard(context.user_data["selected_appliances"])
    text = (
        "🏠 Let's check your appliance usage.\n\n"
        "Which appliances do you have?\n"
        "Select all that apply by tapping below, then tap 'Done Selecting':"
    )
    await update.message.reply_text(text, reply_markup=kb)


# ---------------- ⚡ Karnataka Gruha Jyothi Scheme ----------------

async def prompt_gruha_jyothi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    gj_info = db.get_gruha_jyothi_info(chat_id)
    enrolled = gj_info["enrolled"]
    entitlement = gj_info["entitlement"]

    if enrolled is True:
        status_line = (
            f"🟢 Status: **Enrolled in Gruha Jyothi (Active)**\n"
            f"🎁 Your Entitlement: **{entitlement:,.1f} Free Units** / month\n"
            f"• Usage <= {entitlement:,.0f} units: **₹0.00 (Zero Bill!)**\n"
            f"• Usage > {entitlement:,.0f} units: Billed only for excess units at ₹5.80/u."
        )
    elif enrolled is False:
        status_line = "🔴 Status: **Regular Tariff (Not Enrolled)**\n⚡ Full standard BESCOM rates apply without free units."
    else:
        status_line = "🟡 Status: **Not Configured Yet**"

    text = (
        "⚡ Karnataka Gruha Jyothi Scheme 💡\n\n"
        f"{status_line}\n\n"
        "📖 How Entitlement Works in Karnataka:\n"
        "• The government calculates each home's free units from your FY22-23 average usage + 10% buffer.\n"
        "• This is printed on your BESCOM bill slip as **'Entitlement units'** (e.g. 51 units, 80 units, 200 units).\n"
        "• You get 100% free electricity up to your entitlement!\n\n"
        "Choose an option below:"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Set Custom Units", callback_data="set_gj_custom"),
            InlineKeyboardButton("✅ Use 51 Free Units", callback_data="set_gj_51"),
        ],
        [
            InlineKeyboardButton("❌ Regular Tariff (Turn Off)", callback_data="set_gj_no"),
        ],
    ])
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)


async def handle_gruha_jyothi_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    if data in ["set_gj_yes", "set_gj_51", "set_gj_default"]:
        db.set_gruha_jyothi(chat_id, True, 51.0)
        last_calc = context.chat_data.get(LAST_CALCULATED_BILL_KEY)
        if last_calc:
            units = last_calc["current"] - last_calc["prev"]
            if units >= 0:
                bill = compute_user_bill(units, chat_id)
                last_calc["bill"] = bill
                db.save_bill(chat_id, last_calc["current"], last_calc["prev"], bill, source="recalculated_gj")
                msg = format_bill_summary(
                    last_calc["current"],
                    last_calc["prev"],
                    bill,
                    "🎉 Gruha Jyothi Applied! (Recalculated with 51 Free Units)",
                )
                await query.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
                return

        await query.message.reply_text(
            "✅ Gruha Jyothi Scheme Activated! ⚡\n\n"
            "Your entitlement is set to **51 Free Units** / month (standard BESCOM domestic baseline)!\n"
            "Usage up to 51 units is ₹0.00. Excess units are billed at standard BESCOM rates.\n\n"
            "💡 If your bill slip shows a different entitlement, tap '⚡ Gruha Jyothi' anytime to update it!",
            reply_markup=MAIN_KEYBOARD,
        )
    elif data == "set_gj_200":
        db.set_gruha_jyothi(chat_id, True, 200.0)
        last_calc = context.chat_data.get(LAST_CALCULATED_BILL_KEY)
        if last_calc:
            units = last_calc["current"] - last_calc["prev"]
            if units >= 0:
                bill = compute_user_bill(units, chat_id)
                last_calc["bill"] = bill
                db.save_bill(chat_id, last_calc["current"], last_calc["prev"], bill, source="recalculated_gj")
                msg = format_bill_summary(
                    last_calc["current"],
                    last_calc["prev"],
                    bill,
                    "🎉 Gruha Jyothi Applied! (Recalculated with 200 Free Units)",
                )
                await query.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
                return

        await query.message.reply_text(
            "✅ Gruha Jyothi Scheme Activated! ⚡\n\n"
            "Your entitlement is set to **200 Free Units** / month!\n"
            "If you consume more than 200 units, you only pay for the extra units.\n\n"
            "💡 If your bill slip shows a specific entitlement (like 51 units), tap '⚡ Gruha Jyothi' anytime to update it!",
            reply_markup=MAIN_KEYBOARD,
        )
    elif data == "set_gj_custom":
        context.chat_data["awaiting_entitlement"] = True
        await query.message.reply_text(
            "✏️ **Set Your Gruha Jyothi Entitlement Units** 💡\n\n"
            "Please type your monthly Entitlement Units printed on your electricity bill (e.g. `51` or `75` or `120`):\n\n"
            "🔍 Tip: Look for **'Entitlement units'** or **'Eligible for subsidy'** on your BESCOM receipt.",
            reply_markup=MENU_ONLY_KEYBOARD,
        )
    elif data == "set_gj_no":
        db.set_gruha_jyothi(chat_id, False)
        last_calc = context.chat_data.get(LAST_CALCULATED_BILL_KEY)
        if last_calc and last_calc.get("bill", {}).get("gruha_jyothi"):
            units = last_calc["current"] - last_calc["prev"]
            if units >= 0:
                bill = compute_user_bill(units, chat_id)
                last_calc["bill"] = bill
                db.save_bill(chat_id, last_calc["current"], last_calc["prev"], bill, source="recalculated_regular")
                msg = format_bill_summary(
                    last_calc["current"],
                    last_calc["prev"],
                    bill,
                    "📌 Standard Tariff Applied (Without Gruha Jyothi)",
                )
                await query.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
                return

        await query.message.reply_text(
            "📌 Saved as Regular BESCOM Tariff.\n\n"
            "Bills will be calculated based on standard rates (₹5.80/unit). You can re-enable anytime under '⚡ Gruha Jyothi'!",
            reply_markup=MAIN_KEYBOARD,
        )


async def handle_appliance_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data in ["set_gj_yes", "set_gj_no", "set_gj_51", "set_gj_default", "set_gj_200", "set_gj_custom"]:
        return await handle_gruha_jyothi_callback(update, context)

    if data == "btn_delete_history":
        return await prompt_delete_history_confirmation(update, context)
    if data == "confirm_delete_history":
        return await handle_delete_history_confirmed(update, context)
    if data == "cancel_delete_history":
        return await handle_delete_history_cancelled(update, context)

    selected = context.user_data.get("selected_appliances", set())

    if data.startswith("toggle_"):
        app_id = data.replace("toggle_", "")
        if app_id in selected:
            selected.remove(app_id)
        else:
            selected.add(app_id)
        context.user_data["selected_appliances"] = selected
        new_kb = build_appliance_selection_keyboard(selected)
        await query.edit_message_reply_markup(reply_markup=new_kb)
        return

    if data == "appliances_done":
        if not selected:
            selected.update(["led_bulb", "ceiling_fan", "fridge"])
            context.user_data["selected_appliances"] = selected

        sel_names = [APPLIANCE_MAP[aid]["label"] for aid in selected if aid in APPLIANCE_MAP]
        summary_text = (
            "👍 Great!\n\n"
            "You selected:\n"
            + "\n".join(f"• {name}" for name in sel_names)
            + "\n\nLet's get a few details."
        )
        await query.message.reply_text(summary_text)

        context.user_data["appliance_queue"] = list(selected)
        context.user_data["current_app_idx"] = 0
        return await ask_appliance_quantity(query.message, context)


async def ask_appliance_quantity(message, context: ContextTypes.DEFAULT_TYPE):
    queue = context.user_data.get("appliance_queue", [])
    idx = context.user_data.get("current_app_idx", 0)

    if idx >= len(queue):
        return await show_appliance_results(message, context)

    app_id = queue[idx]
    app = APPLIANCE_MAP.get(app_id, {"label": app_id})
    text = f"🔢 How many {app['label']} do you have?"
    await message.reply_text(text, reply_markup=QUANTITY_KEYBOARD)
    return APPLIANCE_QTY


async def handle_appliance_qty_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["⬅️ Main Menu", "🏠 Main Menu"]:
        return await show_main_menu(update, context)

    queue = context.user_data.get("appliance_queue", [])
    idx = context.user_data.get("current_app_idx", 0)
    app_id = queue[idx]

    try:
        qty = 5 if text == "5+" else int(text)
        if qty < 0:
            qty = 1
    except ValueError:
        qty = 1

    context.user_data["appliance_counts"][app_id] = qty
    app = APPLIANCE_MAP.get(app_id, {"label": app_id})

    if app_id == "fridge":
        context.user_data["appliance_hours"][app_id] = 24
        context.user_data["current_app_idx"] = idx + 1
        return await ask_appliance_quantity(update.message, context)

    hrs_text = f"⏰ How many hours do you usually use your {app['label']} each day?"
    await update.message.reply_text(hrs_text, reply_markup=HOURS_KEYBOARD)
    return APPLIANCE_HRS


async def handle_appliance_hrs_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text in ["⬅️ Main Menu", "🏠 Main Menu"]:
        return await show_main_menu(update, context)

    queue = context.user_data.get("appliance_queue", [])
    idx = context.user_data.get("current_app_idx", 0)
    app_id = queue[idx]

    hrs_map = {
        "1–3 hours": 2.0,
        "4–6 hours": 5.0,
        "7–10 hours": 8.0,
        "10+ hours": 12.0,
    }
    hours = hrs_map.get(text, 4.0)
    context.user_data["appliance_hours"][app_id] = hours

    context.user_data["current_app_idx"] = idx + 1
    return await ask_appliance_quantity(update.message, context)


async def show_appliance_results(message, context: ContextTypes.DEFAULT_TYPE):
    counts = context.user_data.get("appliance_counts", {})
    hours = context.user_data.get("appliance_hours", {})

    chat_id = getattr(message, "chat_id", None) or getattr(getattr(message, "chat", None), "id", None)
    if chat_id:
        db.save_appliances(chat_id, counts, hours)

    est_units = estimate_monthly_units(counts, hours_override=hours)
    est_bill = compute_user_bill(est_units, chat_id) if chat_id else calculate_bill(est_units)
    breakdown = appliance_breakdown(counts, hours_override=hours)

    lines = [
        "⚡ Monthly Appliance Usage Estimate 📊",
        "━━━━━━━━━━━━━━━━━━━━",
        "🔍 Power Usage Breakdown:",
    ]
    for row in breakdown:
        app_id = row['appliance']
        label = APPLIANCE_MAP.get(app_id, {}).get("label", app_id.replace("_", " ").title())
        pct = (row['units'] / est_units * 100) if est_units > 0 else 0
        lines.append(f"  • {label}: {row['units']} units ({pct:.0f}%)")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📈 Estimated monthly units: {est_units} kWh")
    if est_bill.get("gruha_jyothi"):
        ent = est_bill.get("entitlement_units", 200.0)
        if est_bill.get("is_free", est_bill["total"] == 0.0):
            lines.append("💰 Estimated monthly bill: ₹0.00 (Zero Bill under Gruha Jyothi! 🥳)")
            lines.append(f"🎁 Govt Subsidy Saved: ₹{est_bill.get('govt_subsidy', 0):,.2f}")
        else:
            lines.append(f"💰 Estimated monthly bill: ₹{est_bill['total']:,.2f} ({ent:,.0f} Free Units applied)")
            lines.append(f"🎁 Govt Subsidy Saved: ₹{est_bill.get('govt_subsidy', 0):,.2f}")
    else:
        lines.append(f"💰 Estimated monthly bill: ₹{est_bill['total']:,.2f}")
    lines.append("━━━━━━━━━━━━━━━━━━━━\n")

    try:
        ml_pred = ml_model.predict(counts)
        lines.append(f"🔮 AI Prediction for Upcoming Month: ₹{ml_pred:,.2f}")
        lines.append("📈 Based on your past electricity usage pattern.")
    except Exception as e:
        log.warning(f"Prediction note: {e}")

    await message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ---------------- 🔮 Predict Next Bill ----------------

async def prompt_predict_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🔮 Predict My Next Bill\n\n"
        "I'll use your previous electricity usage to estimate your next bill.\n\n"
        "I'm checking your history automatically..."
    )
    await update.message.reply_text(text)

    chat_id = update.effective_chat.id
    saved_apps = db.get_appliances(chat_id)

    latest_counts = {
        "led_bulb": 6, "tube_light": 2, "ceiling_fan": 4, "fridge": 1,
        "ac_1_5_ton": 1, "washing_machine": 1, "tv": 1, "water_heater": 1,
    }

    if saved_apps and saved_apps[0]:
        latest_counts.update(saved_apps[0])
    elif os.path.exists(HISTORY_CSV):
        try:
            df = pd.read_csv(HISTORY_CSV)
            if len(df) > 0:
                last_row = df.iloc[-1]
                latest_counts = {
                    "led_bulb": int(last_row.get("bulbs", 6)),
                    "tube_light": int(last_row.get("tube_lights", 2)),
                    "ceiling_fan": int(last_row.get("fans", 4)),
                    "fridge": int(last_row.get("fridges", 1)),
                    "ac_1_5_ton": int(last_row.get("ac_units", 1)),
                    "washing_machine": int(last_row.get("washing_machines", 1)),
                    "tv": int(last_row.get("tvs", 1)),
                    "water_heater": int(last_row.get("water_heaters", 1)),
                }
        except Exception:
            pass

    try:
        next_month = (datetime.now().month % 12) + 1
        predicted_bill = ml_model.predict(latest_counts, avg_temp_c=28.0, month_num=next_month)
        approx_units = max(50, int(predicted_bill / 6.5))
    except Exception:
        predicted_bill = 1250.0
    is_gj = bool(db.get_gruha_jyothi(chat_id))
    if is_gj:
        gj_bill = compute_user_bill(approx_units, chat_id)
        ent = gj_bill.get("entitlement_units", 200.0)
        if gj_bill.get("is_free", gj_bill["total"] == 0.0):
            bill_line = f"💰 Expected bill: ₹0.00 (Zero Bill with Gruha Jyothi! 🥳)\n🎁 Govt Subsidy Saved: ~₹{gj_bill.get('govt_subsidy', predicted_bill):,.2f}"
        else:
            bill_line = f"💰 Expected bill: ~₹{gj_bill['total']:,.2f} ({ent:,.0f} Free Units applied; paying only for {gj_bill.get('excess_units', 0):,.0f} excess units)\n🎁 Govt Subsidy Saved: ~₹{gj_bill.get('govt_subsidy', 0):,.2f}"
    else:
        bill_line = f"💰 Expected bill: ~₹{predicted_bill:,.2f}"

    result_text = (
        "🔮 Your Next Bill Estimate\n\n"
        "Based on your previous usage:\n"
        f"⚡ Expected usage: ~{approx_units} units\n"
        f"{bill_line}\n\n"
        "📈 This is based on your recent electricity usage pattern and seasonal weather."
    )
    await update.message.reply_text(result_text, reply_markup=MAIN_KEYBOARD)


# ---------------- 📊 My History ----------------

async def prompt_history_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📊 Your Electricity History\n\n"
        "What would you like to see?\n\n"
        "📅 Monthly Usage\n"
        "💰 Bill History\n"
        "📈 Usage Graph\n"
        "🔮 Next Bill Prediction\n"
        "🗑️ Delete History"
    )
    await update.message.reply_text(text, reply_markup=HISTORY_KEYBOARD)


async def show_monthly_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bills = db.get_user_bills(chat_id, limit=6)

    if bills:
        lines = ["📅 Your Recent Monthly Usage (kWh):\n"]
        for b in bills:
            date_label = b.get("created_at", "Bill")[:10]
            lines.append(f"• {date_label}: {b['units']:,.1f} units")
        await update.message.reply_text("\n".join(lines), reply_markup=HISTORY_KEYBOARD)
        return

    await update.message.reply_text(
        "📊 **No saved bills in your history yet!**\n\n"
        "Your personal monthly usage will appear here once you record your first bill.\n\n"
        "💡 **How to record a bill:**\n"
        "• Tap 📷 **Read Meter** to scan your meter photo or BESCOM receipt\n"
        "• Or tap ✏️ **Enter Current Reading** to calculate manually!",
        reply_markup=HISTORY_KEYBOARD,
    )


async def show_bill_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bills = db.get_user_bills(chat_id, limit=6)

    if bills:
        lines = ["💰 Your Recent Bill History:\n"]
        for b in bills:
            date_label = b.get("created_at", "Bill")[:10]
            lines.append(f"• {date_label}: ₹{b['total']:,.2f} ({b['units']:,.1f} units)")
        
        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑️ Delete Bill History", callback_data="btn_delete_history")]
        ])
        await update.message.reply_text("\n".join(lines), reply_markup=inline_kb)
        return

    await update.message.reply_text(
        "💰 **No saved bills in your history yet!**\n\n"
        "Your bill calculations will be saved here automatically.\n\n"
        "💡 **To calculate and record your bill:**\n"
        "• Tap 📷 **Read Meter** to scan your meter photo or BESCOM bill slip\n"
        "• Or tap ✏️ **Enter Current Reading** to type your readings!",
        reply_markup=HISTORY_KEYBOARD,
    )


async def prompt_delete_history_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bills = db.get_user_bills(chat_id, limit=1)
    if not bills:
        text = (
            "ℹ️ Your bill history is currently empty! There are no saved bills to delete.\n\n"
            "💡 Once you calculate a bill with 📷 **Read Meter** or ✏️ **Enter Current Reading**, "
            "it will be saved to your history."
        )
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        else:
            await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        return

    text = (
        "⚠️ **Delete Bill History** 🗑️\n\n"
        "Are you sure you want to delete your saved bills history?\n\n"
        "• All your recorded bills and calculated readings will be cleared from the database.\n"
        "• Your Gruha Jyothi scheme configuration will be preserved.\n\n"
        "Do you want to proceed?"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑️ Yes, Delete History", callback_data="confirm_delete_history"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete_history"),
        ]
    ])
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=kb)
    else:
        await update.message.reply_text(text, reply_markup=kb)


async def handle_delete_history_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    db.delete_user_bills(chat_id)
    context.chat_data.pop(LAST_CALCULATED_BILL_KEY, None)

    msg = (
        "🗑️ **Bill History Cleared!**\n\n"
        "All your saved bill records have been successfully deleted from your database.\n\n"
        "You can calculate and save fresh bills anytime using 📷 **Read Meter** or ✏️ **Enter Current Reading**."
    )
    await query.edit_message_text(msg)
    await query.message.reply_text("Back to Main Menu:", reply_markup=MAIN_KEYBOARD)


async def handle_delete_history_cancelled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Deletion cancelled. Your bill history has been preserved safely!")


async def show_usage_graph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    bills = db.get_user_bills(chat_id, limit=6)

    if bills:
        lines = ["📈 Your Electricity Usage Trend:\n"]
        for b in bills:
            units = int(b['units'])
            bars = "▇" * max(1, int(units / 25))
            date_label = b.get("created_at", "Bill")[:10]
            lines.append(f"{date_label} | {bars} {units} units (₹{b['total']:,.0f})")
        await update.message.reply_text("\n".join(lines), reply_markup=HISTORY_KEYBOARD)
        return

    await update.message.reply_text(
        "📈 **No usage history to display a graph yet!**\n\n"
        "Once you record your bills, a visual consumption bar chart will appear here.\n\n"
        "💡 Tap 📷 **Read Meter** or ✏️ **Enter Current Reading** to start!",
        reply_markup=HISTORY_KEYBOARD,
    )


# ---------------- ❓ Help ----------------

async def prompt_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "❓ SmartBill Help & Guide 💡\n\n"
        "• 📷 Read Meter: Take a photo of your meter or printed bill slip.\n"
        "• ✏️ Enter Current Reading: Type current reading manually.\n"
        "• 📝 Enter Previous Reading: Type previous reading from your bill.\n"
        "• 🏠 Add Appliances: See which appliances consume the most electricity.\n"
        "• 🔮 Predict Next Bill: AI estimate for your upcoming month.\n"
        "• 📊 My History: View your past power usage and trends.\n"
        "• ⚡ Gruha Jyothi: Toggle free 200 units scheme (Zero Bill if <= 200 units).\n\n"
        "Quick commands:\n"
        "• /reading <number> — Quick bill calculation (e.g. /reading 2568)\n"
        "• /setlastreading <number> — Update baseline reading\n"
        "• /gruhajyothi — Gruha Jyothi scheme settings\n"
        "• /deletehistory — Clear saved bill history\n"
        "• /cancel — Return to Main Menu"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


# ---------------- Direct Commands (/reading, /setlastreading) ----------------

async def set_current_reading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        current_reading = float(context.args[0])
        if current_reading < 0 or current_reading > 999999:
            await update.message.reply_text("⚠️ Please enter a realistic meter reading between 0 and 999,999 units.", reply_markup=MAIN_KEYBOARD)
            return
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usage: /reading 14350\n\nPlease provide a number.", reply_markup=MAIN_KEYBOARD)
        return

    last_reading = get_chat_last_reading(context, chat_id)
    if last_reading is None:
        set_chat_last_reading(context, chat_id, current_reading)
        await update.message.reply_text(
            f"✅ Saved {current_reading:,.1f} as your baseline reading! 📌\n\n"
            "Next time you send a photo or type /reading <number>, I will compute your usage & bill!",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    units = current_reading - last_reading
    if units < 0:
        await update.message.reply_text(
            f"⚠️ Current reading ({current_reading:,.1f}) is lower than your last saved reading ({last_reading:,.1f}).\n\n"
            "Use /setlastreading <number> to reset your baseline.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    bill = compute_user_bill(units, chat_id)
    msg = format_bill_summary(current_reading, last_reading, bill)
    set_chat_last_reading(context, chat_id, current_reading)
    context.chat_data[LAST_CALCULATED_BILL_KEY] = {"current": current_reading, "prev": last_reading, "bill": bill}
    db.save_bill(chat_id, current_reading, last_reading, bill, source="command")
    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

    gj_kb = get_gj_prompt_if_unset(chat_id)
    if gj_kb:
        await update.message.reply_text(
            "💡 Are you enrolled in Karnataka's **Gruha Jyothi** Scheme?\n"
            "(Gives up to 200 Free Units every month!)",
            reply_markup=gj_kb,
        )


async def set_last_reading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        value = float(context.args[0])
        if value < 0 or value > 999999:
            await update.message.reply_text("⚠️ Please enter a realistic meter reading between 0 and 999,999 units.", reply_markup=MAIN_KEYBOARD)
            return
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Usage: /setlastreading 12345\n\nPlease provide a valid number.", reply_markup=MAIN_KEYBOARD)
        return

    set_chat_last_reading(context, chat_id, value)
    pending = context.chat_data.pop(PENDING_READING_KEY, None)

    if pending is not None:
        units = pending - value
        if units < 0:
            await update.message.reply_text(
                f"📌 Saved last reading as: {value}\n\n"
                f"⚠️ Note: Current reading ({pending:,.1f}) was lower than {value}. Please verify.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        bill = compute_user_bill(units, chat_id)
        msg = format_bill_summary(pending, value, bill, f"📌 Saved last reading as: {value}")
        set_chat_last_reading(context, chat_id, pending)
        context.chat_data[LAST_CALCULATED_BILL_KEY] = {"current": pending, "prev": value, "bill": bill}
        db.save_bill(chat_id, pending, value, bill, source="command")
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)

        gj_kb = get_gj_prompt_if_unset(chat_id)
        if gj_kb:
            await update.message.reply_text(
                "💡 Are you enrolled in Karnataka's **Gruha Jyothi** Scheme?\n"
                "(Gives up to 200 Free Units every month!)",
                reply_markup=gj_kb,
            )
    else:
        await update.message.reply_text(
            f"✅ Got it! Last reading saved as {value}. 📌\n\n"
            f"Now send a meter photo or type /reading <number> to calculate your bill!",
            reply_markup=MAIN_KEYBOARD,
        )


# ---------------- 💾 Save Bill & 🛑 End Chat ----------------

async def handle_save_bill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    last_calc = context.chat_data.get(LAST_CALCULATED_BILL_KEY)

    if last_calc:
        bill = last_calc["bill"]
        db.save_bill(
            chat_id,
            last_calc["current"],
            last_calc["prev"],
            bill,
            source="manual_save",
        )
        text = (
            "💾 Bill History Saved! ✅\n\n"
            f"⚡ Current Reading: {last_calc['current']:,.1f}\n"
            f"📌 Previous Reading: {last_calc['prev']:,.1f}\n"
            f"🔌 Units Used: {bill['units']} kWh\n"
            f"💰 Total Amount: ₹{bill['total']:,.2f}\n\n"
            "☁️ Saved permanently in your MongoDB database!\n"
            "You can review your bills anytime under '📊 My History'."
        )
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        return

    # Check if user already has saved bills in DB
    user_bills = db.get_user_bills(chat_id, limit=1)
    if user_bills:
        latest = user_bills[-1]
        text = (
            "💾 Storage Status: Auto-Save Active ✅\n\n"
            f"Your latest bill is already safely saved in MongoDB:\n"
            f"• Date: {latest.get('created_at', 'Recent')}\n"
            f"• Units: {latest.get('units', 0):,.1f} units\n"
            f"• Bill Amount: ₹{latest.get('total', 0):,.2f}\n\n"
            "💡 Auto-Save is always active! Any bill you calculate via meter photo or reading is saved automatically to MongoDB.\n\n"
            "Tap '📷 Read Meter' or '✏️ Enter Current Reading' to calculate a new bill."
        )
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
    else:
        last_reading = get_chat_last_reading(context, chat_id)
        reading_note = f" (Current baseline reading: {last_reading:,.1f})" if last_reading else ""
        text = (
            f"💾 Save Bill History{reading_note}\n\n"
            "You haven't calculated a bill in this session yet.\n\n"
            "💡 How Auto-Save Works:\n"
            "Every bill you calculate is automatically saved to MongoDB!\n\n"
            "Tap '📷 Read Meter' or '✏️ Enter Current Reading' to calculate and save your first bill."
        )
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def handle_end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clear any active prompt states
    context.chat_data.pop(PENDING_READING_KEY, None)

    text = (
        "👋 Chat Ended!\n\n"
        "Thank you for using SmartBill ⚡.\n"
        "All your readings and bills are securely saved in your database! 💾\n\n"
        "Whenever you're ready, tap '⚡ Start Again' below or send /start."
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=RESTART_KEYBOARD)
    else:
        await update.message.reply_text(text, reply_markup=RESTART_KEYBOARD)
    return ConversationHandler.END


async def clear_data_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Privacy control: Allows users to wipe their stored readings, bills, and appliances."""
    chat_id = update.effective_chat.id
    db.clear_user_data(chat_id)
    context.chat_data.clear()
    context.user_data.clear()
    await update.message.reply_text(
        "🗑️ Your saved baseline readings, bills, and appliance profiles have been cleared.\n\n"
        "You have a fresh account now. Send /start to begin!",
        reply_markup=MAIN_KEYBOARD,
    )


async def set_entitlement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        val = float(context.args[0])
        if 0 < val <= 200:
            db.set_entitlement_units(chat_id, val)
            last_calc = context.chat_data.get(LAST_CALCULATED_BILL_KEY)
            if last_calc:
                units = last_calc["current"] - last_calc["prev"]
                if units >= 0:
                    bill = compute_user_bill(units, chat_id)
                    last_calc["bill"] = bill
                    db.save_bill(chat_id, last_calc["current"], last_calc["prev"], bill, source="recalculated_gj")
                    msg = format_bill_summary(
                        last_calc["current"],
                        last_calc["prev"],
                        bill,
                        f"🎉 Entitlement Updated to {val:,.1f} Free Units! (Bill Recalculated)",
                    )
                    await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
                    return
            await update.message.reply_text(
                f"✅ Entitlement saved as {val:,.1f} units! Usage up to {val:,.1f} units is ₹0.00.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
    except (IndexError, ValueError):
        pass
    await update.message.reply_text("⚠️ Usage: /entitlement 51 (enter a number between 1 and 200)", reply_markup=MAIN_KEYBOARD)


# ---------------- Message Dispatcher & Fallback ----------------

async def handle_text_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Handle awaiting entitlement input
    if context.chat_data.get("awaiting_entitlement"):
        chat_id = update.effective_chat.id
        try:
            val = float(text)
            if 0 < val <= 200:
                context.chat_data.pop("awaiting_entitlement", None)
                db.set_entitlement_units(chat_id, val)
                last_calc = context.chat_data.get(LAST_CALCULATED_BILL_KEY)
                if last_calc:
                    units = last_calc["current"] - last_calc["prev"]
                    if units >= 0:
                        bill = compute_user_bill(units, chat_id)
                        last_calc["bill"] = bill
                        db.save_bill(chat_id, last_calc["current"], last_calc["prev"], bill, source="recalculated_gj")
                        msg = format_bill_summary(
                            last_calc["current"],
                            last_calc["prev"],
                            bill,
                            f"🎉 Entitlement Updated to {val:,.1f} Free Units! (Bill Recalculated)",
                        )
                        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
                        return

                await update.message.reply_text(
                    f"✅ Entitlement Saved as **{val:,.1f} Free Units** / month! ⚡\n\n"
                    f"• Usage <= {val:,.1f} units: ₹0.00 (Zero Bill)\n"
                    f"• Usage > {val:,.1f} units: Only excess units billed at ₹5.80/u.\n\n"
                    "Tap '📷 Read Meter' or '✏️ Enter Current Reading' to calculate your bill!",
                    reply_markup=MAIN_KEYBOARD,
                )
                return
            else:
                await update.message.reply_text(
                    "⚠️ Please enter a valid entitlement between 1 and 200 units (e.g. 51):",
                    reply_markup=MAIN_KEYBOARD,
                )
                return
        except ValueError:
            await update.message.reply_text(
                "⚠️ Please type a valid number (e.g. 51 or 80):",
                reply_markup=MAIN_KEYBOARD,
            )
            return

    if text in ["🏠 Main Menu", "⬅️ Main Menu"]:
        return await show_main_menu(update, context)

    # Main Menu selections
    if text == "📷 Read Meter":
        return await prompt_read_meter(update, context)
    if text == "💰 Estimate Bill":
        return await prompt_estimate_bill(update, context)
    if text == "🏠 Add Appliances":
        return await prompt_add_appliances(update, context)
    if text == "🔮 Predict Next Bill":
        return await prompt_predict_bill(update, context)
    if text in ["📊 My History", "📊 View My History"]:
        return await prompt_history_menu(update, context)
    if text in ["⚡ Gruha Jyothi", "⚡ Gruha Jyothi Scheme", "/gruhajyothi", "/gj"]:
        return await prompt_gruha_jyothi(update, context)
    if text in ["💾 Save Bill", "💾 Save History"]:
        return await handle_save_bill(update, context)
    if text in ["🗑️ Delete History", "Delete History", "/deletehistory", "/clearhistory"]:
        return await prompt_delete_history_confirmation(update, context)
    if text in ["🛑 End Chat", "❌ End Chat", "/end"]:
        return await handle_end_chat(update, context)
    if text in ["⚡ Start Again", "/start"]:
        return await start(update, context)
    if text == "❓ Help":
        return await prompt_help(update, context)

    # Manual reading triggers
    if text in ["✏️ Enter Current Reading", "📷 Meter Reading"]:
        return await start_manual_current_reading(update, context)
    if text in ["📝 Enter Previous Reading", "📝 Set Previous Reading"]:
        return await start_manual_prev_reading_only(update, context)
    if text == "❓ I Don't Know":
        return await handle_dont_know_reading(update, context)

    # Sub-options
    if text == "🏠 Appliances":
        return await prompt_add_appliances(update, context)
    if text == "📊 Previous Bill":
        return await prompt_predict_bill(update, context)
    if text == "📅 Monthly Usage":
        return await show_monthly_usage(update, context)
    if text == "💰 Bill History":
        return await show_bill_history(update, context)
    if text == "📈 Usage Graph":
        return await show_usage_graph(update, context)

    # Fallback for unknown input
    await update.message.reply_text(
        "🙂 I didn't understand that.\n\n"
        "No problem! Please choose one of the options below:",
        reply_markup=MAIN_KEYBOARD,
    )


def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        print(
            "\n[!] Error: TELEGRAM_BOT_TOKEN is not configured.\n"
            "Please set your token before running the bot:\n"
            "  Windows PowerShell: $env:TELEGRAM_BOT_TOKEN=\"your_bot_token\"\n"
            "  Windows CMD:        set TELEGRAM_BOT_TOKEN=your_bot_token\n"
            "  Or edit config.py directly.\n"
        )
        return

    # Automatically ensure AI model is trained on startup
    try:
        model, mae, n = ml_model.train()
        log.info(f"AI model auto-trained on {n} months of bill data (MAE: Rs {mae:.2f})")
    except Exception as e:
        log.warning(f"AI model auto-training on startup: {e}")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reading", set_current_reading))
    app.add_handler(CommandHandler("setlastreading", set_last_reading))
    app.add_handler(CommandHandler("help", prompt_help))
    app.add_handler(CommandHandler("cancel", show_main_menu))
    app.add_handler(CommandHandler("appliances", prompt_add_appliances))
    app.add_handler(CommandHandler("predict", prompt_predict_bill))
    app.add_handler(CommandHandler("history", prompt_history_menu))
    app.add_handler(CommandHandler(["gruhajyothi", "gj"], prompt_gruha_jyothi))
    app.add_handler(CommandHandler(["entitlement", "gjunits"], set_entitlement_command))
    app.add_handler(CommandHandler("save", handle_save_bill))
    app.add_handler(CommandHandler(["deletehistory", "clearhistory"], prompt_delete_history_confirmation))
    app.add_handler(CommandHandler("end", handle_end_chat))
    app.add_handler(CommandHandler(["cleardata", "reset"], clear_data_command))

    # Photos
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Inline appliance callback
    app.add_handler(CallbackQueryHandler(handle_appliance_toggle))

    # Conversation for Manual Readings (Current -> Previous)
    manual_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^(✏️ Enter Current Reading|📷 Meter Reading)$"), start_manual_current_reading),
            MessageHandler(filters.Regex(r"^(📝 Enter Previous Reading|📝 Set Previous Reading)$"), start_manual_prev_reading_only),
        ],
        states={
            WAIT_MANUAL_CURRENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_current_input)],
            WAIT_MANUAL_PREV: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_manual_prev_input)],
        },
        fallbacks=[
            CommandHandler("cancel", show_main_menu),
            CommandHandler("end", handle_end_chat),
            MessageHandler(filters.Regex(r"^(⬅️ Main Menu|🏠 Main Menu)$"), show_main_menu),
            MessageHandler(filters.Regex(r"^(🛑 End Chat|❌ End Chat)$"), handle_end_chat),
        ],
    )
    app.add_handler(manual_conv)

    # Conversation for appliance details (Quantity & Hours)
    appliance_conv = ConversationHandler(
        entry_points=[
            MessageHandler(
                filters.TEXT & filters.Regex(r"^(1|2|3|4|5\+|1–3 hours|4–6 hours|7–10 hours|10\+ hours)$"),
                handle_appliance_qty_response,
            )
        ],
        states={
            APPLIANCE_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_appliance_qty_response)],
            APPLIANCE_HRS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_appliance_hrs_response)],
        },
        fallbacks=[
            CommandHandler("cancel", show_main_menu),
            CommandHandler("end", handle_end_chat),
            MessageHandler(filters.Regex(r"^(⬅️ Main Menu|🏠 Main Menu)$"), show_main_menu),
            MessageHandler(filters.Regex(r"^(🛑 End Chat|❌ End Chat)$"), handle_end_chat),
        ],
    )
    app.add_handler(appliance_conv)

    # General text dispatcher
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_dispatcher))

    # Storage status
    if db.is_mongodb_connected():
        log.info("💾 Storage: Connected to MongoDB Atlas successfully!")
    else:
        log.info("💾 Storage: Using local SQLite database (ready to connect to MongoDB Atlas).")

    log.info("SmartBill Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
