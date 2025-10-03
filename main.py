# main.py
import os
import asyncio
import logging
from urllib.parse import urljoin, urlparse
import time
import threading

# --- NEW: Import Flask for the web server ---
from flask import Flask

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from telegram.constants import ParseMode

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, ElementClickInterceptedException

import database as db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_STRING = range(1)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING) # Quiets the Flask startup messages
logger = logging.getLogger(__name__)

# --- Flask Web Server Setup ---
app = Flask(__name__)

@app.route('/')
def health_check():
    """Endpoint for Render's health checks."""
    return "Bot is alive and running!", 200

def run_web_server():
    """Runs the Flask app in a separate thread."""
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- Helper Functions ---
def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc]) and result.scheme in ['http', 'https']
    except (ValueError, AttributeError):
        return False

def setup_selenium_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(45)
    return driver

# --- Smart Scraping Logic ---
async def handle_popups_and_scroll_aggressively(driver: webdriver.Chrome):
    logger.info("Searching for popups to click...")
    popup_keywords = ['accept', 'agree', 'enter', 'continue', 'confirm', 'i am 18', 'yes', 'i agree']
    js_script = f"""
    var keywords = {popup_keywords};
    var buttons = document.querySelectorAll('button, a, div, span');
    var clicked = false;
    for (var i = 0; i < buttons.length; i++) {{
        var buttonText = buttons[i].innerText.toLowerCase();
        for (var j = 0; j < keywords.length; j++) {{
            if (buttonText.includes(keywords[j])) {{
                buttons[i].click();
                console.log('Clicked element with text: ' + buttonText);
                clicked = true;
                break;
            }}
        }}
        if (clicked) break;
    }}
    return clicked;
    """
    try:
        if driver.execute_script(js_script):
            logger.info("Successfully clicked a popup element via JavaScript.")
            await asyncio.sleep(3)
    except Exception as e:
        logger.warning(f"Could not execute popup click script: {e}")

    logger.info("Starting advanced scrolling...")
    scroll_pause_time = 2.5
    last_image_count = 0
    stable_count = 0

    while stable_count < 3:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        await asyncio.sleep(scroll_pause_time)
        current_images = driver.find_elements(By.TAG_NAME, "img")
        current_image_count = len([img for img in current_images if img.get_attribute('src')])
        if current_image_count == last_image_count:
            stable_count += 1
            logger.info(f"Image count stable: {stable_count}/3")
        else:
            stable_count = 0
            logger.info(f"Image count increased from {last_image_count} to {current_image_count}.")
        last_image_count = current_image_count

async def scrape_images_from_url(url: str) -> set:
    logger.info(f"Starting to scrape images from: {url}")
    driver = setup_selenium_driver()
    images = set()

    try:
        driver.get(url)
        await handle_popups_and_scroll_aggressively(driver)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src and src.strip(): images.add(urljoin(url, src.strip()))
        for pic in soup.find_all("picture"):
            for source in pic.find_all("source"):
                srcset = source.get("srcset")
                if srcset and srcset.strip():
                    first_url = srcset.strip().split(',')[0].split(' ')[0]
                    images.add(urljoin(url, first_url))
    except Exception as e:
        logger.error(f"Error scraping {url}: {e}", exc_info=True)
    finally:
        driver.quit()
    
    valid_images = {img for img in images if is_valid_url(img)}
    logger.info(f"Found {len(valid_images)} valid images on {url}")
    return valid_images

# --- Userbot and Bot Command Handlers ---
async def get_userbot_client(session_string: str) -> TelegramClient:
    if not session_string: return None
    client = TelegramClient(StringSession(session_string), int(API_ID), API_HASH)
    await client.connect()
    return client

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! Welcome to the Image Scraper Bot 🖼️\n\n"
        "<b>Commands:</b>\n"
        "/login - Log in with your user account.\n"
        "/scrape `[url]` - Scrape all images from a single URL.\n"
        "/deepscrape `[url]` - Scrape all links on a page and upload images.\n"
        "/setgroup `[id]` - Set the target supergroup.\n"
        "/creategroup `[name]` - Create a new supergroup.\n"
        "/help - Show this message."
    )

# --- THIS IS THE FIX ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the help message by reusing the start_command logic."""
    await start_command(update, context)

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Please send your Telethon session string.\nSend /cancel to abort.")
    return SESSION_STRING

async def received_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    session_string = update.message.text.strip()
    await update.message.reply_text("Validating session string...")
    try:
        client = await get_userbot_client(session_string)
        if not client or not await client.is_user_authorized():
            await update.message.reply_text("Login failed. Invalid session.")
            if client: await client.disconnect()
            return ConversationHandler.END
        me = await client.get_me()
        await db.save_user_data(user_id, {'session_string': session_string})
        await update.message.reply_text(f"✅ Logged in as {me.first_name}! Session saved.")
        await client.disconnect()
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}.")
        return ConversationHandler.END
    return ConversationHandler.END

