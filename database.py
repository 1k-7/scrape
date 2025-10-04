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

# --- User Data ---
async def get_user_data(user_id: int) -> dict:
    return await users_collection.find_one({"_id": user_id})

async def save_user_data(user_id: int, data_to_update: dict):
    await users_collection.update_one({"_id": user_id}, {"$set": data_to_update}, upsert=True)

# --- Target Management ---
async def add_target(user_id: int, target_name: str, target_id: str):
    await users_collection.update_one(
        {"_id": user_id},
        {"$push": {"targets": {"name": target_name, "id": target_id}}},
        upsert=True
    )

async def remove_target(user_id: int, target_id: str):
    await users_collection.update_one(
        {"_id": user_id},
        {"$pull": {"targets": {"id": target_id}}}
    )

async def get_targets(user_id: int) -> list:
    user_data = await get_user_data(user_id)
    return (user_data or {}).get("targets", [])

# --- Worker Management ---
async def add_worker_bots(user_id: int, workers_to_add: list):
    await users_collection.update_one(
        {"_id": user_id},
        {"$addToSet": {"worker_bots": {"$each": workers_to_add}}},
        upsert=True
    )

async def remove_worker_bots(user_id: int, worker_ids_to_remove: list):
    await users_collection.update_one(
        {"_id": user_id},
        {"$pull": {"worker_bots": {"id": {"$in": worker_ids_to_remove}}}}
    )

async def get_worker_bots(user_id: int) -> list:
    user_data = await get_user_data(user_id)
    return (user_data or {}).get("worker_bots", [])

# --- Task Management ---
async def create_task(user_id: int, base_url: str, all_links: list, target_ids: list, upload_as: dict, link_range: str, status_message_id: int):
    task = {
        "user_id": user_id,
        "base_url": base_url,
        "status": "pending",
        "all_links": all_links,
        "completed_links": [],
        "target_ids": target_ids,
        "upload_as": upload_as,
        "link_range": link_range,
        "status_message_id": status_message_id,
        "topics_created": 0,
        "images_uploaded": 0,
    }
    result = await tasks_collection.insert_one(task)
    return result.inserted_id

async def get_user_active_task(user_id: int):
    return await tasks_collection.find_one({"user_id": user_id, "status": {"$in": ["running", "paused"]}})

async def update_task_status(task_id, status: str):
    await tasks_collection.update_one({"_id": task_id}, {"$set": {"status": status}})

async def increment_task_image_upload_count(task_id):
    await tasks_collection.update_one({"_id": task_id}, {"$inc": {"images_uploaded": 1}})

async def increment_topic_count(task_id):
    await tasks_collection.update_one({"_id": task_id}, {"$inc": {"topics_created": 1}})

async def complete_link_in_task(task_id, link: str):
    await tasks_collection.update_one({"_id": task_id}, {"$push": {"completed_links": link}})
