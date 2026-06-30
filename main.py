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
import math

# ==========================================
# 🛑 FIX FOR EVENT LOOP ERROR
# ==========================================
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ==========================================

from fastapi import FastAPI, Body, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError

from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId
from pydantic import BaseModel

# ==========================================
# 1. Configuration & Global Variables
# ==========================================
TOKEN = os.getenv("BOT_TOKEN")
MONGO_URL = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003904328439") 
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123") 
BOT_USERNAME = "bdlatestmovie_bot" 

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "-1003497700295")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()
security = HTTPBasic()

app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"], 
    allow_credentials=True, 
    allow_methods=["*"], 
    allow_headers=["*"]
)

client = AsyncIOMotorClient(MONGO_URL)
db = client['movie_database']

admin_cache = set([OWNER_ID]) 
banned_cache = set() 

CATEGORIES = ["Bangla", "Bangla Dubbed", "Hindi Dubbed", "Hollywood", "K-Drama", "Anime", "Horror", "Web Series", "Adult Content"]

# কিউ সিস্টেম যোগ করা হলো
broadcast_queue = asyncio.Queue()

# ==========================================
# 2. FSM States
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_photo = State()
    waiting_for_title = State()
    waiting_for_quality = State() 
    waiting_for_year = State()
    waiting_for_cats = State()
    waiting_for_upc_photo = State()
    waiting_for_upc_title = State()
    waiting_for_upc_date = State()

# ==========================================
# 3. Database Initialization & Caching
# ==========================================
async def load_admins():
    admin_cache.clear()
    admin_cache.add(OWNER_ID)
    async for admin in db.admins.find():
        admin_cache.add(admin["user_id"])

async def load_banned_users():
    banned_cache.clear()
    async for b_user in db.banned.find():
        banned_cache.add(b_user["user_id"])

async def init_db():
    await db.movies.create_index([("title", "text")])
    await db.movies.create_index("title")
    await db.movies.create_index("created_at")
    await db.movies.create_index("categories")
    await db.auto_delete.create_index("delete_at")
    await db.users.create_index("joined_at")
    await db.users.create_index("last_active")
    await db.payments.create_index("trx_id", unique=True)

# ==========================================
# 4. Security & Authentication Methods
# ==========================================
def validate_tg_data(init_data: str) -> bool:
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        hash_val = parsed_data.pop('hash', None)
        auth_date = int(parsed_data.get('auth_date', 0))
        if not hash_val or time.time() - auth_date > 86400:
            return False
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        return calculated_hash == hash_val
    except:
        return False

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect", headers={"WWW-Authenticate": "Basic"})
    return True

# ==========================================
# 5. Background Tasks
# ==========================================
async def auto_delete_worker():
    while True:
        try:
            now = datetime.datetime.utcnow()
            async for msg in db.auto_delete.find({"delete_at": {"$lte": now}}):
                try:
                    await bot.delete_message(chat_id=msg["chat_id"], message_id=msg["message_id"])
                except:
                    pass
                await db.auto_delete.delete_one({"_id": msg["_id"]})
                await asyncio.sleep(0.5)
        except:
            pass
        await asyncio.sleep(60)

async def auto_lock_worker():
    while True:
        try:
            expire_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            result = await db.user_unlocks.delete_many({"unlocked_at": {"$lte": expire_time}})
            if result.deleted_count > 0:
                print(f"🔒 Auto-locked {result.deleted_count} movies (24 hrs expired).")
        except Exception as e:
            print(f"Auto-lock worker error: {e}")
        await asyncio.sleep(3600)

async def broadcast_queue_worker():
    while True:
        try:
            task_data = await broadcast_queue.get()
            await run_movie_broadcast(task_data['data'], task_data['selected_cats'], task_data['admin_id'])
            broadcast_queue.task_done()
        except Exception as e:
            print(f"Queue Worker Error: {e}")
            await asyncio.sleep(5)

@app.on_event("startup")
async def on_startup():
    await init_db()
    await load_admins()
    await load_banned_users()
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(broadcast_queue_worker())
    asyncio.create_task(auto_lock_worker())

# ==========================================
# 6. Telegram Bot Commands
# ==========================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache:
        return await message.answer("🚫 আপনাকে ব্যান করা হয়েছে।", parse_mode="HTML")
        
    await state.clear()
    now = datetime.datetime.utcnow()
    user = await db.users.find_one({"user_id": uid})
    
    if not user:
        args = message.text.split(" ")
        if len(args) > 1 and args[1].startswith("ref_"):
            try:
                referrer_id = int(args[1].split("_")[1])
                if referrer_id != uid:
                    await db.users.update_one({"user_id": referrer_id}, {"$inc": {"refer_count": 1}})
                    ref_user = await db.users.find_one({"user_id": referrer_id})
                    if ref_user and ref_user.get("refer_count", 0) % 5 == 0:
                        current_vip = ref_user.get("vip_until", now)
                        if current_vip < now: current_vip = now
                        await db.users.update_one({"user_id": referrer_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=1)}})
                        try: await bot.send_message(referrer_id, "🎉 ৫ জন রেফারের জন্য ২৪ ঘণ্টা VIP!", parse_mode="HTML")
                        except: pass
            except: pass
        await db.users.insert_one({"user_id": uid, "first_name": message.from_user.first_name, "joined_at": now, "refer_count": 0, "coins": 0, "last_checkin": now - datetime.timedelta(days=2), "vip_until": now - datetime.timedelta(days=1)})
    else:
        await db.users.update_one({"user_id": uid}, {"$set": {"first_name": message.from_user.first_name}})

    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"

    kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]
    ]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n🔥 <b>BD Viral Box</b> জগতে আপনাকে স্বাগতম। নিচের বাটনে ক্লিক করে উপভোগ করুন।"
    if uid in admin_cache: text += "\n\n⚙️ <b>অ্যাডমিন মোড অন.</b>"
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

@dp.message(Command("stats"))
async def bot_stats(m: types.Message):
    if m.from_user.id not in admin_cache: return
    total_users = await db.users.count_documents({})
    total_movies = await db.movies.count_documents({})
    vip_users = await db.users.count_documents({"vip_until": {"$gt": datetime.datetime.utcnow()}})
    text = f"📊 <b>Bot Statistics</b>\n\n👥 Total Users: <b>{total_users}</b>\n💎 VIP Users: <b>{vip_users}</b>\n🎬 Total Movies: <b>{total_movies}</b>"
    await m.answer(text, parse_mode="HTML")

@dp.message(Command("ban"))
async def ban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.update_one({"user_id": uid}, {"$set": {"user_id": uid}}, upsert=True)
        banned_cache.add(uid)
        await m.answer(f"🚫 User <code>{uid}</code> ব্যান করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /ban USER_ID", parse_mode="HTML")

