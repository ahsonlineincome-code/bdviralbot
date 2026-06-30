import os
import asyncio
import datetime
import uvicorn
import time
import hmac
import hashlib
import urllib.parse
import secrets
import logging
import glob
import random
import httpx 
from PIL import Image
from fastapi import FastAPI, Body, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from aiogram import Bot, Dispatcher, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pydantic import BaseModel

# ==========================================
# LOGGING SETUP
# ==========================================
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==========================================
# CONFIGURATION
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "") 
MONGO_URL = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL", "https://example.com")
CHANNEL_ID = os.getenv("CHANNEL_ID", "") 
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "") 
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123") 
BOT_USERNAME = "BDViralBoxProBot"

_db_ch = os.getenv("DB_CHANNEL_ID", "")
DB_CHANNEL_ID = int(_db_ch) if _db_ch and _db_ch.lstrip('-').isdigit() else None

# Initialize bot and dispatcher
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI(title="BD Viral Box", version="2.0")
security = HTTPBasic()

# Pyrogram client (lazy init)
pyro_app = None

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global variables
db = None 
admin_cache = set([OWNER_ID]) 
banned_cache = set() 
trending_cache = {}
list_cache = {}
auto_reply_cache = {} 
keyword_replies_cache = {}
video_queue = None
is_processing = False

# ==========================================
# ADMIN STATES
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_title = State()
    waiting_for_quality = State() 

# ==========================================
# HELPER FUNCTIONS
# ==========================================

async def load_keyword_replies():
    """Load keyword replies from database"""
    if db is None:
        return
    try:
        keyword_replies_cache.clear()
        async for kw in db.keyword_replies.find():
            keyword_replies_cache[kw["keyword"]] = kw["reply_message"]
        logger.info(f"✅ Loaded {len(keyword_replies_cache)} keyword replies")
    except Exception as e:
        logger.error(f"❌ Load keyword replies error: {e}")

def clear_app_cache():
    """Clear application cache"""
    trending_cache.clear()
    list_cache.clear()

async def generate_fast_thumbnail(video_path, output_path):
    """Generate thumbnail from video using ffmpeg"""
    try:
        cmd = f'ffmpeg -ss 10 -i "{video_path}" -vframes 1 -vf "scale=640:360:force_original_aspect_ratio=increase,crop=640:360" -q:v 2 -y "{output_path}"'
        process = await asyncio.create_subprocess_shell(
            cmd, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=30.0)
            return os.path.exists(output_path) and os.path.getsize(output_path) > 0
        except asyncio.TimeoutError:
            process.kill()
            return False
    except Exception as e: 
        logger.error(f"❌ Fast Thumbnail error: {e}")
        return False

async def post_to_channels(photo_id, caption, markup):
    """Post to main channel and log channel"""
    if CHANNEL_ID:
        try: 
            await bot.send_photo(
                chat_id=CHANNEL_ID, 
                photo=photo_id, 
                caption=caption, 
                parse_mode="HTML", 
                reply_markup=markup
            )
            logger.info(f"✅ Posted to CHANNEL_ID: {CHANNEL_ID}")
        except Exception as e: 
            logger.error(f"❌ Channel post error: {e}")
    
    if LOG_CHANNEL_ID:
        try: 
            await bot.send_photo(
                chat_id=LOG_CHANNEL_ID, 
                photo=photo_id, 
                caption=caption, 
                parse_mode="HTML", 
                reply_markup=markup
            )
            logger.info(f"✅ Posted to LOG_CHANNEL_ID: {LOG_CHANNEL_ID}")
        except Exception as e: 
            logger.error(f"❌ Log channel post error: {e}")

