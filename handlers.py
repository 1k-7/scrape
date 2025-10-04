# handlers.py
import asyncio
import logging
from datetime import datetime
from urllib.parse import urljoin
import math

import requests
from bs4 import BeautifulSoup
from telethon import functions
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.types import ChatAdminRights
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    ExtBot,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import BadRequest

import database as db
from scraping import scrape_images_from_url_sync
from helpers import get_url_from_message, get_userbot_client, preprocess_url, create_zip_from_urls
from deepscrape_task import run_deepscrape_task

logger = logging.getLogger(__name__)

# States
(SELECTING_ACTION, AWAITING_TARGET_NAME, AWAITING_TARGET_ID,
 AWAITING_WORKER_TOKEN, AWAITING_LOGIN_SESSION,
 SCRAPE_SELECT_TARGET, SCRAPE_UPLOAD_AS, SCRAPE_LINK_RANGE,
 CONFIRM_TARGET_DELETE, CONFIRM_WORKER_DELETE, AWAITING_WORKER_TARGET,
 SELECT_WORK_TARGET, SELECT_MULTIPLE_TARGETS) = range(13)


# =============================================================================
# 1. MAIN MENU & CORE COMMANDS
# =============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ud = await db.get_user_data(update.effective_user.id)
    session_status = "✅ Logged In" if ud and 'session_string' in ud else "❌ Not Logged In"
    keyboard = [[InlineKeyboardButton("⚙️ Open Settings Menu", callback_data="main_menu")]]
    await update.message.reply_html(
        f"Hi <b>{update.effective_user.mention_html()}</b>! I'm ready to scrape.\n\n"
        f"Your Login Status: {session_status}\n\n"
        "To start, use `/scrape` or `/deepscrape` on a URL (you can also reply to a message containing a URL).\n\n"
        "Use `/work` to add your saved workers to a target group.\n\n"
        "Use the button below to manage targets, workers, and login.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ud = await db.get_user_data(query.from_user.id)
    session_status = "✅ Logged In" if ud and 'session_string' in ud else "❌ Not Logged In"
    keyboard = [
        [InlineKeyboardButton("🎯 Manage Targets", callback_data="targets_menu")],
        [InlineKeyboardButton("🤖 Manage Workers", callback_data="workers_menu")],
        [InlineKeyboardButton("👤 Login Status / Logout", callback_data="login_menu")],
        [InlineKeyboardButton("ヘル Ping", callback_data="ping")],
        [InlineKeyboardButton("✖️ Close Menu", callback_data="close_menu")]
    ]
    try:
        await query.edit_message_text(
            f"<b>⚙️ Settings Menu</b>\n\nLogin Status: {session_status}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.warning(f"Error editing message: {e}")

    return SELECTING_ACTION

async def close_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Settings menu closed.")
    context.user_data.clear()
    return ConversationHandler.END

async def ping_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    start_time = datetime.now()
    await update.callback_query.answer("Pinging...")
    end_time = datetime.now()
    latency = round((end_time - start_time).total_seconds() * 1000)
    await asyncio.sleep(0.1)
    await update.callback_query.answer(f"Pong! Latency: {latency} ms", show_alert=True)
    return SELECTING_ACTION

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    task = await db.get_user_active_task(update.effective_user.id)
    if task:
        await db.update_task_status(task['_id'], "stopped")
        await update.message.reply_html("🛑 Active deepscrape task has been stopped.")
    else:
        await update.message.reply_html("No active tasks to stop.")
    context.user_data.clear()
    return ConversationHandler.END

# =============================================================================
# 2. LOGIN FLOW
# =============================================================================
async def login_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ud = await db.get_user_data(query.from_user.id)
    if ud and 'session_string' in ud:
        keyboard = [
            [InlineKeyboardButton("🔒 Logout", callback_data="logout")],
            [InlineKeyboardButton("« Back", callback_data="main_menu")]
        ]
        await query.edit_message_text("You are already logged in.", reply_markup=InlineKeyboardMarkup(keyboard))
        return SELECTING_ACTION
    else:
        keyboard = [[InlineKeyboardButton("« Back", callback_data="main_menu")]]
        await query.edit_message_text(
            "You are not logged in. Please send your Telethon session string to log in.\n\nUse /stop to cancel.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return AWAITING_LOGIN_SESSION

async def handle_login_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session_string = update.message.text
    msg = await update.message.reply_text("Validating session...")
    try:
        async with get_userbot_client(session_string) as client:
            me = await client.get_me()
            await db.save_user_data(update.effective_user.id, {'session_string': session_string})
            await msg.edit_text(
                f"✅ Successfully logged in as <b>{me.first_name}</b>!",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Settings", callback_data="main_menu")]])
            )
    except Exception as e:
        await msg.edit_text(
            f"❌ Login failed. Error: {e}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Settings", callback_data="main_menu")]])
        )
    return SELECTING_ACTION

