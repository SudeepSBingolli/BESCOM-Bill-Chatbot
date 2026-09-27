"""
Reads meter readings from photos using AI Vision.
Supports:
1. Google Gemini (100% Free tier, gemini-3.8-flash)
2. OpenAI (gpt-4o-mini, gpt-4o)
3. Anthropic Claude (claude-3-5-sonnet)

Handles:
- Physical meter displays (digital LCD or analog dials)
- Printed paper electricity bills / BESCOM receipts
- Automatic EXIF orientation correction and image optimization
"""
import base64
import json
import re
import io
import requests
from PIL import Image, ImageOps

from config import GEMINI_API_KEY, OPENAI_API_KEY, OPENAI_MODEL, ANTHROPIC_API_KEY, CLAUDE_MODEL

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"

PROMPT = """You are an AI assistant specialized in analyzing Karnataka BESCOM electricity bills and reading physical electricity meters.

Your job is to read an image (either a printed BESCOM bill slip or physical electricity meter display), extract the important values, understand the billing period, calculate electricity consumption, calculate the applicable Gruha Jyothi (GRJ) subsidy, calculate chargeable units, reproduce the bill calculation, and explain the final payable amount clearly.

CRITICAL RULES:
1. Do NOT assume that every consumer gets 200 free units.
2. Do NOT assume that consumption above 200 units automatically means zero subsidy without first checking the billing period and the actual values printed on the bill.
3. Multi-Month Billing Rule: If the billing period covers multiple months (e.g. 61 days), monthly entitlement scales with billing days:
   GRJ eligible units = Monthly Entitlement × (Billing Days / 30)
   Chargeable units = Total Consumption - GRJ eligible units
4. Do NOT replace values printed on the bill with guessed values. The actual bill is the primary source.
5. If the image is a physical meter LCD display, extract "reading" and set "is_bill_slip": false.

Return ONLY a valid JSON object matching this structure (no markdown fences, no text outside JSON):
{
  "is_bill_slip": true,
  "reading": 2800.0,
  "previous_reading": 2568.0,
  "units_consumed": 232.0,
  "confidence": "high",
  "notes": "BESCOM bill slip analysis",
  "consumer": {
    "rr_number": "N2EH51557",
    "account_id": "9006628000",
    "consumer_name": "Savitha J and Mahesh K C",
    "address": "634, Kottege Palya",
    "tariff": "LT1",
    "sanctioned_load": "0 kW + 1 HP",
    "meter_number": null
  },
  "billing": {
    "start_date": "05/06/2026",
    "end_date": "05/08/2026",
    "billing_days": 61,
    "reading_date": "05/08/2026",
    "bill_number": "141541266050012",
    "due_date": "19/08/2026"
  },
  "readings": {
    "previous_reading": 2568.0,
    "present_reading": 2800.0,
    "constant": 1.0,
    "recorded_md": 1.200,
    "power_factor": 1.0
  },
  "consumption": 232.0,
  "gruha_jyothi": {
    "fy22_23_avg": 40.92,
    "monthly_entitlement": 51.0,
    "eligible_subsidy_units": 103.7,
    "chargeable_units": 128.3
  },
  "charges": {
    "fixed_charge": 305.00,
    "energy_charge": 1345.60,
    "fppca": 33.17,
    "pg_surcharge": 81.20,
    "tax": 121.10,
    "sub_total_1": 1886.07
  },
  "subsidy": {
    "fixed_subsidy": 305.00,
    "energy_subsidy": 601.46,
    "fppca_subsidy": 14.82,
    "pg_subsidy": 36.29,
    "tax_subsidy": 54.13,
    "sub_total_2": 1011.70
  },
  "adjustments": {
    "amount_after_subsidy": 874.37,
    "md_penalty": 56.25,
    "pf_penalty": 0.0,
    "interest": 0.0,
    "current_demand": 930.62,
    "credit": 216.54,
    "true_up_ec": 41.39,
    "true_up_tax": 3.73,
    "true_up_gj_sub": -27.21,
    "true_up_gj_tax": -2.45,
    "arrears": 0.0
  },
  "net_payable": 730.00
}
"""


def _parse_json_result(text: str) -> dict:
    if not text:
        return {"reading": None, "previous_reading": None, "confidence": "low", "notes": "Empty response from vision AI"}

    # Extract JSON object matching { ... }
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Fallback cleanup
    cleaned = text.strip().strip("`").replace("json\n", "").strip()
    try:
        return json.loads(cleaned)
    except Exception as e:
        return {"reading": None, "previous_reading": None, "confidence": "low", "notes": f"Could not parse model output: {e}"}


