# SmartBill ⚡ - AI BESCOM Electricity Bill Assistant

> **In Simple Words:**  
> SmartBill is an AI-powered Telegram assistant for Bangalore electricity consumers. Simply snap a photo of your meter or printed BESCOM bill slip, or type your reading. The bot calculates your exact electricity bill down to the paisa, accurately prorates Karnataka's **Gruha Jyothi** free units (even across multi-month 60+ day bills), estimates power usage by appliances, predicts your next month's bill using Machine Learning, and safely persists your history in MongoDB Atlas (with SQLite fallback).

---

## 🧭 Visual System Flowchart (n8n Style Architecture)

Here is how all features and components connect together in an **n8n-style node workflow**:

```mermaid
flowchart TD
    %% Styling Classes
    classDef trigger fill:#FF6F00,stroke:#E65100,stroke-width:2px,color:#fff;
    classDef router fill:#0288D1,stroke:#01579B,stroke-width:2px,color:#fff;
    classDef ai fill:#7B1FA2,stroke:#4A148C,stroke-width:2px,color:#fff;
    classDef engine fill:#2E7D32,stroke:#1B5E20,stroke-width:2px,color:#fff;
    classDef db fill:#D84315,stroke:#BF360C,stroke-width:2px,color:#fff;
    classDef output fill:#37474F,stroke:#212121,stroke-width:2px,color:#fff;

    %% Trigger Node
    T1["⚡ Trigger: Telegram Message / Photo / Command"]:::trigger

    %% Router Node
    R1{"🔀 Router: What did the user send?"}:::router

    T1 --> R1

    %% Branch 1: Photo Upload
    R1 -->|"📸 Photo (Meter / Bill Slip)"| N1["👁️ OCR Vision Engine\n(Gemini 2.5 / OpenAI / Claude)"]:::ai
    N1 --> N2{"📄 Bill Slip or Meter Dial?"}:::router
    N2 -->|"🧾 Full Bill Slip"| N3["📑 Bill Slip Extractor\n(Dates, Readings, Load, Rates, Adjustments)"]:::engine
    N2 -->|"⚡ Meter Dial Only"| N4["🔢 Reading Number Extractor\n(Present Reading & Confidence)"]:::engine

    %% Branch 2: Manual Reading
    R1 -->|"✏️ Enter Reading"| N5["⌨️ Manual Reading Handler\n(Current & Previous Readings)"]:::engine

    %% Branch 3: Appliances
    R1 -->|"🏠 Add Appliances"| N6["🔌 Appliance Load Estimator\n(Watts × Hours × Days)"]:::engine

    %% Branch 4: AI Prediction
    R1 -->|"🔮 Predict Next Bill"| N7["🤖 Random Forest ML Model\n(Historical Trends & Temperature)"]:::ai

    %% Branch 5: History & Settings
    R1 -->|"📊 My History"| N8["📈 Usage Trend & Bill Viewer"]:::engine
    R1 -->|"🗑️ Delete History"| N9["⚠️ Confirmation Safety Modal"]:::engine
    R1 -->|"⚡ Gruha Jyothi"| N10["💡 Entitlement Setup\n(Custom Entitlement / 200 Units)"]:::engine

    %% Convergence to Tariff Engine
    N3 --> TE["⚙️ BESCOM Tariff Engine (tariff.py)\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n1. Days & Multi-Month Calculation\n2. Prorated Entitlement: (Entitlement × Days / 30)\n3. Sub-total 1 (Gross Charges)\n4. Sub-total 2 (GRJ Subsidy)\n5. Net Payable + Penalties - Credits + True-ups"]:::engine
    N4 --> TE
    N5 --> TE
    N6 --> TE

    %% Database Storage Node
    TE --> DB[("💾 Storage Manager (database.py)\nMongoDB Atlas Cloud ☁️\n(Automatic SQLite Fallback 📁)")]:::db
    N7 --> DB
    N8 <--> DB
    N9 -->|"Confirmed Delete"| DB
    N10 --> DB

    %% Output Nodes
    TE --> OUT1["📤 8-Section BESCOM Bill Analysis\n(Summary, GRJ, Charges, Subsidy, Final Bill)"]:::output
    N6 --> OUT2["📤 Appliance Consumption Breakdown\n(Ranked by heaviest power hogs)"]:::output
    N7 --> OUT3["📤 AI Predicted Upcoming Bill\n(Estimated Units & Bill Amount)"]:::output
    N8 --> OUT4["📤 Text-based Graph & Monthly History\n+ 🗑️ Delete History Button"]:::output
    N9 --> OUT5["📤 History Cleared Confirmation\n(Account reset cleanly)"]:::output
```

