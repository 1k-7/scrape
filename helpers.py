# helpers.py
import os
import re
from urllib.parse import urlparse

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
