# main.py
import os
import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse, urlunparse
import threading
from collections import Counter, deque
import sys
import traceback
import time
import random

from flask import Flask
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, SessionPasswordNeededError, UserAlreadyParticipantError
from telethon.tl.types import ChatAdminRights

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, PicklePersistence, ConversationHandler, ExtBot
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, BadRequest

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains

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

def get_max_quality_url(url):
    """Applies heuristics to a URL to try and get a higher-resolution version."""
    if not url: return None
    patterns = [
        (r'/[wh]\d{2,4}-[wh]\d{2,4}-c/', '/'), (r'_\d{2,4}x\d{2,4}(\.(jpe?g|png|webp))', r'\1'),
        (r'\.\d{2,4}x\d{2,4}(\.(jpe?g|png|webp))', r'\1'), (r'-\d{2,4}x\d{2,4}(\.(jpe?g|png|webp))', r'\1'),
        (r'/thumb/', '/'), (r'\?(w|h|width|height|size|quality|crop|fit)=.*', '')
    ]
    cleaned_url = url
    for pattern, replacement in patterns:
        cleaned_url = re.sub(pattern, replacement, cleaned_url)
    parsed_url = urlparse(cleaned_url)
    cleaned_url = urlunparse(parsed_url._replace(query='', fragment=''))
    return cleaned_url

# --- Hyper-Aggressive Scraping Logic ---
def setup_selenium_driver():
    """Initializes the Selenium WebDriver, pointing to the pre-installed chromedriver."""
    logger.info("Setting up Selenium driver...")
    service = Service(executable_path="/usr/bin/chromedriver")
    
    chrome_options = Options()
    chrome_options.binary_location = "/usr/bin/google-chrome"
    chrome_options.add_argument("--headless"); chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage"); chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1200")
    
    try:
        driver = webdriver.Chrome(service=service, options=chrome_options)
        logger.info("Selenium driver setup successful.")
        return driver
    except Exception as e:
        logger.critical(f"CRITICAL: Failed to setup Selenium driver: {e}", exc_info=True)
        return None