async def video_queue_worker():
    """Background worker for processing video uploads"""
    global is_processing
    
    while True:
        try:
            if video_queue is None:
                await asyncio.sleep(1)
                continue
            
            # Get task from queue
            task = await video_queue.get()
            if not task:
                await asyncio.sleep(0.5)
                continue
                
            chat_id, message_id, aiogram_file_id, file_type = task
            is_processing = True
            
            downloaded_file = None
            thumb_path = None
            status_msg = None
            photo_id = None
            
            try:
                admin_id = chat_id
                status_msg = await bot.send_message(
                    admin_id, 
                    "⏳ <b>Processing Video...</b>\n📥 Downloading...", 
                    parse_mode="HTML"
                )
                
                # Check pyro_app
                if pyro_app is None:
                    await bot.edit_message_text(
                        "❌ Pyrogram not initialized! Check logs.", 
                        chat_id=admin_id, 
                        message_id=status_msg.message_id
                    )
                    continue
                
                # Get message from pyrogram
                pyro_msg = await pyro_app.get_messages(chat_id, message_id)
                
                # Generate serial number
                total_vids = await db.movies.count_documents({})
                serial_no = total_vids + 1
                
                # Random viral title
                viral_titles = [
                    "🔥 New Viral Trending Clip",
                    "🔞 Leaked Private Video", 
                    "💋 Desi Viral Collection",
                    "⭐ Exclusive Private Clip",
                    "🌶️ Hot Leaked Collection",
                    "💃 Bhabhi Viral Video Clip"
                ]
                auto_title = f"{random.choice(viral_titles)} #{serial_no:04d}"
                
                # File paths
                video_name = f"temp_video_{serial_no}_{int(time.time())}.mp4"
                thumb_path = os.path.abspath(f"fast_thumb_{serial_no}_{int(time.time())}.jpg")
                
                # Download video
                await bot.edit_message_text(
                    "📥 <b>Downloading video...</b>", 
                    chat_id=admin_id, 
                    message_id=status_msg.message_id,
                    parse_mode="HTML"
                )
                
                downloaded_file = await pyro_app.download_media(
                    pyro_msg, 
                    file_name=video_name
                )
                
                if not downloaded_file or not os.path.exists(downloaded_file):
                    await bot.edit_message_text(
                        "❌ <b>Download failed!</b>\nFile could not be downloaded.", 
                        chat_id=admin_id, 
                        message_id=status_msg.message_id,
                        parse_mode="HTML"
                    )
                    continue
                
                # Generate thumbnail
                await bot.edit_message_text(
                    "📸 <b>Generating thumbnail...</b>", 
                    chat_id=admin_id, 
                    message_id=status_msg.message_id,
                    parse_mode="HTML"
                )
                
                thumb_success = await generate_fast_thumbnail(downloaded_file, thumb_path)
                
                if not thumb_success or not os.path.exists(thumb_path):
                    logger.warning("⚠️ Thumbnail generation failed, using default")
                
                # Upload to DB channel for permanent storage
                db_file_id = None
                db_photo_id = None
                
                if DB_CHANNEL_ID and os.path.exists(thumb_path):
                    try:
                        await bot.edit_message_text(
                            "☁️ <b>Uploading to cloud...</b>", 
                            chat_id=admin_id, 
                            message_id=status_msg.message_id,
                            parse_mode="HTML"
                        )
                        
                        # Copy video to DB channel
                        copied_vid = await bot.copy_message(
                            chat_id=DB_CHANNEL_ID, 
                            from_chat_id=chat_id, 
                            message_id=message_id
                        )
                        db_file_id = copied_vid.message_id
                        
                        # Upload thumbnail to DB channel
                        copied_photo = await bot.send_photo(
                            DB_CHANNEL_ID, 
                            photo=FSInputFile(thumb_path)
                        )
                        db_photo_id = copied_photo.message_id
                        
                        # Get photo_id from the sent photo
                        if copied_photo.photo:
                            photo_id = copied_photo.photo[-1].file_id
                        
                        logger.info(f"✅ Uploaded to DB channel: vid={db_file_id}, photo={db_photo_id}")
                        
                    except Exception as e:
                        logger.error(f"❌ DB Channel upload error: {e}")
                
                # If no photo_id yet, send thumbnail to admin and get it
                if not photo_id and os.path.exists(thumb_path):
                    try:
                        photo_msg = await bot.send_photo(
                            admin_id, 
                            photo=FSInputFile(thumb_path),
                            caption=f"📸 <b>{auto_title}</b>"
                        )
                        if photo_msg.photo:
                            photo_id = photo_msg.photo[-1].file_id
                    except Exception as e:
                        logger.error(f"❌ Send thumbnail error: {e}")
                
                # Insert into database
                movie_data = {
                    "title": auto_title, 
                    "quality": "HD", 
                    "photo_id": photo_id, 
                    "file_id": aiogram_file_id, 
                    "file_type": file_type,
                    "db_file_id": db_file_id, 
                    "db_photo_id": db_photo_id,
                    "clicks": 0, 
                    "created_at": datetime.datetime.utcnow(),
                    "status": "active"
                }
                
                result = await db.movies.insert_one(movie_data)
                movie_id_str = str(result.inserted_id)
                
                clear_app_cache()
                
                # Delete status message
                try:
                    await bot.delete_message(chat_id=admin_id, message_id=status_msg.message_id)
                except:
                    pass
                
                # Send success message with download button
                if photo_id:
                    success_kb = [[
                        types.InlineKeyboardButton(
                            text="📥 Download Now", 
                            callback_data=f"get_file_{movie_id_str}"
                        )
                    ]]
                    success_markup = types.InlineKeyboardMarkup(inline_keyboard=success_kb)
                    
                    await bot.send_photo(
                        admin_id,
                        photo=photo_id,
                        caption=f"✅ <b>{auto_title}</b>\n\n🏷 Quality: HD\n📎 ID: <code>{movie_id_str}</code>\n\n👇 Click below to get file!",
                        reply_markup=success_markup,
                        parse_mode="HTML"
                    )
                    
                    # Post to channels
                    bot_info = await bot.get_me()
                    channel_kb = [[
                        types.InlineKeyboardButton(
                            text="📥 Download & Watch 🎬", 
                            url=f"https://t.me/{bot_info.username}?start=play_{movie_id_str}"
                        )
                    ]]
                    channel_markup = types.InlineKeyboardMarkup(inline_keyboard=channel_kb)
                    
                    caption = (
                        f"🔥 <b>নতুন এক্সক্লুসিভ ভাইরাল ভিডিও!</b>\n\n"
                        f"📌 <b>টাইটেল:</b> {auto_title}\n"
                        f"🏷 <b>কোয়ালিটি:</b> HD\n\n"
                        f"👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>"
                    )
                    
                    await post_to_channels(photo_id, caption, channel_markup)
                    
                    logger.info(f"✅ Movie uploaded successfully: {auto_title} ({movie_id_str})")
                else:
                    # No photo case
                    await bot.send_message(
                        admin_id,
                        f"✅ <b>{auto_title}</b> uploaded!\n\n⚠️ But thumbnail failed.\nID: <code>{movie_id_str}</code>",
                        parse_mode="HTML"
                    )
                    
            except Exception as e:
                logger.error(f"❌ Video processing error: {e}", exc_info=True)
                try:
                    await bot.send_message(
                        chat_id, 
                        f"⚠️ <b>Error:</b> <code>{str(e)[:500]}</code>", 
                        parse_mode="HTML"
                    )
                except:
                    pass
                    
            finally:
                # Cleanup files
                if downloaded_file and os.path.exists(downloaded_file): 
                    try: os.remove(downloaded_file)
                    except: pass
                if thumb_path and os.path.exists(thumb_path): 
                    try: os.remove(thumb_path)
                    except: pass
                
                video_queue.task_done()
                is_processing = False
                
        except Exception as e:
            logger.error(f"❌ Queue worker error: {e}", exc_info=True)
            is_processing = False
            await asyncio.sleep(2)

