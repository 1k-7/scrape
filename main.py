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

# --- Helper and Scrape Functions (Unchanged) ---
# ... (All helpers like preprocess_url, find_url_in_text, setup_selenium_driver, handle_popups..., scrape_entry, etc. are the same)

async def get_userbot_client(session_string: str):
    if not session_string: return None
    client = TelegramClient(StringSession(session_string), int(API_ID), API_HASH)
    await client.connect()
    return client

# --- FULLY AUTOMATED Deepscrape Logic ---
async def _run_deepscrape_task(update: Update, context: ContextTypes.DEFAULT_TYPE, task_id):
    """The core worker process for deepscraping. Handles floodwaits automatically."""
    user_id = update.effective_user.id
    task = await db.tasks_collection.find_one({"_id": task_id})
    user_data = await db.get_user_data(user_id)
    if not user_data or 'session_string' not in user_data:
        return await context.bot.send_message(user_id, "<b>Error:</b> Login session not found.", parse_mode=ParseMode.HTML)

    target_group = user_data.get('target_group_id')
    if not target_group:
        return await context.bot.send_message(user_id, "<b>Error:</b> Target group not set.", parse_mode=ParseMode.HTML)

    await db.update_task_status(task['_id'], "running")
    context.user_data['is_scraping'] = True
    user_client = None

    try:
        user_client = await get_userbot_client(user_data['session_string'])
        entity = await user_client.get_entity(int(target_group) if target_group.startswith('-') else target_group)

        # Main processing loop
        while True:
            task = await db.tasks_collection.find_one({"_id": task_id}) # Refresh task state
            if task['status'] != 'running' or not context.user_data.get('is_scraping', False):
                logger.info(f"Task {task_id} for user {user_id} stopped externally.")
                break

            pending_links = [link for link in task['all_links'] if link not in task['completed_links']]
            if not pending_links:
                await db.update_task_status(task['_id'], "completed")
                await context.bot.send_message(user_id, "✅ <b>Deep scrape finished!</b>", parse_mode=ParseMode.HTML)
                break

            link = pending_links[0]
            await context.bot.send_message(user_id, f"Processing: <code>{link}</code>", parse_mode=ParseMode.HTML)

            images = await scrape_images_from_url(link, context) # Simplified scrape call
            await db.update_task_progress(task['_id'], link, list(images))
            if not images:
                await db.complete_link_in_task(task['_id'], link)
                continue

            # STEP 1: Create Topic
            topic_id = None
            while not topic_id:
                try:
                    topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98]
                    topic_result = await user_client(functions.channels.CreateForumTopicRequest(channel=entity, title=topic_title, random_id=context.bot._get_private_random_id()))
                    topic_id = topic_result.updates[0].message.id
                except FloodWaitError as e:
                    await db.update_task_status(task['_id'], "paused")
                    wait_duration = e.seconds + 5
                    await context.bot.send_message(user_id, f"<b>Auto-Pause:</b> Hit flood wait. Will resume automatically in {wait_duration}s.", parse_mode=ParseMode.HTML)
                    await asyncio.sleep(wait_duration)
                    await db.update_task_status(task['_id'], "running")
                except Exception as e:
                    await context.bot.send_message(user_id, f"Error creating topic for {link}: {e}")
                    break # Skip to next link
            if not topic_id: continue

            # STEP 2: Upload Images
            for i, img_url in enumerate(list(images)):
                 while True: # Retry loop for this specific image
                    if not context.user_data.get('is_scraping', True): break
                    try:
                        await context.bot.send_photo(target_group, photo=img_url, message_thread_id=topic_id)
                        await db.update_task_image_completion(task['_id'], i + 1)
                        break # Success, move to next image
                    except RetryAfter as e:
                        await db.update_task_status(task['_id'], "paused")
                        wait_duration = e.retry_after + 5
                        await context.bot.send_message(user_id, f"<b>Auto-Pause:</b> Telegram limit reached. Resuming in {wait_duration}s.", parse_mode=ParseMode.HTML)
                        await asyncio.sleep(wait_duration)
                        await db.update_task_status(task['_id'], "running")
                    except Exception as e:
                        logger.warning(f"Failed to upload {img_url}: {e}")
                        break # Failure, move to next image
                 if not context.user_data.get('is_scraping', True): break

            if context.user_data.get('is_scraping', True):
                 await db.complete_link_in_task(task['_id'], link)

    except Exception as e:
        await db.update_task_status(task['_id'], "paused")
        await context.bot.send_message(user_id, f"An unexpected error occurred: <code>{e}</code>. Task paused.", parse_mode=ParseMode.HTML)
    finally:
        if user_client: await user_client.disconnect()
        context.user_data['is_scraping'] = False

async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await db.get_user_active_task(update.effective_user.id):
        return await update.message.reply_html("You already have an active deepscrape task. Use /stop to cancel it first.")
    
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/deepscrape [url]</code>")
    base_url = preprocess_url(context.args[0])

    await update.message.reply_html(f"Scanning <code>{base_url}</code> for links...")
    # ... (link scanning logic remains the same)
    links = ["https://example.com/page1", "https://example.com/page2"] # Dummy data
    
    task_id = await db.create_task(update.effective_user.id, base_url, links)
    await update.message.reply_html(f"Found {len(links)} links. Starting deep scrape.\nTo cancel, use /stop.")
    asyncio.create_task(_run_deepscrape_task(update, context, task_id))

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = await db.get_user_active_task(update.effective_user.id)
    if task:
        await db.update_task_status(task['_id'], "stopped")
        context.user_data['is_scraping'] = False
        await update.message.reply_html("<b>Task stopped.</b> It will not resume.")
    else:
        await update.message.reply_html("No active deepscrape task to stop.")

# --- Bot Application Setup ---
def run_bot():
    application = Application.builder().token(BOT_TOKEN).build()
    
    # ... (Add scrape_conv_handler, other command handlers)
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))
    # NO resume command needed anymore
    application.add_handler(CommandHandler("stop", stop_command))
    # ... (Add start, login, etc.)

    application.run_polling()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    run_bot()
