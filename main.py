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
# ✅ FIX 1: Added Command import
from aiogram.filters import Command
# ✅ FIX 2: Added FSInputFile import
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pydantic import BaseModel
# ✅ FIX 3: Lazy import - don't import at top level
# from pyrogram import Client as PyroClient  
from assistant.ai_reply import get_smart_reply

# ==========================================
# 0. Logging Setup
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 1. Configuration & Global Variables
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "") 
MONGO_URL = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID", "") 
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "") 
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123") 
BOT_USERNAME = "BDViralBoxProBot"

CHANNEL_LINK = "https://t.me/addlist/MwbWNafSFK4yZjhl"
REQUEST_LINK = "https://t.me/+nmWxIcRtkrg5Y2Vl"

_db_ch = os.getenv("DB_CHANNEL_ID", "")
DB_CHANNEL_ID = int(_db_ch) if _db_ch.lstrip('-').isdigit() else None

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()
security = HTTPBasic()

# ✅ FIX 4: Initialize as None first - will be set in startup
pyro_app = None

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

db = None 
admin_cache = set([OWNER_ID]) 
banned_cache = set() 
trending_cache = {}
list_cache = {}
auto_reply_cache = {} 
keyword_replies_cache = {}

async def load_keyword_replies():
    if db is None:
        return
    keyword_replies_cache.clear()
    try:
        async for kw in db.keyword_replies.find():
            keyword_replies_cache[kw["keyword"]] = kw["reply_message"]
    except Exception as e:
        logger.error(f"Load keyword replies error: {e}")

def clear_app_cache():
    trending_cache.clear()
    list_cache.clear()

video_queue = None
is_processing = False

class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_series_search = State()
    waiting_for_episode_quality = State()

# 🛑 FAST THUMBNAIL GENERATOR
async def generate_fast_thumbnail(video_path, output_path):
    try:
        cmd = f'ffmpeg -ss 10 -i "{video_path}" -vframes 1 -vf "scale=640:360:force_original_aspect_ratio=increase,crop=640:360" -q:v 2 "{output_path}"'
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(process.communicate(), timeout=30.0)
            return os.path.exists(output_path)
        except asyncio.TimeoutError:
            process.kill()
            return False
    except Exception as e: 
        logger.error(f"Fast Thumbnail error: {e}")
        return False

