# deepscrape_task.py
import asyncio
import logging
from collections import deque
from urllib.parse import urlparse

from telegram.constants import ParseMode, ChatType
from telegram.error import RetryAfter, BadRequest
from telegram.ext import Application, ExtBot

import database as db
from scraping import scrape_images_from_url_sync

logger = logging.getLogger(__name__)

async def run_deepscrape_task(user_id, task_id, application: Application, worker_pool: dict):
    """The main background process for a deepscrape task."""
    task = await db.tasks_collection.find_one({"_id": task_id})
    if not task:
        logger.error(f"Task {task_id} not found for user {user_id}.")
        return

    target_group = task['target_id']
    upload_as = task['upload_as']
    link_range = task['link_range']
    status_message_id = task['status_message_id']
    
    worker_bots_data = await db.get_worker_bots(user_id)
    worker_clients = [worker_pool[w['token']] for w in worker_bots_data if w['token'] in worker_pool]
    # Fallback to the main bot if no workers are available
    if not worker_clients:
        worker_clients.append(application.bot)

    await db.update_task_status(task_id, "running")
    
    links_to_process = task['all_links']
    total_original_links = len(links_to_process)
    
    # Parse the link range
    if link_range != 'all':
        try:
            start, end = map(int, link_range.split('-'))
            links_to_process = links_to_process[start-1:end]
        except (ValueError, IndexError):
            pass # Ignore invalid range and process all

    total_links_in_range = len(links_to_process)
    
    # Function to safely edit or send a status message
    async def update_status_message(text, parse_mode=None):
        nonlocal status_message_id
        try:
            await application.bot.edit_message_text(
                chat_id=user_id,
                message_id=status_message_id,
                text=text,
                parse_mode=parse_mode
            )
        except BadRequest: # If message was deleted
            new_msg = await application.bot.send_message(user_id, text, parse_mode=parse_mode)
            status_message_id = new_msg.message_id
            await db.update_task_status_message_id(task_id, status_message_id)

    await update_status_message(
        f"🚀 Deepscrape started with {len(worker_clients)} workers! Processing {total_links_in_range} of {total_original_links} links."
    )

    try:
        # Safeguard Check for Topic Limit
        try:
            chat = await application.bot.get_chat(target_group)
            if chat.type == ChatType.SUPERGROUP and getattr(chat, 'is_forum', False):
                # Telegram has a limit of 199 active topics. We stop at 198 to be safe.
                if len(await chat.get_forum_topics()) >= 198:
                    await update_status_message(
                        f"⚠️ **CRITICAL:** Target group has reached the topic limit. "
                        "Task stopped to prevent issues. Please choose another group or clean up old topics."
                    )
                    await db.update_task_status(task_id, "paused")
                    return
        except Exception as e:
            await update_status_message(f"❌ Could not verify target group '{target_group}'. Task stopped.\nError: {e}")
            await db.update_task_status(task_id, "paused")
            return

        for i, link in enumerate(links_to_process):
            task = await db.tasks_collection.find_one({"_id": task_id})
            if not task or task.get('status') != 'running':
                await update_status_message("🛑 Task stopped by user.")
                break

            await update_status_message(
                f"🔗 **Processing Link {i+1}/{total_links_in_range}**\n\n`{link}`",
                parse_mode=ParseMode.MARKDOWN
            )

            images = await asyncio.to_thread(scrape_images_from_url_sync, link)
            
            # Re-check status after long scraping operation
            task = await db.tasks_collection.find_one({"_id": task_id})
            if task.get('status') == 'stopped': break
            
            if not images:
                await db.complete_link_in_task(task_id, link)
                continue
            
            try:
                # Create a more readable topic title
                topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98] or "Scraped Images"
                # Using a fixed blue color for the topic icon for a nice look
                created_topic = await application.bot.create_forum_topic(chat_id=target_group, name=topic_title, icon_color=0x6FB9F0) # Blue
                topic_id = created_topic.message_thread_id
                await db.increment_topic_count(task_id)
            except Exception as e:
                await update_status_message(f"❌ Failed to create topic. Task paused.\nError: {e}")
                await db.update_task_status(task_id, "paused"); return

            image_queue = deque(images)
            async def upload_worker(bot_client: ExtBot):
                while True:
                    try:
                        img = image_queue.popleft()
                    except IndexError:
                        break # Queue is empty
                    try:
                        if upload_as == 'document':
                            await bot_client.send_document(target_group, document=img, message_thread_id=topic_id)
                        else:
                            await bot_client.send_photo(target_group, photo=img, message_thread_id=topic_id)
                        await db.increment_task_image_upload_count(task_id)
                    except RetryAfter as e:
                        logger.warning(f"Worker {bot_client.bot.id} hit flood wait. Re-queuing image and resting.")
                        image_queue.append(img)
                        await asyncio.sleep(e.retry_after + 2)
                    except Exception as e:
                        logger.error(f"Worker {bot_client.bot.id} failed to upload {img}: {e}")
            
            await asyncio.gather(*[upload_worker(bot) for bot in worker_clients])
            await db.complete_link_in_task(task_id, link)

        # Final status update after the loop
        task = await db.tasks_collection.find_one({"_id": task_id}) # Get latest status
        if task and task.get('status') == 'running':
            await db.update_task_status(task_id, "completed")
            await update_status_message("✅ Deepscrape finished successfully!")

    except Exception as e:
        logger.error(f"Critical error in deepscrape task: {e}", exc_info=True)
        await db.update_task_status(task_id, "paused")
        await update_status_message(f"❌ An unexpected error occurred. Task paused.\nError: `{e}`")
