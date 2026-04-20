 
"""
LuxarPay Telegram Bot - USDT to Airtime
PRODUCTION READY - Copy this entire file
"""

import os
import json
import sqlite3
import logging
import requests
import hashlib
import hmac
import uuid
import asyncio
from datetime import datetime, timedelta
from threading import Thread
from time import sleep
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    ConversationHandler, MessageHandler, filters, ContextTypes
)
from flask import Flask, request, jsonify

# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
VTU_API_KEY = os.getenv("VTU_API_KEY")
VTU_USERNAME = os.getenv("VTU_USERNAME")
VTU_PASSWORD = os.getenv("VTU_PASSWORD")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# Constants
MIN_USDT = 0.5
RATE_LOCK_SECONDS = 300
MAX_RETRIES = 3
RATE_LIMIT_REQUESTS = 10
DB_PATH = "orders.db"

# Conversation States
PHONE, NETWORK, AMOUNT_NGN, CONFIRMATION = range(1, 5)

# Setup Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Rate Limiting
user_requests = defaultdict(list)

def rate_limit(user_id):
    now = datetime.now()
    user_requests[user_id] = [t for t in user_requests[user_id] if now - t < timedelta(minutes=1)]
    if len(user_requests[user_id]) >= RATE_LIMIT_REQUESTS:
        return False
    user_requests[user_id].append(now)
    return True

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_uuid TEXT UNIQUE,
            user_id INTEGER,
            phone TEXT,
            network TEXT,
            amount_ngn REAL,
            amount_usdt REAL,
            locked_rate REAL,
            invoice_id TEXT,
            status TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TIMESTAMP,
            rate_expires_at TIMESTAMP,
            completed_at TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS failed_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            error_message TEXT,
            provider_used TEXT,
            created_at TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date DATE PRIMARY KEY,
            total_orders INTEGER DEFAULT 0,
            total_volume_usdt REAL DEFAULT 0,
            failed_orders INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def save_order(user_id, phone, network, amount_ngn, amount_usdt, locked_rate, invoice_id):
    order_uuid = str(uuid.uuid4())
    expires_at = datetime.now() + timedelta(seconds=RATE_LOCK_SECONDS)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO orders (order_uuid, user_id, phone, network, amount_ngn, amount_usdt, locked_rate, invoice_id, status, created_at, rate_expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (order_uuid, user_id, phone, network, amount_ngn, amount_usdt, locked_rate, invoice_id, "pending", datetime.now(), expires_at))
    conn.commit()
    conn.close()
    return order_uuid

def update_order_status(order_uuid, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE orders SET status = ?, completed_at = ? WHERE order_uuid = ?", (status, datetime.now() if status == "completed" else None, order_uuid))
    conn.commit()
    conn.close()

def get_order_by_invoice(invoice_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM orders WHERE invoice_id = ?", (invoice_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {
            "order_uuid": row[1],
            "user_id": row[2],
            "phone": row[3],
            "network": row[4],
            "amount_ngn": row[5],
            "amount_usdt": row[6],
            "locked_rate": row[7],
            "status": row[9]
        }
    return None

# ==================== RATE FETCHER ====================
cached_rate = {"rate": None, "timestamp": None}

def get_usdt_ngn_rate():
    global cached_rate
    if cached_rate["timestamp"] and (datetime.now() - cached_rate["timestamp"]).seconds < 1800:
        return cached_rate["rate"]
    try:
        url = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
        payload = {"page": 1, "rows": 5, "payTypes": [], "asset": "USDT", "tradeType": "SELL", "fiat": "NGN"}
        response = requests.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get("data"):
            rates = [float(adv["adv"]["price"]) for adv in data["data"][:5]]
            rate = sum(rates) / len(rates)
            cached_rate = {"rate": rate, "timestamp": datetime.now()}
            return rate
        return cached_rate["rate"] or 1500
    except:
        return cached_rate["rate"] or 1500

# ==================== CRYPTO PAY ====================
def create_invoice(amount_usdt, order_uuid):
    try:
        url = "https://pay.crypto.bot/api/invoice"
        headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN, "Content-Type": "application/json"}
        payload = {"asset": "USDT", "amount": str(amount_usdt), "description": f"LuxarPay Order {order_uuid[:8]}", "expires_in": RATE_LOCK_SECONDS}
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        data = response.json()
        if data.get("ok"):
            invoice = data["result"]
            return invoice["invoice_id"], invoice["pay_url"]
        return None, None
    except:
        return None, None

def verify_webhook_signature(request_data, signature_header):
    try:
        secret = CRYPTO_PAY_TOKEN.split(':')[1] if ':' in CRYPTO_PAY_TOKEN else CRYPTO_PAY_TOKEN
        computed = hmac.new(secret.encode(), request_data, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, signature_header)
    except:
        return False

def send_airtime(phone, network, amount_ngn):
    network_map = {"MTN": "mtn", "GLO": "glo", "AIRTEL": "airtel", "9MOBILE": "9mobile"}
    api_code = network_map.get(network.upper(), network.lower())
    for attempt in range(MAX_RETRIES):
        try:
            url = "https://vtu.ng/api/v1/airtime"
            payload = {"username": VTU_USERNAME, "password": VTU_PASSWORD, "phone": phone, "network": api_code, "amount": amount_ngn}
            response = requests.post(url, json=payload, timeout=15)
            data = response.json()
            if data.get("status") == "success" or data.get("code") == "200":
                return True, "Airtime sent"
            if attempt < MAX_RETRIES - 1:
                sleep(2 ** attempt)
        except:
            if attempt < MAX_RETRIES - 1:
                sleep(2 ** attempt)
    return False, "Delivery failed"

