# zenorc.py :: Background Email Payment Processor
#
# This module handles the core logic for the Zenorc system. It is designed
# to be imported and started by a separate application runner (like zen.py).
#
# It performs the following tasks in background threads:
#   1. Polls a GMail inbox for specific payment notification emails.
#   2. Parses these emails to extract a transaction ID.
#   3. Logs the transaction details to a Google Sheet to prevent duplicates.
#   4. Adds the transaction to an in-memory queue.
#   5. A separate processor thread dequeues transactions and sends an MQTT
#      message to signal that a payment has been received.

from __future__ import annotations

import email
import imaplib
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import gspread
import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from oauth2client.service_account import ServiceAccountCredentials

# ╭─ CONFIGURATION ────────────────────────────────────────────────╮
# Load environment variables from a .env file if it exists.
load_dotenv()

# --- Email Configuration ---
EMAIL_ID = os.getenv("EMAIL_ID")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# --- MQTT Broker Configuration ---
MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "Zenorc")
CLIENT_ID = f"zenorc-{uuid.uuid4().hex[:8]}"

# --- Google Sheets Configuration ---
GSHEET_URL = os.getenv("GSHEET_URL")
GSHEET_CREDS_PATH = os.getenv("GSHEET_CREDS_PATH", "/etc/secrets/Zenorc.json")

# --- Processor Logic ---
# A tuple of lowercase strings to identify relevant payment emails.
SEARCH_STRINGS = tuple(s.strip().lower() for s in os.getenv("SEARCH_STRINGS", "₹5,Rs 5,INR 5").split(","))
# Minimum time in seconds between processing queued items.
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "40"))
# ╰────────────────────────────────────────────────────────────────╯


# ╭─ GLOBAL STATE ─────────────────────────────────────────────────╮
# This section manages the shared state of the application. Care is taken
# to handle thread-safety where necessary.

# A thread-safe lock for IMAP operations to prevent simultaneous access.
imap_lock = threading.Lock()

# A set of email UIDs that have been seen in the current session.
# This is an in-memory cache to avoid reprocessing emails immediately.
seen_uids: set[bytes] = set()

# A set of transaction IDs that have been successfully logged to Google Sheets.
# This serves as the persistent record of processed transactions.
# It is bootstrapped from the Google Sheet on startup.
seen_txn_ids: set[str] = set()

# A thread-safe double-ended queue holding transaction IDs that are
# waiting to be processed by the MQTT publisher.
queue: deque[str] = deque()

# A dictionary to track the status of transactions ("Queued", "Processing", etc.).
status: dict[str, str] = {}

# Timestamp of the last time a transaction was processed. Used for cooldown logic.
last_processed: float = 0.0
# ╰────────────────────────────────────────────────────────────────╯


# ╭─ UTILITIES ────────────────────────────────────────────────────╮
def log(msg: str, level: str = "INFO"):
    """Prints a formatted log message to stdout."""
    print(f"[{level}] {msg}", flush=True)

def tz_mumbai() -> ZoneInfo:
    """Returns the Asia/Mumbai timezone, falling back to UTC if not found."""
    try:
        return ZoneInfo("Asia/Mumbai")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
# ╰────────────────────────────────────────────────────────────────╯


# ╭─ GOOGLE SHEETS INTEGRATION ────────────────────────────────────╮
def _sheet() -> gspread.Worksheet:
    """
    Connects to the Google Sheets API and returns the worksheet object.

    Raises:
        RuntimeError: If GSHEET_URL environment variable is not set.
        FileNotFoundError: If the credentials file is not found.
    """
    if not GSHEET_URL:
        raise RuntimeError("GSHEET_URL env var missing")
    if not os.path.isfile(GSHEET_CREDS_PATH):
        raise FileNotFoundError(f"Credentials not found: {GSHEET_CREDS_PATH}")

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GSHEET_CREDS_PATH, scope)
    client = gspread.authorize(creds)
    return client.open_by_url(GSHEET_URL).sheet1