# 🛑 HELPER: POST TO MAIN & LOG CHANNEL
async def post_to_channels(photo_id, caption, markup):
    if CHANNEL_ID:
        try: 
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
        except Exception as e: 
            logger.error(f"Channel post error: {e}")
    if LOG_CHANNEL_ID:
        try: 
            await bot.send_photo(chat_id=LOG_CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
        except Exception as e: 
            logger.error(f"Log channel post error: {e}")

async def video_queue_worker():
    global is_processing
    while True:
        try:
            if video_queue is None:
                await asyncio.sleep(1)
                continue
                
            chat_id, message_id, aiogram_file_id, file_type = await video_queue.get()
            is_processing = True
            downloaded_file = None
            thumb_path = None
            status_msg = None
            
            try:
                admin_id = chat_id
                status_msg = await bot.send_message(admin_id, "⏳ <b>Processing Video...</b> (Downloading)")
                
                # ✅ FIX 5: Check if pyro_app is available
                if pyro_app is None:
                    await bot.edit_message_text("❌ Pyrogram not initialized!", chat_id=admin_id, message_id=status_msg.message_id)
                    continue
                    
                pyro_msg = await pyro_app.get_messages(chat_id, message_id)
                
                total_vids = await db.movies.count_documents({})
                serial_no = total_vids + 1
                
                viral_titles = ["New Viral Trending Clip", "Leaked Private Video", "Desi Viral Collection", "Exclusive Private Clip", "Hot Leaked Collection", "Bhabhi Viral Video Clip"]
                auto_title = f"{random.choice(viral_titles)} #{serial_no:04d}"
                
                video_name = f"temp_video_{serial_no}_{int(time.time())}.mp4"
                thumb_path = os.path.abspath(f"fast_thumb_{serial_no}_{int(time.time())}.jpg")
                
                downloaded_file = await pyro_app.download_media(pyro_msg, file_name=video_name)
                if not downloaded_file:
                    await bot.edit_message_text("❌ ফাইল ডাউনলোড করতে সমস্যা হয়েছে।", chat_id=admin_id, message_id=status_msg.message_id)
                    continue
                    
                await bot.edit_message_text("📸 <b>Generating Thumbnail...</b>", chat_id=admin_id, message_id=status_msg.message_id)
                await generate_fast_thumbnail(downloaded_file, thumb_path)
                    
                db_file_id = None
                db_photo_id = None
                photo_id = None
                
                if DB_CHANNEL_ID and os.path.exists(thumb_path):
                    try:
                        copied_vid = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=chat_id, message_id=message_id)
                        db_file_id = copied_vid.message_id
                        copied_photo = await bot.send_photo(DB_CHANNEL_ID, photo=FSInputFile(thumb_path))
                        db_photo_id = copied_photo.message_id
                        photo_id = copied_photo.photo[-1].file_id
                    except Exception as e:
                        logger.error(f"DB Channel upload error: {e}")
                
                if os.path.exists(thumb_path):
                    photo_msg = await bot.send_photo(admin_id, photo=FSInputFile(thumb_path), caption=f"✅ <b>{auto_title}</b> Successfully Uploaded!")
                    if not photo_id: photo_id = photo_msg.photo[-1].file_id
                
                await db.movies.insert_one({
                    "title": auto_title, "quality": "HD", "photo_id": photo_id, 
                    "file_id": aiogram_file_id, "file_type": file_type,
                    "db_file_id": db_file_id, "db_photo_id": db_photo_id,
                    "clicks": 0, "created_at": datetime.datetime.utcnow()
                })
                clear_app_cache() 
                
                if status_msg:
                    await bot.delete_message(chat_id=admin_id, message_id=status_msg.message_id)

                if photo_id:
                    bot_info = await bot.get_me()
                    kb = [[types.InlineKeyboardButton(text="📥 Download & Watch 🎬", url=f"https://t.me/{bot_info.username}?start=new")]]
                    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
                    caption = f"🔥 <b>নতুন এক্সক্লুসিভ ভাইরাল ভিডিও!</b>\n\n📌 <b>টাইটেল:</b> {auto_title}\n🏷 <b>কোয়ালিটি:</b> HD\n\n👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>"
                    await post_to_channels(photo_id, caption, markup)
                    
            except Exception as e:
                logger.error(f"Video processing error: {e}")
                try:
                    await bot.send_message(chat_id, f"⚠️ Error: {str(e)}")
                except:
                    pass
            finally:
                if downloaded_file and os.path.exists(downloaded_file): 
                    try: os.remove(downloaded_file)
                    except: pass
                if thumb_path and os.path.exists(thumb_path): 
                    try: os.remove(thumb_path)
                    except: pass
                video_queue.task_done()
                is_processing = False
        except Exception as e:
            logger.error(f"Queue worker error: {e}")
            is_processing = False
            await asyncio.sleep(1)

async def load_admins():
    if db is None:
        return
    admin_cache.clear()
    admin_cache.add(OWNER_ID)
    try:
        async for admin in db.admins.find(): 
            admin_cache.add(admin["user_id"])
    except Exception as e:
        logger.error(f"Load admins error: {e}")

async def load_banned_users():
    if db is None:
        return
    banned_cache.clear()
    try:
        async for b_user in db.banned.find(): 
            banned_cache.add(b_user["user_id"])
    except Exception as e:
        logger.error(f"Load banned users error: {e}")

async def init_db():
    if db is None:
        return
    try:
        await db.movies.create_index([("title", "text")])
        await db.movies.create_index("created_at")
        logger.info("✅ Database indexes created!")
    except Exception as e:
        logger.error(f"Init DB error: {e}")

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect Info", headers={"WWW-Authenticate": "Basic"})
    return True