async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Login cancelled.")
    return ConversationHandler.END

async def scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Usage: `/scrape https://example.com`")
    url = context.args[0]
    if not is_valid_url(url): return await update.message.reply_text("Invalid URL.")
    await update.message.reply_text(f"Scraping {url}... This may take some time.")
    images = await scrape_images_from_url(url)
    if not images: return await update.message.reply_text("Could not find any images.")
    await update.message.reply_text(f"Found {len(images)} images. Sending now...")
    for image_url in images:
        try:
            await context.bot.send_photo(update.message.chat_id, photo=image_url)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Failed to send image {image_url}: {e}")
    await update.message.reply_text("Finished sending images!")

async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Usage: `/setgroup -100123...`")
    await db.save_user_data(update.effective_user.id, {'target_group_id': context.args[0]})
    await update.message.reply_text(f"Target supergroup set to: {context.args[0]}.")

async def creategroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = await db.get_user_data(user_id)
    if not user_data or 'session_string' not in user_data: return await update.message.reply_text("You need to /login first.")
    if not context.args: return await update.message.reply_text("Usage: `/creategroup My Group Name`")
    group_name = " ".join(context.args)
    await update.message.reply_text(f"Creating supergroup '{group_name}'...")
    user_client = None
    try:
        user_client = await get_userbot_client(user_data['session_string'])
        created_chat = await user_client(functions.messages.CreateChatRequest(users=["me"], title=group_name))
        chat_id = created_chat.chats[0].id
        await user_client(functions.channels.ConvertToGigagroupRequest(channel=chat_id))
        full_channel = await user_client(functions.channels.GetFullChannelRequest(channel=chat_id))
        supergroup_id = int(f"-100{full_channel.full_chat.id}")
        await db.save_user_data(user_id, {'target_group_id': supergroup_id})
        await update.message.reply_text(f"✅ Created '{group_name}'!\nID: `{supergroup_id}`\nIt's now your target group.", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {e}")
    finally:
        if user_client: await user_client.disconnect()

async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = await db.get_user_data(user_id)
    if not user_data or 'session_string' not in user_data: return await update.message.reply_text("You must /login first.")
    if 'target_group_id' not in user_data: return await update.message.reply_text("No target group set. Use /setgroup or /creategroup.")
    if not context.args: return await update.message.reply_text("Usage: `/deepscrape https://example.com`")
    base_url = context.args[0]
    if not is_valid_url(base_url): return await update.message.reply_text("Invalid URL.")
    target_group, user_client = user_data['target_group_id'], None
    await update.message.reply_text(f"Starting deep scrape of {base_url}. This will take time...")
    try:
        user_client = await get_userbot_client(user_data['session_string'])
        entity = await user_client.get_entity(int(target_group) if target_group.startswith('-') else target_group)
        response = requests.get(base_url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(response.content, 'html.parser')
        links = {urljoin(base_url, a['href']) for a in soup.find_all('a', href=True) if is_valid_url(urljoin(base_url, a.get('href', '')))}
        await update.message.reply_text(f"Found {len(links)} links to process.")
        for i, link in enumerate(links):
            topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98]
            if not topic_title: continue
            await context.bot.send_message(update.effective_chat.id, f"({i+1}/{len(links)}) Processing: {link}")
            try:
                topic = await user_client(functions.channels.CreateForumTopicRequest(channel=entity, title=topic_title, random_id=context.bot._get_private_random_id()))
                topic_id = topic.updates[0].message.id
                images = await scrape_images_from_url(link)
                if not images: await user_client.send_message(entity, message=f"No images found on\n{link}", reply_to=topic_id)
                else:
                    for img_url in images:
                        try:
                            await user_client.send_file(entity, file=img_url, reply_to=topic_id)
                            await asyncio.sleep(1)
                        except Exception as e: logger.warning(f"Could not upload {img_url}: {e}")
            except Exception as e: logger.error(f"Failed to process link {link}: {e}")
        await update.message.reply_text("✅ Deep scrape finished!")
    except Exception as e: await update.message.reply_text(f"A critical error occurred: {e}")
    finally:
        if user_client: await user_client.disconnect()

def run_bot():
    if not all([BOT_TOKEN, API_ID, API_HASH, db.MONGO_URI]):
        logger.critical("One or more environment variables are missing.")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    login_handler = ConversationHandler(
        entry_points=[CommandHandler("login", login_command)],
        states={SESSION_STRING: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_session_string)]},
        fallbacks=[CommandHandler("cancel", cancel_login)],
    )
    application.add_handler(login_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command)) # This line now works
    application.add_handler(CommandHandler("scrape", scrape_command))
    application.add_handler(CommandHandler("setgroup", setgroup_command))
    application.add_handler(CommandHandler("creategroup", creategroup_command))
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))
    
    logger.info("Telegram bot starting polling...")
    application.run_polling()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    run_bot()
