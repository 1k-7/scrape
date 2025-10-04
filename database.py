# database.py
import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = "ImageScraperBot"
logger = logging.getLogger(__name__)

client = AsyncIOMotorClient(MONGO_URI)
db = client[MONGO_DB_NAME]
users_collection = db["users"]
tasks_collection = db["tasks"]

async def get_user_data(user_id: int) -> dict:
    return await users_collection.find_one({"_id": user_id})

async def save_user_data(user_id: int, data_to_update: dict):
    await users_collection.update_one({"_id": user_id}, {"$set": data_to_update}, upsert=True)

async def create_task(user_id: int, base_url: str, all_links: list) -> str:
    task = {
        "user_id": user_id,
        "base_url": base_url,
        "status": "pending", # Start as pending
        "total_links": len(all_links),
        "current_link_index": 0,
        "current_link_url": "Initializing...",
        "current_link_images_found": 0,
        "current_link_images_uploaded": 0,
        "all_links": all_links,
        "completed_links": [],
    }
    result = await tasks_collection.insert_one(task)
    return result.inserted_id

async def get_user_active_task(user_id: int):
    return await tasks_collection.find_one({"user_id": user_id, "status": {"$in": ["running", "paused"]}})

async def update_task_status(task_id, status: str):
    await tasks_collection.update_one({"_id": task_id}, {"$set": {"status": status}})

async def update_task_counters(task_id, index: int, link_url: str, images_found: int, images_uploaded: int = 0):
    await tasks_collection.update_one(
        {"_id": task_id},
        {"$set": {
            "current_link_index": index,
            "current_link_url": link_url,
            "current_link_images_found": images_found,
            "current_link_images_uploaded": images_uploaded,
        }}
    )

async def increment_task_image_upload_count(task_id):
    await tasks_collection.update_one(
        {"_id": task_id},
        {"$inc": {"current_link_images_uploaded": 1}}
    )

async def complete_link_in_task(task_id, link: str):
    await tasks_collection.update_one(
        {"_id": task_id},
        {"$push": {"completed_links": link}}
    )
