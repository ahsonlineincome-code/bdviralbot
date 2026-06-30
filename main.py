import os
import asyncio
import datetime
import uvicorn
import time
import aiohttp
import hmac
import hashlib
import urllib.parse
import secrets
import json
import html
import logging
import glob
import random
import httpx 
from PIL import Image, ImageFilter

from cachetools import TTLCache
import copy

try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from fastapi import FastAPI, Body, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramRetryAfter

from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pydantic import BaseModel
from pyrogram import Client as PyroClient

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
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123") 
# 🛑 NAME CHANGED: BD Viral Link -> BD Viral Box
BOT_USERNAME = "BDViralBoxProBot"

TUTORIAL_LINK = "https://t.me/HowtoDowlnoad/41"
REQUEST_LINK = "https://t.me/+NEMfLNawn2hkNjg9"

_db_ch = os.getenv("DB_CHANNEL_ID", "")
DB_CHANNEL_ID = int(_db_ch) if _db_ch.lstrip('-').isdigit() else None

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

app = FastAPI()
security = HTTPBasic()

if SESSION_STRING:
    pyro_app = PyroClient("user_session", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, in_memory=True, no_updates=True)
else:
    pyro_app = PyroClient("bot_session", api_id=API_ID, api_hash=API_HASH, bot_token=TOKEN, in_memory=True, no_updates=True)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

client = AsyncIOMotorClient(MONGO_URL)
db = client['movie_database']

admin_cache = set([OWNER_ID]) 
banned_cache = set() 

trending_cache = TTLCache(maxsize=10, ttl=300)
list_cache = TTLCache(maxsize=100, ttl=300)
auto_reply_cache = TTLCache(maxsize=1000, ttl=10) 

keyword_replies_cache = {}

async def load_keyword_replies():
    keyword_replies_cache.clear()
    async for kw in db.keyword_replies.find():
        keyword_replies_cache[kw["keyword"]] = kw["reply_message"]

def clear_app_cache():
    trending_cache.clear()
    list_cache.clear()

video_queue = None
is_processing = False

# ==========================================
# 🛑 Pydantic Models
# ==========================================
class UserManageModel(BaseModel):
    user_id: int
    action: str
    value: int = 0

class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_series_search = State()
    waiting_for_episode_quality = State()

def cleanup_temp_files():
    patterns = ["temp_video_*.mp4", "collage_*.jpg", "temp_frame_*.jpg", "temp_in_*.jpg", "temp_out_*.jpg", "fast_thumb_*.jpg"]
    count = 0
    for p in patterns:
        for f in glob.glob(p):
            try:
                os.remove(f)
                count += 1
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
    if count > 0:
        logger.info(f"Cleaned up {count} leftover temp files.")

# 🛑 FAST THUMBNAIL GENERATOR (No slow collage, directly from video)
async def generate_fast_thumbnail(video_path, output_path):
    try:
        # -ss 10 means seek to 10th second directly (very fast, doesn't decode whole video)
        # scale=640:360 forces 16:9 aspect ratio quickly
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

