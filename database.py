"""
Database Storage Manager for SmartBill.
Connects to MongoDB (Atlas or local) with automatic fallback to SQLite.
Persists:
- User baseline meter readings
- Calculated bills history
- User appliance configurations
"""
import os
import sqlite3
import logging
from datetime import datetime
from config import MONGODB_URI, MONGODB_DB_NAME, DEFAULT_ENTITLEMENT_UNITS

log = logging.getLogger(__name__)

# Fallback local sqlite DB path
SQLITE_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "smartbill.db")


class DatabaseManager:
    def __init__(self):
        self.use_mongo = False
        self.mongo_client = None
        self.db = None
        self._init_connection()

    def _init_connection(self):
        if MONGODB_URI and MONGODB_URI.strip() and MONGODB_URI != "PUT_YOUR_MONGODB_URI_HERE":
            try:
                import pymongo
                self.mongo_client = pymongo.MongoClient(
                    MONGODB_URI,
                    serverSelectionTimeoutMS=4000,
                )
                # Verify connection
                self.mongo_client.admin.command("ping")
                self.db = self.mongo_client[MONGODB_DB_NAME]
                self.use_mongo = True
                log.info(f"✅ Connected to MongoDB successfully (database: {MONGODB_DB_NAME})")
            except Exception as e:
                log.warning(f"⚠️ MongoDB connection failed ({e}). Falling back to local storage.")
                self.use_mongo = False

        # SQLite fallback setup (always ensure schema exists)
        self._init_sqlite()

    def _init_sqlite(self):
        os.makedirs(os.path.dirname(SQLITE_DB_PATH), exist_ok=True)
        with sqlite3.connect(SQLITE_DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id TEXT PRIMARY KEY,
                    last_reading REAL,
                    gruha_jyothi INTEGER DEFAULT NULL,
                    entitlement_units REAL DEFAULT 51.0,
                    updated_at TEXT
                )
            """)
            try:
                cur.execute("ALTER TABLE users ADD COLUMN gruha_jyothi INTEGER DEFAULT NULL")
            except Exception:
                pass
            try:
                cur.execute("ALTER TABLE users ADD COLUMN entitlement_units REAL DEFAULT 51.0")
            except Exception:
                pass
            conn.commit()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT,
                    current_reading REAL,
                    previous_reading REAL,
                    units REAL,
                    energy_charge REAL,
                    fixed_charge REAL,
                    fppca REAL,
                    tax REAL,
                    total REAL,
                    source TEXT,
                    created_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS appliances (
                    chat_id TEXT PRIMARY KEY,
                    counts_json TEXT,
                    hours_json TEXT,
                    updated_at TEXT
                )
            """)
            conn.commit()
        log.info("Initialized local database storage.")

    def is_mongodb_connected(self) -> bool:
        return self.use_mongo

    # --- User Baseline Reading ---

    def get_last_reading(self, chat_id: str | int) -> float | None:
        chat_id = str(chat_id)
        if self.use_mongo:
            try:
                doc = self.db.users.find_one({"chat_id": chat_id})
                if doc and "last_reading" in doc:
                    return float(doc["last_reading"])
            except Exception as e:
                log.error(f"MongoDB read error: {e}")

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("SELECT last_reading FROM users WHERE chat_id = ?", (chat_id,))
                row = cur.fetchone()
                if row and row[0] is not None:
                    return float(row[0])
        except Exception as e:
            log.error(f"SQLite read error: {e}")
        return None

    def set_last_reading(self, chat_id: str | int, value: float):
        chat_id = str(chat_id)
        now_str = datetime.now().isoformat()
        if self.use_mongo:
            try:
                self.db.users.update_one(
                    {"chat_id": chat_id},
                    {"$set": {"last_reading": float(value), "updated_at": now_str}},
                    upsert=True,
                )
                return
            except Exception as e:
                log.error(f"MongoDB write error: {e}")

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO users (chat_id, last_reading, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                    last_reading=excluded.last_reading,
                    updated_at=excluded.updated_at
                """, (chat_id, float(value), now_str))
                conn.commit()
        except Exception as e:
            log.error(f"SQLite write error: {e}")

    # --- Gruha Jyothi Scheme Status & Entitlement ---

    def get_gruha_jyothi_info(self, chat_id: str | int) -> dict:
        """Returns {'enrolled': bool|None, 'entitlement': float}."""
        chat_id = str(chat_id)
        enrolled = None
        entitlement = DEFAULT_ENTITLEMENT_UNITS
        if self.use_mongo:
            try:
                doc = self.db.users.find_one({"chat_id": chat_id})
                if doc:
                    if "gruha_jyothi" in doc and doc["gruha_jyothi"] is not None:
                        enrolled = bool(doc["gruha_jyothi"])
                    if "entitlement_units" in doc and doc["entitlement_units"] is not None:
                        entitlement = float(doc["entitlement_units"])
                    return {"enrolled": enrolled, "entitlement": entitlement}
            except Exception as e:
                log.error(f"MongoDB get_gruha_jyothi_info error: {e}")

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("SELECT gruha_jyothi, entitlement_units FROM users WHERE chat_id = ?", (chat_id,))
                row = cur.fetchone()
                if row:
                    if row[0] is not None:
                        enrolled = bool(row[0])
                    if row[1] is not None:
                        entitlement = float(row[1])
        except Exception as e:
            log.error(f"SQLite get_gruha_jyothi_info error: {e}")
        return {"enrolled": enrolled, "entitlement": entitlement}

    def get_gruha_jyothi(self, chat_id: str | int) -> bool | None:
        """Returns True if enrolled, False if not enrolled, None if not yet asked."""
        return self.get_gruha_jyothi_info(chat_id)["enrolled"]

    def get_entitlement_units(self, chat_id: str | int) -> float:
        """Returns the user's Gruha Jyothi entitlement units (default: 200.0 or custom e.g. 51.0)."""
        return self.get_gruha_jyothi_info(chat_id)["entitlement"]

    def set_gruha_jyothi(self, chat_id: str | int, enabled: bool, entitlement: float = None):
        chat_id = str(chat_id)
        now_str = datetime.now().isoformat()
        update_data = {"gruha_jyothi": bool(enabled), "updated_at": now_str}
        if entitlement is not None:
            update_data["entitlement_units"] = float(entitlement)

        if self.use_mongo:
            try:
                self.db.users.update_one(
                    {"chat_id": chat_id},
                    {"$set": update_data},
                    upsert=True,
                )
            except Exception as e:
                log.error(f"MongoDB set_gruha_jyothi error: {e}")

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                if entitlement is not None:
                    cur.execute("""
                        INSERT INTO users (chat_id, gruha_jyothi, entitlement_units, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET
                        gruha_jyothi=excluded.gruha_jyothi,
                        entitlement_units=excluded.entitlement_units,
                        updated_at=excluded.updated_at
                    """, (chat_id, 1 if enabled else 0, float(entitlement), now_str))
                else:
                    cur.execute("""
                        INSERT INTO users (chat_id, gruha_jyothi, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET
                        gruha_jyothi=excluded.gruha_jyothi,
                        updated_at=excluded.updated_at
                    """, (chat_id, 1 if enabled else 0, now_str))
                conn.commit()
        except Exception as e:
            log.error(f"SQLite set_gruha_jyothi error: {e}")

    def set_entitlement_units(self, chat_id: str | int, entitlement: float):
        chat_id = str(chat_id)
        now_str = datetime.now().isoformat()
        entitlement = float(entitlement)

        if self.use_mongo:
            try:
                self.db.users.update_one(
                    {"chat_id": chat_id},
                    {"$set": {"entitlement_units": entitlement, "gruha_jyothi": True, "updated_at": now_str}},
                    upsert=True,
                )
            except Exception as e:
                log.error(f"MongoDB set_entitlement_units error: {e}")

        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO users (chat_id, gruha_jyothi, entitlement_units, updated_at)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                    gruha_jyothi=1,
                    entitlement_units=excluded.entitlement_units,
                    updated_at=excluded.updated_at
                """, (chat_id, entitlement, now_str))
                conn.commit()
        except Exception as e:
            log.error(f"SQLite set_entitlement_units error: {e}")

    # --- Bills History ---

    def save_bill(self, chat_id: str | int, current_reading: float, previous_reading: float, bill: dict, source: str = "manual"):
        chat_id = str(chat_id)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        doc = {
            "chat_id": chat_id,
            "current_reading": float(current_reading),
            "previous_reading": float(previous_reading),
            "units": float(bill["units"]),
            "energy_charge": float(bill["energy_charge"]),
            "fixed_charge": float(bill["fixed_charge"]),
            "fppca": float(bill["fppca"]),
            "tax": float(bill["tax"]),
            "total": float(bill["total"]),
            "source": source,
            "created_at": now_str,
        }

        if self.use_mongo:
            try:
                self.db.bills.insert_one(doc)
                return
            except Exception as e:
                log.error(f"MongoDB save_bill error: {e}")

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO bills (
                        chat_id, current_reading, previous_reading, units,
                        energy_charge, fixed_charge, fppca, tax, total, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    chat_id, doc["current_reading"], doc["previous_reading"],
                    doc["units"], doc["energy_charge"], doc["fixed_charge"],
                    doc["fppca"], doc["tax"], doc["total"], doc["source"], doc["created_at"]
                ))
                conn.commit()
        except Exception as e:
            log.error(f"SQLite save_bill error: {e}")

    def get_user_bills(self, chat_id: str | int, limit: int = 6) -> list[dict]:
        chat_id = str(chat_id)
        if self.use_mongo:
            try:
                cursor = self.db.bills.find({"chat_id": chat_id}).sort("created_at", -1).limit(limit)
                bills = list(cursor)
                bills.reverse()
                return bills
            except Exception as e:
                log.error(f"MongoDB get_user_bills error: {e}")

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute("""
                    SELECT current_reading, previous_reading, units, total, created_at
                    FROM bills WHERE chat_id = ?
                    ORDER BY id DESC LIMIT ?
                """, (chat_id, limit))
                rows = [dict(r) for r in cur.fetchall()]
                rows.reverse()
                return rows
        except Exception as e:
            log.error(f"SQLite get_user_bills error: {e}")
        return []


    # --- Appliance Configurations ---

    def save_appliances(self, chat_id: str | int, counts: dict, hours: dict):
        import json
        chat_id = str(chat_id)
        now_str = datetime.now().isoformat()
        if self.use_mongo:
            try:
                self.db.appliances.update_one(
                    {"chat_id": chat_id},
                    {"$set": {"counts": counts, "hours": hours, "updated_at": now_str}},
                    upsert=True,
                )
                return
            except Exception as e:
                log.error(f"MongoDB save_appliances error: {e}")

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO appliances (chat_id, counts_json, hours_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                    counts_json=excluded.counts_json,
                    hours_json=excluded.hours_json,
                    updated_at=excluded.updated_at
                """, (chat_id, json.dumps(counts), json.dumps(hours), now_str))
                conn.commit()
        except Exception as e:
            log.error(f"SQLite save_appliances error: {e}")

    def get_appliances(self, chat_id: str | int) -> tuple[dict, dict] | None:
        import json
        chat_id = str(chat_id)
        if self.use_mongo:
            try:
                doc = self.db.appliances.find_one({"chat_id": chat_id})
                if doc:
                    return doc.get("counts", {}), doc.get("hours", {})
            except Exception as e:
                log.error(f"MongoDB get_appliances error: {e}")

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("SELECT counts_json, hours_json FROM appliances WHERE chat_id = ?", (chat_id,))
                row = cur.fetchone()
                if row:
                    counts = json.loads(row[0]) if row[0] else {}
                    hours = json.loads(row[1]) if row[1] else {}
                    return counts, hours
        except Exception as e:
            log.error(f"SQLite get_appliances error: {e}")
        return None

    def delete_user_bills(self, chat_id: str | int) -> bool:
        """Deletes all saved bills history for the specified user."""
        chat_id = str(chat_id)
        success = True
        if self.use_mongo:
            try:
                self.db.bills.delete_many({"chat_id": chat_id})
            except Exception as e:
                log.error(f"MongoDB delete_user_bills error: {e}")
                success = False

        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM bills WHERE chat_id = ?", (chat_id,))
                conn.commit()
        except Exception as e:
            log.error(f"SQLite delete_user_bills error: {e}")
            success = False
        return success

    def clear_user_data(self, chat_id: str | int) -> bool:
        """Deletes all user baseline readings, bills history, and appliances for privacy/reset."""
        chat_id = str(chat_id)
        success = True
        if self.use_mongo:
            try:
                self.db.users.delete_many({"chat_id": chat_id})
                self.db.bills.delete_many({"chat_id": chat_id})
                self.db.appliances.delete_many({"chat_id": chat_id})
            except Exception as e:
                log.error(f"MongoDB clear_user_data error: {e}")
                success = False

        # SQLite
        try:
            with sqlite3.connect(SQLITE_DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))
                cur.execute("DELETE FROM bills WHERE chat_id = ?", (chat_id,))
                cur.execute("DELETE FROM appliances WHERE chat_id = ?", (chat_id,))
                conn.commit()
        except Exception as e:
            log.error(f"SQLite clear_user_data error: {e}")
            success = False
        return success


# Global singleton instance
db = DatabaseManager()