def scrape_images_from_url_sync(url: str, user_data: dict):
    """Advanced scraper that simulates human interaction, handles AJAX, and attempts to find max quality images."""
    logger.info(f"Starting MAX-QUALITY scrape for URL: {url}")
    driver = setup_selenium_driver()
    if not driver: return set()

    images = set()
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        logger.info(f"Initial page load complete for {url}.")

        def wait_for_network_idle(driver, timeout=10, idle_time=2):
            logger.info("Waiting for network activity to become idle...")
            script = "const callback=arguments[arguments.length-1];let last_resource_count=0;let stable_checks=0;const check_interval=250;const check_network=()=>{const current_resource_count=window.performance.getEntriesByType('resource').length;if(current_resource_count===last_resource_count)stable_checks++;else{stable_checks=0;last_resource_count=current_resource_count}if(stable_checks*check_interval>=(idle_time*1000))callback(true);else setTimeout(check_network,check_interval)};check_network();"
            try:
                driver.set_script_timeout(timeout); driver.execute_async_script(script)
                logger.info("Network is idle.")
            except Exception:
                logger.warning(f"Network idle check timed out after {timeout}s. Proceeding anyway.")
        
        wait_for_network_idle(driver)

        logger.info("Starting human-like scrolling...")
        total_height = driver.execute_script("return document.body.scrollHeight")
        for i in range(0, total_height, 500):
            driver.execute_script(f"window.scrollTo(0, {i});"); time.sleep(0.3)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        logger.info("Scrolling complete. Waiting for new content.")
        wait_for_network_idle(driver); time.sleep(2)

        logger.info(f"Executing MAX-QUALITY image extraction script on {url}.")
        js_extraction_script = "const imageCandidates=new Map();const imageExtensions=/\\.(jpeg|jpg|png|gif|webp|bmp|svg)/i;function addCandidate(key,url,priority){if(!url||url.startsWith('data:image'))return;if(!imageCandidates.has(key)){imageCandidates.set(key,{url:url,priority:priority})}else if(priority>imageCandidates.get(key).priority){imageCandidates.set(key,{url:url,priority:priority})}}document.querySelectorAll('img').forEach((img,index)=>{const key=img.src||`img_${index}`;addCandidate(key,img.src,1);if(img.dataset.src)addCandidate(key,img.dataset.src,1);if(img.srcset){let maxUrl=null;let maxWidth=0;img.srcset.split(',').forEach(part=>{const parts=part.trim().split(/\\s+/);const url=parts[0];const widthMatch=parts[1]?parts[1].match(/(\\d+)w/):null;if(widthMatch){const width=parseInt(widthMatch[1],10);if(width>maxWidth){maxWidth=width;maxUrl=url}}else{maxUrl=url}});if(maxUrl)addCandidate(key,maxUrl,2)}const parentAnchor=img.closest('a');if(parentAnchor&&parentAnchor.href&&imageExtensions.test(parentAnchor.href)){addCandidate(key,parentAnchor.href,3)}});document.querySelectorAll('*').forEach(el=>{const style=window.getComputedStyle(el,null).getPropertyValue('background-image');if(style&&style.includes('url')){const match=style.match(/url\\([\"']?([^\"']*)[\"']?\\)/);if(match&&match[1]){addCandidate(match[1],match[1],1)}}});return Array.from(imageCandidates.values()).map(c=>c.url);"
        extracted_urls = driver.execute_script(js_extraction_script)
        logger.info(f"JS script extracted {len(extracted_urls)} candidate URLs.")
        
        for img_url in extracted_urls:
            if img_url and img_url.strip():
                absolute_url = urljoin(url, img_url.strip())
                max_quality_guess = get_max_quality_url(absolute_url)
                images.add(max_quality_guess)

    except Exception as e: logger.error(f"Error scraping {url}: {e}", exc_info=True)
    finally:
        logger.info(f"Quitting driver for {url}."); driver.quit()
    
    final_images = {img for img in images if img and img.startswith('http')}
    logger.info(f"Returning {len(final_images)} valid, max-quality image URLs from {url}.")
    return final_images

# --- UTILITY, PERMISSION & WORKER COMMANDS ---
async def get_userbot_client(session_string: str):
    if not session_string: return None
    client = TelegramClient(StringSession(session_string), int(API_ID), API_HASH)
    await client.connect(); return client

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = (f"Hi <b>{user.mention_html()}</b>! Welcome to the Image Scraper Bot.\n\n"
               "<b>Core Commands:</b>\n"
               "• `/scrape [url]` - Scrape a single URL.\n"
               "• `/deepscrape [url]` - Scrape all links found on a URL.\n\n"
               "<b>Setup Commands:</b>\n"
               "• `/settarget [chat_id]` - Set target for single scrapes.\n"
               "• `/setgroup [group_id]` - Set target group for deep scrapes.\n"
               "• `/login` - Login with your user account (needed for `/creategroup` and `/addworkers`).\n\n"
               "<b>Worker Bot Fleet (for Deep Scrapes):</b>\n"
               "• `/addworkers [bot_token] [bot_token]...`\n"
               "• `/listworkers`\n"
               "• `/removeworkers [bot_id] [bot_id]...`\n\n"
               "<b>Other Commands:</b>\n"
               "• `/status`, `/mydata`, `/stop`")
    await update.message.reply_html(message)