async def video_queue_worker():
    global is_processing, video_queue
    while True:
        chat_id, message_id, aiogram_file_id, file_type = await video_queue.get()
        is_processing = True
        downloaded_file = None
        thumb_path = None
        try:
            admin_id = chat_id
            status_msg = await bot.send_message(admin_id, "⏳ <b>Processing Video...</b> (Downloading)")
            pyro_msg = await pyro_app.get_messages(chat_id, message_id)
            
            total_vids = await db.movies.count_documents({})
            serial_no = total_vids + 1
            
            viral_titles = [
                "New Viral Trending Clip",
                "Leaked Private Video",
                "Desi Viral Collection",
                "Exclusive Private Clip",
                "Hot Leaked Collection",
                "New Secret Trending Video",
                "Bhabhi Viral Video Clip",
                "MMS Leaked Video Clip",
                "Hot Garam Masala Video"
            ]
            random_prefix = random.choice(viral_titles)
            auto_title = f"{random_prefix} #{serial_no:04d}"
            
            video_name = f"temp_video_{serial_no}_{int(time.time())}.mp4"
            thumb_path = os.path.abspath(f"fast_thumb_{serial_no}_{int(time.time())}.jpg")
            
            downloaded_file = await pyro_app.download_media(pyro_msg, file_name=video_name)
            if not downloaded_file:
                await bot.edit_message_text("❌ ফাইল ডাউনলোড করতে সমস্যা হয়েছে।", chat_id=admin_id, message_id=status_msg.message_id)
                continue
                
            await bot.edit_message_text("📸 <b>Generating Thumbnail...</b>", chat_id=admin_id, message_id=status_msg.message_id, parse_mode="HTML")
            success = await generate_fast_thumbnail(downloaded_file, thumb_path)
            
            if not success:
                await bot.edit_message_text("❌ <b>থাম্বনেইল তৈরি করতে সমস্যা হয়েছে!</b>", chat_id=admin_id, message_id=status_msg.message_id, parse_mode="HTML")
                continue
                
            db_file_id = None
            db_photo_id = None
            photo_id = None
            
            if DB_CHANNEL_ID and os.path.exists(thumb_path):
                try:
                    copied_vid = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=chat_id, message_id=message_id)
                    db_file_id = copied_vid.message_id
                    
                    copied_photo = await bot.send_photo(DB_CHANNEL_ID, FSInputFile(thumb_path))
                    db_photo_id = copied_photo.message_id
                    photo_id = copied_photo.photo[-1].file_id
                except Exception: pass
            
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
            await bot.delete_message(chat_id=admin_id, message_id=status_msg.message_id)

            if CHANNEL_ID and photo_id:
                try:
                    bot_info = await bot.get_me()
                    kb = [
                        [types.InlineKeyboardButton(text="📥 Download & Watch 🎬", url=f"https://t.me/{bot_info.username}?start=new")],
                        [types.InlineKeyboardButton(text="কিভাবে ডাউনলোড করবেন ❓", url=TUTORIAL_LINK)],
                        [types.InlineKeyboardButton(text="♻️ MOVIE REQUEST ♻️", url=REQUEST_LINK)]
                    ]
                    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
                    caption = (f"🔥 <b>নতুন এক্সক্লুসিভ ভাইরাল ভিডিও!</b>\n\n📌 <b>টাইটেল:</b> {auto_title}\n🏷 <b>কোয়ালিটি:</b> HD (Original)\n\n👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>")
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
                except Exception: pass
        except Exception as e:
            await bot.send_message(chat_id, f"⚠️ Error: {str(e)}")
        finally:
            if downloaded_file and os.path.exists(downloaded_file): os.remove(downloaded_file)
            if thumb_path and os.path.exists(thumb_path): os.remove(thumb_path)
            video_queue.task_done()
            is_processing = False

async def load_admins():
    admin_cache.clear()
    admin_cache.add(OWNER_ID)
    async for admin in db.admins.find(): admin_cache.add(admin["user_id"])

async def load_banned_users():
    banned_cache.clear()
    async for b_user in db.banned.find(): banned_cache.add(b_user["user_id"])

async def init_db():
    await db.movies.create_index([("title", "text")])
    await db.movies.create_index("created_at")
    await db.auto_delete.create_index("delete_at")
    await db.ads.create_index("expires_at")
    await db.movie_views.create_index([("title", 1), ("viewed_at", -1)])
    await db.movie_views.create_index("viewed_at", expireAfterSeconds=2592000)

def validate_tg_data(init_data: str) -> bool:
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        hash_val = parsed_data.pop('hash', None)
        auth_date = int(parsed_data.get('auth_date', 0))
        if not hash_val or time.time() - auth_date > 86400: return False
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == hash_val
    except Exception: return False

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect Info", headers={"WWW-Authenticate": "Basic"})
    return True

async def auto_delete_worker():
    while True:
        try:
            now = datetime.datetime.utcnow()
            expired_msgs = db.auto_delete.find({"delete_at": {"$lte": now}})
            async for msg in expired_msgs:
                try: 
                    await bot.delete_message(chat_id=msg["chat_id"], message_id=msg["message_id"])
                except Exception: pass
                await db.auto_delete.delete_one({"_id": msg["_id"]})
            await db.ads.delete_many({"expires_at": {"$lte": now}})
        except Exception: pass
        await asyncio.sleep(60)