@dp.message(Command("unban"))
async def unban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": uid})
        banned_cache.discard(uid)
        await m.answer(f"✅ User <code>{uid}</code> আনব্যান করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /unban USER_ID", parse_mode="HTML")

@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def handle_user_messages(m: types.Message):
    if m.content_type not in ['text']:
        await m.answer("⚠️ দুঃখিত! আমি শুধুমাত্র টেক্সট মেসেজ গ্রহণ করি।\n\n🎬 দেখতে নিচের 'Watch Now' বাটনে ক্লিক করুন।", parse_mode="HTML")
        return
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✍️ রিপ্লাই", callback_data=f"reply_{m.from_user.id}")
        await bot.send_message(OWNER_ID, f"📩 <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name}</a>:\n\n{m.text}", parse_mode="HTML", reply_markup=builder.as_markup())
    except: pass

@dp.callback_query(F.data.startswith("reply_"))
async def reply_to_user_callback(c: types.CallbackQuery, state: FSMContext):
    if c.from_user.id not in admin_cache: return
    user_id = int(c.data.split("_")[1])
    await state.set_state(AdminStates.waiting_for_reply)
    await state.update_data(reply_user_id=user_id)
    await c.message.answer("✍️ আপনার মেসেজ লিখুন (রিপ্লাই দেওয়ার জন্য):")
    await c.answer()