async def addworkers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = await db.get_user_data(user_id)
    if not ud or 'session_string' not in ud:
        return await update.message.reply_html("You must `/login` with your user account first to manage workers.")
    if not ud.get('target_group_id'):
        return await update.message.reply_html("You must `/setgroup` first before adding workers.")
    if not context.args:
        return await update.message.reply_html("<b>Usage:</b> `/addworkers [bot_token_1] [bot_token_2]...`")

    msg = await update.message.reply_html("Processing worker tokens...")
    added_workers, failed_workers = [], []
    
    target_group_id = int(ud.get('target_group_id'))
    admin_rights = ChatAdminRights(post_messages=True, edit_messages=True, delete_messages=True)

    async with await get_userbot_client(ud['session_string']) as client:
        for token in context.args:
            try:
                worker_bot = ExtBot(token=token)
                worker_info = await worker_bot.get_me()
                
                await msg.edit_text(f"Inviting and promoting @{worker_info.username}...")
                try:
                    await client(functions.channels.InviteToChannelRequest(target_group_id, [worker_info.id]))
                except UserAlreadyParticipantError:
                    pass # Bot is already in the group, that's fine
                await client(functions.channels.EditAdminRequest(target_group_id, worker_info.id, admin_rights, "Worker Bot"))
                
                worker_data = {"id": worker_info.id, "username": worker_info.username, "token": token}
                await db.add_worker_bots(user_id, [worker_data])
                added_workers.append(f"• @{worker_info.username} (<code>{worker_info.id}</code>)")
            except Exception as e:
                failed_workers.append(f"• <code>{token[:8]}...</code> (Error: {e})")

    response_text = "✅ <b>Worker setup complete!</b>\n\n"
    if added_workers:
        response_text += "<b>Added & Promoted:</b>\n" + "\n".join(added_workers) + "\n\n"
    if failed_workers:
        response_text += "<b>Failed:</b>\n" + "\n".join(failed_workers)
    
    await msg.edit_text(response_text, parse_mode=ParseMode.HTML)

async def listworkers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    workers = await db.get_worker_bots(update.effective_user.id)
    if not workers:
        return await update.message.reply_html("You have no worker bots configured.")
    
    worker_list = [f"• @{w['username']} (<code>{w['id']}</code>)" for w in workers]
    await update.message.reply_html("<b>Your configured worker bots:</b>\n" + "\n".join(worker_list))

async def removeworkers_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_html("<b>Usage:</b> `/removeworkers [bot_id_1] [bot_id_2]...`")
    
    try:
        worker_ids = [int(arg) for arg in context.args]
        await db.remove_worker_bots(update.effective_user.id, worker_ids)
        await update.message.reply_html(f"✅ Removed bots with IDs: <code>{', '.join(context.args)}</code> from the worker pool.")
    except ValueError:
        await update.message.reply_html("Invalid bot ID. Please provide numeric IDs only.")

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
    worker_count = len((ud or {}).get('worker_bots', []))
    message = (f"<b>Your Settings:</b>\n\n"
               f"👤 <b>Logged In:</b> {session_set}\n"
               f"🎯 <b>Single Scrape Target:</b> <code>{target_chat}</code>\n"
               f"🗂️ <b>Deep Scrape Target:</b> <code>{target_group}</code>\n"
               f"🤖 <b>Worker Bots:</b> {worker_count}")
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
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/setgroup [supergroup_id]</code>")
    group_id = context.args[0]
    await update.message.reply_html(f"Verifying I am an admin in <code>{group_id}</code>...")
    try:
        chat_admins = await context.bot.get_chat_administrators(chat_id=group_id)
        bot_is_admin = any(admin.user.id == context.bot.id for admin in chat_admins)
        if not bot_is_admin:
            return await update.message.reply_html(f"<b>Permission Denied!</b>\nI am not an administrator in that group.")
    except Exception as e:
        return await update.message.reply_html(f"<b>Could not verify permissions.</b>\n<b>Error:</b> <code>{e}</code>")
    await db.save_user_data(update.effective_user.id, {'target_group_id': group_id})
    await update.message.reply_html(f"✅ Deep scrape target group set to <code>{group_id}</code>.")

# --- CORE LOGIC HANDLERS ---
async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please send your Telethon session string.")
    return 'awaiting_session'
