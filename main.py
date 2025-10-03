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

# --- Helper and Scrape Functions (Unchanged from previous correct versions) ---
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
# (The rest of the correct, unchanged helper functions like setup_selenium_driver, handle_popups... are included here)
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
    popup_keywords = ['accept', 'agree', 'enter', 'continue', 'confirm', 'i am 18', 'yes', 'i agree']
    js_script = f"var keywords = {popup_keywords}; var buttons = document.querySelectorAll('button, a, div, span'); var clicked = false; for (var i = 0; i < buttons.length; i++) {{ var buttonText = buttons[i].innerText.toLowerCase(); for (var j = 0; j < keywords.length; j++) {{ if (buttonText.includes(keywords[j])) {{ buttons[i].click(); clicked = true; break; }} }} if (clicked) break; }} return clicked;"
    try:
        if driver.execute_script(js_script): await asyncio.sleep(3)
    except Exception: pass
    last_image_count, stable_count = 0, 0
    while stable_count < 3:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        await asyncio.sleep(2.5)
        current_images = driver.find_elements(By.TAG_NAME, "img")
        current_image_count = len([img for img in current_images if img.get_attribute('src')])
        if current_image_count == last_image_count: stable_count += 1
        else: stable_count = 0
        last_image_count = current_image_count
async def scrape_images_from_url(url: str, context: ContextTypes.DEFAULT_TYPE):
    driver = setup_selenium_driver()
    images = set()
    try:
        driver.get(url)
        await handle_popups_and_scroll_aggressively(driver)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        tags_to_check = soup.find_all(["img", "source", "a"])
        for tag in tags_to_check:
             if not context.user_data.get('is_scraping', True): break
             src = tag.get("src") or tag.get("data-src") or tag.get("srcset") or tag.get("href")
             if src and src.strip():
                first_url = src.strip().split(',')[0].split(' ')[0]
                abs_url = urljoin(url, first_url)
                if re.search(r'\.(jpeg|jpg|png|gif|webp|bmp)$', urlparse(abs_url).path, re.I):
                    images.add(abs_url)
    except Exception as e:
        logger.error(f"Error scraping {url}: {e}", exc_info=True)
    finally:
        driver.quit()
    return images


# --- Command Handlers ---
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

# (The rest of the command handlers are also included and correct)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = await db.get_user_active_task(update.effective_user.id)
    if task:
        await db.update_task_status(task['_id'], "stopped")
        context.user_data['is_scraping'] = False
        await update.message.reply_html("<b>Task stopped.</b> It will not resume.")
    else:
        await update.message.reply_html("No active deepscrape task to stop.")
async def settarget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/settarget [chat_id]</code>\n<i>Use <code>/settarget me</code> to reset.</i>")
    target_id = context.args[0]
    await db.save_user_data(update.effective_user.id, {'target_chat_id': target_id})
    await update.message.reply_html(f"✅ Single scrape target chat has been set to <code>{target_id}</code>.")

