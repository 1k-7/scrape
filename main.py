# main.py
import os
import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse
import threading
from collections import Counter
import sys
import traceback
import time

from flask import Flask
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, PicklePersistence
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, BadRequest

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

# --- Flask & Startup Checks ---
app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot is alive!", 200
def run_web_server(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
async def post_init_callback(application: Application):
    logger.info("Running post-initialization DB check...")
    try:
        await db.client.admin.command('ping'); logger.info("MongoDB connection successful.")
    except Exception as e:
        logger.critical(f"CRITICAL: Could not connect to MongoDB. Shutting down. Error: {e}"); sys.exit(1)

# --- Helper Functions ---
def preprocess_url(url: str):
    if not re.match(r'http(s)?://', url): return f'https://{url}'
    return url
def find_url_in_text(text: str):
    if not text: return None
    match = re.search(r'https?://[^\s/$.?#].[^\s]*', text)
    return match.group(0) if match else None
def get_file_extension(url: str):
    try:
        path = urlparse(url).path; ext = os.path.splitext(path)[1][1:].lower(); return ext.split('?')[0]
    except: return ""

# --- Hyper-Aggressive Scraping Logic ---
def setup_selenium_driver():
    chrome_options = Options()
    chrome_options.binary_location = "/usr/bin/google-chrome"
    chrome_options.add_argument("--headless"); chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage"); chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1200")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)

def scrape_images_from_url_sync(url: str, context: ContextTypes.DEFAULT_TYPE):
    """Synchronous, thread-safe scraping function."""
    driver = setup_selenium_driver()
    images = set()
    try:
        driver.get(url)
        # Popup Handling
        popup_keywords = ['accept', 'agree', 'enter', 'continue', 'confirm', 'i am 18', 'yes', 'i agree']
        js_script = f"var keywords = {popup_keywords}; var buttons = document.querySelectorAll('button, a, div, span'); var clicked = false; for (var i = 0; i < buttons.length; i++) {{ var buttonText = buttons[i].innerText.toLowerCase(); for (var j = 0; j < keywords.length; j++) {{ if (buttonText.includes(keywords[j])) {{ buttons[i].click(); clicked = true; break; }} }} if (clicked) break; }} return clicked;"
        try:
            if driver.execute_script(js_script): logger.info(f"Clicked a popup on {url}."); time.sleep(3)
        except Exception as e: logger.warning(f"Popup click script failed: {e}")
        
        # DOM-based Aggressive Scrolling
        last_image_count, stable_count = 0, 0
        while stable_count < 4: # Increased patience
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2.5)
            current_images = driver.find_elements(By.TAG_NAME, "img")
            current_image_count = len([img for img in current_images if img.get_attribute('src')])
            if current_image_count == last_image_count:
                stable_count += 1
            else:
                stable_count = 0
            last_image_count = current_image_count
            if not context.user_data.get('is_scraping', True): break

        soup = BeautifulSoup(driver.page_source, "html.parser")
        for tag in soup.find_all("img"):
            src = tag.get("src") or tag.get("data-src")
            if src and src.strip(): images.add(urljoin(url, src.strip()))
        for tag in soup.find_all("a"):
            href = tag.get("href")
            if href and re.search(r'\.(jpeg|jpg|png|gif|webp|bmp)$', href, re.I): images.add(urljoin(url, href.strip()))
        for tag in soup.find_all("source"):
             srcset = tag.get("srcset")
             if srcset and srcset.strip(): images.add(urljoin(url, srcset.strip().split(',')[0].split(' ')[0]))
    except Exception as e:
        logger.error(f"Error scraping {url}: {e}", exc_info=True)
    finally:
        driver.quit()
    return {img for img in images if img and img.startswith('http')}