# ... (scrape_command, status_callback, etc. are largely unchanged)
async def handle_login_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_string = update.message.text.strip(); await update.message.reply_text("Validating session...")
    try:
        async with await get_userbot_client(session_string) as client:
            if not client or not await client.is_user_authorized(): return await update.message.reply_text("❌ Login failed.")
            me = await client.get_me()
            await db.save_user_data(update.effective_user.id, {'session_string': session_string})
            await update.message.reply_html(f"✅ Logged in as <b>{me.first_name}</b>!")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}"); return ConversationHandler.END

async def scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = find_url_in_text(update.message.text) or (context.args[0] if context.args else None)
    if not url: await update.message.reply_html("<b>Usage:</b> <code>/scrape [url]</code>"); return ConversationHandler.END
    
    context.user_data['is_scraping'] = True
    msg = await update.message.reply_html(f"🔎 Scraping <code>{preprocess_url(url)}</code>...")
    images = await asyncio.to_thread(scrape_images_from_url_sync, preprocess_url(url), context.user_data)
    if not context.user_data.get('is_scraping', False): return ConversationHandler.END
    
    if not images: await msg.edit_text("Could not find any images on that page."); return ConversationHandler.END
    
    context.user_data['scraped_images'] = list(images)
    file_types = Counter(get_file_extension(img) for img in images)
    keyboard = [[InlineKeyboardButton(f"{(ext or 'Other').upper()} ({count})", callback_data=f"scrape_{ext or 'none'}")] for ext, count in file_types.items()]
    keyboard.append([InlineKeyboardButton(f"All Files ({len(images)})", callback_data="scrape_all")])
    await msg.edit_text("✅ <b>Scan complete!</b> Choose file type:", reply_markup=InlineKeyboardMarkup(keyboard))
    return 'awaiting_file_type'
async def scrape_file_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
        try: await context.bot.send_photo(target_chat_id, photo=img)
        except Exception as e: logger.warning(f"Failed to send photo {img} to {target_chat_id}: {e}")
    context.user_data['is_scraping'] = False
    await query.message.reply_html("✅ <b>Sending complete!</b>")
    return ConversationHandler.END
async def status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    task = await db.get_user_active_task(update.effective_user.id)
    if not task:
        await query.edit_message_text("No active deepscrape task found, or it has completed.")
        return

    message = (
        f"📊 <b>Task Status:</b> {task['status'].title()}\n"
        f"🔗 <b>Progress:</b> Link {task['current_link_index'] + 1} of {task['total_links']}\n"
        f"📄 <b>Current URL:</b> <code>...{task['current_link_url'][-50:]}</code>\n"
        f"🖼️ <b>Images Found:</b> {task['current_link_images_found']}\n"
        f"📤 <b>Images Uploaded:</b> {task['current_link_images_uploaded']}"
    )
    keyboard = [[InlineKeyboardButton("Refresh Status", callback_data="show_status")]]
    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
async def conv_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stop_command(update, context); return ConversationHandler.END
async def creategroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = await db.get_user_data(update.effective_user.id)
    if not ud or 'session_string' not in ud: return await update.message.reply_html("You must /login first.")
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/creategroup [Name]</code>")
    group_name = " ".join(context.args)
    msg = await update.message.reply_html(f"Creating supergroup '<b>{group_name}</b>'...")
    try:
        async with await get_userbot_client(ud['session_string']) as client:
            created_chat = await client(functions.messages.CreateChatRequest(users=[(await context.bot.get_me()).username], title=group_name))
            chat_id = created_chat.chats[0].id
            await client(functions.channels.ConvertToGigagroupRequest(channel=chat_id))
            full_channel = await client(functions.channels.GetFullChannelRequest(channel=chat_id))
            supergroup_id = int(f"-100{full_channel.full_chat.id}")
            await client.edit_admin(entity=supergroup_id, user=context.bot.id, is_admin=True, manage_topics=True)
            await db.save_user_data(update.effective_user.id, {'target_group_id': str(supergroup_id)})
            await msg.edit_text(f"✅ Supergroup created & set as target!\n<b>ID:</b> <code>{supergroup_id}</code>", parse_mode=ParseMode.HTML)
    except Exception as e: await msg.edit_text(f"An error occurred: {e}", parse_mode=ParseMode.HTML)
