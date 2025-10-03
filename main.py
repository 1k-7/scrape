# main.py
# This is the main file for the Telegram Image Scraper Bot.
# It handles the bot logic, database integration, and on-demand userbot client creation.

import os
import asyncio
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import UserIsBotError, SessionPasswordNeededError

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

# --- Local Imports ---
import database as db

# --- Configuration ---
load_dotenv()

# --- Bot Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- Telethon Userbot Configuration ---
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

# --- Conversation states for /login command ---
SESSION_STRING = range(1)

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Helper Functions ---
def is_valid_url(url):
    """Checks if a string is a valid HTTP/HTTPS URL."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc]) and result.scheme in ['http', 'https']
    except (ValueError, AttributeError):
        return False

def setup_selenium_driver():
    """Initializes and returns a headless Selenium Chrome WebDriver."""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu") # Added for stability in headless environments
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

async def scrape_images_from_url(url: str) -> set:
    """Scrapes all images from a given URL, including those loaded by scrolling."""
    logger.info(f"Starting to scrape images from: {url}")
    driver = setup_selenium_driver()
    images = set()

    try:
        driver.get(url)
        await asyncio.sleep(3)
        last_height = driver.execute_script("return document.body.scrollHeight")
        for _ in range(5): # Limit scrolls to prevent infinite loops
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            await asyncio.sleep(2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        soup = BeautifulSoup(driver.page_source, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                absolute_src = urljoin(url, src)
                if absolute_src.startswith('http') and urlparse(absolute_src).netloc:
                    images.add(absolute_src)
    except Exception as e:
        logger.error(f"An error occurred while scraping {url}: {e}")
    finally:
        driver.quit()
    
    logger.info(f"Found {len(images)} images on {url}")
    return images

# --- Userbot Client Management ---
async def get_userbot_client(session_string: str) -> TelegramClient:
    """Creates and connects a Telethon client from a session string."""
    if not session_string:
        return None
    
    client = TelegramClient(StringSession(session_string), int(API_ID), API_HASH)
    await client.connect()
    return client

# --- Telegram Bot Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}! Welcome to the Image Scraper Bot 🖼️\n\n"
        "I can fetch images from webpages. For advanced features, you'll need to log in with a user account.\n\n"
        "<b>Commands:</b>\n"
        "/login - Log in with your user account using a session string.\n"
        "/scrape `[url]` - Scrape all images from a single URL.\n"
        "/deepscrape `[url]` - Scrape images from all links on a page and upload to a supergroup.\n"
        "/setgroup `[id/@username]` - Set the target supergroup for deep scraping.\n"
        "/creategroup `[name]` - Create a new supergroup and set it as the target.\n"
        "/help - Show this message."
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the login conversation to get the user's session string."""
    await update.message.reply_text(
        "To log in, please send me your Telethon session string.\n\n"
        "You can generate one using the `generate_session.py` script provided with the bot. "
        "Run it on your local machine, log in, and paste the string it gives you here.\n\n"
        "Send /cancel to abort."
    )
    return SESSION_STRING

