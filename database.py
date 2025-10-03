# database.py
# Handles all interactions with the MongoDB database.

import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = "ImageScraperBot"

# --- Logging ---
logger = logging.getLogger(__name__)

# --- Database Client ---
try:
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[MONGO_DB_NAME]
    users_collection = db["users"]
    logger.info("Successfully connected to MongoDB.")
except Exception as e:
    logger.critical(f"Could not connect to MongoDB: {e}")
    client = None
    users_collection = None

# --- Database Functions ---

async def get_user_data(user_id: int) -> dict:
    """
    Retrieves a user's data from the database.
    
    Args:
        user_id: The user's Telegram ID.
        
    Returns:
        A dictionary containing the user's data, or None if not found.
    """
    if not users_collection:
        return None
    return await users_collection.find_one({"_id": user_id})

async def save_user_data(user_id: int, data_to_update: dict):
    """
    Saves or updates a user's data in the database.
    This performs an upsert operation.
    
    Args:
        user_id: The user's Telegram ID.
        data_to_update: A dictionary with the fields to set or update.
    """
    if not users_collection:
        return
    await users_collection.update_one(
        {"_id": user_id},
        {"$set": data_to_update},
        upsert=True
    )