async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ud = await db.get_user_data(user_id)
    if await db.get_user_active_task(user_id): return await update.message.reply_html("You already have an active deepscrape task.")
    if not ud or not ud.get('target_group_id'): return await update.message.reply_html("❌ Target group not set. Use /setgroup.")
    if not context.args: return await update.message.reply_html("<b>Usage:</b> <code>/deepscrape [url]</code>")
    base_url = preprocess_url(context.args[0])
    msg = await update.message.reply_html(f"Scanning <code>{base_url}</code> for links...")
    try:
        links_response = await asyncio.to_thread(requests.get, base_url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(links_response.content, 'html.parser')
        links = sorted(list({urljoin(base_url, a['href']) for a in soup.find_all('a', href=True) if urljoin(base_url, a.get('href', '')) != base_url and not (urljoin(base_url, a.get('href', ''))).endswith(('.zip', '.rar', '.exe', '.pdf'))}))
        if not links: return await msg.edit_text("Found no unique, valid links.")
        task_id = await db.create_task(user_id, base_url, links)
        keyboard = [[InlineKeyboardButton("Show Status", callback_data="show_status")]]
        await msg.edit_text(f"Found {len(links)} links. Starting deep scrape.", reply_markup=InlineKeyboardMarkup(keyboard))
        context.application.create_task(_run_deepscrape_task(user_id, task_id, context.application))
    except Exception as e: await msg.edit_text(f"Failed to fetch links. Error: {e}")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = await db.get_user_active_task(update.effective_user.id)
    if not task: return await update.message.reply_html("No active deepscrape task found.")
    keyboard = [[InlineKeyboardButton("Show Status", callback_data="show_status")]]
    await update.message.reply_html("You have an active deepscrape task.", reply_markup=InlineKeyboardMarkup(keyboard))

async def _run_deepscrape_task(user_id, task_id, application: Application):
    ud = await db.get_user_data(user_id)
    target_group = ud.get('target_group_id')
    if not target_group:
        await application.bot.send_message(user_id, "<b>Error:</b> Target group not set."); return

    worker_bots_data = await db.get_worker_bots(user_id)
    worker_clients = [ExtBot(token=w['token']) for w in worker_bots_data]
    
    # Use main bot as fallback if no workers are configured
    if not worker_clients:
        worker_clients.append(application.bot)
        await application.bot.send_message(user_id, "⚠️ No worker bots found. Using main bot for uploads. This may be slow.")

    worker_pool = deque(worker_clients)
    resting_workers = {} # {bot_id: unban_timestamp}

    await db.update_task_status(task_id, "running")
    await application.bot.send_message(user_id, f"🚀 <b>Deepscrape task started with {len(worker_clients)} uploader(s)!</b>", parse_mode=ParseMode.HTML)
    
    try:
        while True:
            task = await db.tasks_collection.find_one({"_id": task_id})
            if not task or task['status'] != 'running': break

            pending_links = [link for link in task['all_links'] if link not in task['completed_links']]
            if not pending_links:
                await db.update_task_status(task_id, "completed"); await application.bot.send_message(user_id, "✅ <b>Deep scrape finished!</b>"); break

            link = pending_links[0]
            current_link_num = len(task['completed_links']) + 1
            await db.update_task_counters(task_id, current_link_num - 1, link, 0)
            await application.bot.send_message(user_id, f"<b>Processing Link {current_link_num}/{task['total_links']}:</b>\n<code>{link}</code>", parse_mode=ParseMode.HTML)
            
            images = await asyncio.to_thread(scrape_images_from_url_sync, link, {"is_scraping": True})
            if task.get('status') == 'stopped': break
            if not images:
                await db.complete_link_in_task(task_id, link); continue
            
            await db.update_task_counters(task_id, current_link_num - 1, link, len(images))
            
            try:
                topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98] or "Scraped Images"
                created_topic = await application.bot.create_forum_topic(chat_id=target_group, name=topic_title)
                topic_id = created_topic.message_thread_id
            except Exception as e:
                await application.bot.send_message(user_id, f"<b>Error creating topic:</b> {e}. Stopping task."); await db.update_task_status(task_id, "paused"); return
            
            await application.bot.send_message(user_id, f"Found {len(images)} images. Starting upload with worker fleet...")
            
            upload_tasks = []
            image_queue = deque(images)

            async def upload_worker(worker_bot):
                while image_queue:
                    try:
                        img_url = image_queue.popleft()
                    except IndexError:
                        break # Queue is empty
                    
                    try:
                        await worker_bot.send_photo(target_group, photo=img_url, message_thread_id=topic_id)
                        await db.increment_task_image_upload_count(task_id)
                    except RetryAfter as e:
                        logger.warning(f"Worker @{worker_bot.username} hit flood wait for {e.retry_after}s. Resting.")
                        image_queue.appendleft(img_url) # Put image back
                        await asyncio.sleep(e.retry_after + 2)
                    except Exception as e:
                        logger.error(f"Worker @{worker_bot.username} failed to send {img_url}: {e}")
            
            for worker in worker_clients:
                upload_tasks.append(asyncio.create_task(upload_worker(worker)))
            
            await asyncio.gather(*upload_tasks)

            if task.get('status') == 'running':
                await db.complete_link_in_task(task_id, link)
                await application.bot.send_message(user_id, f"Finished uploading for link {current_link_num}.")
    
    except Exception as e:
        logger.error(f"Error in _run_deepscrape_task: {e}", exc_info=True)
        await db.update_task_status(task_id, "paused")
        await application.bot.send_message(user_id, f"An unexpected error occurred: <code>{e}</code>. Task paused.", parse_mode=ParseMode.HTML)