async def load_admins():
    """Load admin users from database"""
    if db is None:
        return
    try:
        admin_cache.clear()
        admin_cache.add(OWNER_ID)
        async for admin in db.admins.find(): 
            admin_cache.add(admin["user_id"])
        logger.info(f"✅ Loaded {len(admin_cache)} admins")
    except Exception as e:
        logger.error(f"❌ Load admins error: {e}")

async def load_banned_users():
    """Load banned users from database"""
    if db is None:
        return
    try:
        banned_cache.clear()
        async for b_user in db.banned.find(): 
            banned_cache.add(b_user["user_id"])
        logger.info(f"✅ Loaded {len(banned_cache)} banned users")
    except Exception as e:
        logger.error(f"❌ Load banned users error: {e}")

async def init_db():
    """Initialize database indexes"""
    if db is None:
        return
    try:
        await db.movies.create_index([("title", "text")])
        await db.movies.create_index("created_at")
        await db.movies.create_index("status")
        logger.info("✅ Database indexes created successfully!")
    except Exception as e:
        logger.error(f"❌ Init DB error: {e}")

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials for web panel"""
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"}
        )
    return True

# ==========================================
# TELEGRAM COMMANDS
# ==========================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    """Handle /start command with ad system"""
    uid = message.from_user.id
    
    # Check ban
    if uid in banned_cache: 
        return await message.answer(
            "🚫 <b>আপনাকে ব্যান করা হয়েছে!</b>\n\nকোনো প্রশ্ন থাকলে Admin কে যোগাযোগ করুন।", 
            parse_mode="HTML"
        )
    
    await state.clear()
    
    # Parse arguments
    args = message.text.split(" ", 1)
    arg = args[1] if len(args) > 1 else ""
    
    # Handle play_ argument (with 10-second ad)
    if arg.startswith("play_"):
        movie_id = arg.replace("play_", "")
        
        # Show waiting message with countdown button
        waiting_kb = [[
            types.InlineKeyboardButton(
                text="⏳ অপেক্ষা করুন... (10 সেকেন্ড)", 
                callback_data=f"ad_wait_{movie_id}"
            )
        ]]
        
        waiting_msg = await message.answer(
            "⏳ <b>ডাউনলোড লিংক তৈরি হচ্ছে...</b>\n\n"
            "⚠️ <b>দয়া করে ১০ সেকেন্ড অপেক্ষা করুন!</b>\n\n"
            "🔄 নিচের বাটনে ক্লিক করুন...", 
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=waiting_kb), 
            parse_mode="HTML"
        )
        
        # Store message info for later edit
        await state.update_data(waiting_msg_id=waiting_msg.message_id, movie_id=movie_id)
        
        # Start countdown timer
        asyncio.create_task(ad_countdown(waiting_msg, movie_id, state))
        
        return
    
    # Normal start - register/update user
    now = datetime.datetime.utcnow()
    
    if db is not None:
        user = await db.users.find_one({"user_id": uid})
        
        if not user:
            await db.users.insert_one({
                "user_id": uid, 
                "first_name": message.from_user.first_name or "User",
                "username": message.from_user.username or "",
                "joined_at": now, 
                "last_active": now,
                "download_count": 0
            })
            logger.info(f"👤 New user: {uid}")
        else:
            await db.users.update_one(
                {"user_id": uid}, 
                {"$set": {"last_active": now}}
            )
    
    # Build response based on user type
    kb = [[types.InlineKeyboardButton(
        text="🎬 Watch Now", 
        web_app=types.WebAppInfo(url=APP_URL)
    )]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    if uid in admin_cache:
        text = (
            "👋 <b>হ্যালো অ্যাডমিন!</b> 👑\n\n"
            "⚙️ <b>Available Commands:</b>\n\n"
            "• <code>/autoupload on|off</code> - Toggle auto-upload\n"
            "• <code>/delmovie title</code> - Delete movie\n"
            "• <code>/stats</code> - View statistics\n"
            "• <code>/cast</code> - Broadcast message\n"
            "• <code>/ban user_id</code> - Ban user\n"
            "• <code>/unban user_id</code> - Unban user"
        )
    else: 
        text = (
            f"👋 <b>Welcome to BD Viral Box!</b> 🎬\n\n"
            f"হ্যালো <b>{message.from_user.first_name or 'User'}</b>! 💐\n\n"
            "🎥 এখানে আপনি পাবেন:\n"
            "• ভাইরাল ভিডিও\n"
            "• এক্সক্লুসিভ কন্টেন্ট\n"
            "• HD Quality ফাইল\n\n"
            "👇 নিচের বাটনে ক্লিক করুন!"
        )
        
    await message.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)

async def ad_countdown(msg, movie_id: str, state: FSMContext):
    """Handle 10-second ad countdown"""
    try:
        await asyncio.sleep(10)
        
        # Get stored data
        data = await state.get_data()
        waiting_msg_id = data.get("waiting_msg_id")
        
        # Create download button
        download_kb = [[
            types.InlineKeyboardButton(
                text="✅ এখন ডাউনলোড করুন 📥", 
                callback_data=f"get_file_{movie_id}"
            )
        ]]
        
        try:
            await msg.edit_text(
                "✅ <b>সময় শেষ!</b> 🎉\n\n"
                "এখন নিচের বাটনে ক্লিক করে ফাইল ডাউনলোড করুন!", 
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=download_kb), 
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not edit ad message: {e}")
            
    except Exception as e:
        logger.error(f"Ad countdown error: {e}")

@dp.callback_query(F.data.startswith("ad_wait_"))
async def ad_wait_cb(c: types.CallbackQuery):
    """Handle click on ad wait button"""
    await c.answer(
        "⏳ দয়া করে ১০ সেকেন্ড অপেক্ষা করুন!\n\nএড দেখতে হবে! 🙏", 
        show_alert=True
    )

@dp.callback_query(F.data.startswith("get_file_"))
async def get_file_cb(c: types.CallbackQuery):
    """Handle file download request"""
    movie_id = c.data.split("_")[2]
    
    try:
        if db is None:
            return await c.answer("❌ Database error!", show_alert=True)
        
        # Find movie in database
        movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
        
        if not movie: 
            return await c.answer("❌ ফাইল পাওয়া যায়নি!", show_alert=True)
        
        # Increment click count
        await db.movies.update_one(
            {"_id": ObjectId(movie_id)}, 
            {"$inc": {"clicks": 1}}
        )
        
        file_id = movie.get("file_id")
        file_type = movie.get("file_type", "video")
        title = movie.get("title", "Unknown")
        quality = movie.get("quality", "HD")
        
        caption = (
            f"🎬 <b>{title}</b>\n"
            f"🏷 Quality: {quality}\n"
            f"📊 Downloads: {movie.get('clicks', 0) + 1}\n\n"
            f"🔗 Powered By: @{BOT_USERNAME}"
        )
        
        # Send file based on type
        if file_type == "video":
            await c.message.answer_video(video=file_id, caption=caption, parse_mode="HTML")
        else:
            await c.message.answer_document(document=file_id, caption=caption, parse_mode="HTML")
        
        # Delete the button message
        try:
            await c.message.delete()
        except:
            pass
            
        logger.info(f"📥 File sent: {title} to user {c.from_user.id}")
            
    except Exception as e:
        logger.error(f"❌ Get file error: {e}", exc_info=True)
        await c.answer(f"❌ Error: {str(e)[:200]}", show_alert=True)

# ==========================================
# ADMIN COMMANDS
# ==========================================

@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    """Show bot statistics"""
    if m.from_user.id not in admin_cache: 
        return await m.answer("❌ আপনি Admin নন!", parse_mode="HTML")
    
    if db is None:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
    
    try:
        uc = await db.users.count_documents({})
        mc = await db.movies.count_documents({})
        dc = sum(1 for _ in db.movies.find({"clicks": {"$gt": 0}}))
        
        text = (
            f"📊 <b>BD Viral Box Statistics</b>\n\n"
            f"👥 Total Users: <code>{uc}</code>\n"
            f"🎬 Total Files: <code>{mc}</code>\n"
            f"📥 Downloads: <code>{dc}</code>\n"
            f"🚫 Banned: <code>{len(banned_cache)}</code>\n"
            f"👑 Admins: <code>{len(admin_cache)}</code>\n\n"
            f"⏰ <i>{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</i>"
        )
        
        await m.answer(text, parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await m.answer("❌ Error fetching stats!", parse_mode="HTML")

@dp.message(Command("autoupload"))
async def toggle_auto_upload(m: types.Message):
    """Toggle auto-upload mode"""
    if m.from_user.id not in admin_cache: 
        return await m.answer("❌ Admin only!", parse_mode="HTML")
    
    if db is None:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
    
    try:
        parts = m.text.split()
        if len(parts) < 2:
            config = await db.settings.find_one({"id": "auto_upload_mode"})
            current = config.get("status", False) if config else False
            status = "চালু ✅" if current else "বন্ধ ❌"
            return await m.answer(
                f"⚙️ Auto Upload Status: <b>{status}</b>\n\nUsage: <code>/autoupload on|off</code>",
                parse_mode="HTML"
            )
        
        state = parts[1].lower()
        if state not in ["on", "off"]:
            return await m.answer("❌ Usage: /autoupload on|off", parse_mode="HTML")
        
        await db.settings.update_one(
            {"id": "auto_upload_mode"}, 
            {"$set": {"status": state == "on"}}, 
            upsert=True
        )
        
        status_text = "চালু ✅" if state == "on" else "বন্ধ ❌"
        await m.answer(f"✅ Auto Upload {status_text}", parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Auto upload toggle error: {e}")
        await m.answer("❌ Error occurred!", parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    """Delete movie by title"""
    if m.from_user.id not in admin_cache: 
        return await m.answer("❌ Admin only!", parse_mode="HTML")
    
    if db is None:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
    
    try:
        parts = m.text.split(" ", 1)
        if len(parts) < 2:
            return await m.answer("❌ Usage: /delmovie <title>", parse_mode="HTML")
        
        title = parts[1].strip()
        result = await db.movies.delete_many({"title": {"$regex": title, "$options": "i"}})
        
        if result.deleted_count > 0:
            clear_app_cache()
            await m.answer(
                f"✅ <b>{result.deleted_count}</b> টি ফাইল ডিলিট হয়েছে!", 
                parse_mode="HTML"
            )
        else:
            await m.answer("❕ কোনো ফাইল পাওয়া যায়নি!", parse_mode="HTML")
            
    except Exception as e:
        logger.error(f"Delete movie error: {e}")
        await m.answer("❌ Error occurred!", parse_mode="HTML")

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    """Prepare for broadcast"""
    if m.from_user.id not in admin_cache: 
        return await m.answer("❌ Admin only!", parse_mode="HTML")
    
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer(
        "📢 <b>Broadcast Mode Activated!</b>\n\n"
        "এখন যে মেসেজটি ব্রডকাস্ট করতে চান সেটি পাঠান।\n\n"
        "⚠️ সব ইউজারকে পাঠানো হবে!",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    """Execute broadcast to all users"""
    await state.clear()
    
    if db is None:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
    
    await m.answer("⏳ <b>ব্রডকাস্ট শুরু হচ্ছে...</b>", parse_mode="HTML")
    
    kb = [[types.InlineKeyboardButton(
        text="🎬 Open BD Viral Box", 
        web_app=types.WebAppInfo(url=APP_URL)
    )]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    success = 0
    failed = 0
    
    async for u in db.users.find():
        try:
            await m.copy_to(chat_id=u['user_id'], reply_markup=markup)
            success += 1
            await asyncio.sleep(0.05)  # Rate limit
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            failed += 1
        except Exception:
            failed += 1
    
    await m.answer(
        f"✅ <b>ব্রডকাস্ট সম্পন্ন!</b>\n\n"
        f"✅ সফল: <b>{success}</b>\n"
        f"❌ ব্যর্থ: <b>{failed}</b>\n"
        f"📊 মোট: <b>{success + failed}</b>",
        parse_mode="HTML"
    )

# ==========================================
# FILE UPLOAD HANDLER
# ==========================================

@dp.message(F.content_type.in_({'video', 'document'}))
async def receive_movie_file(m: types.Message, state: FSMContext):
    """Handle incoming file upload from admin"""
    # Check if admin
    if m.from_user.id not in admin_cache: 
        return await m.answer("❌ শুধুমাত্র Admin ফাইল আপলোড করতে পারবেন!", parse_mode="HTML")
    
    if db is None:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
    
    # Check auto-upload setting
    config = await db.settings.find_one({"id": "auto_upload_mode"})
    is_auto = config.get("status", False) if config else False
    
    # Get file info
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    
    if is_auto:
        # Auto mode - add to queue
        if video_queue is None:
            return await m.answer("❌ Queue not ready! Try again.", parse_mode="HTML")
        
        await video_queue.put((m.chat.id, m.message_id, fid, ftype))
        await m.answer(
            "✅ <b>File added to queue!</b> 📥\n\n"
            "⏳ Processing will start shortly...\n"
            "Please wait!",
            parse_mode="HTML"
        )
        logger.info(f"📥 File queued: {ftype} from admin {m.from_user.id}")
    else:
        # Manual mode - interactive upload
        status_msg = await m.answer(
            "⏳ <b>Processing file...</b>\n📸 Generating thumbnail...",
            parse_mode="HTML"
        )
        
        downloaded_file = None
        thumb_path = f"thumb_manual_{int(time.time())}.jpg"
        photo_id = None
        db_file_id = None
        db_photo_id = None
        
        try:
            # Download using pyrogram
            if pyro_app is None:
                raise Exception("Pyrogram not initialized")
            
            pyro_msg = await pyro_app.get_messages(m.chat.id, m.message_id)
            downloaded_file = await pyro_app.download_media(
                pyro_msg, 
                file_name=f"manual_{int(time.time())}.mp4"
            )
            
            # Generate thumbnail
            if downloaded_file:
                await generate_fast_thumbnail(downloaded_file, thumb_path)
            
            # Upload to DB channel
            if DB_CHANNEL_ID and os.path.exists(thumb_path):
                try:
                    copied = await bot.copy_message(
                        chat_id=DB_CHANNEL_ID, 
                        from_chat_id=m.chat.id, 
                        message_id=m.message_id
                    )
                    db_file_id = copied.message_id
                    
                    copied_photo = await bot.send_photo(
                        DB_CHANNEL_ID, 
                        photo=FSInputFile(thumb_path)
                    )
                    db_photo_id = copied_photo.message_id
                    photo_id = copied_photo.photo[-1].file_id
                    
                except Exception as e:
                    logger.error(f"Manual upload DB error: {e}")
            
            # Fallback: send to admin chat
            if not photo_id and os.path.exists(thumb_path):
                sent_photo = await m.answer_photo(photo=FSInputFile(thumb_path))
                photo_id = sent_photo.photo[-1].file_id
            
            # Ask for title
            await m.answer(
                "✅ <b>Thumbnail ready!</b> 📸\n\n"
                "এবার মুভির <b>টাইটেল (নাম)</b> লিখে পাঠান:",
                parse_mode="HTML"
            )
            
            # Save to state
            await state.update_data(
                file_id=fid, 
                file_type=ftype, 
                db_file_id=db_file_id, 
                photo_id=photo_id, 
                db_photo_id=db_photo_id
            )
            await state.set_state(AdminStates.waiting_for_title)
            
            # Delete status message
            try:
                await bot.delete_message(m.chat.id, status_msg.message_id)
            except:
                pass
                
        except Exception as e:
            logger.error(f"Manual upload error: {e}")
            
            await m.answer(
                "✅ <b>File received!</b> 📥\n\n"
                "এবার মুভির <b>টাইটেল (নাম)</b> লিখে পাঠান:",
                parse_mode="HTML"
            )
            
            await state.update_data(
                file_id=fid, 
                file_type=ftype, 
                db_file_id=None, 
                photo_id=None, 
                db_photo_id=None
            )
            await state.set_state(AdminStates.waiting_for_title)
            
            try:
                await bot.delete_message(m.chat.id, status_msg.message_id)
            except:
                pass
                
        finally:
            # Cleanup temp files
            if downloaded_file and os.path.exists(downloaded_file): 
                try: os.remove(downloaded_file)
                except: pass
            if os.path.exists(thumb_path): 
                try: os.remove(thumb_path)
                except: pass

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    """Receive movie title from admin"""
    if not m.text.strip():
        return await m.answer("❌ টাইটেল খালি থাকতে পারবে না!", parse_mode="HTML")
    
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    
    await m.answer(
        f"✅ নাম: <b>{m.text.strip()}</b>\n\n"
        "এবার ফাইলের <b>কোয়ালিটি</b> দিন:\n"
        "(যেমন: 720p, 1080p, 4K)",
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    """Receive movie quality and save to database"""
    if not m.text.strip():
        return await m.answer("❌ কোয়ালিটি দিতে হবে!", parse_mode="HTML")
    
    await state.update_data(quality=m.text.strip())
    data = await state.get_data()
    await state.clear()
    
    if db is None:
        return await m.answer("❌ Database error!", parse_mode="HTML")
    
    title = data["title"]
    photo_id = data.get("photo_id")
    quality = data["quality"]
    
    # Insert into database
    result = await db.movies.insert_one({
        "title": title, 
        "quality": quality, 
        "photo_id": photo_id, 
        "file_id": data["file_id"], 
        "file_type": data["file_type"],
        "db_file_id": data.get("db_file_id"), 
        "db_photo_id": data.get("db_photo_id"),
        "clicks": 0, 
        "created_at": datetime.datetime.utcnow(),
        "status": "active"
    })
    
    movie_id_str = str(result.inserted_id)
    clear_app_cache()
    
    # Success message with download button
    success_kb = [[
        types.InlineKeyboardButton(
            text="📥 Download Now", 
            callback_data=f"get_file_{movie_id_str}"
        )
    ]]
    success_markup = types.InlineKeyboardMarkup(inline_keyboard=success_kb)
    
    await m.answer(
        f"🎉 <b>{title} [{quality}]</b>\n\n"
        f"✅ BD Viral Box এ যুক্ত হয়েছে!\n"
        f"📎 ID: <code>{movie_id_str}</code>",
        reply_markup=success_markup,
        parse_mode="HTML"
    )
    
    # Post to channels if has photo
    if photo_id:
        try:
            bot_info = await bot.get_me()
            channel_kb = [[
                types.InlineKeyboardButton(
                    text="📥 Download & Watch 🎬", 
                    url=f"https://t.me/{bot_info.username}?start=play_{movie_id_str}"
                )
            ]]
            channel_markup = types.InlineKeyboardMarkup(inline_keyboard=channel_kb)
            
            caption = (
                f"🔥 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n"
                f"📌 <b>টাইটেল:</b> {title}\n"
                f"🏷 <b>কোয়ালিটি:</b> {quality}"
            )
            
            await post_to_channels(photo_id, caption, channel_markup)
            logger.info(f"✅ Manual upload complete: {title}")
            
        except Exception as e:
            logger.error(f"Post to channel error: {e}")

# ==========================================
# WEB APPLICATION APIs
# ==========================================

@app.get("/api/thumb/{file_id}")
async def get_thumbnail(file_id: str):
    """Get thumbnail by file_id"""
    try:
        file = await bot.get_file(file_id)
        if not file.file_path: 
            raise HTTPException(status_code=404, detail="File not found")
        
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}"
        return RedirectResponse(url=url)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Thumbnail error: {e}")
        raise HTTPException(status_code=404, detail="Thumbnail not available")

@app.get("/api/movies/search")
async def search_movies(q: str = "", page: int = 1):
    """Search and list movies API"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    
    limit = 15
    skip = (page - 1) * limit
    
    # Build query
    query = {}
    if q.strip():
        query = {"title": {"$regex": q, "$options": "i"}}
    
    try:
        # Execute query
        cursor = db.movies.find(query).sort("created_at", -1).skip(skip).limit(limit)
        movies = await cursor.to_list(length=limit)
        total = await db.movies.count_documents(query)
        
        # Format results
        result = []
        for m in movies:
            movie_item = {
                "id": str(m["_id"]),
                "title": m.get("title", "Untitled"),
                "quality": m.get("quality", "HD"),
                "photo_id": m.get("photo_id", ""),
                "clicks": m.get("clicks", 0),
                "created_at": m.get("created_at", "").strftime("%Y-%m-%d") if m.get("created_at") else ""
            }
            result.append(movie_item)
        
        pages = (total + limit - 1) // limit if total > 0 else 1
        
        logger.info(f"🔍 Search: q='{q}', page={page}, results={len(result)}, total={total}")
        
        return {
            "results": result, 
            "total": total, 
            "pages": pages,
            "current_page": page
        }
        
    except Exception as e:
        logger.error(f"❌ Search movies error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Search failed")

