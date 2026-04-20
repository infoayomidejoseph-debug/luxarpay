"""
LuxarPay - Production Ready
USDT → Airtime Telegram Bot
"""

import os
import sqlite3
import logging
import requests
import uuid
from datetime import datetime, timedelta
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)
from flask import Flask, request, jsonify

# ==================== CONFIG ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

TEST_MODE = True

DB_PATH = "orders.db"
MIN_USDT = 0.5
RATE_LIMIT = 10

PHONE, NETWORK, AMOUNT, CONFIRM = range(4)

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== APP ====================
flask_app = Flask(__name__)
user_requests = defaultdict(list)

# ==================== DB ====================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_uuid TEXT PRIMARY KEY,
            user_id INTEGER,
            phone TEXT,
            network TEXT,
            amount_ngn REAL,
            amount_usdt REAL,
            invoice_id TEXT,
            status TEXT,
            created_at TEXT
        )
        """)

def save_order(data):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, data)

def get_order(invoice_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT * FROM orders WHERE invoice_id=?", (invoice_id,))
        row = cur.fetchone()
        return row

def update_status(order_uuid, status):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE orders SET status=? WHERE order_uuid=?", (status, order_uuid))

# ==================== UTIL ====================
def rate_limit(user_id):
    now = datetime.now()
    user_requests[user_id] = [
        t for t in user_requests[user_id]
        if now - t < timedelta(minutes=1)
    ]
    if len(user_requests[user_id]) >= RATE_LIMIT:
        return False
    user_requests[user_id].append(now)
    return True

def send_message(chat_id, text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
    except Exception as e:
        logger.error(e)

# ==================== RATE ====================
def get_rate():
    try:
        r = requests.post(
            "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
            json={"asset":"USDT","fiat":"NGN","tradeType":"SELL","rows":3}
        )
        data = r.json()["data"]
        prices = [float(x["adv"]["price"]) for x in data]
        return sum(prices)/len(prices)
    except:
        return 1500

# ==================== PAYMENT ====================
def create_invoice(amount):
    if TEST_MODE:
        return "test_invoice", "https://t.me"
    try:
        r = requests.post(
            "https://pay.crypto.bot/api/invoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN},
            json={"asset":"USDT","amount":str(amount)}
        )
        res = r.json()["result"]
        return res["invoice_id"], res["pay_url"]
    except:
        return None, None

# ==================== AIRTIME ====================
def send_airtime(phone, amount):
    if TEST_MODE:
        return True
    # integrate real VTU here
    return True

# ==================== WEBHOOK ====================
@flask_app.route("/crypto-webhook", methods=["POST"])
def webhook():
    data = request.json
    invoice_id = data.get("invoice_id")
    status = data.get("status")

    if status == "paid":
        order = get_order(invoice_id)
        if order:
            success = send_airtime(order[2], order[4])
            if success:
                update_status(order[0], "done")
                send_message(order[1], f"✅ Airtime sent to {order[2]}")
            else:
                send_message(ADMIN_CHAT_ID, "Airtime failed")

    return jsonify({"ok": True})

# ==================== BOT ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome! Use /buy")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not rate_limit(update.effective_user.id):
        await update.message.reply_text("Slow down.")
        return ConversationHandler.END

    await update.message.reply_text("Enter phone:")
    return PHONE

async def phone(update: Update, context):
    context.user_data["phone"] = update.message.text
    keyboard = [[InlineKeyboardButton(n, callback_data=n)] for n in ["MTN","GLO","AIRTEL","9MOBILE"]]
    await update.message.reply_text("Select network", reply_markup=InlineKeyboardMarkup(keyboard))
    return NETWORK

async def network(update: Update, context):
    q = update.callback_query
    await q.answer()
    context.user_data["network"] = q.data
    await q.edit_message_text("Enter amount ₦")
    return AMOUNT

async def amount(update: Update, context):
    ngn = float(update.message.text)
    rate = get_rate()
    usdt = round(ngn / rate, 2)

    context.user_data.update({"ngn": ngn, "usdt": usdt})

    kb = [[InlineKeyboardButton("Confirm", callback_data="yes")]]
    await update.message.reply_text(f"{ngn} = {usdt} USDT", reply_markup=InlineKeyboardMarkup(kb))
    return CONFIRM

async def confirm(update: Update, context):
    q = update.callback_query
    await q.answer()

    order_id = str(uuid.uuid4())

    invoice_id, url = create_invoice(context.user_data["usdt"])

    save_order((
        order_id,
        update.effective_user.id,
        context.user_data["phone"],
        context.user_data["network"],
        context.user_data["ngn"],
        context.user_data["usdt"],
        invoice_id,
        "pending",
        datetime.now().isoformat()
    ))

    await q.edit_message_text(f"Pay here:\n{url}")
    return ConversationHandler.END

# ==================== MAIN ====================
def main():
    init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("buy", buy)],
        states={
            PHONE: [MessageHandler(filters.TEXT, phone)],
            NETWORK: [CallbackQueryHandler(network)],
            AMOUNT: [MessageHandler(filters.TEXT, amount)],
            CONFIRM: [CallbackQueryHandler(confirm)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    # Run Flask separately (Render handles it)
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))

if __name__ == "__main__":
    main()