# ==================== FLASK WEBHOOK ====================
flask_app = Flask(__name__)

@flask_app.route("/crypto-pay-webhook", methods=["POST"])
def crypto_pay_webhook():
    try:
        signature = request.headers.get("X-Crypto-Pay-Signature")
        if not verify_webhook_signature(request.get_data(), signature):
            return jsonify({"status": "unauthorized"}), 401
        data = request.json
        invoice_id = data.get("invoice_id")
        status = data.get("status")
        if status == "paid" and invoice_id:
            order = get_order_by_invoice(invoice_id)
            if order and order["status"] == "pending":
                success, message = send_airtime(order["phone"], order["network"], order["amount_ngn"])
                if success:
                    update_order_status(order["order_uuid"], "completed")
                    # Notify user
                    app = Application.builder().token(TELEGRAM_TOKEN).build()
                    asyncio.run(app.bot.send_message(
                        chat_id=order["user_id"],
                        text=f"✅ Airtime Delivered!\n₦{order['amount_ngn']:,.0f} sent to {order['phone']}\nNetwork: {order['network']}\n\nThank you for using LuxarPay! 🚀"
                    ))
                else:
                    update_order_status(order["order_uuid"], "failed")
        return jsonify({"status": "ok"}), 200
    except:
        return jsonify({"status": "error"}), 500

@flask_app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()}), 200

# ==================== TELEGRAM BOT ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌟 *Welcome to LuxarPay!* 🌟\n\nBuy airtime instantly with USDT (TRC-20).\n"
        f"Minimum: {MIN_USDT} USDT\n\nClick /buy to start!\n/rate for exchange rate",
        parse_mode="Markdown"
    )

async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rate = get_usdt_ngn_rate()
    await update.message.reply_text(f"💱 1 USDT = ₦{rate:,.0f}\nMinimum: {MIN_USDT} USDT (₦{rate * MIN_USDT:,.0f})", parse_mode="Markdown")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Enter your phone number (e.g., 08012345678):", parse_mode="Markdown")
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    phone_clean = ''.join(filter(str.isdigit, phone))
    if len(phone_clean) not in [10, 11, 13]:
        await update.message.reply_text("❌ Invalid number. Try again:")
        return PHONE
    context.user_data["phone"] = phone_clean
    keyboard = [[InlineKeyboardButton(net, callback_data=net)] for net in ["MTN", "GLO", "AIRTEL", "9MOBILE"]]
    await update.message.reply_text("Select network:", reply_markup=InlineKeyboardMarkup(keyboard))
    return NETWORK

async def get_network(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["network"] = query.data
    await query.edit_message_text(f"Enter amount in Naira (min: ₦{get_usdt_ngn_rate() * MIN_USDT:,.0f}):")
    return AMOUNT_NGN

async def get_amount_ngn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount_ngn = float(update.message.text.strip().replace(',', ''))
        rate = get_usdt_ngn_rate()
        amount_usdt = round(amount_ngn / rate, 2)
        if amount_usdt < MIN_USDT:
            await update.message.reply_text(f"❌ Too low. Minimum {MIN_USDT} USDT (₦{rate * MIN_USDT:,.0f})")
            return AMOUNT_NGN
        context.user_data["amount_ngn"] = amount_ngn
        context.user_data["amount_usdt"] = amount_usdt
        context.user_data["locked_rate"] = rate
        keyboard = [[InlineKeyboardButton("✅ Confirm", callback_data="confirm")], [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]]
        await update.message.reply_text(f"🔍 Confirm:\nPhone: {context.user_data['phone']}\nNetwork: {context.user_data['network']}\n₦{amount_ngn:,.0f} = {amount_usdt} USDT\nRate locked 5 min", reply_markup=InlineKeyboardMarkup(keyboard))
        return CONFIRMATION
    except:
        await update.message.reply_text("Enter valid number:")
        return AMOUNT_NGN

async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel":
        await query.edit_message_text("Cancelled. /buy to restart")
        return ConversationHandler.END
    order_uuid = save_order(update.effective_user.id, context.user_data["phone"], context.user_data["network"], 
                           context.user_data["amount_ngn"], context.user_data["amount_usdt"], context.user_data["locked_rate"], None)
    invoice_id, pay_url = create_invoice(context.user_data["amount_usdt"], order_uuid)
    if not invoice_id:
        await query.edit_message_text("Payment error. Try again.")
        return ConversationHandler.END
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE orders SET invoice_id = ? WHERE order_uuid = ?", (invoice_id, order_uuid))
    conn.commit()
    conn.close()
    await query.edit_message_text(f"💳 Send {context.user_data['amount_usdt']} USDT to:\n[Pay Now]({pay_url})\n\nOrder ID: {order_uuid[:8]}", parse_mode="Markdown", disable_web_page_preview=True)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

# ==================== MAIN ====================
def main():
    global app
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv_handler = ConversationHandler(entry_points=[CommandHandler("buy", buy)], states={
        PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
        NETWORK: [CallbackQueryHandler(get_network)],
        AMOUNT_NGN: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_amount_ngn)],
        CONFIRMATION: [CallbackQueryHandler(confirm_order)],
    }, fallbacks=[CommandHandler("cancel", cancel)])
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(conv_handler)
    
    def run_flask():
        flask_app.run(host="0.0.0.0", port=5000)
    Thread(target=run_flask, daemon=True).start()
    
    if os.getenv("ENV") == "production":
        port = int(os.environ.get("PORT", 8080))
        app.run_webhook(listen="0.0.0.0", port=port, url_path=TELEGRAM_TOKEN, webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    else:
        app.run_polling()

if __name__ == "__main__":
    main()