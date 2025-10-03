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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN, API_ID, API_HASH = os.getenv("BOT_TOKEN"), os.getenv("API_ID"), os.getenv("API_HASH")

# --- Flask & Web Server ---
app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot is alive!", 200
def run_web_server(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

# --- Helper Functions ---
def preprocess_url(url: str):
    if not re.match(r'http(s)?://', url): return f'https://{url}'
    return url

def find_url_in_text(text: str):
    if not text: return None
    match = re.search(r'https?://[^\s/$.?#].[^\s]*', text)
    return match.group(0) if match else None
# ... (Other helpers like get_file_extension, setup_selenium_driver, handle_popups... remain the same)

# --- Core Task & Scrape Logic ---
# (scrape_entry, choose_file_type_callback and their helpers remain the same)
async def get_userbot_client(session_string: str):
    if not session_string: return None
    client = TelegramClient(StringSession(session_string), int(API_ID), API_HASH)
    await client.connect()
    return client

# --- NEW: Refactored Deepscrape Logic ---
async def _run_deepscrape_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task):
    """The core worker process for deepscraping. Can be resumed."""
    user_id = update.effective_user.id
    user_data = await db.get_user_data(user_id)
    if not user_data or 'session_string' not in user_data:
        return await update.message.reply_html("<b>Error:</b> Login session not found. Please /login again.")

    target_group = user_data.get('target_group_id')
    if not target_group:
        return await update.message.reply_html("<b>Error:</b> Target group not set. Please use /setgroup.")

    await db.update_task_status(task['_id'], "running")
    context.user_data['is_scraping'] = True
    user_client = None

    try:
        user_client = await get_userbot_client(user_data['session_string'])
        entity = await user_client.get_entity(int(target_group) if target_group.startswith('-') else target_group)

        pending_links = [link for link in task['all_links'] if link not in task['completed_links']]
        
        # Resume from a partially completed link
        if task['current_link_progress']['link'] and task['current_link_progress']['link'] in pending_links:
             current_link = task['current_link_progress']['link']
             pending_links.remove(current_link)
             links_to_process = [current_link] + pending_links
        else:
            links_to_process = pending_links

        for link in links_to_process:
            if not context.user_data.get('is_scraping', True): break

            await context.bot.send_message(user_id, f"Processing: <code>{link}</code>", parse_mode=ParseMode.HTML)
            
            # --- State Recovery ---
            is_resumed_link = (link == task['current_link_progress']['link'])
            if is_resumed_link:
                images = task['current_link_progress']['images_scraped']
                completed_count = task['current_link_progress']['completed_images_count']
            else:
                images = await scrape_images_from_url(link, context) # Simplified scrape call
                await db.update_task_progress(task['_id'], link, list(images))
                completed_count = 0

            images_to_upload = images[completed_count:]
            
            if not images:
                await db.complete_link_in_task(task['_id'], link)
                continue

            # STEP 1: Create Topic (via Userbot)
            try:
                topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98]
                topic_result = await user_client(functions.channels.CreateForumTopicRequest(channel=entity, title=topic_title, random_id=context.bot._get_private_random_id()))
                topic_id = topic_result.updates[0].message.id
            except FloodWaitError as e:
                await db.update_task_status(task['_id'], "paused")
                await context.bot.send_message(user_id, f"<b>Task Paused:</b> Hit a flood wait of {e.seconds}s while creating a topic. Use /resume later.")
                return
            
            # STEP 2: Upload Images (via Bot)
            for i, img_url in enumerate(images_to_upload, start=1):
                if not context.user_data.get('is_scraping', True): break
                try:
                    await context.bot.send_photo(target_group, photo=img_url, message_thread_id=topic_id)
                    await db.update_task_image_completion(task['_id'], completed_count + i)
                except RetryAfter as e:
                    await db.update_task_status(task['_id'], "paused")
                    await context.bot.send_message(user_id, f"<b>Task Paused:</b> Telegram is requesting a wait of {e.retry_after}s. Use /resume after this period.")
                    return
                except Exception as e:
                    logger.warning(f"Failed to upload {img_url}: {e}")

            if context.user_data.get('is_scraping', True):
                 await db.complete_link_in_task(task['_id'], link)
        
        if context.user_data.get('is_scraping', True):
            await db.update_task_status(task['_id'], "completed")
            await context.bot.send_message(user_id, "✅ <b>Deep scrape finished!</b>")

    except Exception as e:
        await db.update_task_status(task['_id'], "paused")
        await context.bot.send_message(user_id, f"An unexpected error occurred: <code>{e}</code>. Task paused. You can try to /resume later.")
    finally:
        if user_client: await user_client.disconnect()
        context.user_data['is_scraping'] = False

async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/deepscrape [url]</code>")
    base_url = preprocess_url(context.args[0])

    await update.message.reply_html(f"Scanning <code>{base_url}</code> for links...")
    response = requests.get(base_url, headers={'User-Agent': 'Mozilla/5.0'})
    soup = BeautifulSoup(response.content, 'html.parser')
    links = sorted(list({urljoin(base_url, a['href']) for a in soup.find_all('a', href=True)})) # Simplified
    
    task_id = await db.create_task(update.effective_user.id, base_url, links)
    task = await db.tasks_collection.find_one({"_id": task_id})
    
    await update.message.reply_html(f"Found {len(links)} links. Starting deep scrape.\nTo cancel, use /stop.")
    asyncio.create_task(_run_deepscrape_task(update, context, task))

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    task = await db.get_user_task(user_id)
    if not task:
        return await update.message.reply_html("No paused task found to resume.")
    
    await update.message.reply_html(f"Resuming task for <code>{task['base_url']}</code>...")
    asyncio.create_task(_run_deepscrape_task(update, context, task))

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = await db.tasks_collection.find_one({"user_id": update.effective_user.id, "status": "running"})
    if task:
        await db.update_task_status(task['_id'], "stopped")
        context.user_data['is_scraping'] = False
        await update.message.reply_html("<b>Task stopped.</b> It will not resume.")
    else:
        await update.message.reply_html("No active deepscrape task to stop.")

# --- Bot Application Setup ---
def run_bot():
    # ... (Same application setup as before, just add the resume command)
    application = Application.builder().token(BOT_TOKEN).build()
    # ... (Add scrape_conv_handler, other command handlers)
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("stop", stop_command))
    # ... (Add start, login, etc.)
    application.run_polling()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    run_bot()