---

## ⚡ How It Works (Step-by-Step Logic Flowchart)

This flowchart illustrates how the bot calculates multi-month bills, computes Gruha Jyothi free units, and reproduces BESCOM receipts down to the exact rupee:

```mermaid
flowchart TD
    classDef step fill:#1E88E5,stroke:#0D47A1,stroke-width:2px,color:#fff;
    classDef check fill:#F57C00,stroke:#E65100,stroke-width:2px,color:#fff;
    classDef calc fill:#43A047,stroke:#1B5E20,stroke-width:2px,color:#fff;
    classDef result fill:#8E24AA,stroke:#4A148C,stroke-width:2px,color:#fff;

    Start(["📥 User Inputs Meter Reading or Uploads Bill"]):::step --> S1["1. Extract Readings & Dates\nPrevious Reading, Present Reading, Bill Start & End Dates"]:::step

    S1 --> S2["2. Compute Consumption & Duration\n• Consumption = Present - Previous\n• Billing Days = End Date - Start Date\n• Billing Months = Days / 30"]:::calc

    S2 --> C1{"Is User Enrolled in Gruha Jyothi?"}:::check

    %% Disqualification Check
    C1 -->|"Yes"| C2{"Does Consumption Exceed Monthly Cap?\nCap = 200 units × Billing Months"}:::check
    C2 -->|"Exceeded (> 200 u/month)"| D1["❌ Disqualified from Gruha Jyothi\nPay 100% of standard charges"]:::result
    C2 -->|"Within Limit (<= Cap)"| G1["3. Prorate Gruha Jyothi Entitlement\nGRJ Eligible Units = Entitlement × (Days / 30)\nChargeable Units = Total Units - GRJ Eligible Units"]:::calc

    C1 -->|"No (Regular Tariff)"| R1["Standard BESCOM LT-1 / LT-2\nChargeable Units = Total Consumption"]:::calc

    %% Billing Components
    G1 --> B1["4. Calculate Gross Charges (Sub-Total 1)\n• Fixed Charges: (Sanctioned Load × Months) × ₹150\n• Energy Charges: Total Units × ₹5.80\n• FPPCA: Total Units × ₹0.38\n• P&G Surcharge: Total Units × ₹0.35\n• Tax: 9% on Energy Charge"]:::calc
    D1 --> B1
    R1 --> B1

    B1 --> B2["5. Calculate Gruha Jyothi Subsidy (Sub-Total 2)\n• Fixed Charge Subsidy (100%)\n• Energy Subsidy: GRJ Eligible Units × ₹5.80\n• FPPCA Subsidy on eligible units\n• P&G Subsidy on eligible units\n• 9% Tax Subsidy"]:::calc

    B2 --> B3["6. Net Payable Arithmetic\n• Net After Subsidy = Sub-total 1 - Sub-total 2\n• Add Penalties (MD / Excess Load / Interest)\n• Subtract Credits\n• Apply FY25 True-Up Adjustments"]:::calc

    B3 --> Final(["🏁 Final Net Payable Bill (e.g. ₹335.00 or ₹730.00)"]):::result
```

---

## 🛠️ Prerequisites & Installation

Before running the project, make sure you have the following installed on your computer:

