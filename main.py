# main.py
import logging
import os
import sys
import threading

from dotenv import load_dotenv
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

import database as db
from handlers import (
    SELECTING_ACTION, AWAITING_LOGIN_SESSION, AWAITING_TARGET_NAME,
    AWAITING_TARGET_ID, CONFIRM_TARGET_DELETE, AWAITING_WORKER_TARGET,
    AWAITING_WORKER_TOKEN, CONFIRM_WORKER_DELETE, SCRAPE_SELECT_TARGET,
    SCRAPE_UPLOAD_AS, SCRAPE_LINK_RANGE, SELECT_WORK_TARGET,
    SELECT_MULTIPLE_TARGETS,
    start_command, stop_command, main_menu_callback,
    close_menu_callback, ping_callback, login_menu_callback,
    handle_login_session, logout_callback, targets_menu_callback,
    add_target_callback, handle_target_name, handle_target_id,
    delete_target_callback, confirm_delete_target_callback,
    workers_menu_callback, add_worker_callback,
    select_target_for_worker_callback, handle_worker_tokens,
    delete_worker_callback, confirm_delete_worker_callback,
    scrape_command_entry, deepscrape_command_entry,
    scrape_select_target_callback, scrape_upload_as_callback,
    scrape_link_range_callback, scrape_all_links_callback, cancel_scrape_callback,
    work_command, select_work_target_callback,
    select_multiple_targets_callback, toggle_upload_option_callback,
    confirm_upload_options_callback
)

# --- Basic Configuration ---
load_dotenv()
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")
WORKER_BOT_POOL = {}

# --- Flask & Startup ---
from flask import Flask
app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot is alive!", 200
def run_web_server(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

async def post_init_callback(application: Application):
    logger.info("Running post-initialization tasks...")
    try:
        await db.client.admin.command('ping'); logger.info("MongoDB connection successful.")
    except Exception as e:
        logger.critical(f"CRITICAL: Could not connect to MongoDB: {e}"); sys.exit(1)
    
    logger.info("Initializing worker bot fleet...")
    all_users = await db.users_collection.find({}).to_list(length=None)
    unique_workers = {worker['token']: worker for user in all_users for worker in user.get('worker_bots', [])}
    
    for token, worker_data in unique_workers.items():
        try:
            bot_client = application.builder().token(token).build().bot
            await bot_client.get_me()
            WORKER_BOT_POOL[token] = bot_client
            logger.info(f"Successfully initialized worker bot: @{worker_data['username']} (ID: {worker_data['id']})")
        except Exception as e:
            logger.error(f"Failed to initialize worker ID {worker_data['id']}: {e}")
            
    logger.info(f"Worker bot fleet initialization complete. {len(WORKER_BOT_POOL)} workers ready.")
    application.bot_data["WORKER_BOT_POOL"] = WORKER_BOT_POOL


def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN environment variable not set.")
        sys.exit(1)
        
    persistence = PicklePersistence(filepath="./bot_persistence")
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(post_init_callback)
        .build()
    )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_command),
            CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
            CommandHandler("scrape", scrape_command_entry),
            CommandHandler("deepscrape", deepscrape_command_entry),
            CommandHandler("work", work_command),
        ],
        states={
            SELECTING_ACTION: [
                CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"),
                CallbackQueryHandler(close_menu_callback, pattern="^close_menu$"),
                CallbackQueryHandler(ping_callback, pattern="^ping$"),
                CallbackQueryHandler(login_menu_callback, pattern="^login_menu$"),
                CallbackQueryHandler(logout_callback, pattern="^logout$"),
                CallbackQueryHandler(targets_menu_callback, pattern="^targets_menu$"),
                CallbackQueryHandler(add_target_callback, pattern="^add_target$"),
                CallbackQueryHandler(delete_target_callback, pattern=r"^delete_target_"),
                CallbackQueryHandler(workers_menu_callback, pattern="^workers_menu$"),
                CallbackQueryHandler(add_worker_callback, pattern="^add_worker$"),
                CallbackQueryHandler(delete_worker_callback, pattern=r"^delete_worker_"),
            ],
            AWAITING_LOGIN_SESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_login_session)],
            AWAITING_TARGET_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_target_name)],
            AWAITING_TARGET_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_target_id)],
            CONFIRM_TARGET_DELETE: [CallbackQueryHandler(confirm_delete_target_callback, pattern=r"^confirm_delete_target_")],
            AWAITING_WORKER_TARGET: [CallbackQueryHandler(select_target_for_worker_callback, pattern=r"^select_worker_target_")],
            AWAITING_WORKER_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_worker_tokens)],
            CONFIRM_WORKER_DELETE: [CallbackQueryHandler(confirm_delete_worker_callback, pattern=r"^confirm_delete_worker_")],
            
            SELECT_WORK_TARGET: [CallbackQueryHandler(select_work_target_callback, pattern=r"^work_target_")],

            # Single Scrape Workflow
            SCRAPE_SELECT_TARGET: [CallbackQueryHandler(scrape_select_target_callback, pattern=r"^select_target_")],
            
            # Deep Scrape Workflow
            SCRAPE_LINK_RANGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_link_range_callback),
                CommandHandler("all", scrape_all_links_callback)
            ],
            SELECT_MULTIPLE_TARGETS: [CallbackQueryHandler(select_multiple_targets_callback, pattern=r"^multi_target_")],
            SCRAPE_UPLOAD_AS: [
                CallbackQueryHandler(scrape_upload_as_callback, pattern=r"^upload_as_"), # For single scrape
                CallbackQueryHandler(toggle_upload_option_callback, pattern=r"^toggle_"), # For deepscrape multi-select
                CallbackQueryHandler(confirm_upload_options_callback, pattern="^confirm_upload_options") # For deepscrape multi-select
            ],
        },
        fallbacks=[
            CommandHandler("stop", stop_command),
            CallbackQueryHandler(cancel_scrape_callback, pattern="^cancel_scrape$")
        ],
        persistent=True,
        name="main_ui_handler",
        allow_reentry=True,
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("stop", stop_command))

    logger.info("Bot is starting polling...")
    application.run_polling()

if __name__ == "__main__":
    web_thread = threading.Thread(target=run_web_server)
    web_thread.daemon = True
    web_thread.start()
    main()
