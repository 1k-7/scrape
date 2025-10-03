# database.py
import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = "ImageScraperBot"

logger = logging.getLogger(__name__)

# --- Database Client ---
client = AsyncIOMotorClient(MONGO_URI)
db = client[MONGO_DB_NAME]
users_collection = db["users"]
tasks_collection = db["tasks"] # New collection for stateful tasks
logger.info("Successfully connected to MongoDB.")


# --- User Data Functions ---
async def get_user_data(user_id: int) -> dict:
    return await users_collection.find_one({"_id": user_id})

async def save_user_data(user_id: int, data_to_update: dict):
    await users_collection.update_one(
        {"_id": user_id},
        {"$set": data_to_update},
        upsert=True
    )

# --- NEW: Task Management Functions ---
async def create_task(user_id: int, base_url: str, all_links: list) -> str:
    """Creates a new deepscrape task document in the database."""
    task = {
        "user_id": user_id,
        "base_url": base_url,
        "status": "running", # States: running, paused, stopped, completed
        "all_links": all_links,
        "completed_links": [],
        "current_link_progress": {
            "link": None,
            "images_scraped": [],
            "completed_images_count": 0
        }
    }
    result = await tasks_collection.insert_one(task)
    return result.inserted_id

async def get_user_task(user_id: int, status: str = "paused"):
    """Finds a user's incomplete task."""
    return await tasks_collection.find_one({"user_id": user_id, "status": status})

async def update_task_progress(task_id, link: str, images: list):
    """Updates the current link and its scraped images."""
    await tasks_collection.update_one(
        {"_id": task_id},
        {"$set": {
            "current_link_progress.link": link,
            "current_link_progress.images_scraped": images,
            "current_link_progress.completed_images_count": 0
        }}
    )

async def update_task_image_completion(task_id, count: int):
    """Updates the number of completed images for the current link."""
    await tasks_collection.update_one(
        {"_id": task_id},
        {"$set": {"current_link_progress.completed_images_count": count}}
    )

async def complete_link_in_task(task_id, link: str):
    """Moves a link from pending to completed."""
    await tasks_collection.update_one(
        {"_id": task_id},
        {"$push": {"completed_links": link}}
    )

async def update_task_status(task_id, status: str):
    """Updates the overall status of the task."""
    await tasks_collection.update_one({"_id": task_id}, {"$set": {"status": status}})
