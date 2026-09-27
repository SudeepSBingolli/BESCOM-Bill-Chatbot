"""
Comprehensive Test Suite for BESCOM Bill Bot.
Tests all components:
1. tariff.py - Slab calculations, taxes, fixed charges
2. appliance_estimate.py - Appliance wattage, hours override, breakdown
3. ml_model.py - Data prep, Random Forest training, MAE, predictions
4. ocr.py - Missing keys, bad files, mocked API response parsing
5. bot.py - Telegram handlers, state management, conversation flow
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

# Add current directory to path
sys.path.insert(0, os.path.dirname(__file__))

import config
import tariff
import appliance_estimate
import ml_model
import ocr
import bot


class TestTariffCalculations(unittest.TestCase):
    """Test BESCOM LT-1 / LT-2 domestic tariff calculation."""

    def test_energy_charge_zero_units(self):
        self.assertEqual(tariff.energy_charge(0), 0.0)

    def test_energy_charge_flat_kerc(self):
        # 30 units @ 5.80 = 174.00
        self.assertAlmostEqual(tariff.energy_charge(30), 174.0, places=2)
        # 100 units @ 5.80 = 580.00
        self.assertAlmostEqual(tariff.energy_charge(100), 580.0, places=2)

    def test_calculate_bill_full_breakdown(self):
        result = tariff.calculate_bill(100)
        # Energy charge for 100: 580.00
        self.assertEqual(result["units"], 100.0)
        self.assertAlmostEqual(result["energy_charge"], 580.00, places=2)
        # Fixed charge: 150 * 1.0 = 150.0
        self.assertAlmostEqual(result["fixed_charge"], 150.0, places=2)
        # FPPCA: 100 * 0.38 = 38.00
        self.assertAlmostEqual(result["fppca"], 38.0, places=2)
        # P&G: 100 * 0.35 = 35.00
        self.assertAlmostEqual(result["pg_surcharge"], 35.0, places=2)
        # Tax (9% on Energy Charge): 580 * 0.09 = 52.20
        self.assertAlmostEqual(result["tax"], 52.20, places=2)
        # Total: 580 + 150 + 38 + 35 + 52.20 = 855.20
        self.assertAlmostEqual(result["total"], 855.20, places=2)

    def test_actual_bescom_bill_match(self):
        # Actual customer bill: 107 units consumed, 51 entitlement units
        # Gross: Rs 904.56, Subsidy: Rs 509.65, Net: Rs 394.91
        res = tariff.calculate_bill(107, gruha_jyothi=True, entitlement_units=51.0)
        self.assertEqual(res["units"], 107.0)
        self.assertEqual(res["free_units"], 51.0)
        self.assertEqual(res["excess_units"], 56.0)
        self.assertAlmostEqual(res["regular_total"], 904.56, places=2)
        self.assertAlmostEqual(res["govt_subsidy"], 509.65, places=2)
        self.assertAlmostEqual(res["total"], 394.91, places=2)
        self.assertFalse(res["is_free"])

    def test_gruha_jyothi_free_under_entitlement(self):
        # 40 units with 51 entitlement units is 100% free (₹0.0)
        res_free = tariff.calculate_bill(40, gruha_jyothi=True, entitlement_units=51.0)
        self.assertEqual(res_free["total"], 0.0)
        self.assertEqual(res_free["energy_charge"], 0.0)
        self.assertEqual(res_free["tax"], 0.0)
        self.assertEqual(res_free["free_units"], 40.0)
        self.assertTrue(res_free["is_free"])
        self.assertGreater(res_free["govt_subsidy"], 0.0)

        # 51 units exactly is also completely free
        res_51 = tariff.calculate_bill(51, gruha_jyothi=True, entitlement_units=51.0)
        self.assertEqual(res_51["total"], 0.0)
        self.assertEqual(res_51["free_units"], 51.0)
        self.assertTrue(res_51["is_free"])

    def test_gruha_jyothi_disqualified_above_200_units(self):
        # In a 1-month bill (30 days), crossing 200 units/month revokes subsidy
        res_232 = tariff.calculate_bill(232, gruha_jyothi=True, entitlement_units=51.0, billing_days=30)
        self.assertTrue(res_232["disqualified"])
        self.assertEqual(res_232["govt_subsidy"], 0.0)
        self.assertEqual(res_232["free_units"], 0.0)
        self.assertAlmostEqual(res_232["total"], 1786.06, places=2)

    def test_gruha_jyothi_multi_month_61_days(self):
        # In a 2-month bill (61 days), 232 units is NOT disqualified!
        # Monthly entitlement 51 * 61 / 30 = 103.7 eligible units
        # Chargeable = 232 - 103.7 = 128.3 units
        # Final Net Payable reproduces the bill: ₹730.00
        res = tariff.calculate_bill(
            units=232,
            gruha_jyothi=True,
            entitlement_units=51.0,
            billing_days=61,
            sanctioned_load_kw=1.0,
            fixed_charge_override=305.00,
            fppca_charge_override=33.17,
            fppca_sub_override=14.82,
            md_penalty=56.25,
            credit=216.54,
            true_up_ec=41.39,
            true_up_tax=3.73,
            true_up_gj_sub=-27.21,
            true_up_gj_tax=-2.45
        )
        self.assertFalse(res["disqualified"])
        self.assertEqual(res["grj_eligible_units"], 103.7)
        self.assertEqual(res["chargeable_units"], 128.3)
        self.assertAlmostEqual(res["sub_total_1"], 1886.07, places=1)
        self.assertAlmostEqual(res["sub_total_2"], 1011.70, places=1)
        self.assertAlmostEqual(res["total"], 730.00, places=0)


class TestApplianceEstimation(unittest.TestCase):
    """Test appliance load estimation and breakdown."""

    def test_estimate_monthly_units(self):
        # 1 fridge: 150W * 24h * 30d / 1000 = 108 kWh
        counts = {"fridge": 1}
        self.assertAlmostEqual(appliance_estimate.estimate_monthly_units(counts), 108.0, places=2)

    def test_estimate_multiple_appliances(self):
        counts = {
            "led_bulb": 10,       # 10 * 9W * 5h * 30d / 1000 = 13.5 kWh
            "ceiling_fan": 2,     # 2 * 75W * 8h * 30d / 1000 = 36.0 kWh
            "fridge": 1,          # 108.0 kWh
        }
        total = appliance_estimate.estimate_monthly_units(counts)
        self.assertAlmostEqual(total, 157.5, places=2)

    def test_hours_override(self):
        # AC 1.5 ton: 1500W. Default 4h = 180 kWh. Override to 6h = 270 kWh
        counts = {"ac_1_5_ton": 1}
        override = {"ac_1_5_ton": 6}
        units = appliance_estimate.estimate_monthly_units(counts, hours_override=override)
        self.assertAlmostEqual(units, 270.0, places=2)

    def test_appliance_breakdown_sorted(self):
        counts = {
            "led_bulb": 5,        # 6.75 kWh
            "ac_1_5_ton": 1,      # 180 kWh
            "ceiling_fan": 2,     # 36 kWh
        }
        breakdown = appliance_estimate.appliance_breakdown(counts)
        self.assertEqual(len(breakdown), 3)
        # Should be sorted descending by consumption
        self.assertEqual(breakdown[0]["appliance"], "ac_1_5_ton")
        self.assertEqual(breakdown[1]["appliance"], "ceiling_fan")
        self.assertEqual(breakdown[2]["appliance"], "led_bulb")

    def test_zero_or_negative_counts(self):
        counts = {"led_bulb": 0, "ceiling_fan": -2, "fridge": 1}
        units = appliance_estimate.estimate_monthly_units(counts)
        self.assertAlmostEqual(units, 108.0, places=2)


class TestMLModel(unittest.TestCase):
    """Test Random Forest training, evaluation, and prediction."""

    def test_train_model(self):
        model, mae, n_rows = ml_model.train()
        self.assertGreaterEqual(n_rows, 6)
        self.assertIsNotNone(model)
        self.assertIsInstance(mae, float)
        self.assertTrue(os.path.exists(config.MODEL_PATH))

    def test_predict(self):
        counts = {
            "led_bulb": 6,
            "tube_light": 2,
            "ceiling_fan": 4,
            "fridge": 1,
            "ac_1_5_ton": 1,
            "washing_machine": 1,
            "tv": 1,
            "water_heater": 1,
        }
        pred = ml_model.predict(counts, avg_temp_c=30, month_num=5)
        self.assertIsInstance(pred, float)
        self.assertGreater(pred, 500.0)  # Bill should be positive and reasonable
        self.assertLess(pred, 5000.0)

    def test_prep_extracts_month_num(self):
        import pandas as pd
        sample_df = pd.DataFrame([{
            "month": "2026-04",
            "bulbs": 6,
            "fans": 4,
            "units": 150,
            "bill_amount": 1000
        }])
        prepped = ml_model._prep(sample_df)
        self.assertEqual(prepped["month_num"].iloc[0], 4)
        for col in ml_model.FEATURE_COLS:
            self.assertIn(col, prepped.columns)


class TestOCRModule(unittest.TestCase):
    """Test OCR error handling and parsing."""

    def test_unconfigured_api_key(self):
        with patch.object(ocr, "GEMINI_API_KEY", ""), patch.object(ocr, "ANTHROPIC_API_KEY", ""), patch.object(ocr, "OPENAI_API_KEY", ""):
            result = ocr.read_meter_image("dummy.jpg")
            self.assertIsNone(result["reading"])
            self.assertIn("not configured", result["notes"])

    def test_nonexistent_image_file(self):
        with patch.object(ocr, "GEMINI_API_KEY", "valid_key"), patch.object(ocr, "ANTHROPIC_API_KEY", ""), patch.object(ocr, "OPENAI_API_KEY", ""):
            result = ocr.read_meter_image("nonexistent_file_path.jpg")
            self.assertIsNone(result["reading"])
            self.assertIn("Failed to read image file", result["notes"])

    @patch("requests.post")
    def test_mocked_gemini_ocr(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '{"reading": 14250.5, "confidence": "high", "notes": ""}'}
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        dummy_file = "temp_dummy_meter.jpg"
        with open(dummy_file, "wb") as f:
            f.write(b"fake-image-bytes")

        try:
            with patch.object(ocr, "GEMINI_API_KEY", "AIzaSy-mock-key"):
                res = ocr.read_meter_image(dummy_file)
                self.assertEqual(res["reading"], 14250.5)
                self.assertEqual(res["confidence"], "high")
        finally:
            if os.path.exists(dummy_file):
                os.remove(dummy_file)

    @patch("requests.post")
    def test_mocked_openai_ocr(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"reading": 14250.5, "confidence": "high", "notes": ""}'
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        dummy_file = "temp_dummy_meter.jpg"
        with open(dummy_file, "wb") as f:
            f.write(b"fake-image-bytes")

        try:
            with patch.object(ocr, "GEMINI_API_KEY", ""), patch.object(ocr, "OPENAI_API_KEY", "sk-proj-mock-key"):
                res = ocr.read_meter_image(dummy_file)
                self.assertEqual(res["reading"], 14250.5)
                self.assertEqual(res["confidence"], "high")
        finally:
            if os.path.exists(dummy_file):
                os.remove(dummy_file)

    @patch("requests.post")
    def test_mocked_anthropic_ocr(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "content": [
                {
                    "type": "text",
                    "text": '{"reading": 14250.5, "confidence": "high", "notes": ""}'
                }
            ]
        }
        mock_post.return_value = mock_resp

        dummy_file = "temp_dummy_meter.jpg"
        with open(dummy_file, "wb") as f:
            f.write(b"fake-image-bytes")

        try:
            with patch.object(ocr, "GEMINI_API_KEY", ""), patch.object(ocr, "OPENAI_API_KEY", ""), patch.object(ocr, "ANTHROPIC_API_KEY", "sk-test-mock-key"):
                res = ocr.read_meter_image(dummy_file)
                self.assertEqual(res["reading"], 14250.5)
                self.assertEqual(res["confidence"], "high")
        finally:
            if os.path.exists(dummy_file):
                os.remove(dummy_file)
            if os.path.exists(dummy_file):
                os.remove(dummy_file)


class TestBotHandlers(unittest.IsolatedAsyncioTestCase):
    """Test Bot async handlers and conversation logic."""

    async def test_start_command(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await bot.start(update, context)
        update.message.reply_text.assert_awaited_once()
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Welcome to SmartBill", sent_text)

    async def test_set_last_reading_valid(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["12500.0"]
        context.chat_data = {}

        await bot.set_last_reading(update, context)
        self.assertEqual(context.chat_data[bot.LAST_READING_KEY], 12500.0)
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("last reading saved as 12500.0", sent_text.lower())

    async def test_set_last_reading_invalid(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = ["not-a-number"]
        context.chat_data = {}

        await bot.set_last_reading(update, context)
        self.assertNotIn(bot.LAST_READING_KEY, context.chat_data)
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Usage: /setlastreading 12345", sent_text)

    async def test_appliances_flow(self):
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.user_data = {
            "appliance_counts": {"led_bulb": 4, "ceiling_fan": 2, "fridge": 1},
            "appliance_hours": {"led_bulb": 5, "ceiling_fan": 8, "fridge": 24},
        }

        await bot.prompt_add_appliances(update, context)
        update.message.reply_text.assert_awaited()
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Which appliances do you have?", sent_text)

        await bot.show_appliance_results(update.message, context)
        final_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Estimated monthly units", final_text)
        self.assertIn("Estimated monthly bill", final_text)
        self.assertIn("AI Prediction for Upcoming Month", final_text)

    async def test_handle_save_bill(self):
        update = MagicMock()
        update.effective_chat.id = 12345
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.chat_data = {
            bot.LAST_CALCULATED_BILL_KEY: {
                "current": 2600.0,
                "prev": 2450.0,
                "bill": {"units": 150.0, "total": 1150.0, "energy_charge": 900.0, "fixed_charge": 120.0, "fppca": 0.0, "tax": 91.8},
            }
        }
        await bot.handle_save_bill(update, context)
        update.message.reply_text.assert_awaited()
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Bill History Saved", sent_text)
        self.assertIn("MongoDB", sent_text)

    async def test_handle_end_chat(self):
        update = MagicMock()
        update.callback_query = None
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.chat_data = {bot.PENDING_READING_KEY: 2500.0}

        ret = await bot.handle_end_chat(update, context)
        self.assertNotIn(bot.PENDING_READING_KEY, context.chat_data)
        update.message.reply_text.assert_awaited()
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("Chat Ended", sent_text)
        self.assertEqual(ret, bot.ConversationHandler.END)

    async def test_prompt_delete_history_confirmation_empty(self):
        update = MagicMock()
        update.callback_query = None
        update.effective_chat.id = "empty_user_123"
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        await bot.prompt_delete_history_confirmation(update, context)
        update.message.reply_text.assert_awaited()
        sent_text = update.message.reply_text.call_args[0][0]
        self.assertIn("empty", sent_text.lower())

    async def test_handle_delete_history_confirmed(self):
        update = MagicMock()
        update.effective_chat.id = "test_del_user"
        update.callback_query = MagicMock()
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
        update.callback_query.message.reply_text = AsyncMock()
        context = MagicMock()
        context.chat_data = {bot.LAST_CALCULATED_BILL_KEY: {"current": 2500, "prev": 2400, "bill": {}}}

        # Save a bill first
        from database import db
        db.save_bill("test_del_user", 2500, 2400, {"units": 100, "energy_charge": 580, "fixed_charge": 150, "fppca": 38, "tax": 52.2, "total": 820.2}, source="test")
        self.assertGreaterEqual(len(db.get_user_bills("test_del_user")), 1)

        await bot.handle_delete_history_confirmed(update, context)
        self.assertEqual(len(db.get_user_bills("test_del_user")), 0)
        self.assertNotIn(bot.LAST_CALCULATED_BILL_KEY, context.chat_data)
        update.callback_query.edit_message_text.assert_awaited()
        sent_text = update.callback_query.edit_message_text.call_args[0][0]
        self.assertIn("successfully deleted", sent_text.lower())

    def test_auto_model_training(self):
        # Auto-training executes seamlessly without manual command
        model, mae, n = ml_model.train()
        self.assertGreaterEqual(n, 6)
        self.assertLess(mae, 200.0)


class TestDatabaseStorage(unittest.TestCase):
    def setUp(self):
        from database import db
        self.db = db
        self.test_chat_id = "test_user_9999"

    def test_set_and_get_last_reading(self):
        self.db.set_last_reading(self.test_chat_id, 14500.5)
        val = self.db.get_last_reading(self.test_chat_id)
        self.assertAlmostEqual(val, 14500.5)

        # Update to new value
        self.db.set_last_reading(self.test_chat_id, 14750.0)
        val2 = self.db.get_last_reading(self.test_chat_id)
        self.assertAlmostEqual(val2, 14750.0)

    def test_save_and_get_user_bills(self):
        sample_bill = {
            "units": 100.0,
            "energy_charge": 555.0,
            "fixed_charge": 120.0,
            "fppca": 0.0,
            "tax": 60.75,
            "total": 735.75,
        }
        self.db.save_bill(self.test_chat_id, 2600.0, 2500.0, sample_bill, source="test")
        bills = self.db.get_user_bills(self.test_chat_id, limit=5)
        self.assertGreaterEqual(len(bills), 1)
        latest = bills[-1]
        self.assertAlmostEqual(latest["units"], 100.0)
        self.assertAlmostEqual(latest["total"], 735.75)

    def test_delete_user_bills(self):
        sample_bill = {
            "units": 100.0,
            "energy_charge": 580.0,
            "fixed_charge": 150.0,
            "fppca": 38.0,
            "tax": 52.2,
            "total": 820.2,
        }
        self.db.set_last_reading(self.test_chat_id, 2500.0)
        self.db.save_bill(self.test_chat_id, 2600.0, 2500.0, sample_bill, source="test")
        self.assertGreaterEqual(len(self.db.get_user_bills(self.test_chat_id)), 1)

        # Deleting bills clears the bills history but preserves last reading
        self.db.delete_user_bills(self.test_chat_id)
        self.assertEqual(len(self.db.get_user_bills(self.test_chat_id)), 0)
        self.assertEqual(self.db.get_last_reading(self.test_chat_id), 2500.0)

    def test_save_and_get_appliances(self):
        counts = {"led_bulb": 5, "ceiling_fan": 3}
        hours = {"led_bulb": 6.0, "ceiling_fan": 8.0}
        self.db.save_appliances(self.test_chat_id, counts, hours)
        result = self.db.get_appliances(self.test_chat_id)
        self.assertIsNotNone(result)
        saved_counts, saved_hours = result
        self.assertEqual(saved_counts.get("led_bulb"), 5)
        self.assertEqual(saved_hours.get("ceiling_fan"), 8.0)

    def test_clear_user_data(self):
        self.db.set_last_reading(self.test_chat_id, 12000.0)
        self.db.clear_user_data(self.test_chat_id)
        self.assertIsNone(self.db.get_last_reading(self.test_chat_id))
        self.assertEqual(len(self.db.get_user_bills(self.test_chat_id)), 0)

    def test_gruha_jyothi_persistence(self):
        # Set to True
        self.db.set_gruha_jyothi(self.test_chat_id, True)
        self.assertTrue(self.db.get_gruha_jyothi(self.test_chat_id))

        # Set to False
        self.db.set_gruha_jyothi(self.test_chat_id, False)
        self.assertFalse(self.db.get_gruha_jyothi(self.test_chat_id))

        # After clearing data, it resets to None
        self.db.clear_user_data(self.test_chat_id)
        self.assertIsNone(self.db.get_gruha_jyothi(self.test_chat_id))

    def test_entitlement_units_persistence(self):
        self.db.set_entitlement_units(self.test_chat_id, 51.0)
        self.assertEqual(self.db.get_entitlement_units(self.test_chat_id), 51.0)
        self.assertTrue(self.db.get_gruha_jyothi(self.test_chat_id))

        # Reset via clear_user_data
        self.db.clear_user_data(self.test_chat_id)
        self.assertEqual(self.db.get_entitlement_units(self.test_chat_id), 51.0)



if __name__ == "__main__":
    runner = unittest.TextTestRunner(verbosity=2)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
