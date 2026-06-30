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
# 🌐 নেটওয়ার্ক গেটওয়ের জন্য প্রক্সি লাইব্রেরি ইম্পোর্ট করা হলো
import httpx 
from PIL import Image, ImageFilter

# ==========================================
# 🛑 Cache লাইব্রেরি ইম্পোর্ট করা হলো
# ==========================================
from cachetools import TTLCache
import copy

# ==========================================
# 🛑 FIX FOR EVENT LOOP ERROR
# ==========================================
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ==========================================

from fastapi import FastAPI, Body, Request, Depends, HTTPException, status
# 🌐 JSONResponse ইম্পোর্ট তালিকায় যুক্ত করা হলো
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

# 🛑 NEW: AI Assistant Import
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
BOT_USERNAME = "BDViralLinkProBot"

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
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_series_search = State()
    waiting_for_episode_quality = State()

def cleanup_temp_files():
    patterns = ["temp_video_*.mp4", "collage_*.jpg", "temp_frame_*.jpg", "temp_in_*.jpg", "temp_out_*.jpg"]
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

def make_wide_thumbnail(input_path, output_path):
    try:
        img = Image.open(input_path).convert('RGB')
        w, h = img.size
        target_w = int(h * 1.777)
        canvas = Image.new('RGB', (target_w, h))
        bg = img.resize((target_w, h))
        bg = bg.filter(ImageFilter.GaussianBlur(15))
        canvas.paste(bg, (0, 0))
        offset_x = (target_w - w) // 2
        canvas.paste(img, (offset_x, 0))
        canvas.save(output_path, quality=90)
        return True
    except Exception as e: 
        logger.error(f"Thumbnail error: {e}")
        return False

async def get_video_duration(file_path):
    try:
        cmd = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{file_path}"'
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=60.0)
            return float(stdout.decode().strip())
        except asyncio.TimeoutError:
            process.kill()
            return 10.0
    except Exception: 
        return 10.0 

async def generate_collage(video_path, output_path):
    duration = await get_video_duration(video_path)
    timestamps = [max(1, duration * 0.2), duration * 0.5, duration * 0.8]
    images = []
    for i, t in enumerate(timestamps):
        img_name = f"temp_frame_{i}_{int(time.time())}.jpg"
        cmd = f'ffmpeg -y -ss {t} -i "{video_path}" -vframes 1 -q:v 2 "{img_name}"'
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            await asyncio.wait_for(process.communicate(), timeout=120.0)
        except asyncio.TimeoutError:
            process.kill()
            continue

        if os.path.exists(img_name):
            try:
                img = Image.open(img_name)
                h_percent = (360 / float(img.size[1]))
                w_size = int((float(img.size[0]) * float(h_percent)))
                img = img.resize((w_size, 360), Image.Resampling.LANCZOS)
                images.append(img)
            except Exception: pass
            finally:
                if os.path.exists(img_name): os.remove(img_name)
    
    if not images: return False
    while len(images) < 3: images.append(images[-1].copy())
        
    img_w, img_h = images[0].size
    padding = 8
    poster_w = (img_w * 3) + (padding * 4)
    poster_h = img_h + (padding * 2)
    collage = Image.new('RGB', (poster_w, poster_h), color=(15, 23, 42))
    positions = [(padding, padding), (img_w + padding * 2, padding), (img_w * 2 + padding * 3, padding)]
    
    for idx, img in enumerate(images[:3]):
        if img.size != (img_w, img_h): img = img.resize((img_w, img_h), Image.Resampling.LANCZOS)
        collage.paste(img, positions[idx])
        
    collage.save(output_path, quality=90)
    return True