def _bootstrap_txns() -> set[str]:
    """
    Loads existing transaction IDs from the Google Sheet to prevent reprocessing.
    This is called once on startup.
    """
    try:
        log("Bootstrapping transaction history from Google Sheets...")
        processed_txns = set(_sheet().col_values(1))
        log(f"Loaded {len(processed_txns)} existing transaction IDs.")
        return processed_txns
    except Exception as e:
        log(f"Sheets bootstrap failed: {e}", "WARN")
        return set()

def log_payment(txn_id: str, amount: str = "5"):
    """
    Appends a new payment record to the Google Sheet.

    Args:
        txn_id: The unique transaction identifier.
        amount: The amount of the transaction (defaults to "5").
    """
    try:
        sheet = _sheet()
        now = datetime.now(tz_mumbai())
        row_data = [txn_id, amount, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")]
        sheet.append_row(row_data)
        seen_txn_ids.add(txn_id)
        log(f"Logged {txn_id} to Google Sheets.")
    except Exception as e:
        log(f"Failed to log payment to Google Sheets: {e}", "ERROR")
# ╰────────────────────────────────────────────────────────────────╯


# ╭─ MQTT PUBLISHER ───────────────────────────────────────────────╮
def send_mqtt(max_retries: int = 3, retry_delay: int = 5) -> bool:
    """
    Connects to the MQTT broker and publishes a "paid" message.

    Args:
        max_retries: The maximum number of times to retry connecting.
        retry_delay: The delay in seconds between retries.

    Returns:
        True if the message was published successfully, False otherwise.
    """
    for attempt in range(1, max_retries + 1):
        try:
            connected = threading.Event()
            client = mqtt.Client(
                client_id=CLIENT_ID,
                protocol=mqtt.MQTTv311,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2
            )
            if MQTT_USERNAME:
                client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

            def on_connect(c, *_):
                log("↳ MQTT connected successfully.")
                connected.set()

            client.on_connect = on_connect
            client.tls_set()
            client.connect(MQTT_BROKER, MQTT_PORT)
            client.loop_start()

            if not connected.wait(timeout=10):
                raise TimeoutError("MQTT connection timed out")

            rc, mid = client.publish(MQTT_TOPIC, "paid", qos=1)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError(f"Publish failed with code: {mqtt.error_string(rc)}")
            
            log(f"↳ MQTT publish successful (mid={mid})")
            return True
        except Exception as e:
            log(f"MQTT attempt {attempt} failed: {e}", "WARN")
            if attempt < max_retries:
                time.sleep(retry_delay)
        finally:
            if 'client' in locals() and client.is_connected():
                client.loop_stop()
                client.disconnect()
                log("↳ MQTT disconnected.")
    return False
# ╰────────────────────────────────────────────────────────────────╯


# ╭─ EMAIL PROCESSOR ──────────────────────────────────────────────╮
def _imap_login() -> imaplib.IMAP4_SSL:
    """Establishes a connection to the IMAP server."""
    if not EMAIL_ID or not EMAIL_PASSWORD:
        raise RuntimeError("EMAIL_ID or EMAIL_PASSWORD missing from environment.")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(EMAIL_ID, EMAIL_PASSWORD)
    return imap

def _extract_txn_id(body: str) -> str:
    """
    Extracts a transaction ID from the email body using regex patterns.
    Falls back to a timestamp-based ID if no pattern matches.
    """
    patterns = [
        r"Reference\s*(?:No\.?|number)?\s*[:\-]?\s*(\d{8,})",
        r"transaction reference number\s*(?:is)?\s*[:\-]?\s*(\d{8,})",
    ]
    for pat in patterns:
        match = re.search(pat, body, re.IGNORECASE)
        if match:
            return match.group(1)
    # Fallback for emails where the regex fails
    return f"TXN{int(time.time())}"

_AMT_5_RE = re.compile(r"(?:₹|rs\.?|inr)\s*[, ]*\s*5(?:[.,]00)?\b", re.IGNORECASE)

def _is_valid_payment(body_lc: str) -> bool:
    """
    Checks if an email body indicates a valid incoming ₹5 payment.
    """
    is_credit = "credited" in body_lc and "debited" not in body_lc
    is_correct_amount = bool(_AMT_5_RE.search(body_lc))
    return is_credit and is_correct_amount

def poll_email() -> Optional[str]:
    """
    Polls the email inbox for new, unseen payment notifications.

    If a valid, new payment email is found, it extracts the transaction ID,
    marks the email as read, and returns the ID.

    Returns:
        A new transaction ID as a string, or None if no new payments are found.
    """
    with imap_lock:
        try:
            with _imap_login() as mail:
                mail.select("inbox")
                _, data = mail.search(None, "(UNSEEN)")
                # Process the most recent 30 unseen emails
                for uid in reversed((data[0] or b"").split()[-30:]):
                    if uid in seen_uids:
                        continue

                    _, msg_data = mail.fetch(uid, "(RFC822)")
                    msg = email.message_from_bytes(msg_data[0][1])

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                    else:
                        body = msg.get_payload(decode=True).decode(errors="ignore")

                    if not body:
                        continue

                    if not _is_valid_payment(body.lower()):
                        continue

                    txn_id = _extract_txn_id(body)
                    if txn_id in seen_txn_ids:
                        continue

                    # Success: A new, valid transaction was found.
                    seen_uids.add(uid)
                    mail.store(uid, "+FLAGS", "\\Seen")  # Mark email as read
                    log(f"Found new payment. UID: {uid.decode()} -> TXN_ID: {txn_id}")
                    return txn_id
        except Exception as e:
            log(f"Error polling email: {e}", "ERROR")

    return None
# ╰────────────────────────────────────────────────────────────────╯


# ╭─ BACKGROUND WORKER THREADS ────────────────────────────────────╮
def processor():
    """
    The processor thread function.
    
    This loop continuously checks the `queue` for new transaction IDs. When an
    item is found and the cooldown period has passed, it processes the item
    by sending an MQTT message.
    """
    global last_processed
    while True:
        if not queue:
            time.sleep(1)
            continue

        now = time.time()
        if now - last_processed < COOLDOWN_SECONDS:
            remain = int(COOLDOWN_SECONDS - (now - last_processed))
            log(f"Cooldown active. Waiting for {remain}s...")
            time.sleep(1)
            continue

        txn_id = queue.popleft()
        status[txn_id] = "Processing"
        log(f"⚙️  Processing {txn_id}...")
        
        ok = send_mqtt()
        status[txn_id] = "Completed" if ok else "Failed"
        log(("✔" if ok else "❌") + f"  Completed processing {txn_id}")
        last_processed = time.time()

def main_loop():
    """
    The main polling loop function.
    
    This loop continuously calls `poll_email` to check for new payments. If a
    new transaction is found, it logs the payment to Google Sheets and adds
    it to the processing queue.
    """
    log("Scanning inbox for payments...")
    while True:
        txn_id = poll_email()
        if txn_id and txn_id not in status:
            status[txn_id] = "Queued"
            log_payment(txn_id)  # Log to Sheets first
            queue.append(txn_id)
            log(f"📩 Queued {txn_id}. Queue size: {len(queue)}")
        
        # Wait before the next poll
        time.sleep(5)
# ╰────────────────────────────────────────────────────────────────╯


# ╭─ SERVICE ENTRYPOINT ───────────────────────────────────────────╮
def start():
    """
    Initializes and starts the Zenorc background processing threads.
    
    This function is intended to be called by an external application runner
    (e.g., zen.py). It bootstraps the transaction history from Google Sheets
    and then starts the main email polling loop and the transaction processor.
    """
    global seen_txn_ids
    seen_txn_ids = _bootstrap_txns()
    
    log("Starting Zenorc background threads...")
    threading.Thread(target=processor, daemon=True).start()
    threading.Thread(target=main_loop, daemon=True).start()
    log("Zenorc background threads started.")
# ╰────────────────────────────────────────────────────────────────╯
