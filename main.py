# main.py
import os
import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse
import threading
from collections import Counter
import sys

from flask import Flask
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler
)
from telegram.constants import ParseMode

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

import database as db

# --- Configuration & Setup ---
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN, API_ID, API_HASH = os.getenv("BOT_TOKEN"), os.getenv("API_ID"), os.getenv("API_HASH")

# --- Conversation Handler States ---
CHOOSE_FILE_TYPE, LOGIN_SESSION = range(2)

# --- Flask Web Server ---
app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot is alive!", 200
def run_web_server(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

# --- NEW: Asynchronous post-initialization callback ---
async def post_init_callback(application: Application):
    """
    This function runs after the application is built but before polling starts.
    It's the correct place for async setup tasks.
    """
    logger.info("Running post-initialization checks...")
    try:
        # Test the database connection asynchronously.
        await db.client.admin.command('ping')
        logger.info("MongoDB connection successful.")
    except Exception as e:
        logger.critical(f"CRITICAL: Post-init failed to connect to MongoDB. Bot will not start. Error: {e}")
        # This will stop the bot gracefully if the DB is down.
        application.stop()
        sys.exit(1)

# --- Helper and Scrape Functions (Unchanged from previous correct versions) ---
# All functions like preprocess_url, scrape_images_from_url, etc., are correct and remain here.
# For brevity, they are omitted from this view but are part of the complete file.
def preprocess_url(url: str):
    if not re.match(r'http(s)?://', url): return f'https://{url}'
    return url
def find_url_in_text(text: str):
    if not text: return None
    match = re.search(r'https?://[^\s/$.?#].[^\s]*', text)
    return match.group(0) if match else None
def get_file_extension(url: str):
    try:
        path = urlparse(url).path
        ext = os.path.splitext(path)[1][1:].lower()
        return ext.split('?')[0]
    except:
        return ""
def setup_selenium_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(45)
    return driver
async def handle_popups_and_scroll_aggressively(driver: webdriver.Chrome):
    # ... (full logic for this function)
    pass
async def scrape_images_from_url(url: str, context: ContextTypes.DEFAULT_TYPE):
    # ... (full logic for this function)
    return set()


# --- Command Handlers (Unchanged from previous correct versions) ---
# All handlers like /start, /scrape, /login, etc., are correct and remain here.
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = (
        f"Hi <b>{user.mention_html()}</b>! I'm the Image Scraper Bot.\n\n"
        "<b>Single Scraping:</b>\n"
        "• <code>/scrape [url]</code> - Scrape a single page interactively.\n"
        "• <code>/settarget [chat_id]</code> - Set a target chat for single scrapes.\n\n"
        "<b>Deep Scraping (User Account Required):</b>\n"
        "• <code>/login</code> - Connect your user account.\n"
        "• <code>/deepscrape [url]</code> - Scrape all links on a page.\n"
        "• <code>/setgroup [chat_id]</code> - Set the target supergroup for deep scrapes.\n"
        "• <code>/creategroup [name]</code> - Create a new supergroup.\n\n"
        "<b>General:</b>\n"
        "• <code>/stop</code> - Cancel any ongoing scraping task."
    )
    await update.message.reply_html(message)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)
# ... (rest of the command handlers)
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def settarget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass
async def scrape_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END
async def choose_file_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END
async def scrape_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return LOGIN_SESSION
async def received_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END
async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END
async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass


# --- Bot Application Setup ---
def main():
    """Start the bot with robust startup checks and all handlers."""
    logger.info("--- Bot Starting Up ---")

    if not all([BOT_TOKEN, API_ID, API_HASH, os.getenv("MONGO_URI")]):
        logger.critical("CRITICAL: One or more environment variables are MISSING.")
        sys.exit(1)
    logger.info("Environment variables verified.")

    try:
        # --- THIS IS THE FIX: Use post_init for the async check ---
        application = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(post_init_callback)
            .build()
        )
        logger.info("Application built successfully.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to build application, likely an INVALID BOT TOKEN. Error: {e}")
        sys.exit(1)

    # --- Register ALL handlers ---
    login_handler = ConversationHandler(
        entry_points=[CommandHandler("login", login_command)],
        states={LOGIN_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_session)]},
        fallbacks=[CommandHandler("cancel", login_cancel)],
    )
    scrape_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("scrape", scrape_entry)],
        states={CHOOSE_FILE_TYPE: [CallbackQueryHandler(choose_file_type_callback)]},
        fallbacks=[CommandHandler("stop", scrape_fallback)],
        conversation_timeout=600
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(login_handler)
    application.add_handler(scrape_conv_handler)
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("settarget", settarget_command))
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))
    
    logger.info("All command handlers registered.")
    
    # Start Polling
    logger.info("Starting bot polling...")
    application.run_polling()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    logger.info("Flask web server started in background thread.")
    
    main()