async def video_queue_worker():
    global is_processing, video_queue
    while True:
        chat_id, message_id, aiogram_file_id, file_type = await video_queue.get()
        is_processing = True
        downloaded_file = None
        collage_path = None
        try:
            admin_id = chat_id
            status_msg = await bot.send_message(admin_id, "⏳ <b>Processing Video...</b> (Downloading)")
            pyro_msg = await pyro_app.get_messages(chat_id, message_id)
            
            total_vids = await db.movies.count_documents({})
            serial_no = total_vids + 1
            
            # 🛑 রেনডম ও আকর্ষণীয় অটো-টাইটেল জেনারেটর
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
            collage_path = os.path.abspath(f"collage_{serial_no}_{int(time.time())}.jpg")
            
            downloaded_file = await pyro_app.download_media(pyro_msg, file_name=video_name)
            if not downloaded_file:
                await bot.edit_message_text("❌ ফাইল ডাউনলোড করতে সমস্যা হয়েছে।", chat_id=admin_id, message_id=status_msg.message_id)
                continue
                
            await bot.edit_message_text("📸 <b>Generating Screenshots...</b>", chat_id=admin_id, message_id=status_msg.message_id, parse_mode="HTML")
            success = await generate_collage(downloaded_file, collage_path)
            
            if not success:
                await bot.edit_message_text("❌ <b>Screenshot তৈরি করতে সমস্যা হয়েছে!</b>", chat_id=admin_id, message_id=status_msg.message_id, parse_mode="HTML")
                continue
                
            db_file_id = None
            db_photo_id = None
            photo_id = None
            
            if DB_CHANNEL_ID:
                try:
                    copied_vid = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=chat_id, message_id=message_id)
                    db_file_id = copied_vid.message_id
                    
                    copied_photo = await bot.send_photo(DB_CHANNEL_ID, FSInputFile(collage_path))
                    db_photo_id = copied_photo.message_id
                    photo_id = copied_photo.photo[-1].file_id
                except Exception: pass
            
            photo_msg = await bot.send_photo(admin_id, photo=FSInputFile(collage_path), caption=f"✅ <b>{auto_title}</b> Successfully Uploaded!")
            if not photo_id: photo_id = photo_msg.photo[-1].file_id
            
            await db.movies.insert_one({
                "title": auto_title, "quality": "HD", "photo_id": photo_id, 
                "file_id": aiogram_file_id, "file_type": file_type,
                "db_file_id": db_file_id, "db_photo_id": db_photo_id,
                "clicks": 0, "created_at": datetime.datetime.utcnow()
            })
            clear_app_cache() 
            await bot.delete_message(chat_id=admin_id, message_id=status_msg.message_id)

            if CHANNEL_ID:
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
            if collage_path and os.path.exists(collage_path): os.remove(collage_path)
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
    await db.payments.create_index("trx_id", unique=True)
    await db.ads.create_index("expires_at")
    
    # 7 Days Trending Tracking indexes
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
        args = message.text.split(" ")
        if len(args) > 1 and args[1].startswith("ref_"):
            try:
                referrer_id = int(args[1].split("_")[1])
                if referrer_id != uid:
                    await db.users.update_one({"user_id": referrer_id}, {"$inc": {"refer_count": 1, "coins": 10}})
                    try: await bot.send_message(referrer_id, "🎉 <b>Congratulations!</b> You got <b>10 Points</b> for a new referral!", parse_mode="HTML")
                    except: pass
            except Exception: pass

        await db.users.insert_one({
            "user_id": uid, "first_name": message.from_user.first_name, "joined_at": now, "refer_count": 0, "coins": 0, "vip_until": now - datetime.timedelta(days=1), "last_active": now
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
            "🔸 পেমেন্ট নাম্বার: <code>/setbkash নাম্বার</code> | <code>/setnagad নাম্বার</code>\n"
            "🔸 প্রোটেকশন: <code>/protect on/off</code> | অটো-ডিলিট: <code>/settime [মিনিট]</code>\n"
            "🔸 অ্যাড টাইম: <code>/setadtime [সেকেন্ড]</code>\n" 
            "🔸 স্ট্যাটাস: <code>/stats</code> | ব্রডকাস্ট: <code>/cast</code>\n"
            "🔸 মুভি ডিলিট: <code>/delmovie মুভির নাম</code> | <code>/delallmovies</code>\n"
            "🔸 ব্যান: <code>/ban ID</code> | আনব্যান: <code>/unban ID</code>\n"
            "🔸 VIP দিন: <code>/addvip ID দিন</code> | VIP বাতিল: <code>/removevip ID</code>\n"
            "🔸 পয়েন্ট দিন: <code>/addcoin ID পরিমাণ</code> | পয়েন্ট কাটুন: <code>/removecoin ID পরিমাণ</code>\n\n"
            f"🌐 <b>ওয়েব অ্যাডমিন প্যানেল:</b> <a href='{APP_URL}/admin'>এখানে ক্লিক করুন</a>\n"
            "<i>লগিন: admin / admin123</i>\n\n"
            "📥 <b>মুভি অ্যাড করতে প্রথমে ভিডিও বা ডকুমেন্ট ফাইল পাঠান।</b>"
        )
    else: text = f"👋 <b>Welcome {message.from_user.first_name}!</b>\n\nClick the button below to browse movies."
        
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

@dp.message(Command("setbkash"))
async def set_bkash(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        num = m.text.split(" ")[1]
        await db.settings.update_one({"id": "bkash_no"}, {"$set": {"number": num}}, upsert=True)
        await m.answer(f"✅ বিকাশ নাম্বার সেট করা হয়েছে: <b>{num}</b>", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("setnagad"))
async def set_nagad(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        num = m.text.split(" ")[1]
        await db.settings.update_one({"id": "nagad_no"}, {"$set": {"number": num}}, upsert=True)
        await m.answer(f"✅ নগদ নাম্বার সেট করা হয়েছে: <b>{num}</b>", parse_mode="HTML")
    except Exception: pass

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
    
    text = (f"📊 <b>অ্যাডভান্সড স্ট্যাটাস:</b>\n\n👥 মোট ইউজার: <code>{uc}</code>\n🟢 আজকের নতুন ইউজার: <code>{new_users_today}</code>\n"
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

@dp.message(Command("addvip"))
async def add_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30 
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": target_uid})
        if not user: return await m.answer("⚠️ ইউজার ডাটাবেসে নেই।")
        current_vip = user.get("vip_until", now)
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await m.answer(f"✅ <code>{target_uid}</code> কে <b>{days} দিনের</b> VIP দেওয়া হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("removevip"))
async def remove_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        target_uid = int(m.text.split()[1])
        now = datetime.datetime.utcnow()
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": now - datetime.timedelta(days=1)}})
        await m.answer(f"❌ VIP বাতিল করা হয়েছে!", parse_mode="HTML")
    except Exception: pass

@dp.message(Command("addcoin"))
async def add_coin_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        amount = int(args[2])
        
        user = await db.users.find_one({"user_id": target_uid})
        if not user: return await m.answer("⚠️ এই ইউজার ডাটাবেসে নেই।")
            
        await db.users.update_one({"user_id": target_uid}, {"$inc": {"coins": amount}})
        await m.answer(f"✅ ইউজার <code>{target_uid}</code> কে <b>{amount} পয়েন্ট</b> দেওয়া হয়েছে!", parse_mode="HTML")
        
        try:
            await bot.send_message(target_uid, f"🎉 <b>Congratulations!</b>\nআপনি অ্যাডমিনের কাছ থেকে <b>{amount} Points</b> পেয়েছেন! এখন আপনি Premium বা Ad Campaign শুরু করতে পারেন।", parse_mode="HTML")
        except: pass
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/addcoin UserID পরিমাণ</code>\n(যেমন: <code>/addcoin 123456789 500</code>)", parse_mode="HTML")

@dp.message(Command("removecoin"))
async def remove_coin_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        amount = int(args[2])
        
        user = await db.users.find_one({"user_id": target_uid})
        if not user: return await m.answer("⚠️ এই ইউজার ডাটাবেসে নেই।")
            
        await db.users.update_one({"user_id": target_uid}, {"$inc": {"coins": -amount}})
        await m.answer(f"❌ ইউজার <code>{target_uid}</code> থেকে <b>{amount} পয়েন্ট</b> কেটে নেওয়া হয়েছে!", parse_mode="HTML")
    except Exception: 
        await m.answer("⚠️ সঠিক নিয়ম: <code>/removecoin UserID পরিমাণ</code>", parse_mode="HTML")

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 যে মেসেজটি ব্রডকাস্ট করতে চান সেটি পাঠান।\nবাতিল করতে /start দিন।")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("⏳ ব্রডকাস্ট শুরু হয়েছে...")
    kb = [[types.InlineKeyboardButton(text="🎬 ওপেন মুভি অ্যাপ", web_app=types.WebAppInfo(url=APP_URL))]]
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
        await m.answer("⚠️ সঠিক নিয়ম: <code>/addreply কিওয়ার্ড | আপনার রিপ্লাই</code>\n(যেমন: <code>/addreply pushpa 2 | মুভিটি এখনো রিলিজ হয়নি।</code>)", parse_mode="HTML")

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
        # 1. Forward to Admins
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
        
        # 2. Smart AI Auto-Reply
        if m.from_user.id not in auto_reply_cache:
            auto_reply_cache[m.from_user.id] = True
            try:
                kb = [[types.InlineKeyboardButton(text="🎬 Watch Now (মুভি দেখুন)", web_app=types.WebAppInfo(url=APP_URL))]]
                user_markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
                
                if user_text:
                    reply_text = await get_smart_reply(user_text, m.from_user.first_name, db, user_id=m.from_user.id)
                else:
                    reply_text = "হ্যালো! আপনার মেসেজ/ফাইলটি অ্যাডমিনের কাছে পৌঁছে গেছে। প্রয়োজনে অ্যাডমিন আপনাকে রিপ্লাই দেবেন। ধন্যবাদ! ❤️"
                
                await m.reply(reply_text, reply_markup=user_markup, parse_mode="HTML")
            except Exception as e: 
                logger.error(f"Auto-Reply Error: {e}")
    else:
        # 3. If Manual Reply
        if m.from_user.id not in auto_reply_cache:
            auto_reply_cache[m.from_user.id] = True
            try:
                kb = [[types.InlineKeyboardButton(text="🎬 Watch Now (মুভি দেখুন)", web_app=types.WebAppInfo(url=APP_URL))]]
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
        
        db_file_id = None
        if DB_CHANNEL_ID:
            try:
                copied = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=m.chat.id, message_id=m.message_id)
                db_file_id = copied.message_id
            except Exception: pass
            
        await state.update_data(file_id=fid, file_type=ftype, db_file_id=db_file_id)
        
        kb = [
            [types.InlineKeyboardButton(text="🎬 নতুন মুভি/সিরিজ যুক্ত করুন", callback_data="upload_new")],
            [types.InlineKeyboardButton(text="➕ আগের সিরিজের নতুন এপিসোড", callback_data="upload_episode")]
        ]
        markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
        await m.answer("✅ ফাইল পেয়েছি! এটি কি নতুন মুভি নাকি আগের সিরিজের নতুন এপিসোড?", reply_markup=markup)

@dp.callback_query(F.data == "upload_new")
async def upload_new_cb(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_photo)
    await c.message.edit_text("✅ <b>নতুন মুভি/সিরিজ!</b>\nএবার মুভির <b>পোস্টার (Photo)</b> সেন্ড করুন।", parse_mode="HTML")

@dp.callback_query(F.data == "upload_episode")
async def upload_episode_cb(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_series_search)
    await c.message.edit_text("✅ <b>নতুন এপিসোড!</b>\n\nসিরিজের নামের কয়েক অক্ষর লিখে রিপ্লাই দিন (যেমন: Farzi)।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_series_search, F.text)
async def search_series_for_episode(m: types.Message, state: FSMContext):
    query = m.text.strip()
    pipeline = [
        {"$match": {"title": {"$regex": query, "$options": "i"}}},
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "db_photo_id": {"$first": "$db_photo_id"}}},
        {"$limit": 10}
    ]
    results = await db.movies.aggregate(pipeline).to_list(10)

    if not results: return await m.answer("⚠️ এই নামে কোনো সিরিজ পাওয়া যায়নি! আবার সঠিক নাম লিখে পাঠান।")

    await state.update_data(search_results=results)
    
    builder = InlineKeyboardBuilder()
    for idx, res in enumerate(results): builder.button(text=f"📺 {res['_id']}", callback_data=f"sel_series_{idx}")
    builder.adjust(1)
    
    await m.answer("👇 নিচে থেকে আপনার কাঙ্ক্ষিত সিরিজটি সিলেক্ট করুন:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("sel_series_"))
async def selected_series_cb(c: types.CallbackQuery, state: FSMContext):
    idx = int(c.data.split("_")[2])
    data = await state.get_data()
    selected = data["search_results"][idx]

    await state.update_data(title=selected["_id"], photo_id=selected["photo_id"], db_photo_id=selected.get("db_photo_id"))
    
    await state.set_state(AdminStates.waiting_for_episode_quality)
    await c.message.edit_text(f"✅ <b>{selected['_id']}</b> সিলেক্ট হয়েছে!\n\nএবার এই নতুন ফাইলের <b>এপিসোড নাম্বার বা কোয়ালিটি</b> লিখে পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_episode_quality, F.text)
async def finalize_new_episode(m: types.Message, state: FSMContext):
    quality = m.text.strip()
    data = await state.get_data()
    title = data["title"]
    photo_id = data["photo_id"]
    
    await db.movies.insert_one({
        "title": title, "quality": quality, "photo_id": photo_id, 
        "file_id": data["file_id"], "file_type": data["file_type"],
        "db_file_id": data.get("db_file_id"), "db_photo_id": data.get("db_photo_id"),
        "clicks": 0, "created_at": datetime.datetime.utcnow()
    })
    clear_app_cache() 
    
    await state.clear()
    await m.answer(f"🎉 <b>{title} [{quality}]</b> সফলভাবে সিরিজে এড করা হয়েছে!", parse_mode="HTML")

    if CHANNEL_ID:
        try:
            bot_info = await bot.get_me()
            kb = [
                [types.InlineKeyboardButton(text="📥 Download & Watch 🎬", url=f"https://t.me/{bot_info.username}?start=new")],
                [types.InlineKeyboardButton(text="কিভাবে ডাউনলোড করবেন ❓", url=TUTORIAL_LINK)],
                [types.InlineKeyboardButton(text="♻️ MOVIE REQUEST ♻️", url=REQUEST_LINK)]
            ]
            markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
            caption = (f"🔥 <b>নতুন এপিসোড যুক্ত হয়েছে!</b>\n\n📌 <b>টাইটেল:</b> {title}\n🏷 <b>এপিসোড/কোয়ালিটি:</b> {quality}\n\n👇 <i>বট থেকে ভিডিওটি পেতে নিচের বাটনে ক্লিক করুন।</i>")
            await bot.send_photo(chat_id=CHANNEL_ID, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=markup)
        except Exception: pass

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    status_msg = await m.answer("⏳ <b>ছবিটি চ্যাপ্টা (16:9) করা হচ্ছে...</b>", parse_mode="HTML")
    photo_id = m.photo[-1].file_id
    file_info = await bot.get_file(photo_id)
    
    temp_in = f"temp_in_{photo_id}.jpg"
    temp_out = f"temp_out_{photo_id}.jpg"
    await bot.download_file(file_info.file_path, temp_in)
    
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, make_wide_thumbnail, temp_in, temp_out)
    
    db_photo_id = None
    target_file = temp_out if success else temp_in
    
    if DB_CHANNEL_ID:
        try:
            copied_photo = await bot.send_photo(DB_CHANNEL_ID, FSInputFile(target_file))
            db_photo_id = copied_photo.message_id
            photo_id = copied_photo.photo[-1].file_id
        except Exception: pass
    
    if success:
        sent_photo = await m.answer_photo(FSInputFile(temp_out), caption="✅ <b>পোস্টার রেডি!</b>\nএবার <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
        if not DB_CHANNEL_ID: photo_id = sent_photo.photo[-1].file_id
    else:
        await m.answer("✅ পোস্টার পেয়েছি! এবার <b>টাইটেল (নাম)</b> লিখে পাঠান।", parse_mode="HTML")
        
    await state.update_data(photo_id=photo_id, db_photo_id=db_photo_id)
    await state.set_state(AdminStates.waiting_for_title)
    await bot.delete_message(m.chat.id, status_msg.message_id)
    
    if os.path.exists(temp_in): os.remove(temp_in)
    if os.path.exists(temp_out): os.remove(temp_out)

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
    photo_id = data["photo_id"]
    quality = data["quality"]
    
    await db.movies.insert_one({
        "title": title, "quality": quality, "photo_id": photo_id, 
        "file_id": data["file_id"], "file_type": data["file_type"],
        "db_file_id": data.get("db_file_id"), "db_photo_id": data.get("db_photo_id"),
        "clicks": 0, "created_at": datetime.datetime.utcnow()
    })
    clear_app_cache() 
    
    await m.answer(f"🎉 <b>{title} [{quality}]</b> অ্যাপে যুক্ত করা হয়েছে!", parse_mode="HTML")

    if CHANNEL_ID:
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

@dp.callback_query(F.data.startswith("trx_"))
async def handle_trx_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action, _, pay_id = c.data.split("_")
    
    payment = await db.payments.find_one({"_id": ObjectId(pay_id)})
    if not payment or payment["status"] != "pending": return await c.answer("ইতিমধ্যে প্রসেস করা হয়েছে!", show_alert=True)
        
    user_id = payment["user_id"]
    days = payment["days"]
    
    if action == "approve":
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": user_id})
        current_vip = user.get("vip_until", now) if user else now
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": user_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "approved"}})
        await c.message.edit_text(c.message.text + f"\n\n✅ <b>Approve করা হয়েছে!</b>", parse_mode="HTML")
        try: await bot.send_message(user_id, f"🎉 <b>পেমেন্ট সফল!</b> আপনার পেমেন্ট অ্যাপ্রুভ হয়েছে এবং VIP চালু হয়েছে!", parse_mode="HTML")
        except: pass
    else:
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "rejected"}})
        await c.message.edit_text(c.message.text + "\n\n❌ <b>Reject করা হয়েছে!</b>", parse_mode="HTML")

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
# 🌐 NETWORK GATEWAY & REVERSE PROXY SYSTEM (Direct Integration)
# ==========================================
FALLBACK_GATEWAYS = [
    "https://workers.cloudflare.com",
    "https://vercel.live",
    "https://pages.dev"
]

@app.get("/gateway/dns-check")
async def check_network_status(request: Request):
    """
    ইউজারের লোকাল নেটওয়ার্ক কানেকশন টেস্ট করার এন্ডপয়েন্ট।
    """
    return {
        "status": "online",
        "message": "Gateway Connection Successful",
        "client_ip": request.client.host if request.client else "Unknown",
        "suggested_gateways": FALLBACK_GATEWAYS
    }

@app.api_route("/gateway/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def reverse_proxy_gateway(request: Request, path: str):
    """
    রিভার্স প্রক্সি টানেল যা যেকোনো ব্লকড রিকোয়েস্টকে ব্যাকএন্ডে রি-রুট করে।
    """
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
    unlock_cfg = await db.settings.find_one({"id": "unlock_hours"})
    social_cfg = await db.settings.find_one({"id": "social_links"})
    interval_cfg = await db.settings.find_one({"id": "ad_interval"}) 
    
    return {
        "vip_cost": cost_cfg["amount"] if cost_cfg else 30,
        "vip_days": days_cfg["days"] if days_cfg else 1,
        "unlock_hours": unlock_cfg["hours"] if unlock_cfg else 24,
        "ad_interval": interval_cfg["interval"] if interval_cfg else 3, 
        "social_links": social_cfg.get("links", {}) if social_cfg else {}
    }

@app.post("/api/admin/sys_settings")
async def save_sys_settings(data: dict = Body(...), auth: bool = Depends(verify_admin)):
    await db.settings.update_one({"id": "vip_cost"}, {"$set": {"amount": int(data.get("vip_cost", 30))}}, upsert=True)
    await db.settings.update_one({"id": "vip_days"}, {"$set": {"days": int(data.get("vip_days", 1))}}, upsert=True)
    await db.settings.update_one({"id": "unlock_hours"}, {"$set": {"hours": int(data.get("unlock_hours", 24))}}, upsert=True)
    await db.settings.update_one({"id": "ad_interval"}, {"$set": {"interval": int(data.get("ad_interval", 3))}}, upsert=True) 
    
    social_links = data.get("social_links", {})
    await db.settings.update_one({"id": "social_links"}, {"$set": {"links": social_links}}, upsert=True)
    
    clear_app_cache()
    return {"ok": True}

@app.get("/admin", response_class=HTMLResponse)
async def web_admin_panel(auth: bool = Depends(verify_admin)):
    html_content = """
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin Panel - BD Viral Link</title>
        <script src="https://tailwindbcss.com"></script>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <style>
            .neon-card {
                background: rgba(30, 41, 59, 0.7);
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255, 255, 255, 0.05);
                box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
            }
            .pulse-dot {
                animation: blink 1.5s infinite;
            }
            @keyframes blink {
                0%, 100% { opacity: 0.2; transform: scale(0.9); }
                50% { opacity: 1; transform: scale(1.1); }
            }
        </style>
    </head>
    <body class="bg-gray-950 text-white p-5 font-sans">
        <div class="max-w-6xl mx-auto">
            <h1 class="text-3xl font-black text-transparent bg-clip-text bg-gradient-to-r from-red-500 to-amber-500 mb-6 border-b border-gray-800 pb-3 flex items-center gap-2">
                <i class="fa-solid fa-gauge-high"></i> Ultimate Admin Dashboard
            </h1>
            
            <!-- Tabs Menu -->
            <div class="flex flex-wrap gap-2 mb-6 border-b border-gray-800 pb-3">
                <button onclick="switchAdminTab('dashboard')" id="tabBtn-dashboard" class="px-4 py-2 bg-blue-600 rounded text-white font-bold transition">Dashboard & Analytics</button>
                <button onclick="switchAdminTab('users')" id="tabBtn-users" class="px-4 py-2 bg-gray-800 hover:bg-gray-750 rounded text-gray-300 font-bold transition">User Manager</button>
                <button onclick="switchAdminTab('settings')" id="tabBtn-settings" class="px-4 py-2 bg-gray-800 hover:bg-gray-750 rounded text-gray-300 font-bold transition">System Settings</button>
                <button onclick="switchAdminTab('social')" id="tabBtn-social" class="px-4 py-2 bg-gray-800 hover:bg-gray-750 rounded text-gray-300 font-bold transition">Social Links</button>
                <button onclick="switchAdminTab('movies')" id="tabBtn-movies" class="px-4 py-2 bg-gray-800 hover:bg-gray-750 rounded text-gray-300 font-bold transition">Manage Movies</button>
                <button onclick="switchAdminTab('ads')" id="tabBtn-ads" class="px-4 py-2 bg-gray-800 hover:bg-gray-750 rounded text-gray-300 font-bold transition">Ads Manager</button>
                <button onclick="switchAdminTab('keywords')" id="tabBtn-keywords" class="px-4 py-2 bg-gray-800 hover:bg-gray-750 rounded text-gray-300 font-bold transition">Keyword Replies</button>
                <button onclick="switchAdminTab('requests')" id="tabBtn-requests" class="px-4 py-2 bg-gray-800 hover:bg-gray-750 rounded text-gray-300 font-bold transition">User Requests</button>
            </div>

            <!-- Tab Content: Dashboard & Analytics -->
            <div id="adminTab-dashboard" class="admin-tab-content">
                <div class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8" id="statsBoard">
                    <div class="neon-card p-5 rounded-2xl border-l-4 border-green-500 flex items-center justify-between shadow-lg">
                        <div class="flex items-center gap-3">
                            <div class="bg-green-500/10 p-4 rounded-xl text-green-400 text-2xl relative">
                                <i class="fa-solid fa-wave-square"></i>
                                <span class="absolute top-1 right-1 w-3.5 h-3.5 bg-green-500 rounded-full border-2 border-gray-950 pulse-dot"></span>
                            </div>
                            <div>
                                <p class="text-gray-400 text-xs font-bold uppercase tracking-wider">Live Online</p>
                                <h3 class="text-2xl font-black text-green-400" id="stLiveOnline">0</h3>
                            </div>
                        </div>
                        <span class="text-xs text-green-500/80 font-semibold bg-green-500/10 px-2 py-0.5 rounded-full">App Activity</span>
                    </div>
                    
                    <div class="neon-card p-5 rounded-2xl border-l-4 border-blue-500 flex items-center gap-3 shadow-lg">
                        <div class="bg-blue-600/10 p-4 rounded-xl text-blue-400 text-2xl"><i class="fa-solid fa-users"></i></div>
                        <div><p class="text-gray-400 text-xs font-bold uppercase tracking-wider">Total Users</p><h3 class="text-2xl font-black text-blue-400" id="stUsers">...</h3></div>
                    </div>
                    <div class="neon-card p-5 rounded-2xl border-l-4 border-orange-500 flex items-center gap-3 shadow-lg">
                        <div class="bg-orange-600/10 p-4 rounded-xl text-orange-400 text-2xl"><i class="fa-solid fa-film"></i></div>
                        <div><p class="text-gray-400 text-xs font-bold uppercase tracking-wider">Total Uploads</p><h3 class="text-2xl font-black text-orange-400" id="stMovies">...</h3></div>
                    </div>
                    <div class="neon-card p-5 rounded-2xl border-l-4 border-purple-500 flex items-center gap-3 shadow-lg">
                        <div class="bg-purple-600/10 p-4 rounded-xl text-purple-400 text-2xl"><i class="fa-solid fa-eye"></i></div>
                        <div><p class="text-gray-400 text-xs font-bold uppercase tracking-wider">Total Views</p><h3 class="text-2xl font-black text-purple-400" id="stViews">...</h3></div>
                    </div>
                </div>

                <!-- Advanced Analytics widgets -->
                <div class="grid grid-cols-1 md:grid-cols-1 gap-6 mb-8">
                    <div class="neon-card rounded-2xl p-6 shadow-xl">
                        <h2 class="text-lg font-bold text-gray-200 mb-4 flex items-center gap-2"><i class="fa-solid fa-chart-line text-blue-500"></i> Active Statistics</h2>
                        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                            <div class="bg-gray-900/50 p-4 rounded-xl border border-gray-800">
                                <p class="text-xs text-gray-400 font-bold uppercase">Active Today (DAU)</p>
                                <h3 id="analyticsDau" class="text-2xl font-bold text-green-400">0</h3>
                            </div>
                            <div class="bg-gray-900/50 p-4 rounded-xl border border-gray-800">
                                <p class="text-xs text-gray-400 font-bold uppercase">Active Weekly (WAU)</p>
                                <h3 id="analyticsWau" class="text-2xl font-bold text-blue-400">0</h3>
                            </div>
                            <div class="bg-gray-900/50 p-4 rounded-xl border border-gray-800">
                                <p class="text-xs text-gray-400 font-bold uppercase">Total User Reviews</p>
                                <h3 id="analyticsReviews" class="text-2xl font-bold text-yellow-400">0</h3>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Top Rated Movies List Widget -->
                <div class="neon-card rounded-2xl p-6 shadow-xl mb-8">
                    <h2 class="text-lg font-bold text-gray-200 mb-4"><i class="fa-solid fa-star text-yellow-400"></i> Top Rated Movies (By User Reviews)</h2>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm whitespace-nowrap">
                            <thead class="bg-gray-900/80 text-gray-300">
                                <tr>
                                    <th class="p-4 rounded-l-lg">Movie Title</th>
                                    <th class="p-4">Average Rating</th>
                                    <th class="p-4 rounded-r-lg">Total Reviews</th>
                                </tr>
                            </thead>
                            <tbody id="analyticsTopRatedList">
                                <tr><td colspan="3" class="p-4 text-center text-gray-500">Loading top rated movies...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- Tab Content: User Manager -->
            <div id="adminTab-users" class="admin-tab-content hidden">
                <div class="neon-card rounded-2xl shadow-xl p-6 mb-8">
                    <div class="flex flex-col md:flex-row justify-between items-center mb-6 gap-4">
                        <h2 class="text-xl font-bold text-blue-400 flex items-center gap-2"><i class="fa-solid fa-users-gear"></i> User Manager Panel</h2>
                        <div class="relative w-full md:w-1/3">
                            <input type="text" id="userSearchInput" placeholder="🔍 Search UID or Name..." oninput="searchUsers()" class="w-full bg-gray-900 text-white px-4 py-2 rounded-xl border border-gray-800 focus:outline-none focus:border-blue-500">
                        </div>
                    </div>
                    
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm whitespace-nowrap">
                            <thead class="bg-gray-900/80 text-gray-300">
                                <tr>
                                    <th class="p-4 rounded-l-lg">User Info</th>
                                    <th class="p-4">Points</th>
                                    <th class="p-4">VIP Status</th>
                                    <th class="p-4">Invites</th>
                                    <th class="p-4 rounded-r-lg text-right">Actions</th>
                                </tr>
                            </thead>
                            <tbody id="userTableBody">
                                <tr><td colspan="5" class="text-center p-8 text-gray-500">Search for a user by name or UID to begin managing...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- Tab Content: System Settings -->
            <div id="adminTab-settings" class="admin-tab-content hidden">
                <div class="neon-card rounded-2xl shadow-xl p-6 mb-8">
                    <h2 class="text-xl font-bold text-gray-200 mb-4"><i class="fa-solid fa-cogs"></i> System Settings</h2>
                    <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
                        <div>
                            <label class="text-gray-400 text-sm font-bold block mb-1">VIP Cost (Points)</label>
                            <input type="number" id="cfgVipCost" class="w-full bg-gray-700 text-white px-3 py-2 rounded-lg border border-gray-600 focus:outline-none">
                        </div>
                        <div>
                            <label class="text-gray-400 text-sm font-bold block mb-1">VIP Duration (Days)</label>
                            <input type="number" id="cfgVipDays" class="w-full bg-gray-700 text-white px-3 py-2 rounded-lg border border-gray-600 focus:outline-none">
                        </div>
                        <div>
                            <label class="text-gray-400 text-sm font-bold block mb-1">Movie Unlock (Hours)</label>
                            <input type="number" id="cfgUnlockHrs" class="w-full bg-gray-700 text-white px-3 py-2 rounded-lg border border-gray-600 focus:outline-none">
                        </div>
                        <div>
                            <label class="text-gray-400 text-sm font-bold block mb-1">Ad Interval (Movies Limit)</label>
                            <input type="number" id="cfgAdInterval" placeholder="e.g. 3 or 4" class="w-full bg-gray-700 text-white px-3 py-2 rounded-lg border border-gray-600 focus:outline-none">
                        </div>
                    </div>
                    <button onclick="saveSysSettings()" class="mt-4 bg-green-600 hover:bg-green-500 text-white px-6 py-2 rounded font-bold transition">Save Settings</button>
                </div>
            </div>

            <!-- Tab Content: Social Links -->
            <div id="adminTab-social" class="admin-tab-content hidden">
                <div class="neon-card rounded-2xl shadow-xl p-6 mb-8">
                    <h2 class="text-xl font-bold text-blue-400 mb-4"><i class="fa-solid fa-share-nodes"></i> Social Media Links</h2>
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label class="text-gray-400 text-sm font-bold block mb-1">Facebook Group</label>
                            <input type="url" id="cfgFbGroup" placeholder="https://facebook.com/groups/..." class="w-full bg-gray-700 text-white px-3 py-2 rounded-lg border border-gray-600 focus:outline-none">
                        </div>
                        <div>
                            <label class="text-gray-400 text-sm font-bold block mb-1">Facebook Page</label>
                            <input type="url" id="cfgFbPage" placeholder="https://facebook.com/..." class="w-full bg-gray-700 text-white px-3 py-2 rounded-lg border border-gray-600 focus:outline-none">
                        </div>
                        <div>
                            <label class="text-gray-400 text-sm font-bold block mb-1">YouTube Channel</label>
                            <input type="url" id="cfgYoutube" placeholder="https://youtube.com/..." class="w-full bg-gray-700 text-white px-3 py-2 rounded-lg border border-gray-600 focus:outline-none">
                        </div>
                        <div>
                            <label class="text-gray-400 text-sm font-bold block mb-1">Movie Review Channel</label>
                            <input type="url" id="cfgReview" placeholder="https://t.me/..." class="w-full bg-gray-700 text-white px-3 py-2 rounded-lg border border-gray-600 focus:outline-none">
                        </div>
                    </div>
                    <button onclick="saveSysSettings()" class="mt-4 bg-blue-600 hover:bg-blue-500 text-white px-6 py-2 rounded font-bold transition">Save Social Links</button>
                </div>
            </div>

            <!-- Tab Content: Manage Movies -->
            <div id="adminTab-movies" class="admin-tab-content hidden">
                <div class="neon-card rounded-2xl shadow-xl p-6 mb-8">
                    <div class="flex flex-col md:flex-row justify-between items-center mb-6 gap-4">
                        <h2 class="text-xl font-bold text-gray-200"><i class="fa-solid fa-list-ul"></i> Manage Movies</h2>
                        <input type="text" id="adminSearch" placeholder="🔍 Search Movies..." class="bg-gray-700 text-white px-4 py-2 rounded-lg border border-gray-600 focus:outline-none w-full md:w-1/3">
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm whitespace-nowrap">
                            <thead class="bg-gray-700 text-gray-300">
                                <tr><th class="p-4">Title</th><th class="p-4">Views</th><th class="p-4">Files</th><th class="p-4">Action</th></tr>
                            </thead>
                            <tbody id="movieTableBody"><tr><td colspan="4" class="text-center p-8 text-gray-400">Loading...</td></tr></tbody>
                        </table>
                    </div>
                    <div class="flex justify-center items-center gap-3 mt-6" id="adminPagination"></div>
                </div>
            </div>

            <!-- Tab Content: Ads Manager -->
            <div id="adminTab-ads" class="admin-tab-content hidden">
                <div class="neon-card rounded-2xl shadow-xl p-6">
                    <div class="flex flex-col md:flex-row justify-between items-center mb-6 gap-4">
                        <h2 class="text-xl font-bold text-yellow-400"><i class="fa-solid fa-bullhorn"></i> Ads Manager (Sponsored)</h2>
                    </div>
                    
                    <div class="bg-gray-900 p-4 rounded-lg border border-gray-700 mb-6">
                        <h3 class="text-gray-300 font-bold mb-3">Create Free Ad (Admin Only)</h3>
                        <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
                            <input type="text" id="adTitle" placeholder="Ad Title" class="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none">
                            <input type="text" id="adSubtitle" placeholder="Ad Subtitle" class="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none">
                            <input type="text" id="adLink" placeholder="URL / Link" class="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none">
                            <input type="text" id="adImage" placeholder="Image URL (Optional)" class="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none">
                        </div>
                        <button onclick="createAdminAd()" class="bg-yellow-600 hover:bg-yellow-500 text-white px-6 py-2 rounded font-bold whitespace-nowrap">Create Ad</button>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm whitespace-nowrap">
                            <thead class="bg-gray-700 text-gray-300">
                                <tr><th class="p-4">Title</th><th class="p-4">Subtitle</th><th class="p-4">Link</th><th class="p-4">Expires</th><th class="p-4">Action</th></tr>
                            </thead>
                            <tbody id="adsTableBody"><tr><td colspan="5" class="text-center p-8 text-gray-400">Loading Ads...</td></tr></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- Tab Content: Keyword Manager -->
            <div id="adminTab-keywords" class="admin-tab-content hidden">
                <div class="neon-card rounded-2xl border border-gray-700 p-6 shadow mb-8">
                    <h2 class="text-xl font-bold text-gray-200 mb-4"><i class="fa-solid fa-reply text-green-500"></i> Auto-Reply Keyword Manager</h2>
                    
                    <div class="bg-gray-900 p-4 rounded-lg border border-gray-700 mb-6">
                        <h3 class="text-gray-300 font-bold mb-3">Add Custom Keyword Reply</h3>
                        <div class="flex flex-col md:flex-row gap-3">
                            <input type="text" id="kwInput" placeholder="Keyword" class="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none md:w-1/3">
                            <input type="text" id="kwReplyInput" placeholder="Reply Message" class="w-full bg-gray-700 text-white px-3 py-2 rounded border border-gray-600 focus:outline-none flex-grow">
                            <button onclick="addKeywordReply()" class="bg-green-600 hover:bg-green-500 text-white px-6 py-2 rounded font-bold whitespace-nowrap">Add Rule</button>
                        </div>
                    </div>

                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm whitespace-nowrap">
                            <thead class="bg-gray-700 text-gray-300">
                                <tr><th class="p-4">Keyword</th><th class="p-4">Reply Message</th><th class="p-4">Action</th></tr>
                            </thead>
                            <tbody id="keywordsTableBody">
                                <tr><td colspan="3" class="p-4 text-center text-gray-500">Loading custom keyword rules...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- Tab Content: User Requests Status Manager -->
            <div id="adminTab-requests" class="admin-tab-content hidden">
                <div class="neon-card rounded-2xl border border-gray-700 p-6 shadow mb-8">
                    <h2 class="text-xl font-bold text-gray-200 mb-4"><i class="fa-solid fa-code-pull-request text-red-500"></i> User Movie Requests Management</h2>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm whitespace-nowrap">
                            <thead class="bg-gray-700 text-gray-300">
                                <tr>
                                    <th class="p-4">User Name (UID)</th>
                                    <th class="p-4">Requested Movie</th>
                                    <th class="p-4">Priority Status</th>
                                    <th class="p-4">Actions</th>
                                </tr>
                            </thead>
                            <tbody id="requestsTableBody">
                                <tr><td colspan="4" class="text-center p-8 text-gray-400">Loading requests...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>

        </div>
        <script>
            let currentPage = 1;
            let searchQuery = "";
            let searchTimeout = null;

            function switchAdminTab(tabId) {
                document.querySelectorAll('.admin-tab-content').forEach(content => content.classList.add('hidden'));
                document.getElementById('adminTab-' + tabId).classList.remove('hidden');
                
                document.querySelectorAll('[id^="tabBtn-"]').forEach(btn => {
                    btn.className = "px-4 py-2 bg-gray-800 hover:bg-gray-750 rounded text-gray-300 font-bold transition";
                });
                document.getElementById('tabBtn-' + tabId).className = "px-4 py-2 bg-blue-600 rounded text-white font-bold transition";

                if (tabId === 'dashboard') { loadStats(); loadAnalytics(); }
                else if (tabId === 'users') { searchUsers(); }
                else if (tabId === 'settings') { loadSysSettings(); }
                else if (tabId === 'movies') { loadAdminData(1); }
                else if (tabId === 'ads') { loadAds(); }
                else if (tabId === 'keywords') { loadKeywordList(); }
                else if (tabId === 'requests') { loadAdminRequests(); }
            }

            async function loadSysSettings() {
                try {
                    const res = await fetch('/api/admin/sys_settings');
                    const data = await res.json();
                    document.getElementById('cfgVipCost').value = data.vip_cost;
                    document.getElementById('cfgVipDays').value = data.vip_days;
                    document.getElementById('cfgUnlockHrs').value = data.unlock_hours;
                    document.getElementById('cfgAdInterval').value = data.ad_interval || 3;
                    
                    if(data.social_links) {
                        document.getElementById('cfgFbGroup').value = data.social_links.fb_group || '';
                        document.getElementById('cfgFbPage').value = data.social_links.fb_page || '';
                        document.getElementById('cfgYoutube').value = data.social_links.youtube || '';
                        document.getElementById('cfgReview').value = data.social_links.review_channel || '';
                    }
                } catch(e) {}
            }

            async function saveSysSettings() {
                const payload = {
                    vip_cost: document.getElementById('cfgVipCost').value,
                    vip_days: document.getElementById('cfgVipDays').value,
                    unlock_hours: document.getElementById('cfgUnlockHrs').value,
                    ad_interval: document.getElementById('cfgAdInterval').value,
                    social_links: {
                        fb_group: document.getElementById('cfgFbGroup').value,
                        fb_page: document.getElementById('cfgFbPage').value,
                        youtube: document.getElementById('cfgYoutube').value,
                        review_channel: document.getElementById('cfgReview').value
                    }
                };
                try {
                    await fetch('/api/admin/sys_settings', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    alert('Settings saved successfully!');
                } catch(e) {
                    alert('Failed to save settings.');
                }
            }

            async function loadStats() {
                try {
                    const res = await fetch('/api/admin/stats');
                    const data = await res.json();
                    document.getElementById('stUsers').innerText = data.users;
                    document.getElementById('stMovies').innerText = data.movies;
                    document.getElementById('stViews').innerText = data.views;
                } catch(e) {}
            }

            async function loadAnalytics() {
                try {
                    const res = await fetch('/api/admin/analytics');
                    const data = await res.json();
                    
                    document.getElementById('stLiveOnline').innerText = data.live_online;
                    document.getElementById('analyticsDau').innerText = data.active_today;
                    document.getElementById('analyticsWau').innerText = data.active_week;
                    document.getElementById('analyticsReviews').innerText = data.total_reviews;

                    let ratedHtml = '';
                    data.top_rated.forEach(m => {
                        ratedHtml += `
                        <tr class="border-b border-gray-800 hover:bg-gray-900/40">
                            <td class="p-4 font-bold text-yellow-400">${m._id}</td>
                            <td class="p-4 font-semibold"><i class="fa-solid fa-star text-yellow-400 mr-1"></i> ${m.avg_rating.toFixed(1)} / 5</td>
                            <td class="p-4 text-gray-400">${m.total_reviews} Reviews</td>
                        </tr>`;
                    });
                    document.getElementById('analyticsTopRatedList').innerHTML = ratedHtml || '<tr><td colspan="3" class="p-4 text-center text-gray-500">No movie reviews logged yet.</td></tr>';

                } catch(e) { console.log(e); }
            }

            async function searchUsers() {
                const query = document.getElementById('userSearchInput').value.trim();
                const res = await fetch(`/api/admin/users/search?q=${encodeURIComponent(query)}`);
                const data = await res.json();
                let html = '';
                
                if (data.users.length === 0) {
                    html = '<tr><td colspan="5" class="text-center p-8 text-gray-500">No matching users found...</td></tr>';
                } else {
                    data.users.forEach(u => {
                        const vipBadge = u.is_vip ? '<span class="px-2 py-0.5 text-xs bg-yellow-500/10 text-yellow-400 font-bold border border-yellow-500/20 rounded-full">👑 VIP</span>' : '<span class="px-2 py-0.5 text-xs bg-gray-500/10 text-gray-400 rounded-full">Free</span>';
                        const banBtn = u.is_banned ? 
                            `<button onclick="userAction(${u.user_id}, 'unban')" class="bg-emerald-600/10 hover:bg-emerald-600/20 border border-emerald-500/20 text-emerald-400 px-3 py-1 rounded-xl transition font-semibold text-xs">Unban</button>` :
                            `<button onclick="userAction(${u.user_id}, 'ban')" class="bg-red-600/10 hover:bg-red-600/20 border border-red-500/20 text-red-400 px-3 py-1 rounded-xl transition font-semibold text-xs">Ban User</button>`;

                        html += `
                        <tr class="border-b border-gray-800 hover:bg-gray-900/40">
                            <td class="p-4">
                                <span class="font-bold block">${u.first_name}</span>
                                <span class="text-xs text-gray-500 block">${u.user_id}</span>
                            </td>
                            <td class="p-4 text-blue-400 font-semibold">${u.coins} Gems</td>
                            <td class="p-4">${vipBadge}</td>
                            <td class="p-4 text-gray-400">${u.refer_count} referrals</td>
                            <td class="p-4 text-right flex gap-2 justify-end">
                                <button onclick="promptCoins(${u.user_id})" class="bg-blue-600/10 hover:bg-blue-600/20 text-blue-400 border border-blue-500/20 px-2 py-1 rounded-xl text-xs transition">Gems</button>
                                <button onclick="promptVip(${u.user_id})" class="bg-yellow-600/10 hover:bg-yellow-600/20 text-yellow-400 border border-yellow-500/20 px-2 py-1 rounded-xl text-xs transition">VIP</button>
                                ${banBtn}
                            </td>
                        </tr>`;
                    });
                }
                document.getElementById('userTableBody').innerHTML = html;
            }

            async function userAction(userId, action, value = 0) {
                const res = await fetch('/api/admin/users/action', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ user_id: userId, action: action, value: value })
                });
                const d = await res.json();
                if (d.ok) searchUsers();
            }

            function promptCoins(userId) {
                const amount = prompt("Enter amount of points to ADD (positive number) or REMOVE (negative number):", "100");
                if (amount && !isNaN(amount)) {
                    const val = parseInt(amount);
                    if (val >= 0) userAction(userId, 'add_coins', val);
                    else userAction(userId, 'remove_coins', Math.abs(val));
                }
            }

            function promptVip(userId) {
                const days = prompt("Enter days of VIP membership to ADD (Type 0 to remove VIP):", "30");
                if (days !== null && !isNaN(days)) {
                    const val = parseInt(days);
                    if (val === 0) userAction(userId, 'remove_vip');
                    else userAction(userId, 'add_vip', val);
                }
            }

            document.getElementById('adminSearch').addEventListener('input', function(e) {
                clearTimeout(searchTimeout);
                searchQuery = e.target.value.trim();
                searchTimeout = setTimeout(() => loadAdminData(1), 500);
            });

            async function loadAdminData(page = 1) {
                currentPage = page;
                document.getElementById('movieTableBody').innerHTML = '<tr><td colspan="4" class="text-center p-8 text-gray-400">Loading...</td></tr>';
                const res = await fetch(`/api/admin/data?page=${currentPage}&q=${encodeURIComponent(searchQuery)}`); 
                const data = await res.json();
                
                let html = '';
                if(data.movies.length === 0) {
                    html = '<tr><td colspan="4" class="text-center p-8 text-gray-400">No movies found.</td></tr>';
                } else {
                    data.movies.forEach(m => {
                        html += `<tr class="border-b border-gray-700 hover:bg-gray-750">
                            <td class="p-4 font-medium">${m._id}</td>
                            <td class="p-4 text-gray-400">${m.clicks} Views</td>
                            <td class="p-4 text-green-400 font-bold">${m.file_count}</td>
                            <td class="p-4 flex gap-2">
                                <button onclick="addViews('${encodeURIComponent(m._id)}')" class="text-yellow-400 bg-yellow-900 px-3 py-1 rounded transition hover:bg-yellow-800">Boost</button>
                                <button onclick="deleteMovie('${encodeURIComponent(m._id)}')" class="text-red-400 bg-red-900 px-3 py-1 rounded transition hover:bg-red-800">Delete</button>
                            </td>
                        </tr>`;
                    });
                }
                document.getElementById('movieTableBody').innerHTML = html;

                let pageHtml = "";
                if(data.total_pages > 1) {
                    pageHtml += `<button ${currentPage === 1 ? 'disabled class="px-4 py-2 bg-gray-700 text-gray-500 rounded"' : 'class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-white" onclick="loadAdminData(' + (currentPage - 1) + ')"'}>Prev</button>`;
                    pageHtml += `<span class="px-4 py-2 font-bold">Page ${currentPage} of ${data.total_pages}</span>`;
                    pageHtml += `<button ${currentPage === data.total_pages ? 'disabled class="px-4 py-2 bg-gray-700 text-gray-500 rounded"' : 'class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded text-white" onclick="loadAdminData(' + (currentPage + 1) + ')"'}>Next</button>`;
                }
                document.getElementById('adminPagination').innerHTML = pageHtml;
            }

            async function deleteMovie(title) {
                if(!confirm('Are you sure you want to delete ALL files for this movie?')) return;
                await fetch('/api/admin/movie/' + title, {method: 'DELETE'}); 
                loadAdminData(currentPage); loadStats();
            }

            async function addViews(title) {
                let amount = prompt("How many views to add?", "1000");
                if(amount && !isNaN(amount)) {
                    await fetch('/api/admin/movie/' + title, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({add_clicks: parseInt(amount)}) });
                    loadAdminData(currentPage); loadStats();
                }
            }

            async function loadAds() {
                const res = await fetch('/api/admin/ads_list');
                const data = await res.json();
                let html = '';
                data.ads.forEach(ad => {
                    let exp = new Date(ad.expires_at).toLocaleString();
                    let subText = ad.subtitle || "N/A";
                    html += `<tr class="border-b border-gray-700 hover:bg-gray-750">
                        <td class="p-4 font-bold text-yellow-400">${ad.title}</td>
                        <td class="p-4 text-gray-300">${subText}</td>
                        <td class="p-4"><a href="${ad.link}" target="_blank" class="text-blue-400 underline">Link</a></td>
                        <td class="p-4">${exp}</td>
                        <td class="p-4"><button onclick="deleteAd('${ad._id}')" class="bg-red-600 text-white px-3 py-1 rounded">Delete</button></td>
                    </tr>`;
                });
                document.getElementById('adsTableBody').innerHTML = html || '<tr><td colspan="5" class="text-center p-8 text-gray-400">No active ads.</td></tr>';
            }

            async function createAdminAd() {
                const payload = {
                    title: document.getElementById('adTitle').value,
                    subtitle: document.getElementById('adSubtitle').value || "দেরি না করে এখনো সবাই নিয়ে নিন",
                    link: document.getElementById('adLink').value,
                    image_url: document.getElementById('adImage').value
                };
                await fetch('/api/admin/ads/create', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
                alert('Ad created successfully!');
                loadAds();
            }

            async function deleteAd(id) {
                if(confirm('Delete this ad?')) {
                    await fetch('/api/admin/ads/' + id, {method: 'DELETE'});
                    loadAds();
                }
            }

            async function loadKeywordList() {
                try {
                    const res = await fetch('/api/admin/keywords');
                    const data = await res.json();
                    let html = '';
                    data.keywords.forEach(kw => {
                        html += `
                        <tr class="border-b border-gray-700 hover:bg-gray-750">
                            <td class="p-4 font-bold text-green-400">${kw.keyword}</td>
                            <td class="p-4 text-gray-300 whitespace-pre-wrap">${kw.reply_message}</td>
                            <td class="p-4"><button onclick="deleteKeyword('${kw.keyword}')" class="bg-red-600 hover:bg-red-500 text-white px-3 py-1 rounded">Delete</button></td>
                        </tr>`;
                    });
                    document.getElementById('keywordsTableBody').innerHTML = html || '<tr><td colspan="3" class="p-4 text-center text-gray-500">No keyword responses.</td></tr>';
                } catch(e) {}
            }

            async function addKeywordReply() {
                const keyword = document.getElementById('kwInput').value.trim();
                const reply = document.getElementById('kwReplyInput').value.trim();
                if(!keyword || !reply) { alert('Enter Keyword and reply!'); return; }
                
                await fetch('/api/admin/keywords', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ keyword: keyword, reply_message: reply })
                });
                document.getElementById('kwInput').value = '';
                document.getElementById('kwReplyInput').value = '';
                loadKeywordList();
            }

            async function deleteKeyword(keyword) {
                if(confirm(`Delete response rule for keyword "${keyword}"?`)) {
                    await fetch(`/api/admin/keywords/${encodeURIComponent(keyword)}`, { method: 'DELETE' });
                    loadKeywordList();
                }
            }

            async function loadAdminRequests() {
                try {
                    const res = await fetch('/api/admin/requests');
                    const data = await res.json();
                    let html = '';
                    data.requests.forEach(req => {
                        let priorityClass = req.is_vip ? "bg-yellow-900 text-yellow-300 border-yellow-700" : "bg-gray-800 text-gray-400 border-gray-700";
                        let selectPending = req.status === 'pending' ? 'selected' : '';
                        let selectProcessing = req.status === 'processing' ? 'selected' : '';
                        let selectUploaded = req.status === 'uploaded' ? 'selected' : '';
                        
                        html += `
                        <tr class="border-b border-gray-700 hover:bg-gray-750">
                            <td class="p-4">
                                <span class="font-bold text-white block">${req.uname}</span>
                                <span class="text-xs text-gray-500 block">${req.user_id}</span>
                            </td>
                            <td class="p-4 font-bold text-blue-400">${req.movie}</td>
                            <td class="p-4"><span class="px-2 py-1 text-xs font-bold border rounded ${priorityClass}">${req.is_vip ? "⭐ VIP Priority" : "Free"}</span></td>
                            <td class="p-4 flex gap-2 items-center">
                                <select onchange="updateRequestStatus('${req._id}', this.value)" class="bg-gray-700 border border-gray-600 rounded px-2 py-1 text-white">
                                    <option value="pending" ${selectPending}>⏳ Pending</option>
                                    <option value="processing" ${selectProcessing}>⚙️ Processing</option>
                                    <option value="uploaded" ${selectUploaded}>✅ Uploaded</option>
                                </select>
                                <button onclick="deleteRequest('${req._id}')" class="bg-red-600 hover:bg-red-500 text-white px-2 py-1 rounded"><i class="fa-solid fa-trash"></i></button>
                            </td>
                        </tr>`;
                    });
                    document.getElementById('requestsTableBody').innerHTML = html || '<tr><td colspan="4" class="text-center p-8 text-gray-400">No requests log.</td></tr>';
                } catch(e) {}
            }

            async function updateRequestStatus(id, newStatus) {
                await fetch(`/api/admin/requests/${id}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({status: newStatus})
                });
                loadAdminRequests();
            }

            async function deleteRequest(id) {
                if(confirm('Delete this request entry?')) {
                    await fetch(`/api/admin/requests/${id}`, { method: 'DELETE' });
                    loadAdminRequests();
                }
            }
            
            loadSysSettings(); loadStats(); loadAnalytics();
            
            setInterval(() => {
                const activeTab = document.querySelector('.admin-tab-content:not(.hidden)');
                if (activeTab && activeTab.id === 'adminTab-dashboard') {
                    loadStats();
                    loadAnalytics();
                }
            }, 10000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/api/admin/stats")
async def admin_stats_api(auth: bool = Depends(verify_admin)):
    user_count = await db.users.count_documents({})
    movie_count = await db.movies.count_documents({})
    total_views = 0
    views_agg = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1)
    if views_agg: total_views = views_agg[0]["total"]
    return {"users": user_count, "movies": movie_count, "views": total_views}

@app.get("/api/admin/data")
async def get_admin_data(page: int = 1, q: str = "", auth: bool = Depends(verify_admin)):
    limit = 20
    skip = (page - 1) * limit
    match_stage = {"title": {"$regex": q, "$options": "i"}} if q else {}
    
    pipeline = [
        {"$match": match_stage},
        {"$group": {"_id": "$title", "clicks": {"$sum": "$clicks"}, "file_count": {"$sum": 1}, "created_at": {"$max": "$created_at"}}}, 
        {"$sort": {"created_at": -1}}, 
        {"$skip": skip}, 
        {"$limit": limit}
    ]
    movies = await db.movies.aggregate(pipeline).to_list(limit)
    
    total_groups = await db.movies.aggregate([{"$match": match_stage}, {"$group": {"_id": "$title"}}, {"$count": "total"}]).to_list(1)
    total_pages = (total_groups[0]["total"] + limit - 1) // limit if total_groups else 0
    
    return {"movies": movies, "total_pages": total_pages}

@app.delete("/api/admin/movie/{title}")
async def delete_movie_api(title: str, auth: bool = Depends(verify_admin)):
    await db.movies.delete_many({"title": title})
    clear_app_cache() 
    return {"ok": True}

@app.put("/api/admin/movie/{title}")
async def edit_movie_api(title: str, data: dict = Body(...), auth: bool = Depends(verify_admin)):
    if add_clicks := data.get("add_clicks"):
        await db.movies.update_many({"title": title}, {"$inc": {"clicks": int(add_clicks)}})
    clear_app_cache() 
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
async def web_ui():
    support_cfg = await db.settings.find_one({"id": "link_support"})
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    
    ad_time_cfg = await db.settings.find_one({"id": "ad_time"})
    ad_wait_seconds = ad_time_cfg['seconds'] if ad_time_cfg else 10
    
    interval_cfg = await db.settings.find_one({"id": "ad_interval"})
    ad_interval = interval_cfg["interval"] if interval_cfg else 3
    
    support_link = support_cfg['url'] if support_cfg else "https://t.me/YourSupportUsername"
    direct_links = dl_cfg.get('links', []) if dl_cfg else []
    dl_json = json.dumps(direct_links)
    
    social_cfg = await db.settings.find_one({"id": "social_links"})
    social_links_dict = social_cfg.get('links', {}) if social_cfg else {}
    social_json = json.dumps(social_links_dict)

    html_code = r"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>BD Viral Link</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            html { -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }
            body { background: #0f172a; font-family: sans-serif; color: #fff; overflow-x: hidden; width: 100%; -webkit-overflow-scrolling: touch; padding-bottom: 80px; } 
            
            header { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 12px 10px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; width: 100%; transform: translateZ(0); will-change: transform; gap: 8px; }
            .logo { font-size: 22px; font-weight: 900; white-space: nowrap; letter-spacing: 1px; }
            .logo span { background: #ef4444; color: #fff; padding: 2px 6px; border-radius: 4px; margin-left: 3px; font-size: 14px; }
            
            .home-btn { background: rgba(59, 130, 246, 0.1); color: #3b82f6; border: 1px solid rgba(59, 130, 246, 0.5); padding: 4px 12px; border-radius: 20px; font-weight: bold; font-size: 11px; cursor: pointer; display: flex; align-items: center; gap: 4px; transition: 0.2s; white-space: nowrap; }
            .home-btn:active { transform: scale(0.95); background: rgba(59, 130, 246, 0.2); }

            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.98); backdrop-filter: blur(15px); border-top: 1px solid #334155; display: flex; justify-content: space-around; align-items: center; padding: 10px 0; z-index: 2000; padding-bottom: calc(10px + env(safe-area-inset-bottom)); }
            .nav-item { display: flex; flex-direction: column; align-items: center; justify-content: center; color: #94a3b8; font-size: 11px; font-weight: bold; cursor: pointer; transition: 0.2s; width: 25%; gap: 4px; }
            .nav-item i { font-size: 20px; transition: transform 0.2s; }
            .nav-item.active { color: #38bdf8; }
            .nav-item.active i { transform: scale(1.15); }
            .nav-item:active { transform: scale(0.9); }
            
            .dropdown-menu { display: none; position: fixed; bottom: 85px; right: 15px; background: rgba(15, 23, 42, 0.98); backdrop-filter: blur(10px); border: 1px solid #334155; border-radius: 12px; overflow: hidden; box-shadow: 0 -5px 25px rgba(0,0,0,0.5); z-index: 2000; width: 250px; animation: slideUp 0.2s ease-out forwards; }
            @keyframes slideUp { 0% { opacity: 0; transform: translateY(15px); } 100% { opacity: 1; transform: translateY(0); } }
            
            .dropdown-menu a { display: flex; align-items: center; gap: 10px; padding: 12px 15px; color: white; text-decoration: none; font-weight: 600; font-size: 14px; cursor: pointer; transition: background 0.2s ease; border-bottom: 1px solid #334155; }
            .dropdown-menu a:hover, .dropdown-menu a:active { background: rgba(51, 65, 85, 0.5); }
            .dropdown-menu a i { font-size: 16px; width: 20px; text-align: center; }
            
            .coin-tag { background: #3b82f6; color: white; font-weight: 900; padding: 2px 8px; border-radius: 10px; margin-left: 2px; font-size: 12px; }
            .vip-tag { background: linear-gradient(45deg, #fbbf24, #f59e0b); color: #000; font-size: 12px; padding: 3px 8px; border-radius: 12px; font-weight: bold; display: none; margin-left:5px; }

            .search-box { padding: 15px; }
            .search-input { width: 100%; padding: 16px; border-radius: 25px; border: none; outline: none; text-align: center; background: #1e293b; color: #fff; font-size: 18px; font-weight: bold; }

            .section-title { padding: 5px 15px 15px; font-size: 20px; font-weight: 900; display: flex; align-items: center; gap: 8px; color:#ff416c; }
            
            .trending-container { display: flex; overflow-x: auto; gap: 15px; padding: 0 15px 20px; scroll-behavior: smooth; scroll-snap-type: x mandatory; }
            .trending-container::-webkit-scrollbar { display: none; }
            .trending-card { min-width: 280px; max-width: 280px; background: transparent; overflow: hidden; cursor: pointer; flex-shrink: 0; position: relative; transition: transform 0.2s; transform: translateZ(0); will-change: transform; scroll-snap-align: start; }
            .trending-card:active { transform: scale(0.98); }

            .ad-carousel-container {
                width: 100%;
                margin: 5px 0 15px 0;
                display: flex;
                flex-direction: column;
                align-items: center;
            }
            .ad-carousel-track {
                display: flex;
                overflow-x: auto;
                scroll-snap-type: x mandatory;
                gap: 12px;
                width: 100%;
                padding: 10px 0;
                scrollbar-width: none;
            }
            .ad-carousel-track::-webkit-scrollbar {
                display: none;
            }
            .ad-carousel-card {
                min-width: 250px;
                max-width: 250px;
                background: #ffffff;
                color: #1e293b;
                border-radius: 20px;
                overflow: hidden;
                display: flex;
                flex-direction: column;
                scroll-snap-align: start;
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2);
                flex-shrink: 0;
                text-decoration: none;
                transition: transform 0.2s;
            }
            .ad-carousel-card:active {
                transform: scale(0.97);
            }
            .ad-carousel-img-wrap {
                width: 100%;
                aspect-ratio: 16/10;
                background: #e2e8f0;
                overflow: hidden;
            }
            .ad-carousel-img-wrap img {
                width: 100%;
                height: 100%;
                object-fit: cover;
            }
            .ad-carousel-body {
                padding: 12px 15px 15px 15px;
                text-align: left;
                display: flex;
                flex-direction: column;
                align-items: flex-start;
                gap: 4px;
                background: #ffffff;
            }
            .ad-carousel-title {
                font-size: 16px;
                font-weight: 800;
                color: #0f172a;
                white-space: nowrap;
                text-overflow: ellipsis;
                overflow: hidden;
                width: 100%;
                line-height: 1.2;
            }
            .ad-carousel-subtitle {
                font-size: 12px;
                color: #64748b;
                white-space: nowrap;
                text-overflow: ellipsis;
                overflow: hidden;
                width: 100%;
                line-height: 1.4;
                margin-bottom: 8px;
            }
            .ad-carousel-btn {
                background: linear-gradient(135deg, #ff4e2a, #ff7300);
                color: #ffffff;
                font-size: 13px;
                font-weight: 800;
                padding: 6px 20px;
                border-radius: 20px;
                border: none;
                outline: none;
                cursor: pointer;
                box-shadow: 0 4px 10px rgba(255, 78, 42, 0.3);
                display: inline-block;
            }
            .ad-carousel-dots {
                display: flex;
                gap: 5px;
                margin-top: 5px;
                justify-content: center;
            }
            .ad-carousel-dot {
                width: 6px;
                height: 6px;
                border-radius: 50%;
                background: #475569;
                transition: background 0.2s, transform 0.2s;
            }
            .ad-carousel-dot.active {
                background: #ff4e2a;
                transform: scale(1.25);
            }

            .grid { padding: 0 15px 20px; display: flex; flex-direction: column; gap: 20px; }
            .card { background: transparent; overflow: hidden; cursor: pointer; transition: transform 0.2s; border-radius: 0; transform: translateZ(0); will-change: transform; }
            .card:active { transform: scale(0.98); }
            
            .post-content { position: relative; padding: 3px; border-radius: 12px; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 200%; }
            .post-content img { width: 100%; aspect-ratio: 16/9; height: auto; object-fit: cover; display: block; border-radius: 10px; }
            
            .card-footer { padding: 12px 5px 0; display: flex; align-items: flex-start; gap: 12px; text-align: left; }
            .channel-logo { width: 40px; height: 40px; border-radius: 50%; background: white; color: #ef4444; border: 1px solid #e5e7eb; display: flex; align-items: center; justify-content: center; font-weight: 900; font-size: 16px; flex-shrink: 0; }
            .title-text { color: #f8fafc; font-size: 16px; font-weight: bold; line-height: 1.4; overflow: hidden; text-overflow: ellipsis; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; margin-top: 2px; }

            .top-badge, .ep-badge, .view-badge { position: absolute; font-weight: bold; padding: 4px 8px; border-radius: 6px; font-size: 11px; z-index: 10; color: white;}
            .top-badge { top: 10px; left: 10px; background: linear-gradient(45deg, #ff0000, #cc0000); }
            .view-badge { bottom: 10px; left: 10px; background: rgba(0,0,0,0.75); }
            .ep-badge { top: 10px; right: 10px; background: #10b981; }

            .pagination { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 10px 15px 30px; flex-wrap: wrap; }
            .page-btn { background: #1e293b; color: #fff; border: 1px solid #334155; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: bold; outline: none; transition: 0.2s;}
            .page-btn:hover { background: #334155; }
            .page-btn.active { background: #f87171; border-color: #f87171; color: white; }

            .community-section { margin: 10px 15px 30px; padding: 15px; background: rgba(30, 41, 59, 0.5); border: 1px solid #334155; border-radius: 16px; backdrop-filter: blur(10px); }
            .social-grid { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
            .social-btn { display: flex; align-items: center; gap: 8px; padding: 10px 15px; border-radius: 12px; font-weight: bold; font-size: 13px; text-decoration: none; transition: transform 0.2s, box-shadow 0.2s; flex-grow: 1; justify-content: center; min-width: 140px; }
            .social-btn:active { transform: scale(0.95); }
            .fb-btn { background: rgba(24, 119, 242, 0.1); color: #1877f2; border: 1px solid rgba(24, 119, 242, 0.3); }
            .yt-btn { background: rgba(255, 0, 0, 0.1); color: #ff0000; border: 1px solid rgba(255, 0, 0, 0.3); }
            .tg-btn { background: rgba(36, 161, 222, 0.1); color: #24A1DE; border: 1px solid rgba(36, 161, 222, 0.3); }

            .developer-credit { margin: 10px 15px 130px; padding: 22px 15px; background: linear-gradient(135deg, rgba(30, 41, 59, 0.8), rgba(15, 23, 42, 0.95)); border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 16px; text-align: center; box-shadow: 0 8px 20px rgba(0, 0, 0, 0.4), 0 0 15px rgba(56, 189, 248, 0.1); backdrop-filter: blur(10px); position: relative; overflow: hidden; }
            .developer-credit::before { content: ''; position: absolute; top: 0; left: -100%; width: 50%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.08), transparent); animation: shine 3s infinite; }
            @keyframes shine { 100% { left: 200%; } }
            .dev-title { font-size: 12px; color: #94a3b8; font-weight: 700; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 5px; }
            .dev-name { font-size: 22px; font-weight: 900; background: linear-gradient(45deg, #00f2fe, #4facfe); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 8px; }
            .dev-desc { font-size: 13.5px; color: #cbd5e1; margin-bottom: 18px; line-height: 1.5; }
            .dev-btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; background: linear-gradient(45deg, #0ea5e9, #2563eb); color: white; padding: 12px 24px; border-radius: 30px; font-size: 15px; font-weight: bold; border: none; cursor: pointer; box-shadow: 0 4px 15px rgba(37, 99, 235, 0.4); transition: 0.2s; position: relative; z-index: 10; }
            .dev-btn:active { transform: scale(0.95); }

            .floating-btn { position: fixed; right: 15px; color: white; width: 48px; height: 48px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 20px; z-index: 500; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); }
            .btn-tg { bottom: 145px; background: linear-gradient(45deg, #24A1DE, #1b7ba8); }
            .btn-req { bottom: 85px; background: linear-gradient(45deg, #10b981, #059669); }

            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: center; justify-content: center; z-index: 3000; backdrop-filter: blur(5px); }
            .modal-content { background: #1e293b; width: 92%; max-width: 400px; padding: 25px; border-radius: 20px; text-align: center; border: 1px solid #334155; max-height: 85vh; overflow-y: auto; position: relative; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: #334155; color: #fff; display: flex; align-items: center; justify-content: center; cursor: pointer; }
            
            .rgb-border { position: relative; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 200%; padding: 4px; border-radius: 14px; margin-bottom: 12px; cursor: pointer; width: 100%; }
            .rgb-inner { display: flex; justify-content: space-between; align-items: center; background: #0f172a; padding: 20px 18px; border-radius: 12px; color: white; font-weight: 900; font-size: 18px; }

            .btn-submit { background: linear-gradient(45deg, #10b981, #059669); color: white; border: none; padding: 15px 20px; border-radius: 12px; font-weight: bold; width: 100%; font-size: 18px; cursor: pointer; }

            .dl-rgb-wrap { position: relative; background: linear-gradient(45deg, #ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); background-size: 200%; padding: 4px; border-radius: 16px; width: 100%; max-width: 350px; margin: auto; }
            .dl-inner-box { background: rgba(15, 23, 42, 0.98); border-radius: 12px; padding: 30px 20px; display: flex; flex-direction: column; align-items: center; gap: 15px; }
            
            .spinner-new { width: 65px; height: 65px; border: 5px solid rgba(255,255,255,0.1); border-left-color: #10b981; border-radius: 50%; animation: spin-fast 1s linear infinite; margin: 0 auto 15px; }
            @keyframes spin-fast { 100% { transform: rotate(360deg); } }
            .big-processing-text { font-size: 26px; font-weight: 900; color: #4ade80; animation: pulse 1.5s infinite; }
            @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
            
            .wheel-slice { position: absolute; width: 50%; height: 50%; transform-origin: 100% 100%; }
            .spin-win-anim { animation: spin-stop-effect 4s cubic-bezier(0.25, 0.1, 0.25, 1) forwards; }
            
            @keyframes spinRing {
                100% { transform: rotate(360deg); }
            }
            @keyframes pulseGlow {
                from { text-shadow: 0 0 10px #38bdf8, 0 0 18px #0ea5e9; transform: scale(1); }
                to { text-shadow: 0 0 20px #38bdf8, 0 0 35px #2563eb, 0 0 45px #2563eb; transform: scale(1.02); }
            }
        </style>
    </head>
    <body onclick="closeMenu(event)">
        <!-- Startup Splash Screen -->
        <div id="startupSplash" style="position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: #0f172a; z-index: 999999; display: flex; flex-direction: column; align-items: center; justify-content: center; opacity: 1; visibility: visible; transition: opacity 0.8s ease, visibility 0.8s ease;">
            <div style="position: relative; width: 160px; height: 160px; display: flex; align-items: center; justify-content: center; margin-bottom: 25px;">
                <div style="position: absolute; width: 100%; height: 100%; border-radius: 50%; background: conic-gradient(#ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); animation: spinRing 3s linear infinite; filter: blur(8px); opacity: 0.85;"></div>
                <div style="position: absolute; width: calc(100% - 8px); height: calc(100% - 8px); border-radius: 50%; background: conic-gradient(#ff0000, #ff7300, #fffb00, #48ff00, #00ffd5, #002bff, #7a00ff, #ff00c8, #ff0000); animation: spinRing 3s linear infinite;"></div>
                
                <div style="position: absolute; width: calc(100% - 16px); height: calc(100% - 16px); background: #1e293b; border-radius: 50%; display: flex; align-items: center; justify-content: center; overflow: hidden; box-shadow: inset 0 0 20px rgba(0,0,0,0.8); z-index: 2;">
                    <div style="font-size: 36px; font-weight: 900; background: linear-gradient(45deg, #0ea5e9, #38bdf8); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">MZ</div>
                </div>
            </div>
            <h1 id="splashWelcomeText" style="font-size: 30px; font-weight: 900; color: #fff; text-shadow: 0 0 10px #38bdf8, 0 0 20px #0ea5e9; animation: pulseGlow 1.5s ease-in-out infinite alternate; text-align: center; margin-bottom: 12px; letter-spacing: 1px;">BD Viral Link</h1>
            <p style="font-size: 13.5px; font-weight: 700; color: #94a3b8; letter-spacing: 2px; text-transform: uppercase;">Loading Premium Experience...</p>
        </div>

        <header>
            <div class="logo">BD Viral<span>Link</span></div>
            <button onclick="goHome()" class="home-btn"><i class="fa-solid fa-house"></i> Home Page</button>
        </header>
        
        <div id="dropdownMenu" class="dropdown-menu">
            <div style="padding: 12px 15px; border-bottom: 1px solid #334155; display: flex; align-items: center; gap: 12px;">
                <div style="width: 40px; height: 40px; background: #3b82f6; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: bold; font-size: 18px; flex-shrink: 0;">
                    <i class="fa-solid fa-user"></i>
                </div>
                <div style="flex-grow: 1; text-align: left;">
                    <div style="font-size: 15px; font-weight: bold; color: white; line-height: 1.2;" id="menuUname">Guest</div>
                    <div style="font-size: 12px; color: #94a3b8; margin-top: 2px;" id="menuStatus">Free User</div>
                </div>
                <div style="text-align: right;">
                    <div id="coinDisplay" class="coin-tag" style="display:inline-block; margin-bottom:4px;"><i class="fa-solid fa-gem"></i> 0</div>
                    <div id="vipBadge" class="vip-tag" style="display:inline-block;">VIP</div>
                </div>
            </div>
            
            <a onclick="openReferModal()"><i class="fa-solid fa-share-nodes text-blue-400"></i> Refer & Earn</a>
            <a onclick="openRequestsTrackerModal()"><i class="fa-solid fa-code-pull-request text-green-400"></i> Request Movie & Track</a>
            <a onclick="openWatchlistModal()"><i class="fa-solid fa-bookmark text-red-400"></i> My Watchlist</a>
            <a onclick="openAdCampModal()"><i class="fa-solid fa-bullhorn text-yellow-400"></i> Promote Channel/Web</a>
            <div style="height: 1px; background: #334155; margin: 4px 0;"></div>
            <a onclick="tg.showAlert(`How to Download:\n1. Click the Download button.\n2. Wait for ${AD_WAIT_TIME} seconds on the opened link.\n3. Return to the mini app and the video will be automatically sent to your bot inbox!`)"><i class="fa-solid fa-circle-question text-red-400"></i> How to Download</a>
            <a onclick="window.open('{{SUPPORT_LINK}}')"><i class="fa-brands fa-telegram text-blue-400"></i> Support / Contact</a>
            
            <a onclick="window.open(window.location.origin + '/admin', '_blank')" id="adminMenuBtn" style="display: none; color: #ef4444;"><i class="fa-solid fa-screwdriver-wrench"></i> Admin Panel</a>
        </div>

        <div class="search-box">
            <input type="text" id="searchInput" class="search-input" placeholder="🔍 Search Movies or Series...">
        </div>

        <div id="trendingWrapper">
            <div class="section-title"><i class="fa-solid fa-bolt text-yellow-400"></i>Trending now</div>
            <div class="trending-container" id="trendingGrid"></div>
        </div>

        <div class="section-title" id="recentTitle"><i class="fa-solid fa-clock-rotate-left text-blue-400"></i> Recently Added</div>
        <div class="grid" id="movieGrid"></div>
        <div class="pagination" id="paginationBox"></div>
        
        <div id="communityBox"></div>

        <div class="developer-credit">
            <div class="dev-title"><i class="fa-solid fa-laptop-code"></i> Developed & Deployed By</div>
            <div class="dev-name">Bot Developer</div>
            <div class="dev-desc">Do you want to create a high-quality premium movie bot for your channel or group? Contact us today.</div>
            <button class="dev-btn" onclick="window.open('https://t.me/ProBotDeveloperBot', '_blank')">
                <i class="fa-brands fa-telegram"></i> Contact Developer
            </button>
        </div>

        <div class="floating-btn btn-tg" onclick="window.open('https://t.me/MovieeBD')"><i class="fa-brands fa-telegram"></i></div>
        <div class="floating-btn btn-req" onclick="openRequestsTrackerModal()"><i class="fa-solid fa-code-pull-request"></i></div>

        <div class="floating-btn btn-tg" onclick="window.open('https://t.me/MovieeBD')"><i class="fa-brands fa-telegram"></i></div>
        <div class="floating-btn btn-req" onclick="openRequestsTrackerModal()"><i class="fa-solid fa-code-pull-request"></i></div>

        <div class="bottom-nav">
            <div class="nav-item active" id="navHome" onclick="goHome()">
                <i class="fa-solid fa-house"></i>
                <span>Home</span>
            </div>
            <div class="nav-item" id="navSearch" onclick="focusSearch()">
                <i class="fa-solid fa-magnifying-glass"></i>
                <span>Search</span>
            </div>
            <div class="nav-item" id="navVip" onclick="openVipModal()">
                <i class="fa-solid fa-gem"></i>
                <span>Premium</span>
            </div>
            <div class="nav-item" id="navProfile" onclick="toggleMenu(event)">
                <i class="fa-solid fa-user"></i>
                <span>Profile</span>
            </div>
        </div>

        <div id="qualityModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('qualityModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 id="modalTitle" style="color:#38bdf8; margin-bottom: 5px; font-size: 22px; font-weight:900;">Movie Title</h2>
                
                <div style="margin-bottom: 15px; display: flex; justify-content: center; gap: 10px;">
                    <button id="bookmarkBtn" class="home-btn" style="border-radius: 12px; font-size: 13px;" onclick="toggleWatchlist()"></button>
                    <span id="avgRatingBadge" style="background: rgba(251,191,36,0.1); color: #fbbf24; border: 1px solid rgba(251,191,36,0.4); padding: 4px 12px; border-radius: 12px; font-weight: bold; font-size: 13px; display: flex; align-items: center; gap: 4px;"><i class="fa-solid fa-star"></i> <span id="avgRatingVal">0.0</span></span>
                </div>

                <div style="background: rgba(15, 23, 42, 0.9); border-left: 4px solid #f59e0b; padding: 12px; border-radius: 8px; text-align: left; margin-bottom: 15px;">
                    <p style="color:#f59e0b; font-weight:bold; font-size: 14px; margin-bottom: 5px;"><i class="fa-solid fa-circle-info"></i> How to Download?</p>
                    <p style="color:#cbd5e1; font-size: 12.5px; line-height: 1.5;">1. Click the download button below.<br>2. A new page will open, wait there for <b>{{AD_TIME}} seconds</b>.<br>3. Return to the mini app and the video will be automatically sent to your bot inbox!</p>
                </div>

                <div id="qualityList" style="display: flex; flex-direction: column; gap: 8px; margin-bottom: 20px;"></div>

                <div style="border-top: 1px solid #334155; padding-top: 15px; text-align: left;">
                    <h3 style="font-size: 16px; font-weight: bold; margin-bottom: 10px; color: #cbd5e1;"><i class="fa-solid fa-comments text-blue-400"></i> Reviews & Ratings</h3>
                    
                    <div style="background: rgba(15, 23, 42, 0.5); padding: 12px; border-radius: 10px; border: 1px solid #334155; margin-bottom: 15px;">
                        <p style="font-size: 12px; color: #94a3b8; margin-bottom: 6px; font-weight:bold;">Your Rating:</p>
                        <div style="display: flex; gap: 6px; font-size: 20px; color: #475569; cursor: pointer; margin-bottom: 10px;" id="starRatingSelect">
                            <i class="fa-solid fa-star" onclick="setSelectRating(1)"></i>
                            <i class="fa-solid fa-star" onclick="setSelectRating(2)"></i>
                            <i class="fa-solid fa-star" onclick="setSelectRating(3)"></i>
                            <i class="fa-solid fa-star" onclick="setSelectRating(4)"></i>
                            <i class="fa-solid fa-star" onclick="setSelectRating(5)"></i>
                        </div>
                        <textarea id="reviewText" style="width: 100%; height: 50px; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: white; padding: 8px; font-size: 13px; outline: none; resize: none; margin-bottom: 8px;" placeholder="Write a review..."></textarea>
                        <button class="btn-submit" style="font-size: 13px; padding: 6px 12px; width: auto;" onclick="submitReview()">Submit Review</button>
                    </div>

                    <div id="modalReviewsList" style="max-height: 150px; overflow-y: auto; display: flex; flex-direction: column; gap: 8px;"></div>
                </div>
            </div>
        </div>

        <div id="directLinkModal" class="modal">
            <div class="modal-content" style="background: transparent; border: none; padding: 0;">
                <div class="close-icon" onclick="document.getElementById('directLinkModal').style.display='none'" style="top: -15px; right: 5px; z-index: 1000;"><i class="fa-solid fa-xmark"></i></div>
                <div class="dl-rgb-wrap">
                    <div class="dl-inner-box">
                        <h2 style="color: #4ade80; font-size: 24px; font-weight: 900;"><i class="fa-solid fa-unlock-keyhole"></i> Unlock Video</h2>
                        <p id="dlDescText" style="color: #cbd5e1; font-size: 15px; font-weight: 600; text-align:center;">
                            To unlock this file, wait <b>{{AD_TIME}} seconds</b> on the link below.
                        </p>
                        <button id="dlClickBtn" class="btn-submit" style="background: linear-gradient(45deg, #ef4444, #f97316); margin-top: 10px;" onclick="executeDirectLink()">🔗 Click Here (Open Link)</button>
                    </div>
                </div>
            </div>
        </div>

        <div id="vipModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('vipModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                
                <div style="display: flex; gap: 5px; margin-bottom: 15px; border-bottom: 1px solid #334155; padding-bottom: 8px;">
                    <button class="cat-btn active" id="btnTabVip" onclick="switchVipModalTab('vip')">💎 VIP & Buy</button>
                    <button class="cat-btn" id="btnTabSpin" onclick="switchVipModalTab('spin')">🎡 Lucky Spin</button>
                </div>

                <div id="modalTabVipContent">
                    <h2 style="color:#fbbf24; font-size: 22px; margin-bottom:12px;"><i class="fa-solid fa-gem"></i> Premium & Points</h2>
                    <div style="background: rgba(15, 23, 42, 0.9); border: 1px solid #10b981; padding: 12px; border-radius: 12px; margin-bottom: 15px; text-align: left;">
                        <p style="color:#4ade80; font-size: 14px; font-weight:bold; margin-bottom: 6px;"><i class="fa-solid fa-star"></i> VIP Benefits:</p>
                        <ul style="color:#cbd5e1; font-size: 12px; line-height: 1.5; padding-left: 15px;">
                            <li style="margin-bottom: 3px;"><b>Zero Ads:</b> Direct video unlock. No waiting.</li>
                            <li style="margin-bottom: 3px;"><b>Priority Requests:</b> Admins prioritize your movies.</li>
                            <li><b>Exclusive Badge:</b> Golden VIP profile badge.</li>
                        </ul>
                    </div>

                    <div style="background: rgba(15, 23, 42, 0.9); border: 1px solid #3b82f6; padding: 12px; border-radius: 12px; margin-bottom: 15px;">
                        <p style="color:#94a3b8; font-size: 13px; font-weight:bold;">Your Current Points:</p>
                        <h1 style="color:#38bdf8; font-size: 30px; font-weight:900; margin: 3px 0;"><span id="modalCoinText">0</span> <i class="fa-solid fa-gem"></i></h1>
                        <p style="color:#cbd5e1; font-size: 11px;">(<span id="vipDaysText">1</span> Days VIP = <span id="vipCostText">30</span> Points)</p>
                    </div>
                    
                    <button id="dailyCheckinBtn" class="btn-submit" style="background: linear-gradient(45deg, #10b981, #3b82f6); margin-bottom: 12px;" onclick="claimDailyCheckin()">
                        📅 Daily Check-in (+5 Points)
                    </button>

                    <button class="btn-submit" style="background: linear-gradient(45deg, #3b82f6, #2563eb); margin-bottom: 12px;" onclick="window.open('{{SUPPORT_LINK}}')">
                        <i class="fa-brands fa-telegram"></i> Buy Points from Admin
                    </button>

                    <button id="coinAdBtn" class="btn-submit" style="background: linear-gradient(45deg, #ef4444, #f97316); margin-bottom: 12px;" onclick="executeCoinAd()">
                        <i class="fa-solid fa-play"></i> Watch Ad & Get 5 Points
                    </button>
                    
                    <button class="btn-submit" style="background: linear-gradient(45deg, #10b981, #059669);" onclick="buyVipWithCoins()">
                        <i class="fa-solid fa-crown"></i> Get <span id="btnVipDays">1</span> Days VIP for <span id="btnVipCost">30</span> Points
                    </button>
                </div>

                <div id="modalTabSpinContent" style="display: none;">
                    <h2 style="color:#f59e0b; font-size: 22px; margin-bottom:10px;"><i class="fa-solid fa-circle-notch"></i> Lucky Spin Wheel</h2>
                    <p style="color:#94a3b8; font-size:12px; margin-bottom:15px;">Spend <b>5 Points</b> to spin the wheel and win huge points or VIP!</p>
                    
                    <div style="position: relative; width: 180px; height: 180px; margin: auto; border: 6px solid #334155; border-radius: 50%; overflow: hidden; background: #0f172a;" id="wheelOuter">
                        <div style="position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 45px; height: 45px; background: white; border-radius: 50%; border: 4px solid #334155; z-index: 10; display:flex; align-items:center; justify-content:center; color:#0f172a; font-size:18px;"><i class="fa-solid fa-arrow-up"></i></div>
                        <div id="wheelInner" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; border-radius: 50%; background: conic-gradient(#ef4444 0deg 60deg, #3b82f6 60deg 120deg, #10b981 120deg 180deg, #f59e0b 180deg 240deg, #8b5cf6 240deg 300deg, #ec4899 300deg 360deg); transition: transform 4s cubic-bezier(0.25, 0.1, 0.25, 1);"></div>
                    </div>

                    <button id="spinBtn" class="btn-submit" style="background: linear-gradient(45deg, #f59e0b, #ef4444); margin-top: 20px;" onclick="spinWheel()">
                        🎡 Spin (Cost: 5 Points)
                    </button>
                </div>
            </div>
        </div>

        <div id="referModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('referModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <i class="fa-solid fa-share-nodes" style="font-size:60px; color:#38bdf8;"></i>
                <h2 style="margin:15px 0; color:white; font-size: 24px;">Refer & Earn</h2>
                <p style="color:#cbd5e1; font-size:15px; margin-bottom:15px;">Get <b>10 Points</b> for each successful referral!</p>
                <div style="background:#0f172a; padding:15px; border:1px dashed #3b82f6; margin-bottom:15px; word-break:break-all;" id="refLinkText">...</div>
                <button class="btn-submit" onclick="copyReferLink()">Copy Link</button>
            </div>
        </div>
        
        <div id="watchlistModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('watchlistModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#38bdf8; font-size: 22px; margin-bottom:15px;"><i class="fa-solid fa-bookmark"></i> My Watchlist</h2>
                <div id="watchlistModalList" class="grid" style="padding:0; max-height: 60vh; overflow-y:auto; gap: 15px;">
                    <p style="color: #94a3b8;">Loading watchlist...</p>
                </div>
            </div>
        </div>

        <div id="requestsTrackerModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('requestsTrackerModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#10b981; font-size: 22px; margin-bottom:10px;"><i class="fa-solid fa-code-pull-request"></i> Movie Request Status</h2>
                <p style="color:#cbd5e1; font-size:13px; margin-bottom:15px;">Submit and track requested movies!</p>
                
                <div style="display:flex; gap:10px; margin-bottom: 20px;">
                    <input type="text" id="reqTrackerInput" class="search-input" style="border-radius:12px; text-align:left; padding:10px 15px; font-size:15px;" placeholder="Enter Movie/Series name...">
                    <button class="btn-submit" style="width: auto; padding:0 20px; font-size:14px;" onclick="submitReqTracker()">Request</button>
                </div>

                <div id="requestsTrackerList" style="text-align: left; display: flex; flex-direction: column; gap: 12px; max-height: 45vh; overflow-y: auto;"></div>
            </div>
        </div>

        <div id="adCampModal" class="modal">
            <div class="modal-content">
                <div class="close-icon" onclick="document.getElementById('adCampModal').style.display='none'"><i class="fa-solid fa-xmark"></i></div>
                <h2 style="color:#fcd34d; font-size: 22px; margin-bottom:10px;"><i class="fa-solid fa-bullhorn"></i> Promote Channel</h2>
                <p style="color:#cbd5e1; font-size:13px; margin-bottom:15px;">Run your advertisement in front of thousands of users!</p>
                
                <input type="text" id="campTitle" class="search-input" style="border-radius:10px; margin-bottom:10px; font-size:15px;" placeholder="Ad Title">
                <input type="text" id="campSubtitle" class="search-input" style="border-radius:10px; margin-bottom:10px; font-size:15px;" placeholder="Ad Subtitle">
                <input type="url" id="campLink" class="search-input" style="border-radius:10px; margin-bottom:10px; font-size:15px;" placeholder="https://t.me/yourlink">
                <input type="url" id="campImg" class="search-input" style="border-radius:10px; margin-bottom:15px; font-size:15px;" placeholder="Image URL (Optional)">
                
                <select id="campPackage" class="search-input" style="border-radius:10px; margin-bottom:15px; font-size:15px; background:#1e293b; color:white; text-align:left;">
                    <option value="1">1 Day Campaign - 500 Points</option>
                    <option value="3">3 Days Campaign - 1200 Points</option>
                    <option value="7">7 Days Campaign - 2500 Points</option>
                </select>
                
                <button id="campBtn" class="btn-submit" style="background: linear-gradient(45deg, #f59e0b, #d97706);" onclick="submitAdCampaign()">
                    Pay Points & Start
                </button>
            </div>
        </div>

        <script>
            let tg = window.Telegram.WebApp; tg.expand();
            const DIRECT_LINKS = {{DIRECT_LINKS}};
            const SOCIAL_LINKS = {{SOCIAL_LINKS}};
            const INIT_DATA = tg.initData || "";
            const BOT_UNAME = "{{BOT_USER}}";
            const AD_WAIT_TIME = {{AD_TIME}}; 
            const AD_INTERVAL = {{AD_INTERVAL}};
            
            let uid = tg.initDataUnsafe?.user?.id || 0;
            let isUserVip = false;
            let userCoins = 0;
            let loadedMovies = {}; 
            let currentPage = 1; 
            let searchQuery = "";
            let autoScrollInterval;
            let activeAds = [];
            
            let currentSelectRating = 0;
            let isCurrentMovieBookmarked = false;

            function setNavActive(index) {
                const items = document.querySelectorAll('.nav-item');
                items.forEach((item, i) => {
                    if(i === index) item.classList.add('active');
                    else item.classList.remove('active');
                });
            }

            async function fetchUserInfo() {
                try {
                    const res = await fetch('/api/user/' + uid);
                    const data = await res.json();
                    isUserVip = data.vip;
                    userCoins = data.coins || 0;
                    
                    const vCost = data.vip_cost || 30;
                    const vDays = data.vip_days || 1;

                    document.getElementById('vipDaysText').innerText = vDays;
                    document.getElementById('vipCostText').innerText = vCost;
                    document.getElementById('btnVipDays').innerText = vDays;
                    document.getElementById('btnVipCost').innerText = vCost;

                    let firstName = tg.initDataUnsafe?.user?.first_name || 'Guest';
                    document.getElementById('menuUname').innerText = firstName;
                    
                    document.getElementById('coinDisplay').innerHTML = `<i class="fa-solid fa-gem"></i> ${userCoins}`;
                    document.getElementById('modalCoinText').innerText = userCoins;
                    
                    if(isUserVip) {
                        document.getElementById('vipBadge').style.display = 'inline-block';
                        document.getElementById('menuStatus').innerText = '👑 VIP User';
                        document.getElementById('menuStatus').style.color = '#fbbf24';
                    } else {
                        document.getElementById('vipBadge').style.display = 'none';
                        document.getElementById('menuStatus').innerText = 'Free User';
                        document.getElementById('menuStatus').style.color = '#94a3b8';
                    }
                    
                    if(data.admin) {
                        document.getElementById('adminMenuBtn').style.display = 'flex';
                    }

                    document.getElementById('refLinkText').innerText = `https://t.me/${BOT_UNAME}?start=ref_${uid}`;
                } catch(e) {}
            }

            async function fetchActiveAds() {
                try {
                    const res = await fetch('/api/ads/active');
                    activeAds = await res.json();
                } catch(e) {}
            }

            function getAdCarouselHTML(indexId) {
                if(activeAds.length === 0) return '';
                let sliderId = "slider_" + indexId;
                
                let adCards = activeAds.map(ad => {
                    let imgHtml = ad.image_url ? `<img src="${ad.image_url}" onerror="this.src='https://via.placeholder.com/640x360?text=No+Image'">` : `<div style="width:100%; height:100%; display:flex; align-items:center; justify-content:center; background:#cbd5e1;"><i class="fa-solid fa-bullhorn text-slate-400" style="font-size:40px;"></i></div>`;
                    let subText = ad.subtitle || "দেরি না করে এখনো সবাই নিয়ে নিন";
                    return `
                    <div class="ad-carousel-card" onclick="window.open('${ad.link}', '_blank')">
                        <div class="ad-carousel-img-wrap">
                            ${imgHtml}
                        </div>
                        <div class="ad-carousel-body">
                            <div class="ad-carousel-title">${ad.title}</div>
                            <div class="ad-carousel-subtitle">${subText}</div>
                            <button class="ad-carousel-btn">Click Now</button>
                        </div>
                    </div>`;
                }).join('');

                let dotsHtml = activeAds.map((_, dotIdx) => {
                    return `<span class="ad-carousel-dot ${dotIdx === 0 ? 'active' : ''}" id="dot_${sliderId}_${dotIdx}"></span>`;
                }).join('');

                return `
                <div class="ad-carousel-container">
                    <div class="ad-carousel-track" id="track_${sliderId}" onscroll="syncAdDots('${sliderId}', ${activeAds.length})">
                        ${adCards}
                    </div>
                    <div class="ad-carousel-dots">
                        ${dotsHtml}
                    </div>
                </div>`;
            }

            function syncAdDots(sliderId, totalAds) {
                const track = document.getElementById('track_' + sliderId);
                if(!track) return;
                let scrollPos = track.scrollLeft;
                let activeIdx = Math.round(scrollPos / 262);
                
                if (activeIdx >= totalAds) activeIdx = totalAds - 1;
                if (activeIdx < 0) activeIdx = 0;

                for (let i = 0; i < totalAds; i++) {
                    const dot = document.getElementById(`dot_${sliderId}_${i}`);
                    if (dot) {
                        if (i === activeIdx) dot.classList.add('active');
                        else dot.classList.remove('active');
                    }
                }
            }

            function toggleMenu(e) { 
                e.stopPropagation(); 
                setNavActive(3);
                const m = document.getElementById('dropdownMenu'); 
                m.style.display = m.style.display === 'block' ? 'none' : 'block'; 
            }
            
            function closeMenu() { 
                document.getElementById('dropdownMenu').style.display = 'none'; 
            }
            
            function goHome() { 
                setNavActive(0);
                document.getElementById('searchInput').value = ""; 
                searchQuery = ""; 
                
                document.getElementById('trendingWrapper').style.display = 'block';
                loadTrending();
                loadMovies(1); 
                closeMenu(); 
                window.scrollTo({ top: 0, behavior: 'smooth' }); 
            }
            
            function focusSearch() {
                setNavActive(1);
                closeMenu();
                window.scrollTo({ top: 0, behavior: 'smooth' });
                setTimeout(() => document.getElementById('searchInput').focus(), 300);
            }
            
            function openVipModal() { 
                setNavActive(2);
                switchVipModalTab('vip');
                document.getElementById('vipModal').style.display = 'flex'; 
                history.pushState({modal: 'vipModal'}, "");
                checkAndToggleTelegramBackButton();
                closeMenu(); 
            }

            function switchVipModalTab(tab) {
                document.getElementById('modalTabVipContent').style.display = tab === 'vip' ? 'block' : 'none';
                document.getElementById('modalTabSpinContent').style.display = tab === 'spin' ? 'block' : 'none';
                
                document.getElementById('btnTabVip').className = tab === 'vip' ? 'cat-btn active' : 'cat-btn';
                document.getElementById('btnTabSpin').className = tab === 'spin' ? 'cat-btn active' : 'cat-btn';
            }

            function openReferModal() { 
                document.getElementById('referModal').style.display = 'flex'; 
                history.pushState({modal: 'referModal'}, "");
                checkAndToggleTelegramBackButton();
                closeMenu(); 
            }
            
            function copyReferLink() { navigator.clipboard.writeText(document.getElementById('refLinkText').innerText); tg.showAlert("✅ Copied!"); }
            
            function openWatchlistModal() {
                document.getElementById('watchlistModal').style.display = 'flex';
                history.pushState({modal: 'watchlistModal'}, "");
                checkAndToggleTelegramBackButton();
                closeMenu();
                renderWatchlist();
            }

            async function renderWatchlist() {
                try {
                    const res = await fetch(`/api/watchlist/list/${uid}`);
                    const data = await res.json();
                    let html = '';
                    if (!data.watchlist || data.watchlist.length === 0) {
                        html = '<p style="color: #cbd5e1; text-align:center; padding: 20px;">Your Watchlist is empty!</p>';
                    } else {
                        data.watchlist.forEach(m => {
                            loadedMovies[m.title] = {
                                _id: m.title,
                                photo_id: m.photo_id,
                                files: m.files,
                                clicks: m.clicks || 0
                            };
                            
                            html += `
                            <div class="card" onclick="openQualityModal(this)" data-title="${encodeURIComponent(m.title)}">
                                <div class="post-content">
                                    <img src="/api/image/${m.photo_id}" loading="lazy" onerror="this.src='https://via.placeholder.com/640x360?text=No+Image'">
                                    <div class="ep-badge"><i class="fa-solid fa-bookmark text-yellow-400"></i> Saved</div>
                                </div>
                                <div class="card-footer">
                                    <div class="channel-logo">MB</div>
                                    <div class="title-text">${m.title}</div>
                                </div>
                            </div>`;
                        });
                    }
                    document.getElementById('watchlistModalList').innerHTML = html;
                } catch(e) {
                    console.error("Watchlist render error:", e);
                }
            }

            async function toggleWatchlist() {
                const title = document.getElementById('modalTitle').innerText;
                let endpoint = isCurrentMovieBookmarked ? '/api/watchlist/remove' : '/api/watchlist/add';
                try {
                    const res = await fetch(endpoint, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ uid: uid, title: title, initData: INIT_DATA })
                    });
                    const d = await res.json();
                    if (d.ok) {
                        isCurrentMovieBookmarked = !isCurrentMovieBookmarked;
                        updateBookmarkButtonUI();
                        tg.showAlert(isCurrentMovieBookmarked ? "💾 Added to Watchlist!" : "❌ Removed from Watchlist!");
                    }
                } catch(e) {}
            }

            function updateBookmarkButtonUI() {
                const btn = document.getElementById('bookmarkBtn');
                if (isCurrentMovieBookmarked) {
                    btn.innerHTML = '<i class="fa-solid fa-bookmark text-yellow-400"></i> Saved';
                    btn.style.background = 'rgba(250,204,21,0.1)';
                    btn.style.borderColor = 'rgba(250,204,21,0.4)';
                } else {
                    btn.innerHTML = '<i class="fa-regular fa-bookmark"></i> Save Later';
                    btn.style.background = 'rgba(59, 130, 246, 0.1)';
                    btn.style.borderColor = 'rgba(59, 130, 246, 0.5)';
                }
            }

            function setSelectRating(r) {
                currentSelectRating = r;
                const stars = document.querySelectorAll('#starRatingSelect i');
                stars.forEach((star, index) => {
                    if (index < r) {
                        star.className = "fa-solid fa-star text-yellow-400";
                    } else {
                        star.className = "fa-solid fa-star text-gray-600";
                    }
                });
            }

            async function submitReview() {
                const title = document.getElementById('modalTitle').innerText;
                const rText = document.getElementById('reviewText').value.trim();
                const uname = tg.initDataUnsafe?.user?.first_name || 'Guest';

                if (currentSelectRating === 0) { tg.showAlert("Please select a star rating!"); return; }
                if (!rText) { tg.showAlert("Please write a review message!"); return; }

                try {
                    const res = await fetch('/api/reviews/add', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            uid: uid,
                            uname: uname,
                            title: title,
                            rating: currentSelectRating,
                            review: rText,
                            initData: INIT_DATA
                        })
                    });
                    const data = await res.json();
                    if (data.ok) {
                        tg.showAlert("🎉 Review submitted successfully!");
                        document.getElementById('reviewText').value = '';
                        setSelectRating(0);
                        loadReviews(title);
                    }
                } catch(e) {}
            }

            async function loadReviews(title) {
                try {
                    const res = await fetch(`/api/reviews/get/${encodeURIComponent(title)}`);
                    const data = await res.json();
                    
                    document.getElementById('avgRatingVal').innerText = data.avg_rating > 0 ? data.avg_rating.toFixed(1) : '0.0';
                    
                    let html = '';
                    data.reviews.forEach(r => {
                        let starsHtml = '';
                        for(let i=1; i<=5; i++) {
                            starsHtml += i <= r.rating ? '<i class="fa-solid fa-star text-yellow-400 text-xs"></i>' : '<i class="fa-solid fa-star text-gray-700 text-xs"></i>';
                        }
                        html += `
                        <div style="background: rgba(15, 23, 42, 0.4); padding: 10px; border-radius: 8px; border: 1px solid #334155;">
                            <div style="display:flex; justify-content:space-between; margin-bottom: 4px;">
                                <span style="font-weight:bold; font-size:12px; color:#cbd5e1;">${r.uname}</span>
                                <div>${starsHtml}</div>
                            </div>
                            <p style="font-size:12px; color:#94a3b8; line-height:1.4;">${r.review}</p>
                        </div>`;
                    });
                    document.getElementById('modalReviewsList').innerHTML = html || '<p style="color: #64748b; font-size: 12px;">No reviews yet. Be the first to review!</p>';
                } catch(e) {}
            }

            async function claimDailyCheckin() {
                try {
                    const res = await fetch('/api/gamification/daily_checkin', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ uid: uid, initData: INIT_DATA })
                    });
                    const d = await res.json();
                    if (d.ok) {
                        tg.showAlert(`🎉 Checked-in Successfully! You received +5 Points.`);
                        fetchUserInfo();
                    } else {
                        tg.showAlert(`⚠️ ${d.msg}`);
                    }
                } catch(e) {}
            }

            let isSpinning = false;
            async function spinWheel() {
                if (isSpinning) return;
                try {
                    const res = await fetch('/api/gamification/spin', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ uid: uid, initData: INIT_DATA })
                    });
                    const data = await res.json();
                    if (!data.ok) {
                        tg.showAlert(`⚠️ ${data.msg}`);
                        return;
                    }

                    isSpinning = true;
                    const inner = document.getElementById('wheelInner');
                    
                    const degMap = {
                        0: 25,   
                        2: 75,   
                        5: 125,  
                        10: 175, 
                        20: 225, 
                        50: 275, 
                        vip: 325 
                    };

                    let prizeKey = data.reward.type === 'points' ? data.reward.amount : 'vip';
                    let targetDeg = degMap[prizeKey] || 25;
                    let extraRotations = 5 * 360; 
                    let finalRotation = extraRotations + (360 - targetDeg);

                    inner.style.transform = `rotate(${finalRotation}deg)`;

                    setTimeout(() => {
                        tg.showAlert(data.msg);
                        isSpinning = false;
                        inner.style.transition = 'none';
                        inner.style.transform = `rotate(${360 - targetDeg}deg)`;
                        setTimeout(() => { inner.style.transition = 'transform 4s cubic-bezier(0.25, 0.1, 0.25, 1)'; }, 50);
                        fetchUserInfo();
                    }, 4100);

                } catch(e) { isSpinning = false; }
            }

            function openRequestsTrackerModal() {
                document.getElementById('requestsTrackerModal').style.display = 'flex';
                history.pushState({modal: 'requestsTrackerModal'}, "");
                checkAndToggleTelegramBackButton();
                closeMenu();
                renderRequestsTracker();
            }

            async function submitReqTracker() {
                const val = document.getElementById('reqTrackerInput').value.trim();
                if (!val) return;
                const uname = tg.initDataUnsafe?.user?.first_name || 'Guest';

                try {
                    await fetch('/api/request', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ uid: uid, uname: uname, movie: val, initData: INIT_DATA })
                    });
                    document.getElementById('reqTrackerInput').value = '';
                    tg.showAlert('🎉 Request successfully queued!');
                    renderRequestsTracker();
                } catch(e) {}
            }

            async function renderRequestsTracker() {
                try {
                    const res = await fetch(`/api/requests/user_list/${uid}`);
                    const d = await res.json();
                    let html = '';
                    d.requests.forEach(req => {
                        let statusText = req.status === 'pending' ? '⏳ Pending Review' : req.status === 'processing' ? '⚙️ Processing Movie' : '✅ Uploaded successfully!';
                        let pct = req.status === 'pending' ? 30 : req.status === 'processing' ? 70 : 100;
                        let barColor = req.status === 'pending' ? '#f59e0b' : req.status === 'processing' ? '#3b82f6' : '#10b981';
                        
                        html += `
                        <div style="background: rgba(30,41,59,0.5); padding: 15px; border-radius: 12px; border:1px solid #334155;">
                            <div style="display:flex; justify-content:space-between; margin-bottom: 6px;">
                                <span style="font-weight:bold; color:white;">${req.movie}</span>
                                <span style="font-size:11px; font-weight:bold; color:${barColor};">${statusText}</span>
                            </div>
                            <div style="w-full bg-gray-700 h-2 rounded-full overflow-hidden">
                                <div style="height:100%; width:${pct}%; background:${barColor}; border-radius:10px;"></div>
                            </div>
                        </div>`;
                    });
                    document.getElementById('requestsTrackerList').innerHTML = html || '<p style="color: #64748b; text-align:center;">You have not made any movie requests yet.</p>';
                } catch(e) {}
            }

            function openAdCampModal() {
                document.getElementById('adCampModal').style.display = 'flex';
                history.pushState({modal: 'adCampModal'}, "");
                checkAndToggleTelegramBackButton();
                closeMenu();
            }

            async function submitAdCampaign() {
                const title = document.getElementById('campTitle').value;
                const subtitle = document.getElementById('campSubtitle').value || "দেরি না করে এখনো সবাই নিয়ে নিন";
                const link = document.getElementById('campLink').value;
                const img = document.getElementById('campImg').value;
                const packageDays = parseInt(document.getElementById('campPackage').value);
                
                let cost = 500;
                if(packageDays === 3) cost = 1200;
                if(packageDays === 7) cost = 2500;
                
                if(!title || !link) { tg.showAlert("Title and Link are required!"); return; }
                
                if(confirm(`Cost is ${cost} Points for ${packageDays} Days. Proceed?`)) {
                    try {
                        const res = await fetch('/api/ads/create', { 
                            method: 'POST', 
                            headers: {'Content-Type': 'application/json'}, 
                            body: JSON.stringify({uid: uid, initData: INIT_DATA, title: title, subtitle: subtitle, link: link, image_url: img, package: packageDays}) 
                        });
                        const data = await res.json();
                        
                        if(data.ok) {
                            tg.showAlert("🎉 Campaign Started Successfully!");
                            document.getElementById('adCampModal').style.display = 'none';
                            fetchUserInfo(); 
                            fetchActiveAds(); 
                        } else {
                            tg.showAlert("⚠️ " + data.msg);
                        }
                    } catch(e) { tg.showAlert("Network Error!"); }
                }
            }

            function formatViews(n) { if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M'; if (n >= 1000) return (n / 1000).toFixed(1) + 'K'; return n; }
            function makeSafeId(str) { return str.replace(/[^a-zA-Z0-9]/g, '_'); }

            function startAutoScroll() {
                if(autoScrollInterval) clearInterval(autoScrollInterval);
                autoScrollInterval = setInterval(() => {
                    let grid = document.getElementById('trendingGrid');
                    if(grid) {
                        if (grid.scrollLeft >= (grid.scrollWidth - grid.clientWidth - 10)) grid.scrollTo({ left: 0, behavior: 'smooth' });
                        else grid.scrollBy({ left: 295, behavior: 'smooth' });
                    }
                }, 3000);
            }

            async function loadTrending() {
                try {
                    const r = await fetch(`/api/trending?uid=${uid}`);
                    const data = await r.json();
                    const grid = document.getElementById('trendingGrid');
                    if(data.length === 0) return document.getElementById('trendingWrapper').style.display = 'none';
                    grid.innerHTML = data.map(m => {
                        loadedMovies[m._id] = m;
                        return `<div class="trending-card" onclick="openQualityModal(this)" data-title="${encodeURIComponent(m._id)}">
                            <div class="post-content">
                                <div class="top-badge">🔥 TOP</div>
                                <img src="/api/image/${m.photo_id}" loading="lazy" onerror="this.src='https://via.placeholder.com/640x360?text=No+Image'">
                                <div class="ep-badge"><i class="fa-solid fa-list"></i> ${m.files.length}</div>
                                <div class="view-badge" id="trend-view-${makeSafeId(m._id)}"><i class="fa-solid fa-eye"></i> ${formatViews(m.clicks)}</div>
                            </div>
                            <div class="card-footer">
                                <div class="channel-logo">MB</div>
                                <div class="title-text">${m._id}</div>
                            </div>
                        </div>`;
                    }).join('');
                    setTimeout(startAutoScroll, 1000);
                } catch(e) {}
            }

            async function loadMovies(page = 1) {
                currentPage = page;
                const grid = document.getElementById('movieGrid');
                grid.innerHTML = "<p style='color:white; text-align:center;'>Loading...</p>";
                try {
                    const r = await fetch(`/api/list?page=${currentPage}&q=${encodeURIComponent(searchQuery)}&uid=${uid}`);
                    const data = await r.json();
                    if(data.movies.length === 0) return grid.innerHTML = `<p style='text-align:center; color:#fbbf24;'>No movies found!</p>`;
                    
                    let htmlContent = "";
                    
                    data.movies.forEach((m, index) => {
                        loadedMovies[m._id] = m; 
                        let cardHtml = `<div class="card" onclick="openQualityModal(this)" data-title="${encodeURIComponent(m._id)}">
                            <div class="post-content">
                                <img src="/api/image/${m.photo_id}" loading="lazy" onerror="this.src='https://via.placeholder.com/640x360?text=No+Image'">
                                <div class="ep-badge"><i class="fa-solid fa-list"></i> ${m.files.length}</div>
                                <div class="view-badge" id="list-view-${makeSafeId(m._id)}"><i class="fa-solid fa-eye"></i> ${formatViews(m.clicks)}</div>
                            </div>
                            <div class="card-footer">
                                <div class="channel-logo">MB</div>
                                <div class="title-text">${m._id}</div>
                            </div>
                        </div>`;
                        htmlContent += cardHtml;
                        
                        let visualIndex = index + 1;
                        if (activeAds.length > 0 && visualIndex % AD_INTERVAL === 0) {
                            htmlContent += getAdCarouselHTML(visualIndex);
                        }
                    });
                    
                    grid.innerHTML = htmlContent;
                    
                    let html = "";
                    if(data.total_pages > 1) {
                        html += `<button class="page-btn" ${currentPage === 1 ? 'disabled style="opacity:0.5;"' : ''} onclick="loadMovies(${currentPage - 1}); window.scrollTo({ top: document.getElementById('recentTitle').offsetTop - 60, behavior: 'smooth' });"><i class="fa-solid fa-angle-left"></i></button>`;
                        
                        let startP = Math.max(1, currentPage - 1);
                        let endP = Math.min(data.total_pages, currentPage + 1);
                        
                        for(let i=startP; i<=endP; i++) { 
                            html += `<button class="page-btn ${i===currentPage?'active':''}" onclick="loadMovies(${i}); window.scrollTo({ top: document.getElementById('recentTitle').offsetTop - 60, behavior: 'smooth' });">${i}</button>`; 
                        }
                        
                        html += `<button class="page-btn" ${currentPage === data.total_pages ? 'disabled style="opacity:0.5;"' : ''} onclick="loadMovies(${currentPage + 1}); window.scrollTo({ top: document.getElementById('recentTitle').offsetTop - 60, behavior: 'smooth' });"><i class="fa-solid fa-angle-right"></i></button>`;
                    }
                    document.getElementById('paginationBox').innerHTML = html;
                } catch(e) {}
            }

            let timeout = null;
            document.getElementById('searchInput').addEventListener('input', function(e) {
                clearTimeout(timeout); 
                searchQuery = e.target.value.trim();
                
                const elementsToToggle = [
                    document.getElementById('trendingWrapper'),
                    document.getElementById('recentTitle'),
                    document.getElementById('communityBox'),
                    document.querySelector('.developer-credit')
                ];

                if(searchQuery !== "") { 
                    elementsToToggle.forEach(el => { if(el) el.style.display = 'none'; });
                } 
                else { 
                    if(document.getElementById('trendingWrapper')) document.getElementById('trendingWrapper').style.display = 'block';
                    if(document.getElementById('recentTitle')) document.getElementById('recentTitle').style.display = 'flex';
                    if(document.getElementById('communityBox')) document.getElementById('communityBox').style.display = 'block';
                    if(document.querySelector('.developer-credit')) document.querySelector('.developer-credit').style.display = 'block';
                }
                
                timeout = setTimeout(() => loadMovies(1), 500); 
            });

            async function openQualityModal(element) {
                let title = decodeURIComponent(element.getAttribute('data-title'));
                const movie = loadedMovies[title];
                if (!movie) {
                    console.error("Movie not found in loadedMovies:", title);
                    return;
                }
                
                document.getElementById('modalTitle').innerText = title;
                document.getElementById('qualityList').innerHTML = movie.files.map(f => {
                    let isFree = f.is_unlocked || isUserVip;
                    let icon = isFree ? '<i class="fa-solid fa-paper-plane text-green-400"></i>' : '<i class="fa-solid fa-lock text-red-400"></i>';
                    let cls = isFree ? 'border-left: 5px solid #10b981;' : 'border-left: 5px solid #ef4444;';
                    return `<div class="rgb-border" onclick="handleQualityClick('${f.id}', ${f.is_unlocked})"><div class="rgb-inner" style="${cls}"><span><i class="fa-solid fa-download"></i> ${f.quality}</span> ${icon}</div></div>`;
                }).join('');
                document.getElementById('qualityModal').style.display = 'flex';
                
                history.pushState({modal: 'qualityModal'}, "");
                checkAndToggleTelegramBackButton();
                
                document.getElementById('bookmarkBtn').innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Checking...';
                document.getElementById('avgRatingVal').innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
                document.getElementById('modalReviewsList').innerHTML = '<div style="text-align:center; padding:10px; color:#94a3b8;"><i class="fa-solid fa-spinner fa-spin"></i> Loading reviews...</div>';
                
                setSelectRating(0);
                
                fetch(`/api/watchlist/list/${uid}`)
                    .then(res => res.json())
                    .then(wlData => {
                        isCurrentMovieBookmarked = wlData.watchlist.some(w => w.title === title);
                        updateBookmarkButtonUI();
                    })
                    .catch(e => {
                        isCurrentMovieBookmarked = false;
                        updateBookmarkButtonUI();
                    });
                
                loadReviews(title);
                
                fetch('/api/view_movie', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: title})
                }).catch(e => console.log(e));
                
                movie.clicks += 1;
                let safeId = makeSafeId(title);
                let tBadge = document.getElementById('trend-view-' + safeId);
                let lBadge = document.getElementById('list-view-' + safeId);
                if(tBadge) tBadge.innerHTML = '<i class="fa-solid fa-eye"></i> ' + formatViews(movie.clicks);
                if(lBadge) lBadge.innerHTML = '<i class="fa-solid fa-eye"></i> ' + formatViews(movie.clicks);
            }

            let currentFileId = null; 

            function handleQualityClick(fileId, isUnlocked) {
                document.getElementById('qualityModal').style.display = 'none';
                if(isUnlocked || isUserVip) { 
                    sendFileAndClose(fileId); 
                } else { 
                    currentFileId = fileId; 
                    document.getElementById('directLinkModal').style.display = 'flex';
                    history.replaceState({modal: 'directLinkModal'}, "");
                    checkAndToggleTelegramBackButton();
                    resetDlButton();
                }
            }

            let linkOpenedAt = 0;
            let isWaitingForReturn = false;
            let dlTimerInterval = null;

            function resetDlButton() {
                const btn = document.getElementById('dlClickBtn');
                btn.onclick = executeDirectLink;
                btn.innerText = "🔗 Click Here (Open Link)";
                btn.style.background = "linear-gradient(45deg, #ef4444, #f97316)";
                btn.disabled = false;
            }

            function executeDirectLink() {
                if (!DIRECT_LINKS || DIRECT_LINKS.length === 0) { 
                    document.getElementById('directLinkModal').style.display = 'none'; 
                    if (currentFileId) sendFileAndClose(currentFileId); 
                    return; 
                }
                
                tg.openLink(DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)]);
                linkOpenedAt = Date.now(); 
                isWaitingForReturn = true;
                
                const btn = document.getElementById('dlClickBtn');
                btn.disabled = true; 
                let timeLeft = AD_WAIT_TIME; 
                btn.style.background = "#475569";
                
                dlTimerInterval = setInterval(() => {
                    timeLeft--; 
                    if(timeLeft > 0) {
                        btn.innerText = `⏳ Please wait... (${timeLeft}s)`;
                    } else {
                        clearInterval(dlTimerInterval);
                        if(isWaitingForReturn) {
                            isWaitingForReturn = false;
                            document.getElementById('directLinkModal').style.display = 'none';
                            if (currentFileId) sendFileAndClose(currentFileId);
                        }
                    }
                }, 1000);
            }

            let coinLinkOpenedAt = 0; 
            let isWaitingForCoinReturn = false; 
            let coinTimerInterval = null;

            function resetCoinButton() {
                const btn = document.getElementById('coinAdBtn');
                btn.disabled = false;
                btn.onclick = executeCoinAd;
                btn.innerHTML = '<i class="fa-solid fa-play"></i> Watch Ad & Get 5 Points';
                btn.style.background = "linear-gradient(45deg, #ef4444, #f97316)";
            }

            function executeCoinAd() {
                if (!DIRECT_LINKS || DIRECT_LINKS.length === 0) { tg.showAlert("⚠️ No ads available right now!"); return; }
                tg.openLink(DIRECT_LINKS[Math.floor(Math.random() * DIRECT_LINKS.length)]);
                
                coinLinkOpenedAt = Date.now(); 
                isWaitingForCoinReturn = true;
                
                const btn = document.getElementById('coinAdBtn');
                btn.disabled = true; 
                let timeLeft = AD_WAIT_TIME; 
                btn.style.background = "#475569";
                
                coinTimerInterval = setInterval(() => {
                    timeLeft--; 
                    if(timeLeft > 0) {
                        btn.innerHTML = `<i class="fa-solid fa-play"></i> Please wait... (${timeLeft}s)`;
                    } else {
                        clearInterval(coinTimerInterval);
                        if(isWaitingForCoinReturn) {
                            isWaitingForCoinReturn = false;
                            claimAdCoin();
                            resetCoinButton();
                        }
                    }
                }, 1000);
            }

            document.addEventListener("visibilitychange", function() {
                if (document.visibilityState === 'visible') {
                    let now = Date.now();
                    
                    if (isWaitingForReturn) {
                        isWaitingForReturn = false; 
                        clearInterval(dlTimerInterval);
                        
                        let elapsedSeconds = (now - linkOpenedAt) / 1000;
                        if (elapsedSeconds < AD_WAIT_TIME - 1) { 
                            tg.showAlert(`⚠️ You must wait full ${AD_WAIT_TIME} seconds on the link.`);
                            resetDlButton();
                        } else { 
                            document.getElementById('directLinkModal').style.display = 'none'; 
                            if (currentFileId) sendFileAndClose(currentFileId); 
                        }
                    }
                    
                    if (isWaitingForCoinReturn) {
                        isWaitingForCoinReturn = false; 
                        clearInterval(coinTimerInterval);
                        
                        let elapsedSeconds = (now - coinLinkOpenedAt) / 1000;
                        if (elapsedSeconds < AD_WAIT_TIME - 1) {
                            tg.showAlert(`⚠️ You must wait full ${AD_WAIT_TIME} seconds on the link.`);
                            resetCoinButton();
                        } else { 
                            claimAdCoin(); 
                            resetCoinButton();
                        }
                    }
                }
            });

            async function claimAdCoin() {
                try {
                    const res = await fetch('/api/add_coin', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, initData: INIT_DATA}) });
                    const data = await res.json();
                    if(data.ok) { 
                        tg.showAlert("🎉 Congratulations! You received 5 Points.");
                        fetchUserInfo(); 
                    } else { tg.showAlert("⚠️ Error receiving points."); }
                } catch (e) {}
            }

            async function buyVipWithCoins() {
                const vCost = parseInt(document.getElementById('btnVipCost').innerText) || 30;
                const vDays = parseInt(document.getElementById('btnVipDays').innerText) || 1;
                
                if(userCoins < vCost) {
                    tg.showAlert(`⚠️ Not enough points! You need ${vCost} points. Watch ads or refer friends to earn points.`);
                    return;
                }
                if(confirm(`Do you want to buy ${vDays} Days VIP for ${vCost} points?`)) {
                    try {
                        const res = await fetch('/api/buy_vip', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, initData: INIT_DATA}) });
                        const data = await res.json();
                        if(data.ok) { 
                            document.getElementById('vipModal').style.display = 'none';
                            tg.showAlert("🎉 Success! Your VIP has been activated.");
                            fetchUserInfo(); 
                        } else { tg.showAlert(data.msg); }
                    } catch (e) {}
                }
            }

            function showProcessingUI() {
                let procModal = document.getElementById('processingModalCustom');
                if(!procModal) {
                    procModal = document.createElement('div');
                    procModal.id = 'processingModalCustom';
                    procModal.style.cssText = 'position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.95); z-index:9999; display:flex; align-items:center; justify-content:center; flex-direction:column; backdrop-filter: blur(5px);';
                    procModal.innerHTML = `
                        <div class="spinner-new"></div>
                        <div class="big-processing-text">Sending File...</div>
                        <div style="color:#cbd5e1; margin-top:15px; font-size:16px; font-weight:bold;">Please wait, video is going to your bot inbox!</div>
                    `;
                    document.body.appendChild(procModal);
                }
                procModal.style.display = 'flex';
            }

            function hideProcessingUI() {
                let procModal = document.getElementById('processingModalCustom');
                if(procModal) procModal.style.display = 'none';
            }

            async function sendFileAndClose(id) {
                showProcessingUI(); 
                try {
                    const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: id, initData: INIT_DATA}) });
                    const data = await res.json();
                    
                    if(data.ok) { 
                        setTimeout(() => {
                            tg.close();
                        }, 500);
                    } else {
                        hideProcessingUI();
                        tg.showAlert("⚠️ Session expired! Please close and reopen the mini app.");
                    }
                } catch (e) {
                    hideProcessingUI();
                    tg.showAlert("⚠️ Network error! Please try again.");
                }
            }

            function renderCommunitySection() {
                let html = '';
                if(SOCIAL_LINKS.fb_group) html += `<a href="${SOCIAL_LINKS.fb_group}" target="_blank" class="social-btn fb-btn"><i class="fa-brands fa-facebook"></i> FB Group</a>`;
                if(SOCIAL_LINKS.fb_page) html += `<a href="${SOCIAL_LINKS.fb_page}" target="_blank" class="social-btn fb-btn"><i class="fa-brands fa-facebook-f"></i> FB Page</a>`;
                if(SOCIAL_LINKS.youtube) html += `<a href="${SOCIAL_LINKS.youtube}" target="_blank" class="social-btn yt-btn"><i class="fa-brands fa-youtube"></i> YouTube</a>`;
                if(SOCIAL_LINKS.review_channel) html += `<a href="${SOCIAL_LINKS.review_channel}" target="_blank" class="social-btn tg-btn"><i class="fa-solid fa-film"></i> Movie Review</a>`;
                
                if(html !== '') {
                    document.getElementById('communityBox').innerHTML = `
                    <div class="community-section">
                        <div class="section-title" style="justify-content: center; font-size: 18px;"><i class="fa-solid fa-users" style="color: #38bdf8;"></i> Join Our Community</div>
                        <div class="social-grid">${html}</div>
                    </div>`;
                }
            }

            history.replaceState({page: 'home'}, "");

            function checkAndToggleTelegramBackButton() {
                const modals = ['qualityModal', 'directLinkModal', 'vipModal', 'referModal', 'watchlistModal', 'requestsTrackerModal', 'adCampModal'];
                let anyOpen = false;
                modals.forEach(id => {
                    const el = document.getElementById(id);
                    if (el && el.style.display === 'flex') {
                        anyOpen = true;
                    }
                });
                if (anyOpen) {
                    tg.BackButton.show();
                } else {
                    tg.BackButton.hide();
                }
            }

            window.addEventListener('popstate', function(event) {
                const modals = ['qualityModal', 'directLinkModal', 'vipModal', 'referModal', 'watchlistModal', 'requestsTrackerModal', 'adCampModal'];
                modals.forEach(id => {
                    const el = document.getElementById(id);
                    if (el && el.style.display === 'flex') {
                        el.style.display = 'none';
                    }
                });
                checkAndToggleTelegramBackButton();
            });

            tg.BackButton.onClick(function() {
                history.back();
            });

            document.querySelectorAll('.close-icon').forEach(btn => {
                btn.removeAttribute('onclick');
                btn.addEventListener('click', function(e) {
                    e.preventDefault();
                    e.stopPropagation();
                    history.back();
                });
            });

            let splashStartTime = Date.now();
            let welcomeSoundPlayed = false;

            function playWelcomeSound() {
                if (welcomeSoundPlayed) return;
                try {
                    let audioUrl = "https://assets.mixkit.co/active_storage/sfx/2568/2568-preview.mp3";
                    let audio = new Audio(audioUrl);
                    audio.volume = 0.8;
                    audio.play()
                        .then(() => {
                            welcomeSoundPlayed = true;
                        })
                        .catch(err => {});
                } catch (e) {}
            }

            ['click', 'touchstart', 'mousedown'].forEach(eventName => {
                document.addEventListener(eventName, function triggerAudio() {
                    playWelcomeSound();
                    document.removeEventListener(eventName, triggerAudio);
                }, { passive: true });
            });

            async function hideSplashScreen() {
                let elapsed = Date.now() - splashStartTime;
                let delay = Math.max(0, 3000 - elapsed);
                
                setTimeout(() => {
                    let splash = document.getElementById('startupSplash');
                    if (splash) {
                        splash.style.opacity = '0';
                        splash.style.visibility = 'hidden';
                        setTimeout(() => splash.remove(), 800);
                    }
                }, delay);
            }

            async function initApp() {
                try {
                    await Promise.all([
                        fetchUserInfo(),
                        fetchActiveAds(),
                        loadTrending(),
                        loadMovies(1)
                    ]);
                    renderCommunitySection();
                } catch(e) {} finally {
                    hideSplashScreen();
                }
            }

            initApp();
        </script>
    </body>
    </html>
    """
    html_code = html_code.replace("{{DIRECT_LINKS}}", dl_json).replace("{{SUPPORT_LINK}}", support_link).replace("{{BOT_USER}}", BOT_USERNAME).replace("{{AD_TIME}}", str(ad_wait_seconds)).replace("{{AD_INTERVAL}}", str(ad_interval)).replace("{{SOCIAL_LINKS}}", social_json)
    return html_code

# ==========================================
# 8. Optimized APIs
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    now = datetime.datetime.utcnow()
    await db.users.update_one({"user_id": uid}, {"$set": {"last_active": now}})
    
    user = await db.users.find_one({"user_id": uid})
    is_admin = uid in admin_cache
    
    cost_cfg = await db.settings.find_one({"id": "vip_cost"})
    days_cfg = await db.settings.find_one({"id": "vip_days"})
    
    cost = cost_cfg["amount"] if cost_cfg else 30
    days = days_cfg["days"] if days_cfg else 1

    if not user: return {"vip": False, "admin": is_admin, "coins": 0, "vip_cost": cost, "vip_days": days}
    return {
        "vip": user.get("vip_until", now) > now, 
        "admin": is_admin,
        "coins": user.get("coins", 0),
        "vip_cost": cost,
        "vip_days": days
    }

class UserActionModel(BaseModel):
    uid: int
    initData: str

@app.post("/api/add_coin")
async def add_coin_api(d: UserActionModel):
    if d.uid == 0 or not validate_tg_data(d.initData): return {"ok": False}
    await db.users.update_one({"user_id": d.uid}, {"$inc": {"coins": 5}})
    return {"ok": True}

@app.post("/api/buy_vip")
async def buy_vip_api(d: UserActionModel):
    if d.uid == 0 or not validate_tg_data(d.initData): return {"ok": False}
    user = await db.users.find_one({"user_id": d.uid})
    coins = user.get("coins", 0)
    
    cost_cfg = await db.settings.find_one({"id": "vip_cost"})
    days_cfg = await db.settings.find_one({"id": "vip_days"})
    cost = cost_cfg["amount"] if cost_cfg else 30
    days = days_cfg["days"] if days_cfg else 1
    
    if coins < cost: return {"ok": False, "msg": f"Not enough points! Need {cost} points."}
    
    now = datetime.datetime.utcnow()
    current_vip = user.get("vip_until", now) if user.get("vip_until") else now
    if current_vip < now: current_vip = now
    new_vip = current_vip + datetime.timedelta(days=days)
    
    await db.users.update_one({"user_id": d.uid}, {"$inc": {"coins": -cost}, "$set": {"vip_until": new_vip}})
    return {"ok": True}

@app.get("/api/trending")
async def trending_movies(uid: int = 0):
    unlocked_ids = []
    cfg_unlock = await db.settings.find_one({"id": "unlock_hours"})
    unlock_hrs = cfg_unlock['hours'] if cfg_unlock else 24
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=unlock_hrs)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])

    if "trending_list" in trending_cache:
        movies = copy.deepcopy(trending_cache["trending_list"])
    else:
        seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
        pipeline = [
            {"$group": {
                "_id": "$title", 
                "photo_id": {"$first": "$photo_id"}, 
                "db_photo_id": {"$first": "$db_photo_id"}, 
                "clicks": {"$sum": "$clicks"}, 
                "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "HD"]}}}
            }},
            {"$lookup": {
                "from": "movie_views",
                "let": {"movie_title": "$_id"},
                "pipeline": [
                    {"$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$title", "$$movie_title"]},
                                {"$gte": ["$viewed_at", seven_days_ago]}
                            ]
                        }
                    }},
                    {"$count": "count"}
                ],
                "as": "weekly"
            }},
            {"$addFields": {
                "weekly_clicks": {"$ifNull": [{"$arrayElemAt": ["$weekly.count", 0]}, 0]}
            }},
            {"$sort": {"weekly_clicks": -1, "clicks": -1}},
            {"$limit": 10}
        ]
        movies = await db.movies.aggregate(pipeline).to_list(10)
        for m in movies:
            m["photo_id"] = m.get("photo_id") or (f"db_{m['db_photo_id']}" if m.get("db_photo_id") else None)
        trending_cache["trending_list"] = movies
        movies = copy.deepcopy(movies)

    for m in movies:
        for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
    return movies