@dp.message(AdminStates.waiting_for_reply)
async def send_reply_to_user(m: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("reply_user_id")
    await state.clear()
    if user_id:
        try:
            await m.copy_to(chat_id=user_id)
            await m.answer("✅ রিপ্লাই পাঠানো হয়েছে!")
        except:
            await m.answer("❌ রিপ্লাই পাঠাতে ব্যর্থ হয়েছে।")

# ==========================================
# 7. Admin Commands & Movie Upload
# ==========================================
@dp.message(Command("cancel"))
async def cancel_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.clear()
    await m.answer("❌ বর্তমান প্রসেস বাতিল করা হয়েছে!", parse_mode="HTML")

@dp.message(Command("protect"))
async def toggle_protect(m: types.Message):
    if m.from_user.id not in admin_cache: return
    cfg = await db.settings.find_one({"id": "protect_content"})
    current = cfg.get("status", False) if cfg else False
    new_status = not current
    await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": new_status}}, upsert=True)
    status_text = "অন 🔒" if new_status else "অফ 🔓"
    await m.answer(f"✅ ফরোয়ার্ড প্রোটেকশন এখন <b>{status_text}</b>", parse_mode="HTML")

@dp.message(Command("setadcount"))
async def set_ad_count(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        count = int(m.text.split()[1])
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": count}}, upsert=True)
        await m.answer(f"✅ অ্যাড সংখ্যা <b>{count}</b> এ সেট করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /setadcount 2", parse_mode="HTML")

@dp.message(Command("settime"))
async def set_delete_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        minutes = int(m.text.split()[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": minutes}}, upsert=True)
        await m.answer(f"✅ অটো-ডিলিট টাইম <b>{minutes} মিনিট</b> এ সেট করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settime 60 (মিনিট লিখুন)", parse_mode="HTML")

@dp.message(Command("addlink"))
async def add_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ অ্যাড জোন লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /addlink url", parse_mode="HTML")

@dp.message(Command("addadultlink"))
async def add_adult_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ ১৮+ অ্যাড লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /addadultlink url", parse_mode="HTML")

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "tg_link"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ টেলিগ্রাম চ্যানেল লিংক আপডেট হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settg https://t.me/...", parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: await m.answer(f"✅ '<b>{title}</b>' ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ পাওয়া যায়নি")
    except: await m.answer("⚠️ /delmovie মুভির নাম", parse_mode="HTML")

@dp.message(Command("addvip"))
async def add_vip_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        args = m.text.split()
        target_uid = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30 
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": target_uid})
        if not user: return await m.answer("⚠️ ইউজার নেই।")
        current_vip = user.get("vip_until", now)
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": target_uid}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await m.answer(f"✅ <code>{target_uid}</code> কে {days} দিনের VIP দেওয়া হয়েছে!", parse_mode="HTML")
    except: await m.answer("⚠️ /addvip ID দিন", parse_mode="HTML")

@dp.message(Command("addupcoming"))
async def add_upcoming_start(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_upc_photo)
    await m.answer("🌟 আপকামিং মুভির <b>পোস্টার</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_photo, F.photo)
async def receive_upc_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_upc_title)
    await m.answer("✅ এবার <b>মুভির নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_title, F.text)
async def receive_upc_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_upc_date)
    await m.answer("✅ এবার <b>রিলিজ তারিখ</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_upc_date, F.text)
async def receive_upc_date(m: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await db.upcoming.insert_one({"title": data["title"], "photo_id": data["photo_id"], "release_date": m.text.strip()})
    await m.answer(f"🌟 <b>{data['title']}</b> আপকামিং লিস্টে যুক্ত হয়েছে!", parse_mode="HTML")

# ==========================================
# 7.5 Single Movie Upload
# ==========================================
@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_movie_file(m: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await m.answer("⚠️ আপনি অন্য একটি প্রসেসে আটকে আছেন! আগে /cancel করুন।", parse_mode="HTML")
        return
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    await state.set_state(AdminStates.waiting_for_photo)
    await state.update_data(file_id=fid, file_type=ftype, categories=[])
    await m.answer("✅ ফাইল পেয়েছি! এবার <b>পোস্টার</b> পাঠান।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_movie_photo(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ এবার <b>মুভি/সিরিজের নাম</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_photo)
async def fallback_photo(m: types.Message):
    await m.answer("⚠️ পোস্টার হিসেবে শুধুমাত্র <b>ছবি (Photo)</b> পাঠান। ফাইল হিসেবে পাঠাবেন না। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_movie_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_quality)
    await m.answer("✅ এবার <b>এপিসোড বা কোয়ালিটি</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title)
async def fallback_title(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>মুভির নাম (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    await state.set_state(AdminStates.waiting_for_year)
    await m.answer("✅ এবার <b>রিলিজ সাল</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality)
async def fallback_quality(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>কোয়ালিটি (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_year, F.text)
async def receive_movie_year(m: types.Message, state: FSMContext):
    await state.update_data(year=m.text.strip())
    await state.set_state(AdminStates.waiting_for_cats)
    
    builder = InlineKeyboardBuilder()
    for index, cat in enumerate(CATEGORIES): 
        builder.button(text=cat, callback_data=f"selcat_{index}")
    builder.button(text="✅ Done", callback_data="cats_done")
    builder.adjust(2) 
    await m.answer("✅ এবার <b>ক্যাটাগরি সিলেক্ট</b> করুন।", reply_markup=builder.as_markup(), parse_mode="HTML")

@dp.message(AdminStates.waiting_for_year)
async def fallback_year(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>রিলিজ সাল (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.callback_query(AdminStates.waiting_for_cats, F.data.startswith("selcat_"))
async def process_category_selection(c: types.CallbackQuery, state: FSMContext):
    index = int(c.data.split("_")[1])
    cat = CATEGORIES[index]
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if cat in selected_cats: selected_cats.remove(cat)
    else: selected_cats.append(cat)
    await state.update_data(categories=selected_cats)
    
    builder = InlineKeyboardBuilder()
    for i, ct in enumerate(CATEGORIES):
        prefix = "✅ " if ct in selected_cats else ""
        builder.button(text=f"{prefix}{ct}", callback_data=f"selcat_{i}")
    builder.button(text="✅ Done", callback_data="cats_done")
    builder.adjust(3)
    await c.message.edit_reply_markup(reply_markup=builder.as_markup())
    await c.answer()

@dp.callback_query(AdminStates.waiting_for_cats, F.data == "cats_done")
async def finish_category_selection(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    if not selected_cats: return await c.answer("⚠️ অন্তত ১টি সিলেক্ট করুন!", show_alert=True)
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 New Video (Broadcast & Log)", callback_data="action_new_bcast")
    builder.button(text="➕ Add File Only (No Broadcast)", callback_data="action_add_file")
    builder.adjust(1)
    await c.message.edit_text(
        "✅ সব তথ্য নেওয়া হয়েছে!\n\n👇 এখন আপনি কি করতে চান তা নিচের যেকোনো একটি বাটনে ক্লিক করে নির্বাচন করুন:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await c.answer()

@dp.callback_query(F.data == "action_new_bcast")
async def action_new_broadcast(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    await state.clear()
    
    await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "year": data.get("year", "N/A"), "categories": selected_cats, "clicks": 0, "created_at": datetime.datetime.utcnow()})
    
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n⏳ <b>ব্রডকাস্ট কিউতে যোগ করা হয়েছে...</b>\nআপনি চাইলে আরও আপলোড করতে পারেন, বট একটি একটি করে ইউজারদের কাছে মেসেজ পাঠাবে।", parse_mode="HTML")
    
    if LOG_CHANNEL_ID:
        try:
            log_kb = [
                [types.InlineKeyboardButton(text="🎬 Watch Now", url="https://t.me/MovieeBoxx_Bot?start=new")],
                [types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")],
                [types.InlineKeyboardButton(text="📝 Request Movie", url="https://t.me/requestmoviebox")]
            ]
            log_markup = types.InlineKeyboardMarkup(inline_keyboard=log_kb)
            log_text = f"🎬 <b>New Video Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n📂 Categories: {', '.join(selected_cats)}\n\n👤 Uploaded by Admin"
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=log_markup)
        except: pass

    await broadcast_queue.put({"data": data, "selected_cats": selected_cats, "admin_id": c.from_user.id})
    await c.answer("🚀 ব্রডকাস্ট শুরু হচ্ছে...")

@dp.callback_query(F.data == "action_add_file")
async def action_add_file_only(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_cats = data.get("categories", [])
    await state.clear()
    
    await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "year": data.get("year", "N/A"), "categories": selected_cats, "clicks": 0, "created_at": datetime.datetime.utcnow()})
    
    await c.message.edit_text(f"✅ <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n❌ কোনো ব্রডকাস্ট বা লগ পোস্ট করা হয়নি। ফাইলটি শুধুমাত্র ওয়েব অ্যাপে যুক্ত হয়েছে।", parse_mode="HTML")
    await c.answer("✅ ফাইল অ্যাড হয়েছে!")

async def run_movie_broadcast(data, selected_cats, admin_id):
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    web_app_url = APP_URL if APP_URL else "https://t.me/" 
    bcast_kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=web_app_url))], 
        [types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link)],
        [types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)],
        [types.InlineKeyboardButton(text="📝 Request Movie", url="https://t.me/requestmoviebox")],
    ]
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = f"🆕 <b>New Video Alert!</b>\n\n🔥 <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n📅 Year: <b>{data.get('year', 'N/A')}</b>\n\n👇 এখনই দেখুন!"
    
    now = datetime.datetime.utcnow()
    delete_at = now + datetime.timedelta(days=1) 
    
    async for u in db.users.find():
        try:
            sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
            bcast_success += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
                await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
                bcast_success += 1
            except: pass
        except: pass
        
    try:
        await bot.send_message(admin_id, f"✅ <b>{data['title']}</b> এর ব্রডকাস্ট শেষ!\n\nসফলভাবে পাঠানো হয়েছে: <b>{bcast_success}</b> জনকে।\n⏳ নোটিফিকেশনগুলো <b>২৪ ঘণ্টা</b> পর অটো-ডিলিট হবে।", parse_mode="HTML")
    except: pass

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 ব্রডকাস্ট মেসেজ পাঠান। (ভিডিও/ছবি/টেক্সট যেটা পাঠাবেন সেটাই হুবহু সবার কাছে যাবে, কোনো বাটন যুক্ত হবে না)\n\n⚠️ বাতিল করতে /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    if m.text and m.text.startswith("/"):
        await state.clear()
        await m.answer("⚠️ ব্রডকাস্ট বাতিল হয়েছে।", parse_mode="HTML")
        return
    if m.reply_to_message:
        await state.clear()
        await m.answer("⚠️ ব্রডকাস্ট বাতিল করা হয়েছে কারণ আপনি রিপ্লাই করেছেন!", parse_mode="HTML")
        return
    await state.clear()
    prog_msg = await m.answer("⏳ <b>Broadcast started in background...</b>", parse_mode="HTML")
    asyncio.create_task(run_manual_broadcast(m, prog_msg, m.from_user.id))

async def run_manual_broadcast(m, prog_msg, admin_id):
    total_users = await db.users.count_documents({})
    success = 0
    blocked = 0
    async for u in db.users.find():
        try: 
            await m.copy_to(chat_id=u['user_id'])
            success += 1
            await asyncio.sleep(0.05)
        except: 
            blocked += 1
            
    stats_text = f"✅ <b>Broadcast Complete!</b>\n\n👥 Total Users: <b>{total_users}</b>\n✅ Successful: <b>{success}</b>\n🚫 Blocked Users: <b>{blocked}</b>"
    try:
        await prog_msg.edit_text(stats_text, parse_mode="HTML")
    except:
        try: await bot.send_message(admin_id, stats_text, parse_mode="HTML")
        except: pass

@dp.callback_query(F.data.startswith("trx_"))
async def handle_trx_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action = c.data.split("_")[1]; pay_id = c.data.split("_")[2]
    payment = await db.payments.find_one({"_id": ObjectId(pay_id)})
    if not payment or payment["status"] != "pending": return await c.answer("⚠️ প্রসেস করা হয়েছে!", show_alert=True)
    user_id = payment["user_id"]; days = payment["days"]
    if action == "approve":
        now = datetime.datetime.utcnow(); user = await db.users.find_one({"user_id": user_id})
        current_vip = user.get("vip_until", now) if user else now
        if current_vip < now: current_vip = now
        await db.users.update_one({"user_id": user_id}, {"$set": {"vip_until": current_vip + datetime.timedelta(days=days)}})
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "approved"}})
        await c.message.edit_text(c.message.text + "\n\n✅ <b>অ্যাপ্রুভ!</b>", parse_mode="HTML")
    else:
        await db.payments.update_one({"_id": ObjectId(pay_id)}, {"$set": {"status": "rejected"}})
        await c.message.edit_text(c.message.text + "\n\n❌ <b>রিজেক্ট!</b>", parse_mode="HTML")

# ==========================================
# 8. Web Admin Panel API & UI
# ==========================================
@app.get("/panel", response_class=HTMLResponse)
async def admin_panel_ui(auth: bool = Depends(verify_admin)):
    html_code = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin Panel - BD Viral Box</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <style>
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; color: #cbd5e1; margin: 0; padding: 20px; }
            .header { text-align: center; margin-bottom: 30px; color: #fff; }
            .header h1 { margin: 0; font-size: 28px; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-bottom: 40px; }
            .stat-card { background: #1e293b; padding: 20px; border-radius: 16px; border: 1px solid #334155; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
            .stat-card h3 { margin: 0 0 10px 0; font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }
            .stat-card .value { font-size: 32px; font-weight: 800; color: #fff; }
            .stat-card.users .value i { color: #3b82f6; } .stat-card.today-users .value i { color: #10b981; } .stat-card.clicks .value i { color: #f59e0b; } .stat-card.today-clicks .value i { color: #ef4444; }
            .stat-card.live-users { border-color: #10b981; } .stat-card.live-users .value { color: #10b981; }
            .table-container { background: #1e293b; border-radius: 16px; border: 1px solid #334155; overflow-x: auto; }
            .table-header { padding: 20px; border-bottom: 1px solid #334155; display: flex; justify-content: space-between; align-items: center; }
            .table-header h2 { margin: 0; color: #fff; font-size: 20px; }
            table { width: 100%; border-collapse: collapse; min-width: 600px; } th { text-align: left; padding: 15px; color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid #334155; } td { padding: 15px; border-bottom: 1px solid #334155; font-size: 14px; color: #e2e8f0; } tr:last-child td { border-bottom: none; } tr:hover { background: rgba(255,255,255,0.03); }
            .view-badge { background: rgba(59, 130, 246, 0.2); color: #60a5fa; padding: 4px 10px; border-radius: 12px; font-weight: 600; font-size: 12px; }
            .delete-btn { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); padding: 6px 12px; border-radius: 8px; cursor: pointer; font-weight: 600; transition: 0.2s; } .delete-btn:hover { background: #ef4444; color: white; }
            .empty-state { text-align: center; padding: 40px; color: #64748b; }
            .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 2000; align-items: center; justify-content: center; }
            .modal-content { background: #1e293b; padding: 25px; border-radius: 12px; width: 90%; max-width: 400px; color: #fff; }
            .modal-content h3 { margin-top: 0; color: #ef4444; }
            .form-group { margin-bottom: 15px; }
            .form-group label { display: block; margin-bottom: 5px; color: #94a3b8; font-size: 14px; }
            .form-group input { width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #334155; background: #0f172a; color: #fff; box-sizing: border-box; }
            .modal-buttons { display: flex; gap: 10px; margin-top: 20px; }
            .btn-save { background: #22B8FF; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: bold; flex: 1; }
            .btn-cancel { background: #334155; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; flex: 1; }
        </style>
    </head>
    <body>
        <div class="header"><h1><i class="fa-solid fa-shield-halved"></i> Admin Panel</h1><p>BD Viral Box Control Center</p></div>
        <div class="stats-grid">
            <div class="stat-card users"><h3>Total Users</h3><div class="value"><i class="fa-solid fa-users"></i> <span id="totalUsers">0</span></div></div>
            <div class="stat-card today-users"><h3>Today's New Users</h3><div class="value"><i class="fa-solid fa-user-plus"></i> <span id="todayUsers">0</span></div></div>
            <div class="stat-card clicks"><h3>Total Clicks</h3><div class="value"><i class="fa-solid fa-eye"></i> <span id="totalClicks">0</span></div></div>
            <div class="stat-card today-clicks"><h3>Today's Clicks</h3><div class="value"><i class="fa-solid fa-chart-line"></i> <span id="todayClicks">0</span></div></div>
            <div class="stat-card live-users"><h3>Live Active (1m)</h3><div class="value"><i class="fa-solid fa-signal"></i> <span id="activeUsers">0</span></div></div>
        </div>
        <div class="table-container"><div class="table-header">
    <h2><i class="fa-solid fa-film"></i> Uploaded Videos</h2>
    <input type="text" id="movieSearchInput" placeholder="🔍 Search..." style="padding: 8px 12px; border-radius: 8px; border: 1px solid #334155; background: #0f172a; color: #fff; outline: none; width: 150px;">
</div><table><thead><tr><th>Title</th><th>Quality</th><th>Category</th><th>Views</th><th>Action</th></tr></thead><tbody id="movieTableBody"><tr><td colspan="5" class="empty-state">Loading data...</td></tr></tbody></table></div>

        <div id="editModal" class="modal">
            <div class="modal-content">
                <h3>✏️ Edit Video</h3>
                <input type="hidden" id="editId">
                <div class="form-group"><label>Title</label><input type="text" id="editTitle"></div>
                <div class="form-group"><label>Poster Photo ID</label><input type="text" id="editPhoto" placeholder="Paste new Telegram File ID"></div>
                <div class="form-group"><label>Quality</label><input type="text" id="editQuality"></div>
                <div class="form-group"><label>Year</label><input type="text" id="editYear"></div>
                <div class="form-group"><label>Categories (Comma separated)</label><input type="text" id="editCategories" placeholder="e.g. Action, Thriller"></div>
                <div class="modal-buttons">
                    <button class="btn-save" onclick="saveMovieEdit()">💾 Save</button>
                    <button class="btn-cancel" onclick="closeEditModal()">❌ Cancel</button>
                </div>
            </div>
        </div>

        <script>
            async function fetchStats() { try { const res = await fetch('/api/admin/stats'); const data = await res.json(); document.getElementById('totalUsers').innerText = data.total_users; document.getElementById('todayUsers').innerText = data.today_users; document.getElementById('totalClicks').innerText = data.total_clicks; document.getElementById('todayClicks').innerText = data.today_clicks; document.getElementById('activeUsers').innerText = data.active_users; } catch(e) {} }
            let allMovies = [];
            async function fetchMovies() { try { const res = await fetch('/api/admin/movies'); allMovies = await res.json(); renderMovies(allMovies); } catch(e) {} }
            function renderMovies(moviesToRender) {
                const tbody = document.getElementById('movieTableBody'); 
                if(moviesToRender.length === 0) { tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No videos found.</td></tr>'; return; } 
                tbody.innerHTML = moviesToRender.map(m => `<tr id="row-${m._id}"><td><strong>${m.title}</strong><br><small>ID: ${m._id}</small></td><td>${m.quality || 'N/A'}</td><td>${(m.categories || []).join(', ')}</td><td><span class="view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</span></td><td><button class="delete-btn" onclick="deleteMovie('${m._id}')" style="margin-right:5px;"><i class="fa-solid fa-trash"></i></button><button class="btn-save" onclick="openEditModal('${m._id}')" style="padding:6px 12px;font-size:12px;border-radius:4px;cursor:pointer;">✏️</button></td></tr>`).join(''); 
            }
            document.getElementById('movieSearchInput').addEventListener('input', function(e) {
                const s = e.target.value.toLowerCase();
                renderMovies(allMovies.filter(m => (m.title||'').toLowerCase().includes(s) || (m.quality||'').toLowerCase().includes(s) || (m.categories||[]).join(' ').toLowerCase().includes(s)));
            });
            async function deleteMovie(id) { if(!confirm("Delete?")) return; try { const r = await fetch(`/api/admin/movie/${id}`, {method:'DELETE'}); const d = await r.json(); if(d.ok) { document.getElementById(`row-${id}`).remove(); fetchStats(); } } catch(e) {} }
            function openEditModal(id) { const m = allMovies.find(x=>x._id===id); if(!m) return; document.getElementById('editId').value=m._id; document.getElementById('editTitle').value=m.title||''; document.getElementById('editPhoto').value=m.photo_id||''; document.getElementById('editQuality').value=m.quality||''; document.getElementById('editYear').value=m.year||''; document.getElementById('editCategories').value=(m.categories||[]).join(', '); document.getElementById('editModal').style.display='flex'; }
            function closeEditModal() { document.getElementById('editModal').style.display='none'; }
            async function saveMovieEdit() { const id=document.getElementById('editId').value; const data={title:document.getElementById('editTitle').value,photo_id:document.getElementById('editPhoto').value,quality:document.getElementById('editQuality').value,year:document.getElementById('editYear').value,categories:document.getElementById('editCategories').value.split(',').map(s=>s.trim()).filter(s=>s!=='')}; try { const r=await fetch(`/api/admin/movie/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); const result=await r.json(); if(result.ok){alert('✅ Updated!');closeEditModal();fetchMovies();}else{alert('❌ '+(result.detail||'Error'));} } catch(e){alert('❌ Error');} }
            fetchStats(); fetchMovies(); setInterval(fetchStats, 60000);
        </script>
    </body></html>'''
    return HTMLResponse(html_code)

@app.get("/api/admin/stats")
async def admin_stats(auth: bool = Depends(verify_admin)):
    now = datetime.datetime.utcnow(); today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await db.users.count_documents({}); today_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    one_min_ago = now - datetime.timedelta(minutes=1); active_users = await db.users.count_documents({"last_active": {"$gte": one_min_ago}})
    total_clicks_res = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1); total_clicks = total_clicks_res[0]["total"] if total_clicks_res else 0
    today_clicks = await db.user_unlocks.count_documents({"unlocked_at": {"$gte": today_start}})
    return {"total_users": total_users, "today_users": today_users, "active_users": active_users, "total_clicks": total_clicks, "today_clicks": today_clicks}

@app.get("/api/movies/trending")
async def get_trending_movies():
    try:
        now = datetime.datetime.utcnow()
        thirty_days_ago = now - datetime.timedelta(days=30)
        movies = await db.movies.find({"created_at": {"$gte": thirty_days_ago}}).sort("clicks", -1).limit(10).to_list(10)
        for m in movies: m["_id"] = str(m["_id"])
        return movies
    except Exception as e:
        return []

@app.get("/api/movies/recent")
async def get_recent_movies():
    try:
        movies = await db.movies.find({}).sort("created_at", -1).limit(10).to_list(10)
        for m in movies: m["_id"] = str(m["_id"])
        return movies
    except Exception as e:
        return []

@app.get("/api/admin/movies")
async def admin_movies(auth: bool = Depends(verify_admin)):
    movies = await db.movies.find({}).sort("created_at", -1).to_list(1000)
    for m in movies: m["_id"] = str(m["_id"])
    return movies

@app.delete("/api/admin/movie/{movie_id}")
async def delete_movie(movie_id: str, auth: bool = Depends(verify_admin)):
    result = await db.movies.delete_one({"_id": ObjectId(movie_id)})
    if result.deleted_count == 1: return {"ok": True}
    raise HTTPException(status_code=404, detail="Movie not found")

@app.post("/api/user/ping")
async def user_ping(request: Request):
    try:
        body = await request.json()
        user_id = body.get("user_id")
        if user_id:
            await db.users.update_one({"user_id": user_id}, {"$set": {"last_active": datetime.datetime.utcnow()}})
        return {"ok": True}
    except:
        return {"ok": False}

@app.put("/api/admin/movie/{movie_id}")
async def update_movie(movie_id: str, movie_data: dict = Body(...), auth: bool = Depends(verify_admin)):
    update_data = {k: v for k, v in movie_data.items() if v is not None and v != ""}
    result = await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$set": update_data})
    if result.modified_count > 0:
        return {"ok": True, "message": "Updated successfully"}
    raise HTTPException(status_code=400, detail="Failed to update")

# ==========================================
# Get Photo ID for Admin Panel
# ==========================================
@dp.message(F.photo, StateFilter(None))
async def get_file_id_for_admin(message: types.Message):
    if message.from_user.id not in admin_cache: 
        return
    file_id = message.photo[-1].file_id
    await message.answer(
        f"🖼️ <b>Photo File ID:</b>\n\n<code>{file_id}</code>\n\n✅ এই আইডিটি কপি করে Admin Panel এ 'Poster Photo ID' বক্সে পেস্ট করুন।", 
        parse_mode="HTML"
    )

# ==========================================
# 9. Main Web App UI
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    dl_cfg = await db.settings.find_one({"id": "direct_links"}); direct_links = dl_cfg.get('links', []) if dl_cfg else []; dl_json = json.dumps(direct_links)
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); adult_direct_links = adl_cfg.get('links', []) if adl_cfg else []; adl_json = json.dumps(adult_direct_links)

    html_code = '''
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>BD Viral Box</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { background: #0f172a; font-family: 'Inter', sans-serif; color: #fff; overscroll-behavior-y: none; }
            
            /* ===== WELCOME SCREEN ===== */
            #welcomeScreen {
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: linear-gradient(135deg, #0f172a 0%, #1a0a0a 50%, #0f172a 100%);
                display: flex; flex-direction: column; align-items: center; justify-content: center;
                z-index: 9999; transition: opacity 0.5s, transform 0.5s;
            }
            #welcomeScreen.hide { opacity: 0; transform: scale(1.1); pointer-events: none; }
            .welcome-logo {
                width: 120px; height: 120px; border-radius: 30px;
                background: linear-gradient(135deg, #ff416c, #ff4b2b);
                display: flex; align-items: center; justify-content: center;
                font-size: 50px; margin-bottom: 25px;
                box-shadow: 0 10px 40px rgba(255, 65, 108, 0.4);
                animation: pulse-glow 2s ease-in-out infinite;
            }
            @keyframes pulse-glow {
                0%, 100% { box-shadow: 0 10px 40px rgba(255, 65, 108, 0.4); }
                50% { box-shadow: 0 10px 60px rgba(255, 65, 108, 0.7); }
            }
            .welcome-title {
                font-size: 36px; font-weight: 900;
                background: linear-gradient(45deg, #ff416c, #ff4b2b);
                -webkit-background-clip: text; -webkit-text-fill-color: transparent;
                margin-bottom: 8px; letter-spacing: -1px;
            }
            .welcome-tagline { font-size: 14px; color: #94a3b8; margin-bottom: 40px; }
            .welcome-btn {
                background: linear-gradient(135deg, #ff416c, #ff4b2b);
                color: white; border: none; padding: 14px 50px; border-radius: 50px;
                font-size: 16px; font-weight: 700; cursor: pointer;
                box-shadow: 0 5px 25px rgba(255, 65, 108, 0.4);
                transition: transform 0.2s, box-shadow 0.2s;
            }
            .welcome-btn:active { transform: scale(0.95); }
            .welcome-bot { position: absolute; bottom: 30px; color: #475569; font-size: 13px; }

            /* ===== MAIN APP ===== */
            #mainApp { display: none; padding-bottom: 80px; }
            .app-header {
                padding: 16px 20px; display: flex; align-items: center; justify-content: space-between;
                background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px);
                position: sticky; top: 0; z-index: 100; border-bottom: 1px solid rgba(255,255,255,0.05);
            }
            .app-logo { font-size: 20px; font-weight: 800; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            
            /* Search */
            .search-container { padding: 12px 20px; }
            .search-box {
                display: flex; align-items: center; background: #1e293b; border-radius: 12px;
                padding: 12px 16px; border: 1px solid #334155;
            }
            .search-box i { color: #64748b; margin-right: 12px; }
            .search-box input {
                flex: 1; background: none; border: none; outline: none; color: #fff;
                font-size: 14px; font-family: 'Inter', sans-serif;
            }
            .search-box input::placeholder { color: #64748b; }

            /* Category - Only HOME */
            .category-section { padding: 8px 20px 16px; }
            .category-btn {
                background: linear-gradient(135deg, #ff416c, #ff4b2b);
                color: white; border: none; padding: 10px 28px; border-radius: 25px;
                font-size: 13px; font-weight: 700; cursor: pointer;
                box-shadow: 0 4px 15px rgba(255, 65, 108, 0.3);
                transition: transform 0.2s;
            }
            .category-btn:active { transform: scale(0.95); }
            .category-btn.active { box-shadow: 0 4px 20px rgba(255, 65, 108, 0.6); }

            /* Section Title */
            .section-title {
                padding: 10px 20px; font-size: 18px; font-weight: 800;
                display: flex; align-items: center; gap: 10px;
            }
            .section-title .fire { color: #ff416c; }

            /* Movie Grid */
            .movie-grid { padding: 0 20px; display: flex; flex-direction: column; gap: 14px; }
            .movie-card {
                display: flex; gap: 14px; background: #1e293b; border-radius: 16px;
                overflow: hidden; border: 1px solid #334155; cursor: pointer;
                transition: transform 0.2s, border-color 0.2s; position: relative;
            }
            .movie-card:active { transform: scale(0.98); border-color: #ff416c; }
            .movie-card-rank {
                position: absolute; top: 10px; left: 10px; background: rgba(0,0,0,0.7);
                color: #ff416c; font-size: 11px; font-weight: 800; padding: 3px 8px;
                border-radius: 6px; z-index: 2;
            }
            .movie-card-badge {
                position: absolute; top: 10px; right: 10px; background: #ef4444;
                color: white; font-size: 10px; font-weight: 800; padding: 3px 8px;
                border-radius: 6px; z-index: 2;
            }
            .movie-poster {
                width: 110px; min-height: 150px; object-fit: cover; flex-shrink: 0;
                background: #334155;
            }
            .movie-info {
                flex: 1; padding: 14px 14px 14px 0; display: flex; flex-direction: column; justify-content: center;
            }
            .movie-title { font-size: 14px; font-weight: 700; margin-bottom: 6px; line-height: 1.3; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
            .movie-meta { font-size: 12px; color: #94a3b8; margin-bottom: 10px; display: flex; gap: 10px; }
            .movie-meta span { display: flex; align-items: center; gap: 4px; }
            .movie-play-btn {
                display: inline-flex; align-items: center; gap: 6px;
                background: linear-gradient(135deg, #ff416c, #ff4b2b);
                color: white; border: none; padding: 8px 18px; border-radius: 20px;
                font-size: 12px; font-weight: 700; cursor: pointer; width: fit-content;
            }

            /* Detail Page */
            #detailPage {
                display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: #0f172a; z-index: 500; overflow-y: auto; padding-bottom: 100px;
            }
            .detail-back {
                position: sticky; top: 0; z-index: 10; background: rgba(15,23,42,0.95);
                backdrop-filter: blur(10px); padding: 16px 20px; display: flex; align-items: center; gap: 12px;
                border-bottom: 1px solid rgba(255,255,255,0.05); cursor: pointer;
            }
            .detail-back i { font-size: 20px; color: #ff416c; }
            .detail-poster { width: 100%; max-height: 400px; object-fit: cover; }
            .detail-info { padding: 20px; }
            .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 10px; line-height: 1.3; }
            .detail-meta { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
            .detail-meta span {
                background: #1e293b; padding: 6px 14px; border-radius: 20px;
                font-size: 12px; color: #94a3b8; border: 1px solid #334155;
            }
            .detail-download-btn {
                display: flex; align-items: center; justify-content: center; gap: 10px;
                background: linear-gradient(135deg, #ff416c, #ff4b2b);
                color: white; border: none; padding: 16px; border-radius: 16px;
                font-size: 16px; font-weight: 800; width: 100%; cursor: pointer;
                box-shadow: 0 5px 25px rgba(255, 65, 108, 0.4); margin-bottom: 16px;
            }
            .detail-ad-section { margin-top: 10px; }
            .detail-ad-link {
                display: block; background: #1e293b; border: 1px solid #334155; border-radius: 12px;
                padding: 14px 16px; color: #60a5fa; text-decoration: none; font-size: 13px;
                font-weight: 600; margin-bottom: 10px; text-align: center;
                transition: border-color 0.2s;
            }
            .detail-ad-link:active { border-color: #60a5fa; }

            /* Bottom Nav */
            .bottom-nav {
                position: fixed; bottom: 0; left: 0; width: 100%;
                background: rgba(15, 23, 42, 0.98); backdrop-filter: blur(10px);
                display: flex; border-top: 1px solid rgba(255,255,255,0.05);
                z-index: 200; padding: 8px 0;
            }
            .nav-item {
                flex: 1; display: flex; flex-direction: column; align-items: center; gap: 4px;
                color: #64748b; font-size: 10px; font-weight: 600; cursor: pointer;
                padding: 6px 0; transition: color 0.2s;
            }
            .nav-item.active { color: #ff416c; }
            .nav-item i { font-size: 20px; }

            /* Loading */
            .loading { text-align: center; padding: 40px; color: #64748b; }
            .loading i { font-size: 30px; animation: spin 1s linear infinite; margin-bottom: 10px; display: block; }
            @keyframes spin { 100% { transform: rotate(360deg); } }
            .no-results { text-align: center; padding: 60px 20px; color: #64748b; }
            .no-results i { font-size: 50px; margin-bottom: 15px; display: block; color: #334155; }

            /* Ad Banner */
            .ad-banner {
                margin: 10px 20px; background: linear-gradient(135deg, #1e293b, #334155);
                border-radius: 12px; padding: 14px; text-align: center;
                border: 1px solid #475569; cursor: pointer;
            }
            .ad-banner span { color: #f59e0b; font-size: 12px; font-weight: 700; }
            .ad-banner p { color: #e2e8f0; font-size: 13px; margin-top: 4px; }

            /* Scrollbar */
            ::-webkit-scrollbar { width: 0; }
        </style>
    </head>
    <body>

    <!-- ===== WELCOME SCREEN ===== -->
    <div id="welcomeScreen">
        <div class="welcome-logo">🎬</div>
        <div class="welcome-title">BD Viral Box</div>
        <div class="welcome-tagline">খারাপ দুনিয়ায় সাগরম</div>
        <button class="welcome-btn" onclick="enterApp()">▶ Enter Now</button>
        <div class="welcome-bot">@MovieBoxx_bot</div>
    </div>

    <!-- ===== MAIN APP ===== -->
    <div id="mainApp">
        <div class="app-header">
            <div class="app-logo">BD Viral Box</div>
        </div>

        <div class="search-container">
            <div class="search-box">
                <i class="fa-solid fa-magnifying-glass"></i>
                <input type="text" id="searchInput" placeholder="Search videos..." oninput="handleSearch()">
            </div>
        </div>

        <!-- Only HOME button -->
        <div class="category-section">
            <button class="category-btn active" onclick="loadHome()">🏠 HOME</button>
        </div>

        <div id="contentArea">
            <div class="section-title"><span class="fire">🔥</span> Trending Now</div>
            <div class="movie-grid" id="movieGrid">
                <div class="loading"><i class="fa-solid fa-spinner"></i>Loading...</div>
            </div>
        </div>

        <!-- Bottom Nav -->
        <div class="bottom-nav">
            <div class="nav-item active" onclick="loadHome()" id="navHome">
                <i class="fa-solid fa-house"></i><span>Home</span>
            </div>
            <div class="nav-item" onclick="focusSearch()" id="navSearch">
                <i class="fa-solid fa-magnifying-glass"></i><span>Search</span>
            </div>
        </div>
    </div>

    <!-- ===== DETAIL PAGE ===== -->
    <div id="detailPage">
        <div class="detail-back" onclick="closeDetail()">
            <i class="fa-solid fa-arrow-left"></i><span>Back</span>
        </div>
        <img class="detail-poster" id="detailPoster" src="" alt="">
        <div class="detail-info">
            <div class="detail-title" id="detailTitle"></div>
            <div class="detail-meta" id="detailMeta"></div>
            <button class="detail-download-btn" id="detailDownloadBtn" onclick="handleDownload()">
                <i class="fa-solid fa-download"></i> Download Now
            </button>
            <div class="detail-ad-section" id="detailAds"></div>
        </div>
    </div>

    <script>
        const tg = window.Telegram && window.Telegram.WebApp;
        if (tg) { tg.ready(); tg.expand(); }

        let userId = null;
        let userData = null;
        let allMovies = [];
        let currentMovie = null;
        let clickCount = 0;
        let directLinks = ''' + dl_json + ''';
        let adultDirectLinks = ''' + adl_json + ''';

        // Enter App
        function enterApp() {
            document.getElementById('welcomeScreen').classList.add('hide');
            setTimeout(() => {
                document.getElementById('welcomeScreen').style.display = 'none';
                document.getElementById('mainApp').style.display = 'block';
            }, 500);
            initUser();
            loadMovies();
        }

        // Init User
        async function initUser() {
            if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) {
                userId = tg.initDataUnsafe.user.id;
                try {
                    const res = await fetch('/api/user/init', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            user_id: userId,
                            first_name: tg.initDataUnsafe.user.first_name || '',
                            username: tg.initDataUnsafe.user.username || '',
                            init_data: tg.initData
                        })
                    });
                    userData = await res.json();
                } catch(e) {}
                // Ping
                setInterval(() => {
                    fetch('/api/user/ping', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({user_id: userId}) }).catch(()=>{});
                }, 60000);
            }
        }

        // Load Movies
        async function loadMovies() {
            try {
                const res = await fetch('/api/movies/recent');
                allMovies = await res.json();
                renderMovies(allMovies);
            } catch(e) {
                document.getElementById('movieGrid').innerHTML = '<div class="no-results"><i class="fa-solid fa-triangle-exclamation"></i>Failed to load</div>';
            }
        }

        // Render Movies
        function renderMovies(movies) {
            const grid = document.getElementById('movieGrid');
            if (!movies || movies.length === 0) {
                grid.innerHTML = '<div class="no-results"><i class="fa-solid fa-film"></i>No videos found</div>';
                return;
            }
            grid.innerHTML = movies.map((m, i) => `
                <div class="movie-card" onclick="openDetail('${m._id}')">
                    <div class="movie-card-rank">${String(i+1).padStart(2,'0')}</div>
                    <div class="movie-card-badge">18+</div>
                    <img class="movie-poster" src="https://api.telegram.org/file/bot${TOKEN || ''}/` + "`" + `${m.photo_id ? '' : ''}` + "`" + `" alt="${m.title}" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 110 150%22><rect fill=%22%231e293b%22 width=%22110%22 height=%22150%22/><text fill=%22%2364748b%22 x=%2255%22 y=%2275%22 text-anchor=%22middle%22 font-size=%2230%22>🎬</text></svg>'">
                    <div class="movie-info">
                        <div class="movie-title">${m.title}</div>
                        <div class="movie-meta">
                            <span><i class="fa-solid fa-star"></i> ${m.quality || 'N/A'}</span>
                            <span><i class="fa-solid fa-calendar"></i> ${m.year || 'N/A'}</span>
                            <span><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</span>
                        </div>
                        <div class="movie-play-btn"><i class="fa-solid fa-play"></i> Watch</div>
                    </div>
                </div>
            `).join('');
        }

        // Search
        function handleSearch() {
            const q = document.getElementById('searchInput').value.toLowerCase();
            if (!q) { renderMovies(allMovies); return; }
            const filtered = allMovies.filter(m => (m.title||'').toLowerCase().includes(q) || (m.quality||'').toLowerCase().includes(q));
            renderMovies(filtered);
        }

        function focusSearch() {
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            document.getElementById('navSearch').classList.add('active');
            document.getElementById('searchInput').focus();
        }

        function loadHome() {
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            document.getElementById('navHome').classList.add('active');
            document.getElementById('searchInput').value = '';
            renderMovies(allMovies);
            document.getElementById('contentArea').querySelector('.section-title').innerHTML = '<span class="fire">🔥</span> Trending Now';
        }

        // Open Detail
        async function openDetail(id) {
            const movie = allMovies.find(m => m._id === id);
            if (!movie) return;
            currentMovie = movie;

            document.getElementById('detailTitle').innerText = movie.title;
            document.getElementById('detailPoster').src = movie.poster_url || '';
            document.getElementById('detailMeta').innerHTML = `
                <span><i class="fa-solid fa-star"></i> ${movie.quality || 'N/A'}</span>
                <span><i class="fa-solid fa-calendar"></i> ${movie.year || 'N/A'}</span>
                <span><i class="fa-solid fa-eye"></i> ${movie.clicks || 0} views</span>
                ${(movie.categories||[]).map(c => `<span>${c}</span>`).join('')}
            `;

            // Ads
            const adLinks = (movie.categories||[]).some(c => c.toLowerCase().includes('adult')) ? adultDirectLinks : directLinks;
            document.getElementById('detailAds').innerHTML = adLinks.map(l => `<a class="detail-ad-link" href="${l}" target="_blank">${l}</a>`).join('');

            document.getElementById('detailPage').style.display = 'block';
            document.getElementById('mainApp').style.display = 'none';

            // Track click
            try { await fetch('/api/movie/click/' + id, {method:'POST'}); } catch(e) {}
        }

        function closeDetail() {
            document.getElementById('detailPage').style.display = 'none';
            document.getElementById('mainApp').style.display = 'block';
            currentMovie = null;
        }

        // Download
        async function handleDownload() {
            if (!currentMovie) return;
            clickCount++;
            const btn = document.getElementById('detailDownloadBtn');

            if (clickCount === 1) {
                btn.innerHTML = '<i class="fa-solid fa-arrow-up-right-from-square"></i> Open Ad First';
                btn.style.background = 'linear-gradient(135deg, #f59e0b, #d97706)';
                const adLinks = (currentMovie.categories||[]).some(c => c.toLowerCase().includes('adult')) ? adultDirectLinks : directLinks;
                if (adLinks.length > 0) { window.open(adLinks[0], '_blank'); }
            } else {
                btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Preparing...';
                btn.style.pointerEvents = 'none';
                try {
                    const res = await fetch('/api/movie/unlock/' + currentMovie._id, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({user_id: userId})
                    });
                    const data = await res.json();
                    if (data.file_url) {
                        window.open(data.file_url, '_blank');
                        btn.innerHTML = '<i class="fa-solid fa-check"></i> Opened!';
                        btn.style.background = 'linear-gradient(135deg, #10b981, #059669)';
                    } else {
                        btn.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> ' + (data.error || 'Try again');
                        btn.style.background = 'linear-gradient(135deg, #ef4444, #dc2626)';
                        btn.style.pointerEvents = 'auto';
                        clickCount = 0;
                    }
                } catch(e) {
                    btn.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Error';
                    btn.style.background = 'linear-gradient(135deg, #ef4444, #dc2626)';
                    btn.style.pointerEvents = 'auto';
                    clickCount = 0;
                }
            }
        }
    </script>
    </body>
    </html>'''
    return HTMLResponse(html_code)


# ==========================================
# 10. Additional API Endpoints
# ==========================================
@app.post("/api/user/init")
async def init_user(request: Request):
    try:
        body = await request.json()
        user_id = body.get("user_id")
        init_data = body.get("init_data", "")
        
        if not validate_tg_data(init_data):
            return {"error": "Invalid auth"}
        
        now = datetime.datetime.utcnow()
        user = await db.users.find_one({"user_id": user_id})
        if not user:
            await db.users.insert_one({
                "user_id": user_id,
                "first_name": body.get("first_name", ""),
                "username": body.get("username", ""),
                "joined_at": now,
                "refer_count": 0,
                "coins": 0,
                "last_checkin": now - datetime.timedelta(days=2),
                "vip_until": now - datetime.timedelta(days=1)
            })
        else:
            await db.users.update_one({"user_id": user_id}, {"$set": {"last_active": now}})
        
        is_vip = user["vip_until"] > now if user else False
        return {"ok": True, "is_vip": is_vip, "first_name": body.get("first_name", "")}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/movie/click/{movie_id}")
async def movie_click(movie_id: str):
    try:
        await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$inc": {"clicks": 1}})
        return {"ok": True}
    except:
        return {"ok": False}

@app.post("/api/movie/unlock/{movie_id}")
async def unlock_movie(movie_id: str, request: Request):
    try:
        body = await request.json()
        user_id = body.get("user_id")
        
        movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
        if not movie:
            return {"error": "Video not found"}
        
        # Check if already unlocked recently
        existing = await db.user_unlocks.find_one({"user_id": user_id, "movie_id": movie_id})
        if existing:
            file_url = f"https://api.telegram.org/file/bot{TOKEN}/{movie['file_id']}"
            return {"file_url": file_url}
        
        # Record unlock
        await db.user_unlocks.insert_one({
            "user_id": user_id,
            "movie_id": movie_id,
            "unlocked_at": datetime.datetime.utcnow()
        })
        
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{movie['file_id']}"
        return {"file_url": file_url}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/movie/poster/{movie_id}")
async def get_movie_poster(movie_id: str):
    try:
        movie = await db.movies.find_one({"_id": ObjectId(movie_id)})
        if not movie:
            return {"error": "Not found"}
        return {"photo_id": movie.get("photo_id", "")}
    except:
        return {"error": "Not found"}

# ==========================================
# 11. Run Server
# ==========================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
