# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# Validate required environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is required")

DB_PATH = os.getenv("DB_PATH", "glasses.db")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.isdigit())
PAYMENT_QR_FILE = os.getenv("PAYMENT_QR_FILE", "qr_code.jpg")  # Default to qr_code.jpg in project directory
PAYMENT_UPI_ID = os.getenv("PAYMENT_UPI_ID", "your-upi@bank")
TOL_MM = float(os.getenv("TOL_MM", 0.5))
FUZZY_THRESHOLD = int(os.getenv("FUZZY_THRESHOLD", 70))

PLANS = {
    "free": (
        0,
        "Free Plan: Default plan with up to 10 queries per day and basic compatibility checks."
    ),
    "pro": (
        99,
        "Pro Plan: Unlimited queries, access to verified compatible devices list, "
        "batch compatibility checks, dimension-based searches, and priority support."
    )
}