async def received_session_string(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Saves the received session string to the database."""
    user_id = update.effective_user.id
    session_string = update.message.text.strip()
    
    await update.message.reply_text("Validating session string...")

    try:
        # Test the session string by trying to connect
        client = await get_userbot_client(session_string)
        if not client or not await client.is_user_authorized():
            await update.message.reply_text("Login failed. The session string seems to be invalid or expired. Please generate a new one.")
            await client.disconnect()
            return ConversationHandler.END
        
        me = await client.get_me()
        await db.save_user_data(user_id, {'session_string': session_string})
        await update.message.reply_text(f"✅ Successfully logged in as {me.first_name}! Your session is saved.")
        await client.disconnect()

    except SessionPasswordNeededError:
        await update.message.reply_text("This session string is protected by a 2FA password, which is not supported in this mode. Please generate a new session without 2FA or remove it.")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Session validation failed for user {user_id}: {e}")
        await update.message.reply_text(f"An error occurred during validation: {e}. Please try again.")
        return ConversationHandler.END

    return ConversationHandler.END

async def cancel_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the login conversation."""
    await update.message.reply_text("Login process cancelled.")
    return ConversationHandler.END

async def scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scrapes images from a single URL."""
    chat_id = update.message.chat_id
    if not context.args:
        await update.message.reply_text("Usage: `/scrape https://example.com`")
        return

    url = context.args[0]
    if not is_valid_url(url):
        await update.message.reply_text("Invalid URL. Please include `http://` or `https://`.")
        return

    await update.message.reply_text(f"Scraping {url}... This may take a moment.")
    images = await scrape_images_from_url(url)

    if not images:
        await update.message.reply_text("Could not find any images on that page.")
        return

    await update.message.reply_text(f"Found {len(images)} images. Sending now...")
    for image_url in images:
        try:
            await context.bot.send_photo(chat_id, photo=image_url)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Failed to send image {image_url}: {e}")

    await update.message.reply_text("Finished sending images!")

async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the target supergroup for the user."""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: `/setgroup -100123456789` or `/setgroup @mychannel`")
        return
    
    group_id = context.args[0]
    await db.save_user_data(user_id, {'target_group_id': group_id})
    await update.message.reply_text(f"Target supergroup set to: {group_id}.")

async def creategroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Creates a new supergroup using the userbot."""
    user_id = update.effective_user.id
    user_data = await db.get_user_data(user_id)
    if not user_data or 'session_string' not in user_data:
        await update.message.reply_text("You need to log in first. Use the /login command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/creategroup My Scraped Images`")
        return
        
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
        await update.message.reply_text(
            f"✅ Successfully created '{group_name}'!\nID: `{supergroup_id}`\nIt's now your target group.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Failed to create group for user {user_id}: {e}")
        await update.message.reply_text(f"An error occurred: {e}")
    finally:
        if user_client: await user_client.disconnect()

async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deep scrapes a URL and uploads to a supergroup."""
    user_id = update.effective_user.id
    user_data = await db.get_user_data(user_id)

    if not user_data or 'session_string' not in user_data:
        await update.message.reply_text("You must log in first with /login.")
        return
    if 'target_group_id' not in user_data:
        await update.message.reply_text("No target supergroup set. Use /setgroup or /creategroup.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/deepscrape https://example.com`")
        return
    
    base_url = context.args[0]
    if not is_valid_url(base_url):
        await update.message.reply_text("Invalid URL.")
        return

    target_group = user_data['target_group_id']
    await update.message.reply_text(f"Starting deep scrape of {base_url}. This will take time. Results will be in `{target_group}`.", parse_mode=ParseMode.MARKDOWN)

    user_client = None
    try:
        user_client = await get_userbot_client(user_data['session_string'])
        entity = await user_client.get_entity(int(target_group) if target_group.startswith('-') else target_group)
        
        response = requests.get(base_url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(response.content, 'html.parser')
        links = {urljoin(base_url, a['href']) for a in soup.find_all('a', href=True) if is_valid_url(urljoin(base_url, a.get('href', '')))}
        
        await update.message.reply_text(f"Found {len(links)} valid links to process.")

        for i, link in enumerate(links):
            topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98]
            if not topic_title: continue

            await context.bot.send_message(update.effective_chat.id, f"({i+1}/{len(links)}) Processing: {link}\nTopic: '{topic_title}'")
            
            try:
                topic_result = await user_client(functions.channels.CreateForumTopicRequest(channel=entity, title=topic_title, random_id=context.bot._get_private_random_id()))
                topic_id = topic_result.updates[0].message.id
                
                images = await scrape_images_from_url(link)
                if not images:
                    await user_client.send_message(entity, message=f"No images found on\n{link}", reply_to=topic_id)
                    continue
                
                for img_url in images:
                    try:
                        await user_client.send_file(entity, file=img_url, reply_to=topic_id)
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.warning(f"Could not upload {img_url} to topic {topic_id}: {e}")
                        await user_client.send_message(entity, message=f"Failed to upload:\n`{img_url}`", reply_to=topic_id)

            except Exception as e:
                logger.error(f"Failed to process link {link}: {e}")
                await context.bot.send_message(update.effective_chat.id, f"Error on {link}: {e}")
        
        await update.message.reply_text("✅ Deep scrape finished!")

    except Exception as e:
        logger.error(f"Critical error during deep scrape for user {user_id}: {e}")
        await update.message.reply_text(f"A critical error occurred: {e}")
    finally:
        if user_client: await user_client.disconnect()


# --- Main Application Setup ---
def main():
    """Start the bot."""
    if not all([BOT_TOKEN, API_ID, API_HASH, db.MONGO_URI]):
        logger.critical("One or more environment variables are missing (BOT_TOKEN, API_ID, API_HASH, MONGO_URI).")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    login_handler = ConversationHandler(
        entry_points=[CommandHandler("login", login_command)],
        states={
            SESSION_STRING: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_session_string)],
        },
        fallbacks=[CommandHandler("cancel", cancel_login)],
    )

    application.add_handler(login_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("scrape", scrape_command))
    application.add_handler(CommandHandler("setgroup", setgroup_command))
    application.add_handler(CommandHandler("creategroup", creategroup_command))
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))

    logger.info("Telegram bot starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
