# helpers.py
import os
import re
from urllib.parse import urlparse
import asyncio
import aiohttp
import zipfile
from io import BytesIO

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

def get_url_from_message(message):
    text_to_check = ""
    if message.text:
        text_to_check += " " + message.text
    if message.caption:
        text_to_check += " " + message.caption
        
    if message.reply_to_message:
        if message.reply_to_message.text:
            text_to_check += " " + message.reply_to_message.text
        if message.reply_to_message.caption:
            text_to_check += " " + message.reply_to_message.caption

    match = re.search(r'https?://[^\s/$.?#].[^\s]*', text_to_check)
    return match.group(0) if match else None

def preprocess_url(url: str):
    if not re.match(r'http(s)?://', url):
        return f'https://{url}'
    return url

def get_userbot_client(session_string: str):
    if not session_string:
        return None
    return TelegramClient(StringSession(session_string), int(API_ID), API_HASH)

def generate_zip_filename(url: str) -> str:
    """Creates a clean, title-cased filename from a URL."""
    try:
        path = urlparse(url).path
        # Use the last part of the path as the base for the name
        base_name = path.strip('/').split('/')[-1] or "Scraped Images"
        # Replace hyphens and underscores with spaces, then title-case it
        title_cased_name = base_name.replace('-', ' ').replace('_', ' ').title()
        # Sanitize the filename to remove invalid characters
        sanitized_name = re.sub(r'[<>:"/\\|?*]', '', title_cased_name)
        return f"{sanitized_name}.zip"
    except Exception:
        return "Scraped_Images.zip"

async def fetch_image(session, url):
    try:
        async with session.get(url, timeout=30) as response:
            if response.status == 200:
                return await response.read()
    except Exception as e:
        print(f"Failed to fetch {url}: {e}")
        return None

async def create_zip_from_urls(urls: list) -> BytesIO | None:
    zip_buffer = BytesIO()
    
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_image(session, url) for url in urls]
        results = await asyncio.gather(*tasks)
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for i, image_bytes in enumerate(results):
                if image_bytes:
                    filename = f"image_{i+1:04d}.jpg"
                    zip_file.writestr(filename, image_bytes)

    if zip_buffer.getbuffer().nbytes > 22: # 22 is the size of an empty zip
        zip_buffer.seek(0)
        return zip_buffer
    return None
