# main.py
# This is the main file for the Telegram Image Scraper Bot.
# It handles both the bot logic for user interaction and the userbot logic for channel management.

import os
import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telethon import TelegramClient, functions
from telethon.tl.types import InputPeerChannel
from telethon.errors import UserIsBotError

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

# --- Configuration ---
# Load environment variables from .env file
load_dotenv()

# --- Basic Bot Configuration ---
BOT_TOKEN = os.getenv("BOT_TOKEN")

# --- Telethon Userbot Configuration ---
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = "userbot_session"

# --- State Management (for simplicity, in-memory. For production, use a database) ---
# Stores the target supergroup ID for each user. {user_id: target_group_id}
user_target_group = {}

# --- Logging Setup ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --- Telethon Client Initialization ---
# We initialize it here to be used by command handlers
try:
    user_client = TelegramClient(SESSION_NAME, int(API_ID), API_HASH)
except (ValueError, TypeError) as e:
    logger.error(f"API_ID or API_HASH are not set correctly in your .env file. Please check them. Error: {e}")
    exit(1)


# --- Helper Functions ---
def is_valid_url(url):
    """Checks if a string is a valid HTTP/HTTPS URL."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc]) and result.scheme in ['http', 'https']
    except ValueError:
        return False

def setup_selenium_driver():
    """Initializes and returns a headless Selenium Chrome WebDriver."""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    return driver

async def scrape_images_from_url(url: str) -> set:
    """
    Scrapes all images from a given URL, including those loaded by scrolling.
    """
    logger.info(f"Starting to scrape images from: {url}")
    driver = setup_selenium_driver()
    images = set()

    try:
        driver.get(url)
        # Wait for the page to initially load
        await asyncio.sleep(3)

        # Scroll down to trigger lazy loading
        last_height = driver.execute_script("return document.body.scrollHeight")
        while True:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            await asyncio.sleep(2)  # Wait for new images to load
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        # Parse the final page source with BeautifulSoup
        soup = BeautifulSoup(driver.page_source, "html.parser")

        for img in soup.find_all("img"):
            src = img.get("src")
            if src:
                # Convert relative URLs to absolute
                absolute_src = urljoin(url, src)
                # Filter out small or invalid-looking images (e.g., base64 data URIs)
                if absolute_src.startswith('http') and urlparse(absolute_src).netloc:
                    images.add(absolute_src)
    
    except Exception as e:
        logger.error(f"An error occurred while scraping {url}: {e}")
    finally:
        driver.quit()
    
    logger.info(f"Found {len(images)} images on {url}")
    return images


# --- Telegram Bot Command Handlers ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends a welcome message when the /start command is issued."""
    user = update.effective_user
    await update.message.reply_html(
        rf"Hi {user.mention_html()}!",
        text=(
            "Welcome to the Image Scraper Bot! 🖼️\n\n"
            "I can fetch all images from a webpage for you. For more advanced features, you'll need to set up a target supergroup.\n\n"
            "Here are the available commands:\n"
            "/scrape `[url]` - Scrapes all images from a single URL and sends them here.\n"
            "/deepscrape `[url]` - Scrapes images from all links found on the main URL and uploads them to a supergroup with topics.\n"
            "/setgroup `[supergroup_id or @username]` - Sets the target supergroup for `/deepscrape`.\n"
            "/creategroup `[name]` - Creates a new supergroup and sets it as the target (requires userbot).\n"
            "/help - Shows this help message again.\n\n"
            "**Important**: `/deepscrape` and `/creategroup` require a user account (userbot) to be configured and running."
        ),
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the help message."""
    await start_command(update, context) # Re-use the start message as help

async def scrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scrapes images from a single URL."""
    chat_id = update.message.chat_id
    if not context.args:
        await context.bot.send_message(chat_id, "Please provide a URL. Usage: `/scrape https://example.com`")
        return

    url = context.args[0]
    if not is_valid_url(url):
        await context.bot.send_message(chat_id, "That doesn't look like a valid URL. Please include `http://` or `https://`.")
        return

    await context.bot.send_message(chat_id, f"Scraping images from {url}... This might take a moment, especially for pages with lots of scrolling.")

    images = await scrape_images_from_url(url)

    if not images:
        await context.bot.send_message(chat_id, "Couldn't find any images on that page, or the page could not be accessed.")
        return

    await context.bot.send_message(chat_id, f"Found {len(images)} images. Sending them now...")
    
    for image_url in images:
        try:
            # Using send_photo directly with URL is efficient
            await context.bot.send_photo(chat_id, photo=image_url)
            await asyncio.sleep(0.5) # Avoid hitting rate limits
        except Exception as e:
            logger.warning(f"Failed to send image {image_url}: {e}")
            await context.bot.send_message(chat_id, f"Could not send image: `{image_url}`", parse_mode=ParseMode.MARKDOWN)

    await context.bot.send_message(chat_id, "Finished sending all images!")

async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the target supergroup for the user."""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Please provide a supergroup ID or username. Usage: `/setgroup -100123456789` or `/setgroup @mychannel`")
        return
    
    group_id = context.args[0]
    user_target_group[user_id] = group_id
    await update.message.reply_text(f"Target supergroup set to: {group_id}. The `/deepscrape` command will now use this group.")

async def creategroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Creates a new supergroup using the userbot."""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Please provide a name for the new supergroup. Usage: `/creategroup My Scraped Images`")
        return
        
    group_name = " ".join(context.args)
    await update.message.reply_text(f"Attempting to create a supergroup named '{group_name}'... This requires the userbot to be active.")

    try:
        if not user_client.is_connected():
            await user_client.connect()
        
        # Userbot must be authenticated
        if not await user_client.is_user_authorized():
             await update.message.reply_text("Userbot is not authorized. Please run the script in your terminal and log in first.")
             return

        # Create a new chat
        created_chat = await user_client(functions.messages.CreateChatRequest(
            users=["me"],  # A chat needs at least one other user; 'me' is fine.
            title=group_name
        ))
        chat_id = created_chat.chats[0].id

        # Convert it to a supergroup (gigagroup)
        await user_client(functions.channels.ConvertToGigagroupRequest(channel=chat_id))

        # Get the full channel entity to get the new ID
        full_channel = await user_client(functions.channels.GetFullChannelRequest(channel=chat_id))
        supergroup_id = full_channel.full_chat.id
        
        # Supergroup IDs are prefixed with -100
        supergroup_id_full = int(f"-100{supergroup_id}")

        user_target_group[user_id] = supergroup_id_full
        await update.message.reply_text(
            f"Successfully created supergroup '{group_name}'!\n"
            f"Its ID is: `{supergroup_id_full}`\n"
            "It has been set as your target for `/deepscrape`."
        )

    except UserIsBotError:
        await update.message.reply_text("Error: Bots cannot create groups. This action must be performed by a user account configured as a userbot.")
    except Exception as e:
        logger.error(f"Failed to create group: {e}")
        await update.message.reply_text(f"An error occurred while creating the group: {e}")

async def deepscrape_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finds all links on a page, scrapes images from each, and uploads to topics in a supergroup."""
    user_id = update.effective_user.id
    if user_id not in user_target_group:
        await update.message.reply_text("No target supergroup set. Please use `/setgroup` or `/creategroup` first.")
        return

    if not context.args:
        await update.message.reply_text("Please provide a URL. Usage: `/deepscrape https://example.com`")
        return
    
    base_url = context.args[0]
    if not is_valid_url(base_url):
        await update.message.reply_text("That doesn't look like a valid URL.")
        return

    target_group = user_target_group[user_id]
    await update.message.reply_text(f"Starting deep scrape of {base_url}. This will take a considerable amount of time. Results will be posted in `{target_group}`.", parse_mode=ParseMode.MARKDOWN)

    try:
        # Connect and authorize userbot if not already
        if not user_client.is_connected(): await user_client.connect()
        if not await user_client.is_user_authorized():
            await update.message.reply_text("Userbot is not authorized. Please log in via the terminal.")
            return

        # Get entity for the target group
        try:
            entity = await user_client.get_entity(target_group if isinstance(target_group, str) and target_group.startswith('@') else int(target_group))
        except (ValueError, TypeError) as e:
            await update.message.reply_text(f"Invalid group ID or username: `{target_group}`. Error: {e}")
            return

        # 1. Scrape the main page for links
        response = requests.get(base_url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(response.content, 'html.parser')
        links = {urljoin(base_url, a['href']) for a in soup.find_all('a', href=True)}
        
        await update.message.reply_text(f"Found {len(links)} unique links to process.")

        # 2. Process each link
        for i, link in enumerate(links):
            if not is_valid_url(link):
                continue
            
            # Use link path or a slug as topic title
            topic_title = urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc
            topic_title = (topic_title[:95] + '...') if len(topic_title) > 98 else topic_title
            if not topic_title: continue # Skip if title is empty

            await context.bot.send_message(update.effective_chat.id, f"Processing link {i+1}/{len(links)}: {link}\nCreating topic: '{topic_title}'")
            
            try:
                # Create a new topic in the supergroup
                topic_result = await user_client(functions.channels.CreateForumTopicRequest(
                    channel=entity,
                    title=topic_title,
                    random_id=context.bot._get_private_random_id()
                ))
                topic_id = topic_result.updates[0].message.id
                
                # Scrape images from the link
                images = await scrape_images_from_url(link)
                if not images:
                    await user_client.send_message(entity, message=f"No images found on\n{link}", reply_to=topic_id)
                    continue
                
                # Upload images to the topic
                for img_url in images:
                    try:
                        await user_client.send_file(entity, file=img_url, reply_to=topic_id)
                        await asyncio.sleep(1) # Be gentle with APIs
                    except Exception as e:
                        logger.warning(f"Could not upload image {img_url} to topic {topic_id}: {e}")
                        await user_client.send_message(entity, message=f"Failed to upload:\n`{img_url}`", reply_to=topic_id)

            except Exception as e:
                logger.error(f"Failed to process link {link} or create topic: {e}")
                await context.bot.send_message(update.effective_chat.id, f"Error processing {link}: {e}")
        
        await update.message.reply_text("Deep scrape finished! All images have been uploaded.")

    except Exception as e:
        logger.error(f"A critical error occurred during deep scrape: {e}")
        await update.message.reply_text(f"A critical error occurred: {e}")


# --- Main Application Setup ---
async def main():
    """Start the bot and the userbot client."""
    
    if not all([BOT_TOKEN, API_ID, API_HASH]):
        logger.critical("BOT_TOKEN, API_ID, or API_HASH environment variables are missing. Please check your .env file.")
        return

    # Create the Telegram bot Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("scrape", scrape_command))
    application.add_handler(CommandHandler("setgroup", setgroup_command))
    application.add_handler(CommandHandler("creategroup", creategroup_command))
    application.add_handler(CommandHandler("deepscrape", deepscrape_command))

    # Connect the user client
    await user_client.start()
    logger.info("Userbot client started.")

    # Run the bot until the user presses Ctrl-C
    async with application:
        await application.initialize()
        await application.start()
        logger.info("Telegram bot started.")
        await application.updater.start_polling()
        
        # Keep the script running
        await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shutting down.")