def format_views(n):
    if n >= 1000000: return f"{n/1000000:.1f}M".replace(".0M", "M")
    if n >= 1000: return f"{n/1000:.1f}K".replace(".0K", "K")
    return str(n)

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache: return await message.answer("🚫 <b>আপনাকে ব্যান করা হয়েছে।</b>", parse_mode="HTML")
        
    await state.clear()
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": uid})
    
    if not user:
        await db.users.insert_one({
            "user_id": uid, "first_name": message.from_user.first_name, "joined_at": now, "last_active": now
        })
    else:
        await db.users.update_one({"user_id": uid}, {"$set": {"last_active": now}})
    
    kb = [[types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    if uid in admin_cache:
        text = (
            "👋 <b>হ্যালো অ্যাডমিন!</b>\n\n"
            "⚙️ <b>কমান্ড:</b>\n"
            "🔸 অটো আপলোড: <code>/autoupload on/off</code>\n"
            "🔸 অ্যাডমিন প্যানেল: <code>/addadmin ID</code> | <code>/deladmin ID</code> | <code>/adminlist</code>\n"
            "🔸 ডাইরেক্ট লিংক: <code>/addlink লিংক</code> | <code>/dellink লিংক</code> | <code>/seelinks</code>\n"
            "🔸 সাপোর্ট লিংক: <code>/setsupport লিংক</code>\n"
            "🔸 প্রোটেকশন: <code>/protect on/off</code> | অটো-ডিলিট: <code>/settime [মিনিট]</code>\n"
            "🔸 অ্যাড টাইম: <code>/setadtime [সেকেন্ড]</code>\n" 
            "🔸 স্ট্যাটাস: <code>/stats</code> | ব্রডকাস্ট: <code>/cast</code>\n"
            "🔸 মুভি ডিলিট: <code>/delmovie মুভির নাম</code> | <code>/delallmovies</code>\n"
            "🔸 ব্যান: <code>/ban ID</code> | আনব্যান: <code>/unban ID</code>\n\n"
            f"🌐 <b>ওয়েব অ্যাডমিন প্যানেল:</b> <a href='{APP_URL}/admin'>এখানে ক্লিক করুন</a>\n"
            "<i>লগিন: admin / admin123</i>\n\n"
            "📥 <b>মুভি অ্যাড করতে প্রথমে ভিডিও বা ডকুমেন্ট ফাইল পাঠান।</b>"
        )
    else: text = f"👋 <b>Welcome to BD Viral Box {message.from_user.first_name}!</b>\n\nClick the button below to browse movies."
        
    await message.answer(text, reply_markup=markup, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("setadtime"))
async def set_ad_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        secs = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "ad_time"}, {"$set": {"seconds": secs}}, upsert=True)
        await m.answer(f"✅ অ্যাড ওয়েটিং টাইম <b>{secs} সেকেন্ড</b> সেট করা হয়েছে।", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/setadtime ১৫</code>", parse_mode="HTML")

@dp.message(Command("autoupload"))
async def toggle_auto_upload(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        await db.settings.update_one({"id": "auto_upload_mode"}, {"$set": {"status": state == "on"}}, upsert=True)
        await m.answer(f"✅ Auto Upload {'চালু' if state=='on' else 'বন্ধ'} করা হয়েছে।")
    except: pass

@dp.message(Command("addlink"))
async def add_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer(f"✅ লিংক অ্যাড করা হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("dellink"))
async def del_direct_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$pull": {"links": url}})
        await m.answer(f"❌ লিংকটি ডিলিট করা হয়েছে:\n<code>{url}</code>", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("seelinks"))
async def see_direct_links(m: types.Message):
    if m.from_user.id not in admin_cache: return
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    links = dl_cfg.get("links", []) if dl_cfg else []
    if not links: return await m.answer("⚠️ কোনো ডাইরেক্ট লিংক নেই।")
    text = "🔗 <b>বর্তমান ডাইরেক্ট লিংক সমূহ:</b>\n\n"
    for i, link in enumerate(links, 1): text += f"{i}. <code>{link}</code>\n"
    await m.answer(text, parse_mode="HTML", disable_web_page_preview=True)

@dp.message(Command("setsupport"))
async def set_support_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ")[1]
        await db.settings.update_one({"id": "link_support"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ সাপোর্ট লিংক আপডেট করা হয়েছে।")
    except Exception: pass

@dp.message(Command("protect"))
async def protect_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        state = m.text.split(" ")[1].lower()
        await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": state == "on"}}, upsert=True)
        await m.answer(f"✅ ফরোয়ার্ড প্রোটেকশন {'চালু' if state=='on' else 'বন্ধ'} করা হয়েছে।")
    except Exception: pass

@dp.message(Command("settime"))
async def set_del_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        mins = int(m.text.split(" ")[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": mins}}, upsert=True)
        await m.answer(f"✅ অটো-ডিলিট টাইম {mins} মিনিট সেট করা হয়েছে।")
    except Exception: pass

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0:
            clear_app_cache() 
            await m.answer(f"✅ '<b>{title}</b>' নামের {result.deleted_count} টি ফাইল ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ এই নামের কোনো মুভি পাওয়া যায়নি।")
    except Exception: await m.answer("⚠️ সঠিক নিয়ম: <code>/delmovie মুভির নাম</code>", parse_mode="HTML")

@dp.message(Command("delallmovies"))
async def del_all_movies_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    result = await db.movies.delete_many({})
    clear_app_cache()
    await m.answer(f"🗑 <b>সতর্কতা:</b> ডাটাবেস থেকে সর্বমোট <b>{result.deleted_count}</b> টি মুভি ডিলিট করা হয়েছে!", parse_mode="HTML")

@dp.message(Command("stats"))
async def stats_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    uc = await db.users.count_documents({})
    mc = await db.movies.count_documents({})
    now = datetime.datetime.utcnow()
    today_start = datetime.datetime(now.year, now.month, now.day)
    new_users_today = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    
    text = (f"📊 <b>BD Viral Box স্ট্যাটাস:</b>\n\n👥 মোট ইউজার: <code>{uc}</code>\n🟢 আজকের নতুন ইউজার: <code>{new_users_today}</code>\n"
            f"🎬 মোট ফাইল আপলোড: <code>{mc}</code>")
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("ban"))
async def ban_user_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        if target_uid in admin_cache: return await m.answer("⚠️ অ্যাডমিনকে ব্যান করা যাবে না!")
        await db.banned.update_one({"user_id": target_uid}, {"$set": {"user_id": target_uid}}, upsert=True)
        banned_cache.add(target_uid)
        await m.answer(f"🚫 ইউজার <code>{target_uid}</code> কে ব্যান করা হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("unban"))
async def unban_user_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": target_uid})
        banned_cache.discard(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> আনব্যান হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("addadmin"))
async def add_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র মেইন Owner অ্যাডমিন অ্যাড করতে পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        await db.admins.update_one({"user_id": target_uid}, {"$set": {"user_id": target_uid}}, upsert=True)
        admin_cache.add(target_uid)
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে অ্যাডমিন বানানো হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("deladmin"))
async def del_admin_cmd(m: types.Message):
    if m.from_user.id != OWNER_ID: return await m.answer("⚠️ শুধুমাত্র Owner অ্যাডমিন রিমুভ করতে পারবে!")
    try:
        target_uid = int(m.text.split()[1])
        if target_uid == OWNER_ID: return await m.answer("⚠️ Main Owner কে ডিলিট করা সম্ভব নয়!")
        await db.admins.delete_one({"user_id": target_uid})
        admin_cache.discard(target_uid)
        await m.answer(f"❌ ইউজার <code>{target_uid}</code> রিমুভ করা হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("adminlist"))
async def list_admin_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    text = f"👑 <b>Owner:</b> <code>{OWNER_ID}</code>\n\n👮‍♂️ <b>Admins:</b>\n"
    async for a in db.admins.find(): text += f"▪️ <code>{a['user_id']}</code>\n"
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 যে মেসেজটি ব্রডকাস্ট করতে চান সেটি পাঠান।\nবাতিল করতে /start দিন।")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("⏳ ব্রডকাস্ট শুরু হয়েছে...")
    kb = [[types.InlineKeyboardButton(text="🎬 ওপেন BD Viral Box", web_app=types.WebAppInfo(url=APP_URL))]]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    success = 0
    async for u in db.users.find():
        try:
            await m.copy_to(chat_id=u['user_id'], reply_markup=markup)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await m.copy_to(chat_id=u['user_id'], reply_markup=markup)
                success += 1
            except Exception: pass
        except Exception: pass
    await m.answer(f"✅ সম্পন্ন! সর্বমোট <b>{success}</b> জনকে মেসেজ পাঠানো হয়েছে।", parse_mode="HTML")

@dp.message(Command("addreply"))
async def add_keyword_reply(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split(" ", 1)[1]
        keyword, reply_msg = [x.strip() for x in args.split("|", 1)]
        keyword = keyword.lower()
        await db.keyword_replies.update_one({"keyword": keyword}, {"$set": {"keyword": keyword, "reply_message": reply_msg}}, upsert=True)
        await load_keyword_replies()
        await m.answer(f"✅ <b>{keyword}</b> এর জন্য ম্যানুয়াল রিপ্লাই সেট হয়েছে!", parse_mode="HTML")
    except Exception:
        await m.answer("⚠️ সঠিক নিয়ম: <code>/addreply কিওয়ার্ড | আপনার রিপ্লাই</code>", parse_mode="HTML")

@dp.message(Command("delreply"))
async def del_keyword_reply(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        keyword = m.text.split(" ", 1)[1].strip().lower()
        res = await db.keyword_replies.delete_one({"keyword": keyword})
        if res.deleted_count > 0:
            await load_keyword_replies()
            await m.answer(f"✅ কিওয়ার্ড <b>{keyword}</b> ডিলিট করা হয়েছে!", parse_mode="HTML")
        else:
            await m.answer("⚠️ এই কিওয়ার্ড পাওয়া যায়নি।")
    except Exception:
        await m.answer("⚠️ সঠিক নিয়ম: <code>/delreply কিওয়ার্ড</code>", parse_mode="HTML")

# ==========================================
# 🛑 SMART AUTO-RESPONDER
# ==========================================
@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache and (m.text is None or not m.text.startswith("/")))
async def forward_to_admin(m: types.Message):
    user_text = m.text.strip() if m.text else ""
    user_text_lower = user_text.lower()
    
    reply_text = ""
    is_manual_reply = False

    if user_text:
        for kw, rep_msg in keyword_replies_cache.items():
            if kw in user_text_lower:
                reply_text = rep_msg
                is_manual_reply = True
                break

    if not is_manual_reply:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই দিন", callback_data=f"reply_{m.from_user.id}")
        markup = builder.as_markup()
        
        all_admins = set([OWNER_ID])
        async for a in db.admins.find(): all_admins.add(a["user_id"])
            
        for admin_id in all_admins:
            try:
                await bot.send_message(
                    admin_id, 
                    f"📩 <b>Message from <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a></b> (<code>{m.from_user.id}</code>):\n\n{m.text or '[Media/File]'}", 
                    parse_mode="HTML",
                    reply_markup=markup
                )
            except Exception: pass
        
        if m.from_user.id not in auto_reply_cache:
            auto_reply_cache[m.from_user.id] = True
            try:
                kb = [[types.InlineKeyboardButton(text="🎬 Watch Now (BD Viral Box)", web_app=types.WebAppInfo(url=APP_URL))]]
                user_markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
                
                if user_text:
                    reply_text = await get_smart_reply(user_text, m.from_user.first_name, db, user_id=m.from_user.id)
                else:
                    reply_text = "হ্যালো! আপনার মেসেজ/ফাইলটি অ্যাডমিনের কাছে পৌঁছে গেছে। প্রয়োজনে অ্যাডমিন আপনাকে রিপ্লাই দেবেন। ধন্যবাদ! ❤️"
                
                await m.reply(reply_text, reply_markup=user_markup, parse_mode="HTML")
            except Exception as e: 
                logger.error(f"Auto-Reply Error: {e}")
    else:
        if m.from_user.id not in auto_reply_cache:
            auto_reply_cache[m.from_user.id] = True
            try:
                kb = [[types.InlineKeyboardButton(text="🎬 Watch Now (BD Viral Box)", web_app=types.WebAppInfo(url=APP_URL))]]
                user_markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
                await m.reply(reply_text, reply_markup=user_markup, parse_mode="HTML")
                await db.messages.insert_one({
                    "user_id": str(m.from_user.id),
                    "text": user_text,
                    "reply": reply_text,
                    "timestamp": datetime.datetime.utcnow()
                })
            except Exception as e:
                logger.error(f"Auto-Reply Error: {e}")

# ==========================================
# 🛑 MANUAL UPLOAD HANDLER (Auto Thumbnail Added)
# ==========================================
@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    config = await db.settings.find_one({"id": "auto_upload_mode"})
    is_auto = config["status"] if config else False
    
    if is_auto:
        aiogram_fid = m.video.file_id if m.video else m.document.file_id
        file_type = "video" if m.video else "document"
        await video_queue.put((m.chat.id, m.message_id, aiogram_fid, file_type))
        await m.answer(f"✅ ভিডিও অটো-প্রসেস কিউতে যুক্ত হয়েছে! সিরিয়াল: <b>{video_queue.qsize()}</b>", parse_mode="HTML")
    else:
        fid = m.video.file_id if m.video else m.document.file_id
        ftype = "video" if m.video else "document"
        
        status_msg = await m.answer("⏳ <b>ফাইল প্রসেস হচ্ছে... (অটো থাম্বনেইল তৈরি করা হচ্ছে)</b>", parse_mode="HTML")
        
        downloaded_file = None
        thumb_path = f"fast_thumb_{int(time.time())}.jpg"
        photo_id = None
        db_photo_id = None
        db_file_id = None

        try:
            # Download file to generate thumbnail
            pyro_msg = await pyro_app.get_messages(m.chat.id, m.message_id)
            downloaded_file = await pyro_app.download_media(pyro_msg, file_name=f"temp_manual_{int(time.time())}.mp4")
            
            if downloaded_file:
                await generate_fast_thumbnail(downloaded_file, thumb_path)
            
            if DB_CHANNEL_ID:
                try:
                    copied = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=m.chat.id, message_id=m.message_id)
                    db_file_id = copied.message_id
                except Exception: pass
                
                if os.path.exists(thumb_path):
                    try:
                        copied_photo = await bot.send_photo(DB_CHANNEL_ID, FSInputFile(thumb_path))
                        db_photo_id = copied_photo.message_id
                        photo_id = copied_photo.photo[-1].file_id
                    except Exception: pass
            
            if os.path.exists(thumb_path) and not photo_id:
                sent_photo = await m.answer_photo(FSInputFile(thumb_path), caption="✅ <b>থাম্বনেইল অটো-তৈরি হয়েছে!</b>\nএবার মুভির <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
                photo_id = sent_photo.photo[-1].file_id
            elif not photo_id:
                await m.answer("✅ ফাইল পেয়েছি! এবার <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
            else:
                await m.answer("✅ <b>থাম্বনেইল অটো-তৈরি হয়েছে!</b>\nএবার মুভির <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
                
            await state.update_data(file_id=fid, file_type=ftype, db_file_id=db_file_id, photo_id=photo_id, db_photo_id=db_photo_id)
            await state.set_state(AdminStates.waiting_for_title)
            await bot.delete_message(m.chat.id, status_msg.message_id)
            
        except Exception as e:
            await m.answer(f"⚠️ থাম্বনেইল তৈরিতে সমস্যা হয়েছে, তবে আপনি এগিয়ে যেতে পারেন। এবার <b>টাইটেল (নাম)</b> লিখে পাঠান।\nError: {str(e)}", parse_mode="HTML")
            await state.update_data(file_id=fid, file_type=ftype, db_file_id=db_file_id, photo_id=None, db_photo_id=None)
            await state.set_state(AdminStates.waiting_for_title)
            await bot.delete_message(m.chat.id, status_msg.message_id)
            
        finally:
            if downloaded_file and os.path.exists(downloaded_file): os.remove(downloaded_file)
            if os.path.exists(thumb_path): os.remove(thumb_path)

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ নাম সেভ হয়েছে! এবার ফাইলের <b>কোয়ালিটি বা এপিসোড নাম্বার</b> দিন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    data = await state.get_data()
    await state.clear()
    
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
    
    await m.answer(f"🎉 <b>{title} [{quality}]</b> BD Viral Box এ যুক্ত করা হয়েছে!", parse_mode="HTML")

    if CHANNEL_ID and photo_id:
        try:
            bot_info = await bot.get_me()
            kb = [
                [types.InlineKeyboardButton(text="📥 Download & Watch 🎬", url=f"https://t.me/{bot_info.username}?start=new")],
                [types.InlineKeyboardButton(text="কিভাবে ডাউনলোড করবেন ❓", url=TUTORIAL_LINK)],
                [types.InlineKeyboardButton(text="♻️ MOVIE REQUEST ♻️", url=REQUEST_LINK)]
            ]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            caption = (f"🔥 <b>নতুন ফাইল যুক্ত হয়েছে!</b>\n\n📌 <b>টাইটেল:</b> {title}\n🏷 <b>কোয়ালিটি:</b> {quality}\n\n👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>")
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
        except Exception: pass

@dp.callback_query(F.data.startswith("reply_"))
async def process_reply_cb(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(target_uid=user_id)
    await c.message.reply("✍️ <b>ইউজারকে কী রিপ্লাই দিতে চান তা লিখে পাঠান:</b>", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_reply)
async def send_reply(m: types.Message, state: FSMContext):
    data = await state.get_data()
    target_uid = data.get("target_uid")
    await state.clear()
    try:
        if m.text: await bot.send_message(target_uid, f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.text}", parse_mode="HTML")
        else: await m.copy_to(target_uid, caption=f"📩 <b>অ্যাডমিন রিপ্লাই:</b>\n\n{m.caption or ''}", parse_mode="HTML")
        await m.answer("✅ ইউজারকে রিপ্লাই পাঠানো হয়েছে!")
    except Exception: await m.answer("⚠️ রিপ্লাই পাঠানো যায়নি!")

# ==========================================
# 🌐 NETWORK GATEWAY & REVERSE PROXY SYSTEM
# ==========================================
FALLBACK_GATEWAYS = [
    "https://workers.cloudflare.com",
    "https://vercel.live",
    "https://pages.dev"
]

@app.get("/gateway/dns-check")
async def check_network_status(request: Request):
    return {
        "status": "online",
        "message": "BD Viral Box Gateway Connection Successful",
        "client_ip": request.client.host if request.client else "Unknown",
        "suggested_gateways": FALLBACK_GATEWAYS
    }

@app.api_route("/gateway/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def reverse_proxy_gateway(request: Request, path: str):
    target_url = f"http://127.0.0.1:8000/{path}" 
    headers = dict(request.headers)
    headers.pop("host", None)
    
    async with httpx.AsyncClient() as client:
        try:
            content = await request.body()
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                params=request.query_params,
                content=content,
                timeout=12.0
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=dict(response.headers)
            )
        except Exception as e:
            return JSONResponse(
                status_code=502,
                content={"error": "Gateway Timeout", "details": str(e)}
            )

# ==========================================
# 8. Optimized APIs
# ==========================================
@app.get("/api/admin/sys_settings")
async def get_sys_settings(auth: bool = Depends(verify_admin)):
    cost_cfg = await db.settings.find_one({"id": "vip_cost"})
    days_cfg = await db.settings.find_one({"id": "vip_days"})
    social_cfg = await db.settings.find_one({"id": "social_links"})
    interval_cfg = await db.settings.find_one({"id": "ad_interval"}) 
    
    return {
        "vip_cost": cost_cfg["amount"] if cost_cfg else 30,
        "vip_days": days_cfg["days"] if days_cfg else 1,
        "ad_interval": interval_cfg["interval"] if interval_cfg else 3, 
        "social_links": social_cfg.get("links", {}) if social_cfg else {}
    }

@app.post("/api/admin/sys_settings")
async def save_sys_settings(data: dict = Body(...), auth: bool = Depends(verify_admin)):
    if "vip_cost" in data:
        await db.settings.update_one({"id": "vip_cost"}, {"$set": {"amount": int(data.get("vip_cost", 30))}}, upsert=True)
    if "vip_days" in data:
        await db.settings.update_one({"id": "vip_days"}, {"$set": {"days": int(data.get("vip_days", 1))}}, upsert=True)
    if "ad_interval" in data:
        await db.settings.update_one({"id": "ad_interval"}, {"$set": {"interval": int(data.get("ad_interval", 3))}}, upsert=True)
    if "social_links" in data:
        await db.settings.update_one({"id": "social_links"}, {"$set": {"links": data["social_links"]}}, upsert=True)
    return {"status": "ok"}

@app.get("/api/movies/search")
async def search_movies(q: str = "", page: int = 1):
    cache_key = f"{q}_{page}"
    if cache_key in list_cache: return list_cache[cache_key]
    
    limit = 15
    skip = (page - 1) * limit
    query = {"title": {"$regex": q, "$options": "i"}} if q else {}
    
    movies = await db.movies.find(query).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
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
        
    list_cache[cache_key] = {"results": result, "total": total, "pages": (total + limit - 1) // limit}
    return list_cache[cache_key]

@app.get("/api/movie/{movie_id}")
async def get_movie_file(movie_id: str, user_data: str = None):
    try:
        movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
        if not movie: raise HTTPException(status_code=404, detail="Not found")
        
        await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$inc": {"clicks": 1}})
        clear_app_cache()
        
        return {
            "file_id": movie.get("file_id"),
            "db_file_id": movie.get("db_file_id"),
            "db_channel_id": DB_CHANNEL_ID,
            "file_type": movie.get("file_type", "video"),
            "title": movie.get("title")
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/")
async def web_app():
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BD Viral Box</title>
    <style>
        body{font-family:'Segoe UI',Tahoma,Geneva,Verdana,sans-serif;margin:0;padding:0;background:#0f172a;color:#fff;}
        .header{background:linear-gradient(135deg,#1e293b,#0f172a);padding:20px;text-align:center;box-shadow:0 4px 15px rgba(0,0,0,0.5);}
        .search-box{margin:20px;padding:0 20px;}
        input[type=text]{width:100%;padding:15px;border-radius:10px;border:none;background:#1e293b;color:#fff;font-size:16px;outline:none;box-sizing:border-box;}
        .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:15px;padding:20px;}
        .card{background:#1e293b;border-radius:12px;overflow:hidden;cursor:pointer;transition:0.3s;}
        .card:hover{transform:scale(1.05);box-shadow:0 0 15px #3b82f6;}
        .card img{width:100%;height:200px;object-fit:cover;}
        .card .info{padding:10px;}
        .card .title{font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
        .card .quality{font-size:11px;color:#3b82f6;margin-top:5px;}
    </style>
</head>
<body>
    <div class="header">
        <h1>🎬 BD Viral Box</h1>
        <p style="color:#94a3b8;">Exclusive Viral Collections</p>
    </div>
    <div class="search-box">
        <input type="text" id="searchInput" placeholder="🔍 Search movies here..." onkeyup="searchMovies()">
    </div>
    <div class="grid" id="movieGrid"></div>

    <script>
        let tg = window.Telegram.WebApp;
        tg.expand();

        function searchMovies() {
            const q = document.getElementById('searchInput').value;
            fetch(`/api/movies/search?q=${q}`)
                .then(res => res.json())
                .then(data => {
                    const grid = document.getElementById('movieGrid');
                    grid.innerHTML = '';
                    data.results.forEach(m => {
                        grid.innerHTML += `
                            <div class="card" onclick="openMovie('${m.id}')">
                                <img src="https://api.telegram.org/file/bot${tg.initDataUnsafe?.user ? '' : ''}${m.photo_id ? '' : ''}" onerror="this.src='https://via.placeholder.com/150x200/1e293b/ffffff?text=No+Image'">
                                <div class="info">
                                    <div class="title">${m.title}</div>
                                    <div class="quality">${m.quality} • ${m.clicks} views</div>
                                </div>
                            </div>
                        `;
                    });
                });
        }

        function openMovie(id) {
            tg.openTelegramLink(`https://t.me/${tg.initDataUnsafe.user?.username || 'BDViralBoxProBot'}?start=play_${id}`);
        }

        searchMovies();
    </script>
</body>
</html>""")

# ==========================================
# STARTUP & SHUTDOWN EVENT (BOT POLLING FIX)
# ==========================================
@app.on_event("startup")
async def startup_event():
    global video_queue
    video_queue = asyncio.Queue()
    cleanup_temp_files()
    await init_db()
    await load_admins()
    await load_banned_users()
    await load_keyword_replies()
    
    # 🛑 বটকে টেলিগ্রামের মেসেজ শুনতে বলা হচ্ছে (এটাই মূল কাজ)
    asyncio.create_task(dp.start_polling(bot))
    
    # ব্যাকগ্রাউন্ড ওয়ার্কার চালু করা হচ্ছে
    asyncio.create_task(video_queue_worker())
    asyncio.create_task(auto_delete_worker())

@app.on_event("shutdown")
async def shutdown_event():
    # সার্ভার বন্ধ হলে বটের পোলিং ও সেশন সুন্দরভাবে বন্ধ করা হচ্ছে
    await dp.stop_polling()
    await bot.session.close()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