async def logout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await db.users_collection.update_one({"_id": query.from_user.id}, {"$unset": {"session_string": ""}})
    await query.answer("You have been logged out.", show_alert=True)
    await main_menu_callback(update, context)
    return SELECTING_ACTION

# =============================================================================
# 3. TARGET MANAGEMENT FLOW
# =============================================================================
async def targets_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    targets = await db.get_targets(query.from_user.id)
    keyboard = [
        [InlineKeyboardButton(f"🗑️ {t['name']} ({t['id']})", callback_data=f"delete_target_{t['id']}")]
        for t in targets
    ]
    keyboard.append([InlineKeyboardButton("➕ Add New Target", callback_data="add_target")])
    keyboard.append([InlineKeyboardButton("« Back", callback_data="main_menu")])
    await query.edit_message_text(
        "<b>🎯 Target Management</b>\n\nHere are your saved targets. Bots need admin rights in these chats to function.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
    )
    return SELECTING_ACTION

async def add_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Please send me a short, memorable name for this target (e.g., 'My Main Group').\n\nUse /stop to cancel."
    )
    return AWAITING_TARGET_NAME

async def handle_target_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['target_name'] = update.message.text
    await update.message.reply_text("Great. Now send me the Chat ID for this target (e.g., -100123456789 or 'me').")
    return AWAITING_TARGET_ID

async def handle_target_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_id = update.message.text.strip()
    target_name = context.user_data.pop('target_name')
    msg = await update.message.reply_html(f"Verifying permissions for <code>{target_id}</code>...")

    try:
        if target_id.lower() != 'me':
            chat = await context.bot.get_chat(target_id)
            bot_member = await context.bot.get_chat_member(target_id, context.bot.id)
            if not bot_member.status in ['administrator', 'creator']:
                 raise Exception("I am not an admin in that chat.")

        await db.add_target(update.effective_user.id, target_name, target_id)
        await msg.edit_text(
            f"✅ Target '{target_name}' added successfully!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Settings", callback_data="main_menu")]])
        )
    except Exception as e:
        await msg.edit_text(
            f"❌ Failed to verify target.\n<b>Error:</b> {e}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Settings", callback_data="main_menu")]])
        )
    return SELECTING_ACTION