@app.get("/api/stats")
async def api_stats():
    """Public stats API"""
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    
    try:
        mc = await db.movies.count_documents({})
        uc = await db.users.count_documents({})
        
        return {
            "movies": mc,
            "users": uc,
            "status": "online"
        }
    except Exception as e:
        logger.error(f"Stats API error: {e}")
        raise HTTPException(status_code=500, detail="Failed")

@app.get("/")
async def web_app():
    """Main web application - Movie browser"""
    return HTMLResponse(content="""
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>BD Viral Box - 🎬</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #fff;
            min-height: 100vh;
            padding-bottom: 90px;
        }
        
        .header {
            background: rgba(15, 23, 42, 0.95);
            backdrop-filter: blur(10px);
            padding: 16px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(59, 130, 246, 0.2);
        }
        
        .header h1 { 
            font-size: 20px; 
            font-weight: 700;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .home-btn { 
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white; 
            border: none; 
            padding: 10px 20px; 
            border-radius: 10px; 
            cursor: pointer; 
            font-weight: 600;
            font-size: 14px;
            transition: transform 0.2s;
        }
        
        .home-btn:active { transform: scale(0.95); }
        
        .search-box { 
            padding: 20px 16px;
        }
        
        input[type=text] { 
            width: 100%; 
            padding: 14px 18px; 
            border-radius: 12px; 
            border: 2px solid rgba(59, 130, 246, 0.3);
            background: rgba(30, 41, 59, 0.8);
            color: #fff;
            font-size: 16px;
            outline: none;
            transition: all 0.3s;
        }
        
        input[type=text]:focus { 
            border-color: #3b82f6;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
        }
        
        input[type=text]::placeholder {
            color: #94a3b8;
        }
        
        .grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); 
            gap: 16px; 
            padding: 0 16px;
        }
        
        .card { 
            background: rgba(30, 41, 59, 0.7);
            backdrop-filter: blur(10px);
            border-radius: 16px; 
            overflow: hidden; 
            cursor: pointer;
            transition: all 0.3s;
            border: 1px solid rgba(255,255,255,0.05);
        }
        
        .card:hover {
            transform: translateY(-4px);
            box-shadow: 0 10px 30px rgba(59, 130, 246, 0.3);
            border-color: rgba(59, 130, 246, 0.5);
        }
        
        .card:active { transform: scale(0.96); }
        
        .card img { 
            width: 100%; 
            height: 220px; 
            object-fit: cover;
            background: #1e293b;
        }
        
        .info { 
            padding: 12px; 
        }
        
        .title { 
            font-size: 14px; 
            font-weight: 600;
            white-space: nowrap; 
            overflow: hidden; 
            text-overflow: ellipsis; 
            margin-bottom: 6px;
            line-height: 1.3;
        }
        
        .quality { 
            font-size: 12px; 
            color: #3b82f6; 
            font-weight: 700;
            text-transform: uppercase;
        }
        
        .pagination { 
            display: flex; 
            justify-content: center; 
            align-items: center; 
            gap: 16px; 
            padding: 20px; 
            position: fixed; 
            bottom: 0; 
            left: 0; 
            right: 0; 
            background: rgba(15, 23, 42, 0.98);
            backdrop-filter: blur(10px);
            border-top: 1px solid rgba(59, 130, 246, 0.2);
            z-index: 100;
        }
        
        .page-btn { 
            background: linear-gradient(135deg, #3b82f6, #2563eb);
            color: white; 
            border: none; 
            padding: 12px 24px; 
            border-radius: 10px; 
            cursor: pointer; 
            font-weight: 700;
            font-size: 14px;
            transition: all 0.3s;
        }
        
        .page-btn:disabled { 
            background: #334155; 
            color: #64748b; 
            cursor: not-allowed;
            opacity: 0.5;
        }
        
        #pageInfo { 
            font-weight: 700; 
            color: #94a3b8;
            font-size: 16px;
            min-width: 80px;
            text-align: center;
        }
        
        .loading { 
            text-align: center; 
            padding: 60px 20px;
            color: #94a3b8;
        }
        
        .loading-spinner {
            width: 40px;
            height: 40px;
            border: 4px solid rgba(59, 130, 246, 0.2);
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 16px;
        }
        
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .empty-state {
            text-align: center;
            padding: 80px 20px;
            color: #64748b;
        }
        
        .empty-icon {
            font-size: 64px;
            margin-bottom: 16px;
        }
        
        .stats-bar {
            text-align: center;
            padding: 16px;
            background: rgba(30, 41, 59, 0.5);
            margin: 0 16px 16px;
            border-radius: 12px;
            font-size: 14px;
            color: #94a3b8;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🎬 BD Viral Box</h1>
        <button class="home-btn" onclick="goHome()">🏠 Home</button>
    </div>
    
    <div class="search-box">
        <input type="text" id="searchInput" placeholder="🔍 Search movies..." onkeyup="handleSearch(event)">
    </div>
    
    <div class="stats-bar" id="statsBar">
        Loading...
    </div>
    
    <div class="grid" id="movieGrid"></div>
    
    <div class="pagination">
        <button class="page-btn" id="prevBtn" onclick="changePage(-1)">◀ Prev</button>
        <span id="pageInfo">1 / 1</span>
        <button class="page-btn" id="nextBtn" onclick="changePage(1)">Next ▶</button>
    </div>

    <script>
        // Initialize Telegram WebApp
        let tg = window.Telegram ? window.Telegram.WebApp : null;
        if (tg) {
            tg.expand();
            tg.setHeaderColor('#0f172a');
            tg.setBackgroundColor('#0f172a');
        }

        // State
        let currentPage = 1;
        let totalPages = 1;
        let searchTimeout = null;
        let isLoading = false;

        function goHome() {
            document.getElementById('searchInput').value = '';
            currentPage = 1;
            loadMovies();
        }

        function handleSearch(e) {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                currentPage = 1;
                loadMovies();
            }, 500);
        }

        function changePage(dir) {
            const newPage = currentPage + dir;
            if (newPage >= 1 && newPage <= totalPages) {
                currentPage = newPage;
                loadMovies();
            }
        }

        async function loadStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();
                document.getElementById('statsBar').innerHTML = 
                    `🎬 ${data.movies || 0} Movies • 👥 ${data.users || 0} Users`;
            } catch (e) {
                document.getElementById('statsBar').innerHTML = '🎬 BD Viral Box';
            }
        }

        async function loadMovies() {
            if (isLoading) return;
            isLoading = true;
            
            const q = document.getElementById('searchInput').value.trim();
            const grid = document.getElementById('movieGrid');
            
            grid.innerHTML = `
                <div class="loading" style="width:100%">
                    <div class="loading-spinner"></div>
                    <div>Loading movies...</div>
                </div>
            `;
            
            try {
                const response = await fetch(`/api/movies/search?q=${encodeURIComponent(q)}&page=${currentPage}`);
                const data = await response.json();
                
                grid.innerHTML = '';
                totalPages = data.pages || 1;
                
                // Update pagination UI
                document.getElementById('pageInfo').textContent = `${currentPage} / ${totalPages}`;
                document.getElementById('prevBtn').disabled = currentPage === 1;
                document.getElementById('nextBtn').disabled = currentPage === totalPages;
                
                // Empty state
                if (!data.results || data.results.length === 0) {
                    grid.innerHTML = `
                        <div class="empty-state" style="width:100%;grid-column:1/-1">
                            <div class="empty-icon">🎬</div>
                            <h3>No Movies Found</h3>
                            <p>${q ? 'Try different keywords' : 'Check back later for new content!'}</p>
                        </div>
                    `;
                    isLoading = false;
                    return;
                }
                
                // Render movie cards
                data.results.forEach(m => {
                    const card = document.createElement('div');
                    card.className = 'card';
                    card.onclick = () => openMovie(m.id);
                    
                    const thumbUrl = m.photo_id 
                        ? `/api/thumb/${m.photo_id}` 
                        : 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 200"><rect fill="%231e293b" width="150" height="200"/><text x="75" y="100" text-anchor="middle" fill="%2364748b" font-size="40">🎬</text></svg>';
                    
                    card.innerHTML = `
                        <img src="${thumbUrl}" 
                             alt="${m.title}" 
                             loading="lazy"
                             onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 150 200%22><rect fill=%22%231e293b%22 width=%22150%22 height=%22200%22/><text x=%2275%22 y=%22100%22 text-anchor=%22middle%22 fill=%22%2364748b%22 font-size=%2240%22>🎬</text></svg>'">
                        <div class="info">
                            <div class="title">${escapeHtml(m.title)}</div>
                            <div class="quality">${escapeHtml(m.quality)}</div>
                        </div>
                    `;
                    
                    grid.appendChild(card);
                });
                
                console.log(`✅ Loaded ${data.results.length} movies`);
                
            } catch (error) {
                console.error('❌ Load error:', error);
                grid.innerHTML = `
                    <div class="empty-state" style="width:100%;grid-column:1/-1">
                        <div class="empty-icon">⚠️</div>
                        <h3>Error Loading</h3>
                        <p>Please try again later</p>
                    </div>
                `;
            } finally {
                isLoading = false;
            }
        }

        function openMovie(id) {
            if (tg) {
                tg.openTelegramLink(`https://t.me/${BOT_USERNAME || 'BDViralBoxProBot'}?start=play_${id}`);
            } else {
                window.open(`https://t.me/${BOT_USERNAME || 'BDViralBoxProBot'}?start=play_${id}`, '_blank');
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Initialize
        loadStats();
        loadMovies();
        
        // Refresh stats every 30 seconds
        setInterval(loadStats, 30000);
    </script>
</body>
</html>""")