# ==========================================
# 🛑 10 SEC AD SYSTEM + START COMMAND
# ==========================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache: 
        return await message.answer("🚫 <b>আপনাকে ব্যান করা হয়েছে।</b>", parse_mode="HTML")
    await state.clear()
    
    args = message.text.split(" ")
    if len(args) > 1:
        if args[1].startswith("play_"):
            movie_id = args[1].split("_")[1]
            kb = [[types.InlineKeyboardButton(text="⏳ অপেক্ষা করুন... (১০ সেকেন্ড)", callback_data="noop_ad")]]
            waiting_msg = await message.answer(
                "⏳ <b>ডাউনলোড লিংক তৈরি হচ্ছে...</b>\n\n⚠️ দয়া করে <b>১০ সেকেন্ড</b> পর নিচের বাটনে ক্লিক করুন।\nআগে ক্লিক করলে ফাইল পাবেন না!", 
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=kb), 
                parse_mode="HTML"
            )
            await asyncio.sleep(10)
            
            new_kb = [[types.InlineKeyboardButton(text="📥 এখন ডাউনলোড করুন", callback_data=f"get_file_{movie_id}")]]
            try:
                await waiting_msg.edit_text(
                    "✅ <b>সময় শেষ!</b> এখন নিচের বাটনে ক্লিক করে ফাইল নিন।", 
                    reply_markup=types.InlineKeyboardMarkup(inline_keyboard=new_kb), 
                    parse_mode="HTML"
                )
            except Exception: 
                pass
            return

    now = datetime.datetime.utcnow()
    user = None
    if db:
        user = await db.users.find_one({"user_id": uid})
    
    if not user and db:
        await db.users.insert_one({"user_id": uid, "first_name": message.from_user.first_name, "joined_at": now, "last_active": now})
    elif db:
        await db.users.update_one({"user_id": uid}, {"$set": {"last_active": now}})
    
    kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    if uid in admin_cache:
        text = "👋 <b>হ্যালো অ্যাডমিন!</b>\n\n⚙️ অটো আপলোড: <code>/autoupload on/off</code>\n🔸 মুভি ডিলিট: <code>/delmovie নাম</code>\n🔸 স্ট্যাটাস: <code>/stats</code>\n🔸 ব্রডকাস্ট: <code>/cast</code>"
    else: 
        text = f"👋 <b>Welcome to BD Viral Box {message.from_user.first_name}!</b>\n\nClick the button below to browse movies."
        
    await message.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)

@dp.callback_query(F.data == "noop_ad")
async def noop_ad_cb(c: types.CallbackQuery):
    await c.answer("⚠️ দয়া করে ১০ সেকেন্ড অপেক্ষা করুন! এড দেখতে হবে।", show_alert=True)

@dp.callback_query(F.data.startswith("get_file_"))
async def get_file_cb(c: types.CallbackQuery):
    movie_id = c.data.split("_")[2]
    try:
        if not db:
            return await c.answer("Database error!", show_alert=True)
            
        movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
        if not movie: 
            return await c.answer("ফাইল পাওয়া যায়নি!", show_alert=True)
            
        file_id = movie.get("file_id")
        file_type = movie.get("file_type", "video")
        caption = f"🎬 <b>{movie.get('title', '')}</b>\n🏷 Quality: {movie.get('quality', 'HD')}\n\n🔗 Powered By: @BDViralBoxProBot"
        
        if file_type == "video":
            await c.message.answer_video(video=file_id, caption=caption, parse_mode="HTML")
        else:
            await c.message.answer_document(document=file_id, caption=caption, parse_mode="HTML")
            
        await c.message.delete()
    except Exception as e:
        logger.error(f"Get file error: {e}")
        await c.answer(f"এরর: {str(e)}", show_alert=True)

# ==========================================
# ADMIN COMMANDS
# ==========================================
@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: 
        return
    if not db:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
        
    uc = await db.users.count_documents({})
    mc = await db.movies.count_documents({})
    await m.answer(f"📊 <b>BD Viral Box স্ট্যাটাস:</b>\n\n👥 মোট ইউজার: <code>{uc}</code>\n🎬 মোট ফাইল: <code>{mc}</code>", parse_mode="HTML")