async def delete_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    target_id = query.data.split('_', 2)[2]
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Delete It", callback_data=f"confirm_delete_target_{target_id}")],
        [InlineKeyboardButton("❌ No, Keep It", callback_data="targets_menu")]
    ]
    await query.edit_message_text(
        f"Are you sure you want to delete the target with ID `{target_id}`?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return CONFIRM_TARGET_DELETE

async def confirm_delete_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    target_id = query.data.split('_', 3)[3]
    await db.remove_target(query.from_user.id, target_id)
    await query.answer("Target deleted successfully.", show_alert=True)
    await targets_menu_callback(update, context)
    return SELECTING_ACTION

# =============================================================================
# 4. WORKER MANAGEMENT FLOW
# =============================================================================
async def workers_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    workers = await db.get_worker_bots(query.from_user.id)
    keyboard = [
        [InlineKeyboardButton(f"🗑️ @{w['username']}", callback_data=f"delete_worker_{w['id']}")]
        for w in workers
    ]
    keyboard.append([InlineKeyboardButton("➕ Add New Worker", callback_data="add_worker")])
    keyboard.append([InlineKeyboardButton("« Back", callback_data="main_menu")])
    await query.edit_message_text(
        f"<b>🤖 Worker Management ({len(workers)} active)</b>\n\nAdd or remove worker bots for deep scraping.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
    )
    return SELECTING_ACTION

async def add_worker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ud = await db.get_user_data(query.from_user.id)
    if not ud or 'session_string' not in ud:
        await query.answer("You must be logged in to add workers.", show_alert=True)
        return SELECTING_ACTION

    targets = await db.get_targets(query.from_user.id)
    if not targets:
        await query.answer("You must add at least one target group before adding workers.", show_alert=True)
        return SELECTING_ACTION

    keyboard = [
        [InlineKeyboardButton(t['name'], callback_data=f"select_worker_target_{t['id']}")]
        for t in targets
    ]
    keyboard.append([InlineKeyboardButton("« Back", callback_data="workers_menu")])
    await query.edit_message_text(
        "Please choose a target group to add the worker(s) to:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AWAITING_WORKER_TARGET

async def select_target_for_worker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['worker_target_id'] = query.data.split('_', 3)[3]
    await query.edit_message_text(
        "Please send me the bot token(s) for the new worker(s), separated by a space.\n\nUse /stop to cancel."
    )
    return AWAITING_WORKER_TOKEN

async def handle_worker_tokens(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    ud = await db.get_user_data(user_id)
    tokens = update.message.text.split()
    target_group_id_str = context.user_data.pop('worker_target_id')
    
    try:
        target_group_id = int(target_group_id_str)
    except ValueError:
        await update.message.reply_html(f"Invalid Target ID: <code>{target_group_id_str}</code>. Please use a valid chat ID.")
        return SELECTING_ACTION

    msg = await update.message.reply_html("Processing worker tokens...")
    added, failed = [], []
    admin_rights = ChatAdminRights(delete_messages=True)

    async with get_userbot_client(ud['session_string']) as client:
        try:
            current_admins = {p.id for p in await client.get_participants(target_group_id, filter=None) if p.participant}
        except Exception as e:
            await msg.edit_text(f"Could not fetch admins from target group. Error: {e}"); return SELECTING_ACTION

        for token in tokens:
            try:
                worker_bot = ExtBot(token=token)
                worker_info = await worker_bot.get_me()
                
                if token not in context.application.bot_data.get("WORKER_BOT_POOL", {}):
                    context.application.bot_data.setdefault("WORKER_BOT_POOL", {})[token] = worker_bot
                    logger.info(f"Dynamically initialized worker {worker_info.id}")

                if worker_info.id not in current_admins:
                    await msg.edit_text(f"Inviting & promoting @{worker_info.username}...")
                    worker_entity = await client.get_input_entity(worker_info.username)
                    try:
                        await client(functions.channels.InviteToChannelRequest(target_group_id, [worker_entity]))
                    except UserAlreadyParticipantError: pass
                    await client(functions.channels.EditAdminRequest(target_group_id, worker_entity, admin_rights, "Worker Bot"))
                
                worker_data = {"id": worker_info.id, "username": worker_info.username, "token": token}
                await db.add_worker_bots(user_id, [worker_data])
                added.append(f"• @{worker_info.username}")
            except Exception as e:
                failed.append(f"• Token `{token[:8]}...` ({e})")
    
    response = "✅ <b>Worker setup complete!</b>\n"
    if added: response += "\n<b>Added to Pool:</b>\n" + "\n".join(added)
    if failed: response += "\n\n<b>Failed:</b>\n" + "\n".join(failed)
    await msg.edit_text(response, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Settings", callback_data="main_menu")]]))
    return SELECTING_ACTION

async def delete_worker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    worker_id = int(query.data.split('_', 2)[2])
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Remove It", callback_data=f"confirm_delete_worker_{worker_id}")],
        [InlineKeyboardButton("❌ No, Keep It", callback_data="workers_menu")]
    ]
    await query.edit_message_text(f"Are you sure you want to remove the worker with ID `{worker_id}` from your pool?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    return CONFIRM_WORKER_DELETE

async def confirm_delete_worker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    worker_id = int(query.data.split('_', 3)[3])
    await db.remove_worker_bots(query.from_user.id, [worker_id])
    await query.answer("Worker removed from your pool.", show_alert=True)
    await workers_menu_callback(update, context)
    return SELECTING_ACTION

# =============================================================================
# 5. /work COMMAND FLOW
# =============================================================================
async def work_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    ud = await db.get_user_data(user_id)
    if not ud or 'session_string' not in ud:
        await update.message.reply_html("You must be logged in to use this command. Please log in via the settings menu.")
        return ConversationHandler.END

    workers = await db.get_worker_bots(user_id)
    if not workers:
        await update.message.reply_html("You have no saved workers. Please add workers via the settings menu first.")
        return ConversationHandler.END

    targets = await db.get_targets(user_id)
    if not targets:
        await update.message.reply_html("You have no saved targets. Please add a target via the settings menu first.")
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(t['name'], callback_data=f"work_target_{t['id']}")] for t in targets]
    keyboard.append([InlineKeyboardButton("✖️ Cancel", callback_data="cancel_scrape")])
    
    await update.message.reply_html(
        f"You have {len(workers)} worker(s) saved. Please choose a target channel to add and promote them to:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_WORK_TARGET

async def select_work_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    target_id_str = query.data.split('_', 2)[2]
    user_id = query.from_user.id
    ud = await db.get_user_data(user_id)
    workers = await db.get_worker_bots(user_id)

    try:
        target_group_id = int(target_id_str)
    except ValueError:
        await query.edit_message_text(f"Invalid Target ID: <code>{target_id_str}</code>.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    await query.edit_message_text("🚀 Starting worker deployment...")
    
    added, failed = [], []
    admin_rights = ChatAdminRights(delete_messages=True)

    async with get_userbot_client(ud['session_string']) as client:
        for worker in workers:
            try:
                await query.edit_message_text(f"Processing @{worker['username']}...")
                worker_entity = await client.get_input_entity(worker['username'])
                try:
                    await client(functions.channels.InviteToChannelRequest(target_group_id, [worker_entity]))
                except UserAlreadyParticipantError:
                    pass
                await client(functions.channels.EditAdminRequest(target_group_id, worker_entity, admin_rights, "Worker Bot"))
                added.append(f"• @{worker['username']}")
            except Exception as e:
                failed.append(f"• @{worker['username']} ({e})")
    
    response = "✅ <b>Worker deployment complete!</b>\n"
    if added: response += "\n<b>Successfully deployed:</b>\n" + "\n".join(added)
    if failed: response += "\n\n<b>Failed to deploy:</b>\n" + "\n".join(failed)
    
    await query.edit_message_text(response, parse_mode=ParseMode.HTML)
    return ConversationHandler.END

# =============================================================================
# 6. SCRAPE & DEEPSCRAPE WORKFLOWS
# =============================================================================
async def scrape_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = get_url_from_message(update.message)
    if not url:
        await update.message.reply_text("Please provide a URL to scrape. Reply or send `/scrape [url]`.")
        return ConversationHandler.END
    
    context.user_data['url'] = url
    context.user_data['scrape_type'] = 'single'
    
    targets = await db.get_targets(update.effective_user.id)
    if not targets:
        await update.message.reply_text(
            "You have no targets. Please add one via the settings menu.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Open Settings", callback_data="main_menu")]])
        )
        return ConversationHandler.END
        
    keyboard = [[InlineKeyboardButton(t['name'], callback_data=f"select_target_{t['id']}")] for t in targets]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_scrape")])
    await update.message.reply_text("Please choose a target for this scrape:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    return SCRAPE_SELECT_TARGET

async def scrape_select_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['target_id'] = query.data.split('_', 2)[2]
    
    keyboard = [
        [InlineKeyboardButton("🖼️ As Photos", callback_data="upload_as_photo")],
        [InlineKeyboardButton("📄 As Documents", callback_data="upload_as_document")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_scrape")]
    ]
    await query.edit_message_text("How should the images be uploaded?", reply_markup=InlineKeyboardMarkup(keyboard))
    
    return SCRAPE_UPLOAD_AS

async def scrape_upload_as_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['upload_as'] = query.data.split('_', 2)[2]
    
    await query.edit_message_text("Starting single scrape...")
    await start_single_scrape(update, context)
    return ConversationHandler.END

async def deepscrape_command_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    url = get_url_from_message(update.message)
    if not url:
        await update.message.reply_text("Please provide a URL. Reply or send `/deepscrape [url]`.")
        return ConversationHandler.END
        
    context.user_data['url'] = url
    context.user_data['scrape_type'] = 'deep'
    
    msg = await update.message.reply_text("Scanning URL for links...")
    try:
        response = await asyncio.to_thread(requests.get, url, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(response.content, 'html.parser')
        links = sorted(list({
            urljoin(url, a['href']) for a in soup.find_all('a', href=True)
            if urljoin(url, a.get('href', '')) != url and not (urljoin(url, a.get('href', ''))).endswith(('.zip', '.rar', '.exe', '.pdf'))
        }))
        context.user_data['all_links'] = links
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"Failed to fetch links. Error: {e}")
        return ConversationHandler.END

    if not context.user_data.get('all_links'):
        await update.message.reply_text("Found no valid links on that page.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Please specify the range of links to process (e.g., `1-77`)\n\nor use /all to process all links."
    )
    return SCRAPE_LINK_RANGE

async def scrape_link_range_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['link_range'] = update.message.text.strip().lower()
    return await prompt_for_targets(update, context)

async def scrape_all_links_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['link_range'] = 'all'
    return await prompt_for_targets(update, context)

async def prompt_for_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    all_links = context.user_data['all_links']
    link_range = context.user_data['link_range']
    
    if link_range == 'all':
        links_to_process = all_links
    else:
        try:
            start, end = map(int, link_range.split('-'))
            links_to_process = all_links[start-1:end]
        except (ValueError, IndexError):
            links_to_process = all_links

    num_links = len(links_to_process)
    num_targets_needed = math.ceil(num_links / 180) if num_links > 0 else 1
    context.user_data['num_targets_needed'] = num_targets_needed
    context.user_data['selected_targets'] = []
    
    targets = await db.get_targets(update.effective_user.id)
    if len(targets) < num_targets_needed:
        await update.message.reply_text(
            f"This scrape requires {num_targets_needed} target(s), but you only have {len(targets)} saved. "
            "Please add more targets via the settings menu."
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(t['name'], callback_data=f"multi_target_{t['id']}")] for t in targets]
    await update.message.reply_text(
        f"This scrape requires {num_targets_needed} target(s). Please select target 1:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_MULTIPLE_TARGETS

async def select_multiple_targets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    target_id = query.data.split('_', 2)[2]
    
    selected_targets = context.user_data['selected_targets']
    if target_id not in selected_targets:
        selected_targets.append(target_id)
    
    num_needed = context.user_data['num_targets_needed']
    
    if len(selected_targets) < num_needed:
        targets = await db.get_targets(query.from_user.id)
        available_targets = [t for t in targets if t['id'] not in selected_targets]
        keyboard = [[InlineKeyboardButton(t['name'], callback_data=f"multi_target_{t['id']}")] for t in available_targets]
        await query.edit_message_text(
            f"Target {len(selected_targets)} of {num_needed} selected. Please select target {len(selected_targets) + 1}:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return SELECT_MULTIPLE_TARGETS
    else:
        context.user_data.setdefault('upload_as', {'photo': False, 'document': False, 'zip': False})
        return await show_upload_options(update, context)

async def show_upload_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    upload_as = context.user_data['upload_as']
    
    keyboard = [
        [InlineKeyboardButton(f"{'✅' if upload_as['photo'] else '🔲'} As Photos", callback_data="toggle_photo")],
        [InlineKeyboardButton(f"{'✅' if upload_as['document'] else '🔲'} As Documents", callback_data="toggle_document")],
        [InlineKeyboardButton(f"{'✅' if upload_as['zip'] else '🔲'} As ZIP Archive", callback_data="toggle_zip")],
        [InlineKeyboardButton("▶️ Continue", callback_data="confirm_upload_options")],
        [InlineKeyboardButton("✖️ Cancel", callback_data="cancel_scrape")]
    ]
    
    await query.edit_message_text("Select upload format(s):", reply_markup=InlineKeyboardMarkup(keyboard))
    return SCRAPE_UPLOAD_AS

async def toggle_upload_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    option = query.data.split('_')[1]
    context.user_data['upload_as'][option] = not context.user_data['upload_as'][option]
    await query.answer()
    return await show_upload_options(update, context)

async def confirm_upload_options_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    upload_as = context.user_data['upload_as']
    
    if not any(upload_as.values()):
        await query.answer("Please select at least one upload format.", show_alert=True)
        return SCRAPE_UPLOAD_AS
        
    await query.edit_message_text("Starting deep scrape...")
    await start_deep_scrape(update, context)
    return ConversationHandler.END

async def cancel_scrape_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Scrape cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

async def start_single_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_data = context.user_data
    url, target_id, upload_as = user_data['url'], user_data['target_id'], user_data['upload_as']
    
    await query.edit_message_text(f"🔎 Scraping `{url}`...", parse_mode=ParseMode.MARKDOWN)
    images = await asyncio.to_thread(scrape_images_from_url_sync, url)

    if not images:
        await query.edit_message_text("Could not find any images on that page."); return

    await query.edit_message_text(f"Found {len(images)} images. Starting upload to `{target_id}` as {upload_as}s...")
    
    for img_url in images:
        try:
            if upload_as == 'photo':
                await context.bot.send_photo(chat_id=target_id, photo=img_url)
            else:
                await context.bot.send_document(chat_id=target_id, document=img_url)
        except Exception as e:
            logger.warning(f"Failed to send {img_url} to {target_id}: {e}")

    await query.message.reply_text("✅ Single scrape and upload complete!")
    context.user_data.clear()

async def start_deep_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if update.callback_query:
        chat_id = update.callback_query.message.chat.id
    
    user_data = context.user_data
    url, target_ids, upload_as, link_range, all_links = (
        user_data['url'], user_data['selected_targets'], user_data['upload_as'], 
        user_data['link_range'], user_data['all_links']
    )
    
    msg = await context.bot.send_message(chat_id, "Initializing deep scrape task...")

    task_id = await db.create_task(
        update.effective_user.id, url, all_links, target_ids, upload_as, link_range, msg.message_id
    )
    
    await msg.edit_message_text(f"Deep scrape has started in the background. Use /stop to cancel.")
    
    context.application.create_task(
        run_deepscrape_task(
            user_id=update.effective_user.id,
            task_id=task_id,
            application=context.application,
            worker_pool=context.application.bot_data.get("WORKER_BOT_POOL", {})
        )
    )
    context.user_data.clear()
