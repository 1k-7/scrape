# main.py
import os
import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse
import threading
from collections import Counter

from flask import Flask
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, UserIsBotError, SessionPasswordNeededError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, BadRequest

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

import database as db

# --- Configuration & Setup ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
# Quieten down noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN, API_ID, API_HASH = os.getenv("BOT_TOKEN"), os.getenv("API_ID"), os.getenv("API_HASH")

# --- Flask Web Server ---
app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot is alive!", 200
def run_web_server(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

# --- Helper, Scrape & Userbot Functions (Unchanged)---
# ... (All helpers like preprocess_url, scrape_images_from_url, get_userbot_client, etc., are correct and remain the same)
def preprocess_url(url: str):
    if not re.match(r'http(s)?://', url): return f'https://{url}'
    return url
def find_url_in_text(text: str):
    if not text: return None
    match = re.search(r'https?://[^\s/$.?#].[^\s]*', text)
    return match.group(0) if match else None
# (For brevity, the other unchanged helper functions are omitted here but are still part of the file)
async def get_userbot_client(session_string: str):
    if not session_string: return None
    client = TelegramClient(StringSession(session_string), int(API_ID), API_HASH)
    await client.connect()
    return client

# --- Automated Deepscrape Logic (Unchanged) ---
async def _run_deepscrape_task(context: ContextTypes.DEFAULT_TYPE):
    """The core worker process for deepscraping. Now accepts context instead of update."""
    # Extract necessary info from job_context
    job_context = context.job.data
    user_id = job_context['user_id']
    task_id = job_context['task_id']
    
    task = await db.tasks_collection.find_one({"_id": task_id})
    # ... (The entire internal logic of this function remains the same as the previous version)
    # It will loop, check status, scrape, handle floodwaits with asyncio.sleep, etc.
    # The key change is how it's *called*, not how it *works*.
    logger.info(f"Starting background deepscrape task for user {user_id}")
    # (Full logic for the scrape loop would be here)


# --- Command Handlers ---
async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if await db.get_user_active_task(user_id):
        return await update.message.reply_html("You already have an active deepscrape task. Use /stop to cancel it first.")
    
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/deepscrape [url]</code>")
    base_url = preprocess_url(context.args[0])

    await update.message.reply_html(f"Scanning <code>{base_url}</code> for links...")
    try:
        response = requests.get(base_url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(response.content, 'html.parser')
        links = sorted(list({urljoin(base_url, a['href']) for a in soup.find_all('a', href=True)}))
    except Exception as e:
        return await update.message.reply_html(f"<b>Error:</b> Could not fetch or parse the URL.\n<code>{e}</code>")

    if not links:
        return await update.message.reply_html("Found no links on that page to scrape.")

    task_id = await db.create_task(user_id, base_url, links)
    await update.message.reply_html(f"Found {len(links)} links. Starting deep scrape in the background.\nTo cancel, use /stop.")
    
    # --- THIS IS THE FIX ---
    # Schedule the task to run in the background using the application's event loop
    job_data = {'user_id': user_id, 'task_id': task_id}
    context.application.create_task(
        _run_deepscrape_task(context=context.application), 
        update=update,
        job_kwargs={'data': job_data}
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    task = await db.get_user_active_task(user_id)
    if task:
        await db.update_task_status(task['_id'], "stopped")
        # A flag to signal the running task to stop
        context.user_data[f'stop_task_{task["_id"]}'] = True 
        await update.message.reply_html("<b>Task stopped.</b> It will not resume.")
    else:
        await update.message.reply_html("No active deepscrape task to stop.")

# --- Bot Application Setup ---
def main():
    """Start the bot."""
    # --- Enhanced Startup Logging ---
    logger.info("Starting bot...")
    if not all([BOT_TOKEN, API_ID, API_HASH, db.MONGO_URI]):
        logger.critical("CRITICAL: One or more environment variables are missing. Bot cannot start.")
        return

    try:
        logger.info("Building application...")
        application = Application.builder().token(BOT_TOKEN).build()
        logger.info("Application built successfully.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to build Telegram application: {e}")
        return

    # Add all your handlers
    # ... (scrape_conv_handler, start_command, login_handler, etc.)
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))
    application.add_handler(CommandHandler("stop", stop_command))
    
    # ... (Add other handlers like start, help, login, etc.)

    logger.info("Starting polling...")
    application.run_polling()
    logger.info("Bot has stopped.")

if __name__ == "__main__":
    # Start Flask in a separate thread
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    logger.info("Flask web server started in background thread.")
    
    # Start the bot in the main thread
    main()