# --- Main Application Setup ---
def main():
    if not BOT_TOKEN: logger.critical("CRITICAL: BOT_TOKEN is MISSING."); sys.exit(1)
    persistence = PicklePersistence(filepath="./bot_persistence")
    application = (Application.builder().token(BOT_TOKEN).persistence(persistence).post_init(post_init_callback).build())

    scrape_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("scrape", scrape_command)],
        states={'awaiting_file_type': [CallbackQueryHandler(scrape_file_type_callback, pattern="^scrape_")]},
        fallbacks=[CommandHandler("cancel", conv_fallback), CommandHandler("stop", conv_fallback)],
        persistent=True, name="scrape_conv", conversation_timeout=600
    )
    login_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("login", login_command)],
        states={'awaiting_session': [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_session)]},
        fallbacks=[CommandHandler("cancel", conv_fallback), CommandHandler("stop", conv_fallback)],
        persistent=True, name="login_conv"
    )

    handlers = [
        CommandHandler("start", start_command), CommandHandler("help", help_command),
        CommandHandler("stop", stop_command), CommandHandler("cancel", cancel_command),
        CommandHandler("mydata", mydata_command), CommandHandler("status", status_command),
        CommandHandler("settarget", settarget_command), CommandHandler("setgroup", setgroup_command),
        CommandHandler("creategroup", creategroup_command), CommandHandler("deepscrape", deepscrape_command),
        CommandHandler("addworkers", addworkers_command), CommandHandler("listworkers", listworkers_command),
        CommandHandler("removeworkers", removeworkers_command),
        login_conv_handler, scrape_conv_handler,
        CallbackQueryHandler(status_callback, pattern="^show_status$"),
    ]
    application.add_handlers(handlers)
    logger.info("All handlers registered. Starting bot polling...")
    application.run_polling()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    main()