# ==========================================
# STARTUP & SHUTDOWN EVENTS
# ==========================================

@app.on_event("startup")
async def startup_event():
    """Application startup initialization"""
    global db, video_queue, pyro_app
    
    print("\n" + "="*60)
    print("🚀 Starting BD Viral Box...")
    print("="*60 + "\n")
    
    # Connect to MongoDB
    try:
        print("📦 Connecting to MongoDB...")
        db_client = AsyncIOMotorClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        # Test connection
        await db_client.admin.command('ping')
        db = db_client['bd_viral_box']
        print("✅ MongoDB Connected Successfully!")
    except Exception as e:
        print(f"❌ MongoDB Connection Failed: {e}")
        db = None
    
    # Initialize Pyrogram
    try:
        print("📱 Initializing Pyrogram...")
        from pyrogram import Client as PyroClient
        
        if SESSION_STRING and SESSION_STRING.strip():
            pyro_app = PyroClient(
                "bd_viral_session", 
                api_id=API_ID, 
                api_hash=API_HASH, 
                session_string=SESSION_STRING, 
                in_memory=True, 
                no_updates=True
            )
            print("✅ Pyrogram User Client Ready!")
        else:
            pyro_app = PyroClient(
                "bd_viral_bot", 
                api_id=API_ID, 
                api_hash=API_HASH, 
                bot_token=TOKEN, 
                in_memory=True, 
                no_updates=True
            )
            print("✅ Pyrogram Bot Client Ready!")
            
    except Exception as e:
        print(f"⚠️ Pyrogram Init Failed: {e}")
        print("⚠️ Video features will be disabled!")
        pyro_app = None
    
    # Initialize queue
    video_queue = asyncio.Queue()
    print("✅ Video Queue Initialized!")
    
    # Initialize database
    if db is not None:
        await init_db()
        await load_admins()
        await load_banned_users()
        await load_keyword_replies()
    
    # Start background tasks
    print("🤖 Starting Bot Polling...")
    asyncio.create_task(dp.start_polling(bot))
    
    print("⚙️ Starting Video Worker...")
    asyncio.create_task(video_queue_worker())
    
    print("\n" + "="*60)
    print("🎉 BD Viral Box Started Successfully!")
    print("="*60 + "\n")

@app.on_event("shutdown")
async def shutdown_event():
    """Application shutdown cleanup"""
    print("\n🛑 Shutting down BD Viral Box...")
    
    try:
        await dp.stop_polling()
        print("✅ Stopped polling")
    except Exception as e:
        print(f"⚠️ Stop polling error: {e}")
    
    try:
        await bot.session.close()
        print("✅ Closed bot session")
    except Exception as e:
        print(f"⚠️ Close session error: {e}")
    
    if pyro_app:
        try:
            await pyro_app.stop()
            print("✅ Stopped Pyrogram")
        except Exception as e:
            print(f"⚠️ Stop pyrogram error: {e}")
    
    print("👋 Goodbye!\n")

# ==========================================
# MAIN ENTRY POINT
# ==========================================

if __name__ == "__main__":
    print("""
╔═══════════════════════════════════════╗
║     🎬 BD Viral Box v2.0              ║
║     Powered by AI & Telegram          ║
╚═══════════════════════════════════════╝
    """)
    
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=int(os.getenv("PORT", 8000)),
        log_level="info"
    )