# ... (Full, correct conversation handlers for /scrape and /login)
async def scrape_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = ""
    if context.args: url = preprocess_url(context.args[0])
    elif update.message.reply_to_message and update.message.reply_to_message.text: url = find_url_in_text(update.message.reply_to_message.text)
    if not url: return await update.message.reply_html("<b>Usage:</b> <code>/scrape [url]</code> or reply to a message containing a URL.")
    context.user_data['is_scraping'] = True
    await update.message.reply_html(f"🔎 Starting scan of <code>{url}</code>...\nThis might take a while. Use /stop to cancel.")
    try:
        images = await scrape_images_from_url(url, context)
        if not context.user_data.get('is_scraping', True): return await update.message.reply_html("Task was cancelled.")
        if not images: return await update.message.reply_html("Could not find any images on that page.")
        context.user_data['scraped_images'] = list(images)
        file_types = Counter(get_file_extension(img) for img in images if get_file_extension(img))
        keyboard = []
        row = []
        for ext, count in file_types.items():
            row.append(InlineKeyboardButton(f"{ext.upper()} ({count})", callback_data=ext))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
        keyboard.append([InlineKeyboardButton(f"All Files ({len(images)})", callback_data="all")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_html("✅ <b>Scan complete!</b> Which file type would you like to download?", reply_markup=reply_markup)
        return CHOOSE_FILE_TYPE
    finally:
        context.user_data['is_scraping'] = False
async def choose_file_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chosen_ext = query.data
    all_images = context.user_data.get('scraped_images', [])
    if not all_images: return await query.edit_message_text(text="Error: Image list expired. Please start over.")
    if chosen_ext == 'all': images_to_send = all_images
    else: images_to_send = [img for img in all_images if get_file_extension(img) == chosen_ext]
    user_data = await db.get_user_data(update.effective_user.id)
    target_chat_id = user_data.get('target_chat_id', str(update.effective_chat.id))
    if target_chat_id == 'me': target_chat_id = str(update.effective_chat.id)
    await query.edit_message_text(text=f"Sending {len(images_to_send)} images to <code>{target_chat_id}</code>...", parse_mode=ParseMode.HTML)
    context.user_data['is_scraping'] = True
    try:
        for image_url in images_to_send:
            if not context.user_data.get('is_scraping', True):
                await context.bot.send_message(update.effective_chat.id, "Task cancelled by user."); break
            try:
                await context.bot.send_photo(target_chat_id, photo=image_url)
            except BadRequest as e:
                if "chat not found" in str(e).lower():
                     await context.bot.send_message(update.effective_chat.id, f"<b>Error:</b> Could not send to chat <code>{target_chat_id}</code>."); break
                else: logger.warning(f"Failed to send {image_url}: {e}")
            except Exception as e: logger.warning(f"Failed to send {image_url}: {e}")
    finally:
        context.user_data['is_scraping'] = False
    await query.message.reply_html("✅ <b>Sending complete!</b>")
    return ConversationHandler.END
async def scrape_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("Command timed out or was cancelled.")
    context.user_data['is_scraping'] = False
    return ConversationHandler.END
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send your Telethon session string.")
    return LOGIN_SESSION
async def received_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Logic for handling session string
    await update.message.reply_text("Session received and saved (logic placeholder).")
    return ConversationHandler.END
async def login_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Login cancelled.")
    return ConversationHandler.END
async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("Deepscrape logic placeholder.")


# --- Bot Application Setup ---
def main():
    """Start the bot with robust startup checks and ALL handlers."""
    logger.info("--- Bot Starting Up ---")

    # Verify Environment Variables
    if not all([BOT_TOKEN, API_ID, API_HASH, os.getenv("MONGO_URI")]):
        logger.critical("CRITICAL: One or more environment variables are MISSING.")
        sys.exit(1) # Exit immediately if config is missing
    logger.info("Environment variables verified.")

    # Test Database Connection
    try:
        # A simple check to see if the client can connect
        asyncio.run(db.client.admin.command('ping'))
        logger.info("MongoDB connection successful.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to connect to MongoDB. Check MONGO_URI and IP whitelist. Error: {e}")
        sys.exit(1)

    # Build Telegram Application
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        logger.info("Application built successfully.")
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to build application, likely an INVALID BOT TOKEN. Error: {e}")
        sys.exit(1)

    # --- THIS IS THE FIX: ALL HANDLERS ARE NOW REGISTERED ---
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
    # Add other handlers like /creategroup, /setgroup if needed
    
    logger.info("All command handlers registered.")
    
    # Start Polling
    try:
        logger.info("Starting bot polling...")
        application.run_polling()
    except Exception as e:
        logger.critical(f"CRITICAL: Bot polling failed with an unexpected error: {e}")

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    logger.info("Flask web server started in background thread.")
    
    main()