@dp.message(Command("autoupload"))
async def toggle_auto_upload(m: types.Message):
    if m.from_user.id not in admin_cache: 
        return
    if not db:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
        
    try:
        parts = m.text.split(" ")
        if len(parts) < 2:
            return await m.answer("❌ Usage: /autoupload on|off", parse_mode="HTML")
            
        state = parts[1].lower()
        if state not in ["on", "off"]:
            return await m.answer("❌ Usage: /autoupload on|off", parse_mode="HTML")
            
        await db.settings.update_one({"id": "auto_upload_mode"}, {"$set": {"status": state == "on"}}, upsert=True)
        await m.answer(f"✅ Auto Upload {'চালু' if state=='on' else 'বন্ধ'}।", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Auto upload toggle error: {e}")
        await m.answer("❌ Error occurred!", parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: 
        return
    if not db:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
        
    try:
        parts = m.text.split(" ", 1)
        if len(parts) < 2:
            return await m.answer("❌ Usage: /delmovie movie_name", parse_mode="HTML")
            
        title = parts[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0:
            clear_app_cache()
            await m.answer(f"✅ {result.deleted_count} টি ফাইল ডিলিট হয়েছে!", parse_mode="HTML")
        else:
            await m.answer("❕ কোনো ফাইল পাওয়া যায়নি!", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Delete movie error: {e}")
        await m.answer("❌ Error occurred!", parse_mode="HTML")

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: 
        return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 ব্রডকাস্ট করতে চান এমন মেসেজটি পাঠান।")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    await state.clear()
    if not db:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
        
    await m.answer("⏳ ব্রডকাস্ট শুরু হয়েছে...")
    kb = [[types.InlineKeyboardButton(text="🎬 ওপেন BD Viral Box", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    success = 0
    failed = 0
    
    async for u in db.users.find():
        try:
            await m.copy_to(chat_id=u['user_id'], reply_markup=markup)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            failed += 1
        except Exception:
            failed += 1
            
    await m.answer(f"✅ সম্পন্ন!\n✅ সফল: <b>{success}</b>\n❌ ব্যর্থ: <b>{failed}</b>", parse_mode="HTML")

# ==========================================
# 🛑 MANUAL UPLOAD
# ==========================================
@dp.message(F.content_type.in_({'video', 'document'}))
async def receive_movie_file(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: 
        return
    if not db:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
        
    config = await db.settings.find_one({"id": "auto_upload_mode"})
    is_auto = config.get("status", False) if config else False
    
    if is_auto:
        aiogram_fid = m.video.file_id if m.video else m.document.file_id
        file_type = "video" if m.video else "document"
        if video_queue:
            await video_queue.put((m.chat.id, m.message_id, aiogram_fid, file_type))
            await m.answer(f"✅ ভিডিও অটো-প্রসেস কিউতে যুক্ত হয়েছে!", parse_mode="HTML")
        else:
            await m.answer("❌ Queue not ready!", parse_mode="HTML")
    else:
        fid = m.video.file_id if m.video else m.document.file_id
        ftype = "video" if m.video else "document"
        status_msg = await m.answer("⏳ <b>অটো থাম্বনেইল তৈরি হচ্ছে...</b>", parse_mode="HTML")
        
        downloaded_file = None
        thumb_path = f"fast_thumb_{int(time.time())}.jpg"
        photo_id = None
        db_file_id = None
        db_photo_id = None

        try:
            # ✅ FIX 6: Check pyro_app before using
            if pyro_app is None:
                raise Exception("Pyrogram not initialized")
                
            pyro_msg = await pyro_app.get_messages(m.chat.id, m.message_id)
            downloaded_file = await pyro_app.download_media(pyro_msg, file_name=f"temp_manual_{int(time.time())}.mp4")
            
            if downloaded_file:
                await generate_fast_thumbnail(downloaded_file, thumb_path)
            
            if DB_CHANNEL_ID and os.path.exists(thumb_path):
                try:
                    copied = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=m.chat.id, message_id=m.message_id)
                    db_file_id = copied.message_id
                    copied_photo = await bot.send_photo(DB_CHANNEL_ID, photo=FSInputFile(thumb_path))
                    db_photo_id = copied_photo.message_id
                    photo_id = copied_photo.photo[-1].file_id
                except Exception as e:
                    logger.error(f"Manual upload DB channel error: {e}")
            
            if os.path.exists(thumb_path) and not photo_id:
                sent_photo = await m.answer_photo(photo=FSInputFile(thumb_path))
                photo_id = sent_photo.photo[-1].file_id
                
            await m.answer("✅ থাম্বনেইল রেডি! এবার মুভির <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
            await state.update_data(file_id=fid, file_type=ftype, db_file_id=db_file_id, photo_id=photo_id, db_photo_id=db_photo_id)
            await state.set_state(AdminStates.waiting_for_title)
            await bot.delete_message(m.chat.id, status_msg.message_id)
            
        except Exception as e:
            logger.error(f"Manual upload error: {e}")
            await m.answer(f"✅ ফাইল পেয়েছি! এবার মুভির <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
            await state.update_data(file_id=fid, file_type=ftype, db_file_id=None, photo_id=None, db_photo_id=None)
            await state.set_state(AdminStates.waiting_for_title)
            try:
                await bot.delete_message(m.chat.id, status_msg.message_id)
            except:
                pass
        finally:
            if downloaded_file and os.path.exists(downloaded_file): 
                try: os.remove(downloaded_file)
                except: pass
            if os.path.exists(thumb_path): 
                try: os.remove(thumb_path)
                except: pass

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    if not m.text.strip():
        return await m.answer("❌ টাইটেল খালি থাকতে পারবে না!", parse_mode="HTML")
        
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ নাম সেভ হয়েছে! এবার ফাইলের <b>কোয়ালিটি</b> দিন (যেমন: 720p, 1080p).", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    if not m.text.strip():
        return await m.answer("❌ কোয়ালিটি খালি থাকতে পারবে না!", parse_mode="HTML")
        
    await state.update_data(quality=m.text.strip())
    data = await state.get_data()
    await state.clear()
    
    if not db:
        return await m.answer("❌ Database not connected!", parse_mode="HTML")
        
    title = data["title"]
    photo_id = data.get("photo_id")
    quality = data["quality"]
    
    await db.movies.insert_one({
        "title": title, "quality": quality, "photo_id": photo_id, 
        "file_id": data["file_id"], "file_type": data["file_type"],
        "db_file_id": data.get("db_file_id"), "db_photo_id": data.get("db_photo_id"),
        "clicks": 0, "created_at": datetime.datetime.utcnow()
    })
    clear_app_cache() 
    await m.answer(f"🎉 <b>{title} [{quality}]</b> BD Viral Box এ যুক্ত হয়েছে!", parse_mode="HTML")

    if photo_id:
        try:
            bot_info = await bot.get_me()
            kb = [ [types.InlineKeyboardButton(text="📥 Download & Watch 🎬", url=f"https://t.me/{bot_info.username}?start=new")] ]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            caption = f"🔥 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n📌 <b>টাইটেল:</b> {title}\n🏷 <b>কোয়ালিটি:</b> {quality}"
            await post_to_channels(photo_id, caption, markup)
        except Exception as e:
            logger.error(f"Post to channel error: {e}")

@dp.callback_query(F.data.startswith("reply_"))
async def process_reply_cb(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: 
        return
    user_id = int(c.data.split("_")[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(target_uid=user_id)
    await c.message.reply("✍️ ইউজারকে রিপ্লাই লিখে পাঠান:", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_reply)
async def send_reply(m: types.Message, state: FSMContext):
    data = await state.get_data()
    target_uid = data.get("target_uid")
    await state.clear()
    try:
        if m.text: 
            await bot.send_message(target_uid, f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.text}", parse_mode="HTML")
        await m.answer("✅ রিপ্লাই পাঠানো হয়েছে!")
    except Exception as e:
        logger.error(f"Send reply error: {e}")
        await m.answer("⚠️ রিপ্লাই পাঠানো যায়নি!")

# ==========================================
# WEB APP & APIS
# ==========================================
@app.get("/api/thumb/{file_id}")
async def get_thumbnail(file_id: str):
    try:
        file = await bot.get_file(file_id)
        if not file.file_path: 
            raise HTTPException(status_code=404, detail="File not found")
        return RedirectResponse(url=f"https://api.telegram.org/file/bot{TOKEN}/{file.file_path}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Thumbnail error: {e}")
        raise HTTPException(status_code=404, detail="Thumbnail not found")

@app.get("/api/movies/search")
async def search_movies(q: str = "", page: int = 1):
    if not db:
        raise HTTPException(status_code=503, detail="Database not connected")
        
    limit = 15
    skip = (page - 1) * limit
    query = {"title": {"$regex": q, "$options": "i"}} if q else {}
    
    try:
        movies_cursor = db.movies.find(query).sort("created_at", -1).skip(skip).limit(limit)
        movies = await movies_cursor.to_list(length=limit)
        total = await db.movies.count_documents(query)
        
        result = []
        for m in movies:
            result.append({
                "id": str(m["_id"]),
                "title": m.get("title", "Unknown"),
                "quality": m.get("quality", "HD"),
                "photo_id": m.get("photo_id", ""),
                "clicks": m.get("clicks", 0)
            })
            
        return {"results": result, "total": total, "pages": (total + limit - 1) // limit}
    except Exception as e:
        logger.error(f"Search movies error: {e}")
        raise HTTPException(status_code=500, detail="Search failed")

@app.get("/")
async def web_app():
    return HTMLResponse(content="""
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>BD Viral Box</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; color: #fff; padding-bottom: 80px; }
        .header { background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); padding: 15px; position: sticky; top: 0; z-index: 100; box-shadow: 0 4px 15px rgba(0,0,0,0.5); display: flex; justify-content: space-between; align-items: center; }
        .header h1 { font-size: 18px; }
        .home-btn { background: #3b82f6; color: white; border: none; padding: 8px 15px; border-radius: 8px; cursor: pointer; font-weight: bold; }
        .search-box { padding: 15px; }
        input[type=text] { width: 100%; padding: 12px; border-radius: 10px; border: 1px solid #334155; background: #1e293b; color: #fff; font-size: 15px; outline: none; }
        input[type=text]:focus { border-color: #3b82f6; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; padding: 0 15px; }
        .card { background: #1e293b; border-radius: 12px; overflow: hidden; cursor: pointer; transition: transform 0.2s; }
        .card:active { transform: scale(0.95); }
        .card img { width: 100%; height: 200px; object-fit: cover; }
        .info { padding: 10px; }
        .title { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
        .quality { font-size: 11px; color: #3b82f6; font-weight: bold; }
        .pagination { display: flex; justify-content: center; align-items: center; gap: 15px; padding: 20px; position: fixed; bottom: 0; left: 0; right: 0; background: #1e293b; border-top: 1px solid #334155; z-index: 100; }
        .page-btn { background: #3b82f6; color: white; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; }
        .page-btn:disabled { background: #334155; color: #64748b; cursor: not-allowed; }
        #pageInfo { font-weight: bold; color: #94a3b8; }
        .loading { text-align: center; padding: 20px; color: #94a3b8; }
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
    <div class="grid" id="movieGrid"></div>
    
    <div class="pagination">
        <button class="page-btn" id="prevBtn" onclick="changePage(-1)">◀ Prev</button>
        <span id="pageInfo">1 / 1</span>
        <button class="page-btn" id="nextBtn" onclick="changePage(1)">Next ▶</button>
    </div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();
        tg.setHeaderColor('#0f172a');
        tg.setBackgroundColor('#0f172a');

        let currentPage = 1;
        let totalPages = 1;
        let searchTimeout;
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
            let newPage = currentPage + dir;
            if (newPage >= 1 && newPage <= totalPages) {
                currentPage = newPage;
                loadMovies();
            }
        }

        function loadMovies() {
            if (isLoading) return;
            isLoading = true;
            
            const q = document.getElementById('searchInput').value;
            const grid = document.getElementById('movieGrid');
            grid.innerHTML = '<div class="loading">Loading...</div>';
            
            fetch(`/api/movies/search?q=${encodeURIComponent(q)}&page=${currentPage}`)
                .then(res => res.json())
                .then(data => {
                    grid.innerHTML = '';
                    totalPages = data.pages || 1;
                    
                    document.getElementById('pageInfo').innerText = `${currentPage} / ${totalPages}`;
                    document.getElementById('prevBtn').disabled = currentPage === 1;
                    document.getElementById('nextBtn').disabled = currentPage === totalPages;

                    if (data.results.length === 0) {
                        grid.innerHTML = '<div style="text-align:center;width:100%;padding:40px;color:#94a3b8;">কোনো মুভি পাওয়া যায়নি।</div>';
                        isLoading = false;
                        return;
                    }

                    data.results.forEach(m => {
                        const card = document.createElement('div');
                        card.className = 'card';
                        card.onclick = () => openMovie(m.id);
                        card.innerHTML = `
                            <img src="/api/thumb/${m.photo_id}" onerror="this.src='https://via.placeholder.com/150x200/1e293b/ffffff?text=No+Img'" loading="lazy">
                            <div class="info">
                                <div class="title">${m.title}</div>
                                <div class="quality">${m.quality}</div>
                            </div>
                        `;
                        grid.appendChild(card);
                    });
                    isLoading = false;
                })
                .catch(err => {
                    console.error('Load error:', err);
                    grid.innerHTML = '<div style="text-align:center;width:100%;padding:40px;color:#ef4444;">Error loading movies!</div>';
                    isLoading = false;
                });
        }

        function openMovie(id) {
            tg.openTelegramLink(`https://t.me/BDViralBoxProBot?start=play_${id}`);
        }

        loadMovies();
    </script>
</body>
</html>""")

# ==========================================
# STARTUP & SHUTDOWN EVENT
# ==========================================
@app.on_event("startup")
async def startup_event():
    global db, video_queue, pyro_app
    
    # ✅ FIX 7: DATABASE CONNECTED INSIDE STARTUP
    try:
        db_client = AsyncIOMotorClient(MONGO_URL)
        db = db_client['movie_database']
        logger.info("✅ Database connected successfully!")
    except Exception as e:
        logger.error(f"❌ Database connection failed: {e}")
        db = None
    
    # ✅ FIX 8: PYROGRAM INITIALIZED INSIDE STARTUP (FIXES EVENT LOOP ERROR!)
    try:
        from pyrogram import Client as PyroClient
        
        if SESSION_STRING:
            pyro_app = PyroClient(
                "user_session", 
                api_id=API_ID, 
                api_hash=API_HASH, 
                session_string=SESSION_STRING, 
                in_memory=True, 
                no_updates=True
            )
        else:
            pyro_app = PyroClient(
                "bot_session", 
                api_id=API_ID, 
                api_hash=API_HASH, 
                bot_token=TOKEN, 
                in_memory=True, 
                no_updates=True
            )
        
        logger.info("✅ Pyrogram client initialized successfully!")
    except Exception as e:
        logger.error(f"❌ Pyrogram initialization failed: {e}")
        logger.warning("⚠️ Video download features will be disabled!")
        pyro_app = None
    
    video_queue = asyncio.Queue()
    
    if db:
        await init_db()
        await load_admins()
        await load_banned_users()
        await load_keyword_replies()
    
    # Start polling and queue worker
    asyncio.create_task(dp.start_polling(bot))
    asyncio.create_task(video_queue_worker())
    
    logger.info("🚀 Bot started successfully!")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Shutting down...")
    try:
        await dp.stop_polling()
    except:
        pass
    try:
        await bot.session.close()
    except:
        pass
    # ✅ FIX 9: Clean up pyro_app
    if pyro_app:
        try:
            await pyro_app.stop()
        except:
            pass
    logger.info("✅ Shutdown complete!")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
