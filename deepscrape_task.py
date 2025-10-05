# deepscrape_task.py
import asyncio
import logging
from collections import deque
from urllib.parse import urlparse
import math
from datetime import datetime

from telegram.constants import ParseMode, ChatType
from telegram.error import RetryAfter, BadRequest
from telegram.ext import Application, ExtBot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import database as db
from scraping import scrape_images_from_url_sync
from helpers import create_zip_from_urls

logger = logging.getLogger(__name__)

async def run_deepscrape_task(user_id, task_id, application: Application, worker_pool: dict):
    task = await db.tasks_collection.find_one({"_id": task_id})
    if not task:
        logger.error(f"Task {task_id} not found for user {user_id}.")
        return

    # Load all task settings
    target_groups = task['target_ids']
    use_splitting = task.get('use_splitting', True)
    doc_upload_style = task.get('doc_upload_style', 'topics')
    upload_as = task['upload_as']
    link_range = task['link_range']
    status_message_id = task['status_message_id']
    
    worker_bots_data = await db.get_worker_bots(user_id)
    worker_clients = [worker_pool.get(w['token']) for w in worker_bots_data]
    worker_clients = [bot for bot in worker_clients if bot] # Filter out any missing bots
    if not worker_clients:
        worker_clients.append(application.bot)

    async def update_status_message(text, parse_mode=None, reply_markup=None):
        nonlocal status_message_id
        try:
            await application.bot.edit_message_text(
                chat_id=user_id, message_id=status_message_id, text=text, parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except BadRequest:
            try:
                new_msg = await application.bot.send_message(user_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
                status_message_id = new_msg.message_id
                await db.tasks_collection.update_one({"_id": task_id}, {"$set": {"status_message_id": status_message_id}})
            except Exception as e:
                logger.error(f"Failed to send new status message for task {task_id}: {e}")

    try:
        await db.update_task_status(task_id, "running", start_time=datetime.now())
        
        links_to_process = task['all_links']
        if link_range != 'all':
            try:
                start, end = map(int, link_range.split('-'))
                links_to_process = links_to_process[start-1:end]
            except (ValueError, IndexError):
                pass

        for i, link in enumerate(links_to_process):
            await db.update_task_link_progress(task_id, link_url=link, found=0, uploaded=0)
            
            task = await db.tasks_collection.find_one({"_id": task_id})
            if not task or task.get('status') != 'running':
                await update_status_message("🛑 Task stopped by user.", reply_markup=None)
                break
            
            if use_splitting:
                current_target_index = math.floor(i / 180)
                if current_target_index >= len(target_groups):
                    await update_status_message("⚠️ Ran out of target groups. Task paused.", reply_markup=None)
                    await db.update_task_status(task_id, "paused")
                    return
                target_group = target_groups[current_target_index]
            else:
                target_group = target_groups[0]

            try:
                for bot in worker_clients:
                    await bot.get_chat(target_group)
            except Exception as e:
                logger.error(f"A worker bot could not access channel {target_group}. Skipping link. Error: {e}")
                await db.complete_link_in_task(task_id, link)
                continue

            images = await asyncio.to_thread(scrape_images_from_url_sync, link)
            await db.update_task_link_progress(task_id, found=len(images))
            
            if not images:
                await db.complete_link_in_task(task_id, link)
                continue

            topic_id = None
            create_topics = (doc_upload_style == 'topics')

            if create_topics:
                try:
                    topic_title = (urlparse(link).path.strip('/').replace('/', '-') or urlparse(link).netloc)[:98] or "Scraped Images"
                    created_topic = await application.bot.create_forum_topic(chat_id=target_group, name=topic_title, icon_color=0x6FB9F0)
                    topic_id = created_topic.message_thread_id
                    await db.increment_topic_count(task_id)
                except Exception as e:
                    logger.error(f"Failed to create topic for {link}, skipping link. Error: {e}")
                    await db.complete_link_in_task(task_id, link)
                    continue
            else:
                await application.bot.send_message(target_group, f"--- Files for: `{link}` ---", parse_mode=ParseMode.MARKDOWN)

            async def upload_media(bot_client, img_url, format_type):
                try:
                    if format_type == 'photo':
                        await bot_client.send_photo(target_group, photo=img_url, message_thread_id=topic_id)
                    elif format_type == 'document':
                        await bot_client.send_document(target_group, document=img_url, message_thread_id=topic_id)
                    await db.increment_task_image_upload_count(task_id, 1)
                except RetryAfter as re:
                    logger.warning(f"Worker {bot_client.id} hit flood wait. Resting for {re.retry_after+2}s.")
                    await asyncio.sleep(re.retry_after + 2)
                    await upload_media(bot_client, img_url, format_type)
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
                            filename=f"{(urlparse(link).path.strip('/').replace('/', '-') or 'images')}.zip", 
                            caption=f"ZIP archive for {link}"
                        )
                    except Exception as e:
                         logger.error(f"Failed to upload ZIP for {link}: {e}")

            await db.complete_link_in_task(task_id, link)
            logger.info(f"Task {task_id}: Finished link {i+1} - {link}")

        task = await db.tasks_collection.find_one({"_id": task_id})
        if task and task.get('status') == 'running':
            await db.update_task_status(task_id, "completed")
            await update_status_message("✅ Deepscrape finished successfully!", reply_markup=None)

    except Exception as e:
        logger.error(f"CRITICAL ERROR in deepscrape task {task_id}: {e}", exc_info=True)
        await db.update_task_status(task_id, "paused")
        await update_status_message(f"❌ An unexpected error occurred. Task paused.\nError: `{e}`", reply_markup=None, parse_mode=ParseMode.MARKDOWN)