### 1. System Requirements
* **Python**: Version **3.10**, **3.11**, **3.12**, or **3.13** ([Download Python](https://www.python.org/downloads/))
* **Git**: Optional, for cloning the repository ([Download Git](https://git-scm.com/))
* **Operating System**: Windows, macOS, or Linux

### 2. API Keys & Accounts (Free)
1. **Telegram Bot Token**:
   * Open Telegram and search for `@BotFather`.
   * Send `/newbot`, choose a name and username, and copy your API Token.
2. **AI Vision OCR Key (100% Free)**:
   * Get a free Google Gemini API key from [Google AI Studio](https://aistudio.google.com/).
   * *(Optional alternatives: OpenAI API key or Anthropic Claude API key).*
3. **MongoDB Atlas Database (Free Tier)**:
   * Create a free cluster on [MongoDB Atlas](https://www.mongodb.com/atlas).
   * Get your connection string (e.g. `mongodb+srv://user:password@cluster...`).
   * *If left blank, SmartBill automatically runs on local SQLite without any setup!*

---

## 📦 Step-by-Step Installation

### Step 1: Clone or Open the Project
```bash
git clone https://github.com/your-username/smartbill-bot.git
cd smartbill-bot
```

### Step 2: Install Python Dependencies
```bash
pip install -r requirements.txt
```

The core libraries installed:
* `python-telegram-bot` (Telegram Bot Framework)
* `requests` & `Pillow` (HTTP communication & image preprocessing)
* `pandas` & `scikit-learn` & `joblib` (AI bill prediction & history analysis)
* `pymongo` & `dnspython` (MongoDB Atlas cloud database connection)

### Step 3: Configure Environment Variables
Create a `.env` file in the root folder or edit [config.py](file:///c:/Users/SUDEE/Desktop/Ai%20bot/config.py):

```env
# Telegram Bot Token (from @BotFather)
TELEGRAM_BOT_TOKEN="your_telegram_bot_token"

# Vision OCR API Key (Free Gemini API Key)
GEMINI_API_KEY="your_gemini_api_key"

# Database: MongoDB Atlas (Optional, falls back to SQLite)
MONGODB_URI="mongodb+srv://<username>:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority"
MONGODB_DB_NAME="smartbill_db"
```

---

## 🚀 Running the Bot

Start the bot with:

```bash
python bot.py
```

* Once started, open Telegram, find your bot, and send `/start` or tap **⚡ Start Again**.
* The interactive keyboard will appear with all features ready to use!

---

## 📱 Bot Features & Menu Options

| Button / Command | Description |
| :--- | :--- |
| **📷 Read Meter** | Send a photo of your meter or printed paper bill slip. Auto-detects readings and outputs an 8-section breakdown. |
| **✏️ Enter Current Reading** | Type your meter reading manually without uploading a photo. |
| **💰 Estimate Bill** | Calculate your estimated bill using readings, previous bills, or home appliances. |
| **🏠 Add Appliances** | Interactive appliance picker (LED bulbs, ACs, Geysers, Fridge). Tells you which appliance consumes the most power. |
| **🔮 Predict Next Bill** | Machine Learning predictor forecasting your upcoming month's electricity units and cost. |
| **📊 My History** | View your past bills, consumption, and visual text-based trend charts. |
| **🗑️ Delete History** | Button to delete your saved bills history with a safety confirmation prompt. |
| **⚡ Gruha Jyothi** | Configure your entitlement units (e.g. 51 free units, 200 free units, or regular tariff). |
| **💾 Save Bill** | Manually save or auto-save your latest calculated bill to MongoDB. |
| **🛑 End Chat** | Closes active chat session safely and presents a restart button. |

---

## 🧪 Automated Test Suite

SmartBill includes comprehensive automated tests covering OCR fallback, tariff math, multi-month Gruha Jyothi prorating, machine learning training, and database persistence.

To run the complete test suite:

```bash
python test_suite.py
```

```text
Ran 36 tests in 3.458s

OK
```
All **36 tests pass 100%**!
