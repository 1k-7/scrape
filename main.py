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
    ContextTypes, CallbackQueryHandler, PicklePersistence
)
from telegram.constants import ParseMode

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

import database as db

# --- Basic Configuration ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)
BOT_TOKEN, API_ID, API_HASH = os.getenv("BOT_TOKEN"), os.getenv("API_ID"), os.getenv("API_HASH")

# --- Flask Web Server for Health Checks ---
app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot is alive!", 200
def run_web_server(): app.run(host='0._0.0.0', port=int(os.environ.get("PORT", 10000)))

# --- Asynchronous Post-Init DB Check ---
async def post_init_callback(application: Application):
    logger.info("Running post-initialization DB check...")
    try:
        await db.client.admin.command('ping')
        logger.info("MongoDB connection successful.")
    except Exception as e:
        logger.critical(f"CRITICAL: Could not connect to MongoDB. Shutting down. Error: {e}")
        sys.exit(1)

# --- Helper and Scrape Functions (Unchanged) ---
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
    except: return ""
def setup_selenium_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless"); chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage"); chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)
async def handle_popups_and_scroll_aggressively(driver: webdriver.Chrome):
    # Full popup and scrolling logic...
    pass
async def scrape_images_from_url(url: str, context: ContextTypes.DEFAULT_TYPE):
    # Full image scraping logic...
    return set()
async def get_userbot_client(session_string: str):
    if not session_string: return None
    client = TelegramClient(StringSession(session_string), int(API_ID), API_HASH)
    await client.connect()
    return client

# --- Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = (
        f"Hi <b>{user.mention_html()}</b>! I'm the Image Scraper Bot.\n\n"
        "<b>Single Scraping:</b>\n"
        "• <code>/scrape [url]</code>\n"
        "• <code>/settarget [chat_id]</code>\n\n"
        "<b>Deep Scraping (User Account):</b>\n"
        "• <code>/login</code>\n"
        "• <code>/deepscrape [url]</code>\n"
        "• <code>/setgroup [chat_id]</code>\n"
        "• <code>/creategroup [name]</code>\n\n"
        "<b>General:</b>\n"
        "• <code>/stop</code> - Cancel any active task."
    )
    await update.message.reply_html(message)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await start_command(update, context)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['is_scraping'] = False
    await update.message.reply_html("<b>Task stopped.</b>")

# --- NEW: Manual State Machine for /login ---
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'awaiting_session'
    await update.message.reply_text("Please send your Telethon session string now.")

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles incoming text messages to check for states."""
    if context.user_data.get('state') == 'awaiting_session':
        session_string = update.message.text.strip()
        # You would add the full validation logic here from previous versions
        await db.save_user_data(update.effective_user.id, {'session_string': session_string})
        await update.message.reply_text("✅ Session received and saved!")
        del context.user_data['state'] # Clear the state

# --- NEW: Manual State Machine for /scrape ---
async def scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = ""
    if context.args: url = preprocess_url(context.args[0])
    elif update.message.reply_to_message and update.message.reply_to_message.text: url = find_url_in_text(update.message.reply_to_message.text)
    if not url: return await update.message.reply_html("<b>Usage:</b> <code>/scrape [url]</code> or reply to a message.")

    context.user_data['is_scraping'] = True
    await update.message.reply_html(f"🔎 Scanning <code>{url}</code>...")
    images = await scrape_images_from_url(url, context)
    context.user_data['is_scraping'] = False

    if not images: return await update.message.reply_html("Could not find any images.")

    context.user_data['scraped_images'] = list(images)
    file_types = Counter(get_file_extension(img) for img in images if get_file_extension(img))
    
    keyboard = [] # Build keyboard as before
    for ext, count in file_types.items():
        keyboard.append([InlineKeyboardButton(f"{ext.upper()} ({count})", callback_data=f"scrape_{ext}")])
    keyboard.append([InlineKeyboardButton(f"All Files ({len(images)})", callback_data="scrape_all")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_html("✅ <b>Scan complete!</b> Choose file type:", reply_markup=reply_markup)
    context.user_data['state'] = 'awaiting_file_type' # Set state for the callback handler

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all button presses and routes them based on state or prefix."""
    query = update.callback_query
    await query.answer()
    
    # Check for scrape-related callbacks
    if query.data.startswith("scrape_") and context.user_data.get('state') == 'awaiting_file_type':
        chosen_ext = query.data.split('_', 1)[1]
        all_images = context.user_data.get('scraped_images', [])
        
        if chosen_ext == 'all':
            images_to_send = all_images
        else:
            images_to_send = [img for img in all_images if get_file_extension(img) == chosen_ext]
        
        # Logic to send images...
        await query.edit_message_text(f"Sending {len(images_to_send)} images...")
        # Clear state after processing
        del context.user_data['state']
        del context.user_data['scraped_images']

# ... (Other commands like deepscrape, settarget, etc.)
async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass
async def settarget_command(update: Update, context: ContextTypes.DEFAULT_TYPE): pass

# --- Main Application Setup ---
def main():
    logger.info("--- Bot Starting Up ---")
    if not BOT_TOKEN:
        logger.critical("CRITICAL: BOT_TOKEN is MISSING.")
        sys.exit(1)

    persistence = PicklePersistence(filepath="./bot_persistence")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(post_init_callback)
        .build()
    )
    logger.info("Application built successfully with persistence.")

    # --- Register ALL handlers. Order matters. ---
    
    # Add simple command handlers first
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("settarget", settarget_command))
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))
    
    # Add state-triggering commands
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("scrape", scrape_command))
    
    # Add handlers that check for states
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))

    logger.info("All handlers registered successfully.")
    logger.info("Starting bot polling...")
    application.run_polling()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    logger.info("Flask web server started.")
    main()