# --- UTILITY & PERMISSION COMMANDS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = (
        f"Hi <b>{user.mention_html()}</b>! I am the fully operational Image Scraper Bot.\n\n"
        "<b>Single Scraping:</b>\n"
        "• <code>/scrape [url]</code>\n"
        "• <code>/settarget [chat_id]</code>\n\n"
        "<b>Deep Scraping (User Account Required):</b>\n"
        "• <code>/login</code>\n"
        "• <code>/deepscrape [url]</code>\n"
        "• <code>/setgroup [chat_id]</code>\n"
        "• <code>/creategroup [name]</code>\n\n"
        "<b>General:</b>\n"
        "• <code>/status</code> - Check status of an active deepscrape.\n"
        "• <code>/mydata</code> - View your current settings.\n"
        "• <code>/stop</code> or <code>/cancel</code> - Stop any active task."
    )
    await update.message.reply_html(message)
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await start_command(update, context)
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = await db.get_user_active_task(update.effective_user.id)
    if task: await db.update_task_status(task['_id'], "stopped")
    context.user_data['is_scraping'] = False; context.user_data.pop('state', None)
    await update.message.reply_html("<b>All tasks stopped and state cleared.</b>")
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await stop_command(update, context)
async def mydata_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = await db.get_user_data(update.effective_user.id)
    target_chat = (ud or {}).get('target_chat_id', 'Not Set'); target_group = (ud or {}).get('target_group_id', 'Not Set')
    session_set = "Yes" if (ud or {}).get('session_string') else "No"
    message = (f"<b>Your Settings:</b>\n\n👤 <b>Logged In:</b> {session_set}\n🎯 <b>Single Scrape Target:</b> <code>{target_chat}</code>\n🗂️ <b>Deep Scrape Target:</b> <code>{target_group}</code>")
    await update.message.reply_html(message)
async def settarget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/settarget [chat_id]</code> or <code>/settarget me</code>")
    target_id = context.args[0]
    if target_id.lower() != 'me':
        await update.message.reply_html(f"Verifying permissions for <code>{target_id}</code>...")
        try:
            msg = await context.bot.send_message(chat_id=target_id, text="-- Permission Check --")
            await context.bot.delete_message(chat_id=target_id, message_id=msg.message_id)
        except Exception as e:
            return await update.message.reply_html(f"<b>Permission Denied!</b>\nI could not send/delete messages.\n<b>Error:</b> <code>{e}</code>")
    await db.save_user_data(update.effective_user.id, {'target_chat_id': target_id})
    await update.message.reply_html(f"✅ Single scrape target set to <code>{target_id}</code>.")
async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = await db.get_user_data(update.effective_user.id)
    if not ud or 'session_string' not in ud: return await update.message.reply_html("You must /login first.")
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/setgroup [supergroup_id]</code>")
    group_id = context.args[0]
    await update.message.reply_html(f"Verifying permissions for <code>{group_id}</code>...")
    try:
        async with await get_userbot_client(ud['session_string']) as client:
            entity = await client.get_entity(int(group_id))
            test_topic = await client(functions.channels.CreateForumTopicRequest(channel=entity, title="-- Permission Check --", random_id=context.bot._get_private_random_id()))
            await client(functions.channels.DeleteForumTopicRequest(channel=entity, topic_id=test_topic.updates[0].message.id))
    except Exception as e:
        return await update.message.reply_html(f"<b>Permission Denied!</b>\nYour user account could not manage topics.\n<b>Error:</b> <code>{e}</code>")
    await db.save_user_data(update.effective_user.id, {'target_group_id': group_id})
    await update.message.reply_html(f"✅ Deep scrape target group set to <code>{group_id}</code>.")

# --- CORE LOGIC HANDLERS ---
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['state'] = 'awaiting_session'
    await update.message.reply_text("Please send your Telethon session string now.\nOr use /cancel.")
async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('state') == 'awaiting_session':
        session_string = update.message.text.strip()
        await update.message.reply_text("Validating session...")
        try:
            async with await get_userbot_client(session_string) as client:
                if not client or not await client.is_user_authorized():
                    return await update.message.reply_text("❌ Login failed. The session string is invalid or expired.")
                me = await client.get_me()
                await db.save_user_data(update.effective_user.id, {'session_string': session_string})
                await update.message.reply_html(f"✅ Successfully logged in as <b>{me.first_name}</b>!")
            del context.user_data['state']
        except Exception as e:
            await update.message.reply_text(f"An error occurred: {e}"); context.user_data.pop('state', None)