@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0):
    unlocked_ids = []
    cfg_unlock = await db.settings.find_one({"id": "unlock_hours"})
    unlock_hrs = cfg_unlock['hours'] if cfg_unlock else 24
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=unlock_hrs)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}):
            unlocked_ids.append(u["movie_id"])

    cache_key = f"{page}_{q}"
    if cache_key in list_cache:
        data = copy.deepcopy(list_cache[cache_key])
        movies = data["movies"]
        total_pages = data["total_pages"]
    else:
        limit = 100  
        skip = (page - 1) * limit
        match_stage = {}
        if q: match_stage["title"] = {"$regex": q, "$options": "i"}

        pipeline = [
            {"$match": match_stage},
            {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "db_photo_id": {"$first": "$db_photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "HD"]}}}}},
            {"$sort": {"created_at": -1}}, {"$skip": skip}, {"$limit": limit}
        ]
        total_groups = (await db.movies.aggregate([{"$match": match_stage}, {"$group": {"_id": "$title"}}, {"$count": "total"}]).to_list(1))
        total_pages = (total_groups[0]["total"] + limit - 1) // limit if total_groups else 0
        movies = await db.movies.aggregate(pipeline).to_list(limit)
        for m in movies:
            m["photo_id"] = m.get("photo_id") or (f"db_{m['db_photo_id']}" if m.get("db_photo_id") else None)
        list_cache[cache_key] = {"movies": movies, "total_pages": total_pages}
        movies = copy.deepcopy(movies)

    for m in movies:
        for f in m["files"]: f["is_unlocked"] = f["id"] in unlocked_ids
    return {"movies": movies, "total_pages": total_pages}

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        cache = await db.file_cache.find_one({"photo_id": photo_id})
        now = datetime.datetime.utcnow()
        file_path = None
        if cache and cache.get("expires_at", now) > now: 
            file_path = cache["file_path"]
        else:
            actual_file_id = photo_id
            db_msg_id = None
            if photo_id.startswith("db_"):
                parts = photo_id.split("_")
                if len(parts) > 1 and parts[1].isdigit():
                    db_msg_id = int(parts[1])
                movie = await db.movies.find_one({"db_photo_id": db_msg_id})
                if movie and movie.get("photo_id"): actual_file_id = movie["photo_id"]
            try:
                file_path = (await bot.get_file(actual_file_id)).file_path
            except Exception:
                if db_msg_id and DB_CHANNEL_ID:
                    try:
                        copied = await bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=DB_CHANNEL_ID, message_id=db_msg_id)
                        new_photo_id = copied.photo[-1].file_id
                        await bot.delete_message(chat_id=DB_CHANNEL_ID, message_id=copied.message_id)
                        await db.movies.update_many({"db_photo_id": db_msg_id}, {"$set": {"photo_id": new_photo_id}})
                        file_path = (await bot.get_file(new_photo_id)).file_path
                    except Exception: pass
            if file_path:
                await db.file_cache.update_one({"photo_id": photo_id}, {"$set": {"file_path": file_path, "expires_at": now + datetime.timedelta(minutes=50)}}, upsert=True)
        if not file_path: return {"error": "not found"}
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        async def stream_image():
            async with aiohttp.ClientSession() as session:
                async with session.get(file_url) as resp:
                    async for chunk in resp.content.iter_chunked(1024): yield chunk
        return StreamingResponse(stream_image(), media_type="image/jpeg")
    except Exception: return {"error": "error"}

class ViewRequestModel(BaseModel):
    title: str

@app.post("/api/view_movie")
async def increment_movie_view(d: ViewRequestModel):
    try:
        await db.movies.update_many({"title": d.title}, {"$inc": {"clicks": 1}})
        await db.movie_views.insert_one({"title": d.title, "viewed_at": datetime.datetime.utcnow()})
    except Exception: pass
    return {"ok": True}

class SendRequestModel(BaseModel):
    userId: int
    movieId: str
    initData: str

@app.post("/api/send")
async def send_file(d: SendRequestModel):
    if d.userId == 0 or not validate_tg_data(d.initData): return {"ok": False}
    try:
        m = await db.movies.find_one({"_id": ObjectId(d.movieId)})
        if m:
            now = datetime.datetime.utcnow()
            user = await db.users.find_one({"user_id": d.userId})
            is_vip = user and user.get("vip_until", now) > now
            time_cfg = await db.settings.find_one({"id": "del_time"})
            del_minutes = time_cfg['minutes'] if time_cfg else 60
            protect_cfg = await db.settings.find_one({"id": "protect_content"})
            is_protected = protect_cfg['status'] if protect_cfg else True
            caption = f"🎥 <b>{m['title']} [{m.get('quality', 'HD')}]</b>\n\n📥 Join: @TGLinkBase"
            if not is_vip: caption += f"\n\n⏳ <i>সতর্কতা: সিকিউরিটির জন্য এই ভিডিওটি <b>{del_minutes} মিনিট</b> পর অটোমেটিক ডিলিট হয়ে যাবে!</i>"
            db_file_id = m.get("db_file_id")
            sent_msg = None
            if db_file_id and DB_CHANNEL_ID:
                sent_msg = await bot.copy_message(chat_id=d.userId, from_chat_id=DB_CHANNEL_ID, message_id=db_file_id, caption=caption, parse_mode="HTML", protect_content=is_protected)
            else:
                if m.get("file_type") == "video": sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
                else: sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected)
            await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
            if sent_msg and not is_vip: await db.auto_delete.insert_one({"chat_id": d.userId, "message_id": sent_msg.message_id, "delete_at": now + datetime.timedelta(minutes=del_minutes)})
    except Exception: pass
    return {"ok": True}

