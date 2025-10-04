# deepscrape_task.py
import asyncio
import logging
from collections import deque
from urllib.parse import urlparse
import math

from telegram.constants import ParseMode, ChatType
from telegram.error import RetryAfter, BadRequest
from telegram.ext import Application, ExtBot

import database as db
from scraping import scrape_images_from_url_sync
from helpers import create_zip_from_urls

logger = logging.getLogger(__name__)

async def run_deepscrape_task(user_id, task_id, application: Application, worker_pool: dict):
    task = await db.tasks_collection.find_one({"_id": task_id})
    if not task:
        logger.error(f"Task {task_id} not found for user {user_id}.")
        return

    target_groups = task['target_ids']
    upload_as = task['upload_as']
    link_range = task['link_range']
    status_message_id = task['status_message_id']
    
    worker_bots_data = await db.get_worker_bots(user_id)
    worker_clients = [worker_pool[w['token']] for w in worker_bots_data if w['token'] in worker_pool]
    if not worker_clients:
        worker_clients.append(application.bot)

    await db.update_task_status(task_id, "running")
    
    links_to_process = task['all_links']
    if link_range != 'all':
        try:
            start, end = map(int, link_range.split('-'))
            links_to_process = links_to_process[start-1:end]
        except (ValueError, IndexError):
            pass

    total_links_in_range = len(links_to_process)
    
    async def update_status_message(text, parse_mode=None):
        nonlocal status_message_id
        try:
            await application.bot.edit_message_text(
                chat_id=user_id, message_id=status_message_id, text=text, parse_mode=parse_mode
            )
        except BadRequest:
            new_msg = await application.bot.send_message(user_id, text, parse_mode=parse_mode)
            status_message_id = new_msg.message_id
            await db.tasks_collection.update_one({"_id": task_id}, {"$set": {"status_message_id": status_message_id}})

    await update_status_message(f"🚀 Deepscrape started! Processing {total_links_in_range} links.")

    try:
        for i, link in enumerate(links_to_process):
            task = await db.tasks_collection.find_one({"_id": task_id})
            if not task or task.get('status') != 'running':
                await update_status_message("🛑 Task stopped by user.")
                break
            
            current_target_index = math.floor(i / 180)
            if current_target_index >= len(target_groups):
                await update_status_message("⚠️ Ran out of target groups. Task paused.")
                await db.update_task_status(task_id, "paused")
                return
            target_group = target_groups[current_target_index]
            
            await update_status_message(
                f"🔗 **Processing Link {i+1}/{total_links_in_range}**\n"
                f"🎯 Target: `{target_group}`\n\n"
                f"`{link}`",
                parse_mode=ParseMode.MARKDOWN
            )

            images = await asyncio.to_thread(scrape_images_from_url_sync, link)
            
            task = await db.tasks_collection.find_one({"_id": task_id})
            if task.get('status') == 'stopped': break
            if not images:
                await db.complete_link_in_task(task_id, link)
                continue
            
            try:
                topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98] or "Scraped Images"
                created_topic = await application.bot.create_forum_topic(chat_id=target_group, name=topic_title, icon_color=0x6FB9F0)
                topic_id = created_topic.message_thread_id
                await db.increment_topic_count(task_id)
            except Exception as e:
                await update_status_message(f"❌ Failed to create topic. Task paused.\nError: {e}")
                await db.update_task_status(task_id, "paused"); return

            # Define upload tasks based on user selection
            upload_tasks = []
            
            async def upload_media(bot_client, img_url, format_type):
                try:
                    if format_type == 'photo':
                        await bot_client.send_photo(target_group, photo=img_url, message_thread_id=topic_id)
                    elif format_type == 'document':
                        await bot_client.send_document(target_group, document=img_url, message_thread_id=topic_id)
                    await db.increment_task_image_upload_count(task_id)
                except RetryAfter as e:
                    logger.warning(f"Worker {bot_client.id} hit flood wait. Resting.")
                    await asyncio.sleep(e.retry_after + 2)
                    await upload_media(bot_client, img_url, format_type) # Retry
                except Exception as ex:
                    logger.error(f"Worker {bot_client.id} failed to upload {img_url}: {ex}")

            if upload_as.get('photo') or upload_as.get('document'):
                image_queue = deque(images)
                worker_tasks = []
                for bot in worker_clients:
                    async def worker_loop(b):
                        while True:
                            try:
                                img = image_queue.popleft()
                                if upload_as.get('photo'):
                                    await upload_media(b, img, 'photo')
                                if upload_as.get('document'):
                                    await upload_media(b, img, 'document')
                            except IndexError:
                                break
                    worker_tasks.append(worker_loop(bot))
                await asyncio.gather(*worker_tasks)
            
            if upload_as.get('zip'):
                zip_file = await create_zip_from_urls(list(images))
                if zip_file:
                    try:
                        await application.bot.send_document(
                            target_group, document=zip_file, message_thread_id=topic_id,
                            filename=f"{topic_title}.zip", caption=f"ZIP archive for {link}"
                        )
                    except Exception as e:
                         logger.error(f"Failed to upload ZIP for {link}: {e}")

            await db.complete_link_in_task(task_id, link)

        task = await db.tasks_collection.find_one({"_id": task_id})
        if task and task.get('status') == 'running':
            await db.update_task_status(task_id, "completed")
            await update_status_message("✅ Deepscrape finished successfully!")

    except Exception as e:
        logger.error(f"Critical error in deepscrape task: {e}", exc_info=True)
        await db.update_task_status(task_id, "paused")
        await update_status_message(f"❌ An unexpected error occurred. Task paused.\nError: `{e}`")