async def scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = ""
    if context.args: url = preprocess_url(context.args[0])
    elif update.message.reply_to_message and update.message.reply_to_message.text: url = find_url_in_text(update.message.reply_to_message.text)
    if not url: return await update.message.reply_html("<b>Usage:</b> <code>/scrape [url]</code> or reply to a message.")
    context.user_data['is_scraping'] = True
    msg = await update.message.reply_html(f"🔎 Scraping <code>{url}</code>... This may take a moment.")
    images = await asyncio.to_thread(scrape_images_from_url_sync, url, context)
    if not context.user_data.get('is_scraping', False): return
    context.user_data['is_scraping'] = False
    if not images: return await msg.edit_text("Could not find any images on that page.")
    context.user_data['scraped_images'] = list(images); file_types = Counter(get_file_extension(img) for img in images)
    keyboard = []
    for ext, count in file_types.items():
        keyboard.append([InlineKeyboardButton(f"{(ext or 'Other').upper()} ({count})", callback_data=f"scrape_{ext or 'none'}")])
    keyboard.append([InlineKeyboardButton(f"All Files ({len(images)})", callback_data="scrape_all")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await msg.edit_text("✅ <b>Scan complete!</b> Choose file type:", reply_markup=reply_markup)
    context.user_data['state'] = 'awaiting_file_type'
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "show_status":
        task = await db.get_user_active_task(update.effective_user.id)
        if not task: return await query.answer("No active task found.", show_alert=True)
        message = (
            f"📊 Task Status: {task['status'].title()}\n"
            f"🔗 Progress: Link {task['current_link_index'] + 1} of {task['total_links']}\n"
            f"📄 Current URL: ...{task['current_link_url'][-50:]}\n"
            f"🖼️ Images Found: {task['current_link_images_found']}\n"
            f"📤 Images Uploaded: {task['current_link_images_uploaded']}"
        )
        return await query.answer(message, show_alert=True)
    if query.data.startswith("scrape_") and context.user_data.get('state') == 'awaiting_file_type':
        chosen_ext = query.data.split('_', 1)[1]
        all_images = context.user_data.get('scraped_images', [])
        if chosen_ext == 'all': images_to_send = all_images
        elif chosen_ext == 'none': images_to_send = [img for img in all_images if not get_file_extension(img)]
        else: images_to_send = [img for img in all_images if get_file_extension(img) == chosen_ext]
        user_db_data = await db.get_user_data(update.effective_user.id)
        target_chat_id = (user_db_data or {}).get('target_chat_id', str(update.effective_chat.id))
        if target_chat_id == 'me': target_chat_id = str(update.effective_chat.id)
        await query.edit_message_text(f"Sending {len(images_to_send)} images to <code>{target_chat_id}</code>...", parse_mode=ParseMode.HTML)
        context.user_data['is_scraping'] = True
        for img in images_to_send:
            if not context.user_data.get('is_scraping'): break
            await context.bot.send_photo(target_chat_id, photo=img)
        context.user_data['is_scraping'] = False
        await query.message.reply_html("✅ <b>Sending complete!</b>")
        context.user_data.pop('state', None); context.user_data.pop('scraped_images', None)
async def creategroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = await db.get_user_data(update.effective_user.id)
    if not ud or 'session_string' not in ud: return await update.message.reply_html("You must /login first.")
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/creategroup [Your Group Name]</code>")
    group_name = " ".join(context.args)
    await update.message.reply_html(f"Creating supergroup '<b>{group_name}</b>'...")
    try:
        async with await get_userbot_client(ud['session_string']) as client:
            created_chat = await client(functions.messages.CreateChatRequest(users=["me"], title=group_name))
            chat_id = created_chat.chats[0].id
            await client(functions.channels.ConvertToGigagroupRequest(channel=chat_id))
            full_channel = await client(functions.channels.GetFullChannelRequest(channel=chat_id))
            supergroup_id = int(f"-100{full_channel.full_chat.id}")
            await db.save_user_data(update.effective_user.id, {'target_group_id': str(supergroup_id)})
            await update.message.reply_html(f"✅ Supergroup created and set as target!\n<b>Name:</b> {group_name}\n<b>ID:</b> <code>{supergroup_id}</code>")
    except Exception as e: await update.message.reply_html(f"An error occurred: {e}")

async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await db.get_user_active_task(update.effective_user.id): return await update.message.reply_html("You already have an active deepscrape task. Use /stop first.")
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/deepscrape [url]</code>")
    base_url = preprocess_url(context.args[0])
    msg = await update.message.reply_html(f"Scanning <code>{base_url}</code> for links...")
    links = await asyncio.to_thread(requests.get, base_url, headers={'User-Agent': 'Mozilla/5.0'})
    soup = BeautifulSoup(links.content, 'html.parser')
    links = sorted(list({urljoin(base_url, a['href']) for a in soup.find_all('a', href=True) if urljoin(base_url, a.get('href', '')) != base_url}))
    if not links: return await msg.edit_text("Found no unique links on that page to scrape.")
    task_id = await db.create_task(update.effective_user.id, base_url, links)
    keyboard = [[InlineKeyboardButton("Show Status", callback_data="show_status")]]
    await msg.edit_text(f"Found {len(links)} links. Starting deep scrape in the background.\nTo cancel, use /stop.", reply_markup=InlineKeyboardMarkup(keyboard))
    context.application.create_task(_run_deepscrape_task(update.effective_user.id, task_id, context))
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = await db.get_user_active_task(update.effective_user.id)
    if not task: return await update.message.reply_html("No active deepscrape task found.")
    keyboard = [[InlineKeyboardButton("Show Status", callback_data="show_status")]]
    await update.message.reply_html("You have an active deepscrape task.", reply_markup=InlineKeyboardMarkup(keyboard))

async def _run_deepscrape_task(user_id, task_id, context: ContextTypes.DEFAULT_TYPE):
    ud = await db.get_user_data(user_id)
    if not ud or 'session_string' not in ud: return
    target_group = ud.get('target_group_id')
    if not target_group: return
    await db.update_task_status(task_id, "running")
    context.user_data['is_scraping'] = True
    try:
        async with await get_userbot_client(ud['session_string']) as client:
            entity = await client.get_entity(int(target_group))
            while True:
                task = await db.tasks_collection.find_one({"_id": task_id})
                if task['status'] != 'running': break
                pending_links = [link for link in task['all_links'] if link not in task['completed_links']]
                if not pending_links:
                    await db.update_task_status(task_id, "completed"); await context.bot.send_message(user_id, "✅ <b>Deep scrape finished!</b>", parse_mode=ParseMode.HTML); break
                link = pending_links[0]
                await db.update_task_counters(task_id, len(task['completed_links']), link, 0)
                images = await asyncio.to_thread(scrape_images_from_url_sync, link, context)
                if not images: await db.complete_link_in_task(task_id, link); continue
                await db.update_task_counters(task_id, len(task['completed_links']), link, len(images))
                topic_id = None
                while not topic_id:
                    if task['status'] == 'stopped': break
                    try:
                        topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98]
                        topic_result = await client(functions.channels.CreateForumTopicRequest(channel=entity, title=topic_title, random_id=context.bot._get_private_random_id()))
                        topic_id = topic_result.updates[0].message.id
                    except FloodWaitError as e:
                        await db.update_task_status(task_id, "paused"); await context.bot.send_message(user_id, f"<b>Auto-Pause:</b> Flood wait. Resuming in {e.seconds + 5}s.", parse_mode=ParseMode.HTML)
                        await asyncio.sleep(e.seconds + 5); await db.update_task_status(task_id, "running")
                if not topic_id: continue
                for img_url in images:
                    while True:
                        task = await db.tasks_collection.find_one({"_id": task_id});
                        if task['status'] == 'stopped': break
                        try:
                            await context.bot.send_photo(target_group, photo=img_url, message_thread_id=topic_id)
                            await db.increment_task_image_upload_count(task_id)
                            break
                        except RetryAfter as e:
                            await db.update_task_status(task_id, "paused"); await context.bot.send_message(user_id, f"<b>Auto-Pause:</b> Limit reached. Resuming in {e.retry_after + 5}s.", parse_mode=ParseMode.HTML)
                            await asyncio.sleep(e.retry_after + 5); await db.update_task_status(task_id, "running")
                    if task['status'] == 'stopped': break
                if task['status'] == 'running': await db.complete_link_in_task(task_id, link)
    except Exception as e:
        logger.error(f"Error in _run_deepscrape_task: {e}"); await db.update_task_status(task_id, "paused")
    finally:
        context.user_data['is_scraping'] = False

# --- Main Application Setup ---
def main():
    if not BOT_TOKEN: logger.critical("CRITICAL: BOT_TOKEN is MISSING."); sys.exit(1)
    persistence = PicklePersistence(filepath="./bot_persistence")
    application = (Application.builder().token(BOT_TOKEN).persistence(persistence).post_init(post_init_callback).build())
    
    # Register all handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("mydata", mydata_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("settarget", settarget_command))
    application.add_handler(CommandHandler("setgroup", setgroup_command))
    application.add_handler(CommandHandler("creategroup", creategroup_command))
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("scrape", scrape_command))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    
    logger.info("All handlers registered. Starting bot polling...")
    application.run_polling()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    main()