class ReqModel(BaseModel):
    uid: int
    uname: str
    movie: str
    initData: str

@app.post("/api/request")
async def handle_request(data: ReqModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    user = await db.users.find_one({"user_id": data.uid})
    is_vip = False
    if user and user.get("vip_until", datetime.datetime.utcnow()) > datetime.datetime.utcnow(): is_vip = True
    vip_tag = "🔥 <b>[VIP PRIORITY]</b>\n" if is_vip else ""
    now = datetime.datetime.utcnow()
    await db.requests.insert_one({"user_id": data.uid, "uname": data.uname, "movie": data.movie, "status": "pending", "created_at": now, "is_vip": is_vip})
    all_admins = set([OWNER_ID])
    async for a in db.admins.find(): all_admins.add(a["user_id"])
    for admin_id in all_admins:
        try: await bot.send_message(admin_id, f"{vip_tag}🔔 <b>নতুন মুভি রিকোয়েস্ট!</b>\n👤 ইউজার: {data.uname} (<code>{data.uid}</code>)\n🎬 মুভি: <b>{data.movie}</b>", parse_mode="HTML")
        except Exception: pass
    return {"ok": True}

class AdCreateModel(BaseModel):
    uid: int
    initData: str
    title: str
    subtitle: str = "দেরি না করে এখনো সবাই নিয়ে নিন"
    link: str
    image_url: str
    package: int

@app.post("/api/ads/create")
async def create_sponsored_ad(d: AdCreateModel):
    if not validate_tg_data(d.initData): return {"ok": False, "msg": "Invalid Request"}
    costs = {1: 500, 3: 1200, 7: 2500}
    cost = costs.get(d.package, 500)
    days = d.package if d.package in costs else 1
    user = await db.users.find_one({"user_id": d.uid})
    if not user or user.get("coins", 0) < cost: return {"ok": False, "msg": f"Not enough points! Need {cost} points."}
    now = datetime.datetime.utcnow()
    await db.users.update_one({"user_id": d.uid}, {"$inc": {"coins": -cost}})
    await db.ads.insert_one({"user_id": d.uid, "title": d.title, "subtitle": d.subtitle, "link": d.link, "image_url": d.image_url, "created_at": now, "expires_at": now + datetime.timedelta(days=days)})
    try: await bot.send_message(OWNER_ID, f"📢 <b>New Ad Campaign Started!</b>\n👤 User ID: <code>{d.uid}</code>\n📝 Title: {d.title}\n🔗 Link: {d.link}\n⏳ Duration: {days} Days\n💰 Paid: {cost} Coins", parse_mode="HTML")
    except: pass
    return {"ok": True, "msg": "Ad campaign started successfully!"}

@app.get("/api/ads/active")
async def get_active_ads():
    now = datetime.datetime.utcnow()
    ads = await db.ads.find({"expires_at": {"$gte": now}}).to_list(20)
    for ad in ads: ad['_id'] = str(ad['_id'])
    return ads

class AdminAdModel(BaseModel):
    title: str
    subtitle: str = "দেরি না করে এখনো সবাই নিয়ে নিন"
    link: str
    image_url: str

@app.post("/api/admin/ads/create")
async def create_admin_ad(d: AdminAdModel, auth: bool = Depends(verify_admin)):
    await db.ads.insert_one({"user_id": 0, "title": d.title, "subtitle": d.subtitle, "link": d.link, "image_url": d.image_url, "created_at": datetime.datetime.utcnow(), "expires_at": datetime.datetime.utcnow() + datetime.timedelta(days=365)})
    return {"ok": True}

@app.get("/api/admin/ads_list")
async def get_all_ads(auth: bool = Depends(verify_admin)):
    ads = await db.ads.find().sort("created_at", -1).to_list(50)
    for ad in ads: ad['_id'] = str(ad['_id'])
    return {"ads": ads}

@app.delete("/api/admin/ads/{ad_id}")
async def delete_ad(ad_id: str, auth: bool = Depends(verify_admin)):
    await db.ads.delete_one({"_id": ObjectId(ad_id)})
    return {"ok": True}

class WatchlistModel(BaseModel):
    uid: int
    title: str
    initData: str

@app.post("/api/watchlist/add")
async def add_to_watchlist(d: WatchlistModel):
    if not validate_tg_data(d.initData): return {"ok": False}
    await db.users.update_one({"user_id": d.uid}, {"$addToSet": {"watchlist": d.title}})
    return {"ok": True}

@app.post("/api/watchlist/remove")
async def remove_from_watchlist(d: WatchlistModel):
    if not validate_tg_data(d.initData): return {"ok": False}
    await db.users.update_one({"user_id": d.uid}, {"$pull": {"watchlist": d.title}})
    return {"ok": True}

@app.get("/api/watchlist/list/{uid}")
async def get_watchlist(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return {"watchlist": []}
    watchlist = user.get("watchlist", [])
    if not watchlist: return {"watchlist": []}
    pipeline = [{"$match": {"title": {"$in": watchlist}}}, {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "db_photo_id": {"$first": "$db_photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "HD"]}}}}}, {"$sort": {"created_at": -1}}]
    movies = await db.movies.aggregate(pipeline).to_list(len(watchlist))
    formatted_movies = []
    for m in movies:
        p_id = m.get("photo_id") or (f"db_{m['db_photo_id']}" if m.get("db_photo_id") else None)
        formatted_movies.append({"title": m["_id"], "photo_id": p_id, "files": m["files"], "clicks": m.get("clicks", 0)})
    return {"watchlist": formatted_movies}

class ReviewModel(BaseModel):
    uid: int
    uname: str
    title: str
    rating: int
    review: str
    initData: str

@app.post("/api/reviews/add")
async def add_review(d: ReviewModel):
    if not validate_tg_data(d.initData): return {"ok": False}
    now = datetime.datetime.utcnow()
    await db.reviews.update_one({"user_id": d.uid, "movie_title": d.title}, {"$set": {"user_id": d.uid, "uname": d.uname, "movie_title": d.title, "rating": d.rating, "review": d.review, "created_at": now}}, upsert=True)
    return {"ok": True}

@app.get("/api/reviews/get/{title}")
async def get_reviews(title: str):
    reviews = await db.reviews.find({"movie_title": title}).sort("created_at", -1).to_list(50)
    avg_r = sum(r["rating"] for r in reviews) / len(reviews) if reviews else 0
    for r in reviews:
        r["_id"] = str(r["_id"])
        r["created_at"] = r["created_at"].isoformat()
    return {"reviews": reviews, "avg_rating": round(avg_r, 1)}

@app.post("/api/gamification/daily_checkin")
async def daily_checkin(d: UserActionModel):
    if not validate_tg_data(d.initData): return {"ok": False}
    user = await db.users.find_one({"user_id": d.uid})
    if not user: return {"ok": False, "msg": "User not found"}
    now = datetime.datetime.utcnow()
    last_c = user.get("last_check_in")
    if last_c and last_c.date() == now.date(): return {"ok": False, "msg": "Already checked in today!"}
    await db.users.update_one({"user_id": d.uid}, {"$set": {"last_check_in": now}, "$inc": {"coins": 5}})
    return {"ok": True, "coins": user.get("coins", 0) + 5}

@app.post("/api/gamification/spin")
async def spin_wheel(d: UserActionModel):
    if not validate_tg_data(d.initData): return {"ok": False}
    user = await db.users.find_one({"user_id": d.uid})
    if not user or user.get("coins", 0) < 5: return {"ok": False, "msg": "Not enough points! Need 5 points to spin."}
    rewards = [{"type": "points", "amount": 0, "weight": 35}, {"type": "points", "amount": 2, "weight": 25}, {"type": "points", "amount": 5, "weight": 20}, {"type": "points", "amount": 10, "weight": 12}, {"type": "points", "amount": 20, "weight": 5}, {"type": "points", "amount": 50, "weight": 2}, {"type": "vip", "days": 1, "weight": 1}]
    choices = []
    for r in rewards: choices.extend([r] * r["weight"])
    reward = random.choice(choices)
    await db.users.update_one({"user_id": d.uid}, {"$inc": {"coins": -5}})
    msg = ""
    if reward["type"] == "points":
        if reward["amount"] > 0:
            await db.users.update_one({"user_id": d.uid}, {"$inc": {"coins": reward["amount"]}})
            msg = f"You won {reward['amount']} Points!"
        else: msg = "Better luck next time!"
    elif reward["type"] == "vip":
        now = datetime.datetime.utcnow()
        cv = user.get("vip_until", now) if user.get("vip_until") else now
        if cv < now: cv = now
        await db.users.update_one({"user_id": d.uid}, {"$set": {"vip_until": cv + datetime.timedelta(days=1)}})
        msg = "Congratulations! You won 1 Day VIP Pass!"
    return {"ok": True, "reward": reward, "msg": msg}

@app.get("/api/requests/user_list/{uid}")
async def user_requests(uid: int):
    reqs = await db.requests.find({"user_id": uid}).sort("created_at", -1).to_list(50)
    for r in reqs:
        r["_id"] = str(r["_id"])
        r["created_at"] = r["created_at"].isoformat()
    return {"requests": reqs}

@app.get("/api/admin/requests")
async def admin_get_requests(auth: bool = Depends(verify_admin)):
    reqs = await db.requests.find().sort("created_at", -1).to_list(100)
    for r in reqs:
        r["_id"] = str(r["_id"])
        r["created_at"] = r["created_at"].isoformat()
    return {"requests": reqs}

@app.put("/api/admin/requests/{req_id}")
async def admin_update_request(req_id: str, data: dict = Body(...), auth: bool = Depends(verify_admin)):
    await db.requests.update_one({"_id": ObjectId(req_id)}, {"$set": {"status": data.get("status")}})
    return {"ok": True}

@app.delete("/api/admin/requests/{req_id}")
async def admin_delete_request(req_id: str, auth: bool = Depends(verify_admin)):
    await db.requests.delete_one({"_id": ObjectId(req_id)})
    return {"ok": True}

@app.get("/api/admin/keywords")
async def get_keywords_api(auth: bool = Depends(verify_admin)):
    kws = await db.keyword_replies.find().to_list(100)
    for kw in kws: kw["_id"] = str(kw["_id"])
    return {"keywords": kws}

@app.post("/api/admin/keywords")
async def add_keyword_api(data: dict = Body(...), auth: bool = Depends(verify_admin)):
    kw = data.get("keyword", "").lower().strip()
    rep = data.get("reply_message", "").strip()
    if not kw or not rep: raise HTTPException(status_code=400, detail="Missing data")
    await db.keyword_replies.update_one({"keyword": kw}, {"$set": {"keyword": kw, "reply_message": rep}}, upsert=True)
    await load_keyword_replies()
    return {"ok": True}

@app.delete("/api/admin/keywords/{keyword}")
async def delete_keyword_api(keyword: str, auth: bool = Depends(verify_admin)):
    await db.keyword_replies.delete_one({"keyword": keyword.lower()})
    await load_keyword_replies()
    return {"ok": True}

@app.get("/api/admin/users/search")
async def search_users(q: str = "", auth: bool = Depends(verify_admin)):
    ms = {}
    if q:
        if q.isdigit(): ms["user_id"] = int(q)
        else: ms["first_name"] = {"$regex": q, "$options": "i"}
    users = await db.users.find(ms).limit(20).to_list(20)
    form = []
    now = datetime.datetime.utcnow()
    for u in users:
        uid = u["user_id"]
        is_b = uid in banned_cache or (await db.banned.find_one({"user_id": uid}) is not None)
        form.append({"user_id": uid, "first_name": u.get("first_name", "User"), "coins": u.get("coins", 0), "refer_count": u.get("refer_count", 0), "is_vip": u.get("vip_until", now) > now, "is_banned": is_b})
    return {"users": form}

@app.post("/api/admin/users/action")
async def manage_user_action(d: UserManageModel, auth: bool = Depends(verify_admin)):
    uid = d.user_id
    now = datetime.datetime.utcnow()
    if d.action == "ban":
        await db.banned.update_one({"user_id": uid}, {"$set": {"user_id": uid}}, upsert=True)
        banned_cache.add(uid)
    elif d.action == "unban":
        await db.banned.delete_one({"user_id": uid})
        banned_cache.discard(uid)
    elif d.action == "add_coins": await db.users.update_one({"user_id": uid}, {"$inc": {"coins": d.value}})
    elif d.action == "remove_coins": await db.users.update_one({"user_id": uid}, {"$inc": {"coins": -d.value}})
    elif d.action == "add_vip":
        user = await db.users.find_one({"user_id": uid})
        cv = user.get("vip_until", now) if user else now
        if cv < now: cv = now
        await db.users.update_one({"user_id": uid}, {"$set": {"vip_until": cv + datetime.timedelta(days=d.value)}})
    elif d.action == "remove_vip": await db.users.update_one({"user_id": uid}, {"$set": {"vip_until": now - datetime.timedelta(days=1)}})
    return {"ok": True}

@app.get("/api/admin/analytics")
async def get_analytics(auth: bool = Depends(verify_admin)):
    now = datetime.datetime.utcnow()
    t_start = datetime.datetime(now.year, now.month, now.day)
    seven_d = t_start - datetime.timedelta(days=7)
    live = await db.users.count_documents({"last_active": {"$gte": now - datetime.timedelta(minutes=5)}})
    a_t = await db.user_unlocks.distinct("user_id", {"unlocked_at": {"$gte": t_start}})
    a_w = await db.user_unlocks.distinct("user_id", {"unlocked_at": {"$gte": seven_d}})
    c_s = []  # Category রিমুভ করায় এটি খালি রাখা হলো যেন কোনো এরর না আসে
    t_r = await db.reviews.aggregate([{"$group": {"_id": "$movie_title", "avg_rating": {"$avg": "$rating"}, "total_reviews": {"$sum": 1}}}, {"$sort": {"avg_rating": -1, "total_reviews": -1}}, {"$limit": 5}]).to_list(5)
    return {"live_online": live, "active_today": len(a_t), "active_week": len(a_w), "total_reviews": await db.reviews.count_documents({}), "total_requests": await db.requests.count_documents({}), "pending_requests": await db.requests.count_documents({"status": "pending"}), "category_stats": c_s, "top_rated": t_r}

async def start():
    global video_queue
    video_queue = asyncio.Queue()
    cleanup_temp_files()
    await init_db()
    await load_admins()
    await load_banned_users()
    await load_keyword_replies()
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)), loop="asyncio")
    server = uvicorn.Server(config)
    await pyro_app.start()
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(video_queue_worker()) 
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(server.serve())
    await dp.start_polling(bot)

if __name__ == "__main__": 
    try: asyncio.run(start())
    except (KeyboardInterrupt, SystemExit): pass