def _prepare_image(image_path: str) -> tuple[str, str]:
    """Optimizes image, corrects mobile phone EXIF orientation, and returns base64 string."""
    try:
        with Image.open(image_path) as img:
            # Auto-orient using EXIF tag (corrects mobile photos taken in portrait/landscape)
            img = ImageOps.exif_transpose(img)

            if img.mode != "RGB":
                img = img.convert("RGB")

            # Scale down if very large to speed up upload & OCR accuracy
            max_dim = 1600
            if max(img.width, img.height) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"
    except Exception:
        # Fallback to direct file read if Pillow encountered an issue
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
            media_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
            return b64, media_type


def _read_with_gemini(img_b64: str, media_type: str) -> dict:
    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "contents": [
                {
                    "parts": [
                        {"text": PROMPT},
                        {
                            "inline_data": {
                                "mime_type": media_type,
                                "data": img_b64
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 400}
        },
        timeout=30,
    )
    if resp.status_code != 200:
        err_msg = resp.text
        try:
            err_data = resp.json()
            if "error" in err_data and "message" in err_data["error"]:
                err_msg = err_data["error"]["message"]
        except Exception:
            pass
        return {"reading": None, "previous_reading": None, "confidence": "none", "notes": f"Gemini API error ({resp.status_code}): {err_msg}"}

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        return {"reading": None, "previous_reading": None, "confidence": "none", "notes": "Gemini returned no response"}
    text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
    return _parse_json_result(text)


def _read_with_openai(img_b64: str, media_type: str) -> dict:
    resp = requests.post(
        OPENAI_URL,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL or "gpt-4o-mini",
            "max_tokens": 400,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{img_b64}"
                            },
                        },
                    ],
                }
            ],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        err_msg = resp.text
        try:
            err_data = resp.json()
            if "error" in err_data and "message" in err_data["error"]:
                err_msg = err_data["error"]["message"]
        except Exception:
            pass
        return {"reading": None, "previous_reading": None, "confidence": "none", "notes": f"OpenAI API error ({resp.status_code}): {err_msg}"}

    data = resp.json()
    text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _parse_json_result(text)


def _read_with_anthropic(img_b64: str, media_type: str) -> dict:
    resp = requests.post(
        ANTHROPIC_URL,
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 400,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        err_msg = resp.text
        try:
            err_data = resp.json()
            if "error" in err_data and "message" in err_data["error"]:
                err_msg = err_data["error"]["message"]
        except Exception:
            pass
        return {"reading": None, "previous_reading": None, "confidence": "none", "notes": f"Anthropic API error ({resp.status_code}): {err_msg}"}

    data = resp.json()
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    return _parse_json_result(text)


def read_meter_image(image_path: str) -> dict:
    has_gemini = bool(GEMINI_API_KEY and GEMINI_API_KEY != "PUT_YOUR_KEY_HERE")
    has_openai = bool(OPENAI_API_KEY and OPENAI_API_KEY != "PUT_YOUR_KEY_HERE")
    has_anthropic = bool(ANTHROPIC_API_KEY and ANTHROPIC_API_KEY != "PUT_YOUR_KEY_HERE")

    if not has_gemini and not has_openai and not has_anthropic:
        return {
            "reading": None,
            "previous_reading": None,
            "confidence": "none",
            "notes": "Vision API key not configured. Set GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY in config.py",
        }

    try:
        img_b64, media_type = _prepare_image(image_path)
    except OSError as e:
        return {"reading": None, "previous_reading": None, "confidence": "none", "notes": f"Failed to read image file: {e}"}

    # 1. Prioritize Gemini (Fastest & Free)
    if has_gemini:
        try:
            return _read_with_gemini(img_b64, media_type)
        except requests.RequestException as e:
            return {"reading": None, "previous_reading": None, "confidence": "none", "notes": f"Gemini network error: {e}"}

    # 2. Try OpenAI
    if has_openai:
        try:
            return _read_with_openai(img_b64, media_type)
        except requests.RequestException as e:
            return {"reading": None, "previous_reading": None, "confidence": "none", "notes": f"OpenAI network error: {e}"}

    # 3. Try Anthropic Claude
    if has_anthropic:
        try:
            return _read_with_anthropic(img_b64, media_type)
        except requests.RequestException as e:
            return {"reading": None, "previous_reading": None, "confidence": "none", "notes": f"Anthropic network error: {e}"}

    return {
        "reading": None,
        "previous_reading": None,
        "confidence": "none",
        "notes": "No active Vision API key found.",
    }
