import os
import asyncio
import datetime
import uvicorn
import time
import hmac
import hashlib
import urllib.parse
import secrets
import json

# ==========================================
# 🛑 FIX FOR EVENT LOOP ERROR
# ==========================================
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ==========================================

from fastapi import FastAPI, Body, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
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

# ==========================================
# 1. Configuration
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
broadcast_queue = asyncio.Queue()

# ==========================================
# 2. FSM States - শুধু ৩টি লাগবে!
# ==========================================
class AdminStates(StatesGroup):
    waiting_for_bcast = State()
    waiting_for_reply = State()
    waiting_for_photo = State()   # ১ম ধাপ
    waiting_for_title = State()   # ২য় ধাপ
    # quality, year, cats - REMOVED!

# ==========================================
# 3. Database Init
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
    await db.auto_delete.create_index("delete_at")
    await db.users.create_index("joined_at")
    await db.users.create_index("last_active")
    try:
        await db.payments.create_index("trx_id", unique=True)
    except:
        pass

# ==========================================
# 4. Security
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
# 5. Background Workers
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
                print(f"🔒 Auto-locked {result.deleted_count} videos.")
        except Exception as e:
            print(f"Auto-lock error: {e}")
        await asyncio.sleep(3600)

async def broadcast_queue_worker():
    while True:
        try:
            task_data = await broadcast_queue.get()
            await run_movie_broadcast(task_data['data'], task_data['admin_id'])
            broadcast_queue.task_done()
        except Exception as e:
            print(f"Queue Error: {e}")
            await asyncio.sleep(5)

# ==========================================
# 🚀 STARTUP - Bot Polling
# ==========================================
@app.on_event("startup")
async def on_startup():
    print("🚀 Starting BD Viral Box Bot...")
    await init_db()
    await load_admins()
    await load_banned_users()
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(broadcast_queue_worker())
    asyncio.create_task(auto_lock_worker())
    asyncio.create_task(dp.start_polling(bot, skip_updates=True))
    print("✅ Bot polling started!")

@app.on_event("shutdown")
async def on_shutdown():
    await dp.stop_polling()
    await bot.session.close()

# ==========================================
# 6. /start Command
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
        await db.users.insert_one({
            "user_id": uid, "first_name": message.from_user.first_name, 
            "joined_at": now, "refer_count": 0, "coins": 0, 
            "last_checkin": now - datetime.timedelta(days=2), 
            "vip_until": now - datetime.timedelta(days=1)
        })
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
    
    text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n🔥 <b>BD Viral Box</b> - খারাপ দুনিয়ায় সাগরম"
    if uid in admin_cache: text += "\n\n⚙️ <b>অ্যাডমিন মোড অন.</b>"
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

# ==========================================
# 7. Basic Commands
# ==========================================
@dp.message(Command("stats"))
async def bot_stats(m: types.Message):
    if m.from_user.id not in admin_cache: return
    total_users = await db.users.count_documents({})
    total_videos = await db.movies.count_documents({})
    vip_users = await db.users.count_documents({"vip_until": {"$gt": datetime.datetime.utcnow()}})
    await m.answer(f"📊 <b>Stats</b>\n\n👥 Users: <b>{total_users}</b>\n💎 VIP: <b>{vip_users}</b>\n🎬 Videos: <b>{total_videos}</b>", parse_mode="HTML")

@dp.message(Command("ban"))
async def ban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.update_one({"user_id": uid}, {"$set": {"user_id": uid}}, upsert=True)
        banned_cache.add(uid)
        await m.answer(f"🚫 <code>{uid}</code> ব্যান হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /ban USER_ID", parse_mode="HTML")

@dp.message(Command("unban"))
async def unban_user(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        uid = int(m.text.split()[1])
        await db.banned.delete_one({"user_id": uid})
        banned_cache.discard(uid)
        await m.answer(f"✅ <code>{uid}</code> আনব্যান হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /unban USER_ID", parse_mode="HTML")

@dp.message(Command("cancel"))
async def cancel_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.clear()
    await m.answer("❌ বাতিল করা হয়েছে!", parse_mode="HTML")

# User messages forward to admin
@dp.message(lambda m: m.chat.type == "private" and m.from_user.id not in admin_cache)
async def handle_user_messages(m: types.Message):
    if m.content_type not in ['text']:
        await m.answer("⚠️ শুধু টেক্সট পাঠান। ভিডিও দেখতে 'Watch Now' বাটনে ক্লিক করুন।", parse_mode="HTML")
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
    await c.message.answer("✍️ রিপ্লাই লিখুন:")
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
            await m.answer("❌ ব্যর্থ।")

# ==========================================
# 8. Admin Settings Commands
# ==========================================
@dp.message(Command("setadcount"))
async def set_ad_count(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        count = int(m.text.split()[1])
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": count}}, upsert=True)
        await m.answer(f"✅ অ্যড সংখ্যা <b>{count}</b>", parse_mode="HTML")
    except: await m.answer("⚠️ /setadcount 2", parse_mode="HTML")

@dp.message(Command("settime"))
async def set_delete_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        minutes = int(m.text.split()[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": minutes}}, upsert=True)
        await m.answer(f"✅ অটো-ডিলিট <b>{minutes} মিনিট</b>", parse_mode="HTML")
    except: await m.answer("⚠️ /settime 60", parse_mode="HTML")

@dp.message(Command("addlink"))
async def add_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ অ্যড লিংক অ্যড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /addlink url", parse_mode="HTML")

@dp.message(Command("addadultlink"))
async def add_adult_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ ১৮+ অ্যড লিংক অ্যড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /addadultlink url", parse_mode="HTML")

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "tg_link"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ চ্যানেল লিংক আপডেট হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settg https://t.me/...", parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: await m.answer(f"✅ '{title}' ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ পাওয়া যায়নি")
    except: await m.answer("⚠️ /delmovie নাম", parse_mode="HTML")

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
        await m.answer(f"✅ <code>{target_uid}</code> কে {days} দিন VIP!", parse_mode="HTML")
    except: await m.answer("⚠️ /addvip ID", parse_mode="HTML")

# ==========================================
# 9. 🎯 SIMPLIFIED UPLOAD - মাত্র ৩ ধাপ!
# ==========================================

# ধাপ ১: ভিডিও পাঠালে
@dp.message(F.content_type.in_({'video', 'document'}), lambda m: m.from_user.id in admin_cache)
async def receive_video_file(m: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is not None:
        await m.answer("⚠️ আগে /cancel করুন!", parse_mode="HTML")
        return
    
    fid = m.video.file_id if m.video else m.document.file_id
    ftype = "video" if m.video else "document"
    
    await state.set_state(AdminStates.waiting_for_photo)
    await state.update_data(file_id=fid, file_type=ftype)
    
    await m.answer("✅ ভিডিও পেয়েছি!\n\nএবার <b>পোস্টার ছবি</b> পাঠান।", parse_mode="HTML")

# ধাপ ২: পোস্টার পাঠালে
@dp.message(AdminStates.waiting_for_photo, F.photo)
async def receive_poster(m: types.Message, state: FSMContext):
    await state.update_data(photo_id=m.photo[-1].file_id)
    await state.set_state(AdminStates.waiting_for_title)
    await m.answer("✅ পোস্টার পেয়েছি!\n\nএবার <b>ভিডিওর নাম</b> লিখুন।", parse_mode="HTML")

# যদি ছবির বদলে অন্য কিছু পাঠায়
@dp.message(AdminStates.waiting_for_photo)
async def wrong_photo(m: types.Message):
    await m.answer("⚠️ শুধু <b>ছবি (Photo)</b> পাঠান!\nঅথবা /cancel লিখুন।", parse_mode="HTML")

# ধাপ ৩: নাম পাঠালে → সরাসরি Done বাটন!
@dp.message(AdminStates.waiting_for_title, F.text)
async def receive_title_and_finish(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    
    # সরাসরি বাটন দেখাও - কোনো quality/year/category নেই!
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Upload + Broadcast", callback_data="action_new_bcast")
    builder.button(text="➕ Upload Only (No Broadcast)", callback_data="action_add_file")
    builder.adjust(1)
    
    await m.answer(
        "✅ সব তথ্য নেওয়া হয়েছে!\n\n👇 কি করতে চান?",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

# যদি নামের বদলে অন্য কিছু পাঠায়
@dp.message(AdminStates.waiting_for_title)
async def wrong_title(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>নাম (টেক্সট)</b> লিখুন!\nঅথবা /cancel লিখুন।", parse_mode="HTML")

# ==========================================
# 10. Upload Actions
# ==========================================
@dp.callback_query(F.data == "action_new_bcast")
async def action_new_broadcast(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    # ডাটাবেসে সেভ - স্বয়ংক্রিয়ভাবে Adult Content ক্যাটাগরি
    await db.movies.insert_one({
        "title": data["title"],
        "quality": "N/A",
        "photo_id": data["photo_id"],
        "file_id": data["file_id"],
        "file_type": data["file_type"],
        "year": "N/A",
        "categories": ["Adult Content"],  # অটো সেট
        "clicks": 0,
        "created_at": datetime.datetime.utcnow()
    })
    
    await c.message.edit_text(f"🎉 <b>{data['title']}</b> যুক্ত হয়েছে!\n\n⏳ ব্রডকাস্ট শুরু হচ্ছে...", parse_mode="HTML")
    
    # লগ চ্যানেলে পোস্ট
    if LOG_CHANNEL_ID:
        try:
            log_kb = [
                [types.InlineKeyboardButton(text="🎬 Watch Now", url=f"https://t.me/{BOT_USERNAME}?start=new")]
            ]
            log_markup = types.InlineKeyboardMarkup(inline_keyboard=log_kb)
            log_text = f"🎬 <b>New Video Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📂 Category: Adult Content\n\n👤 Uploaded by Admin"
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=log_markup)
        except Exception as e:
            print(f"Log error: {e}")

    # ব্রডকাস্ট কিউতে পাঠাও
    await broadcast_queue.put({"data": data, "admin_id": c.from_user.id})
    await c.answer("🚀 ব্রডকাস্ট শুরু!")

@dp.callback_query(F.data == "action_add_file")
async def action_add_file_only(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    await db.movies.insert_one({
        "title": data["title"],
        "quality": "N/A",
        "photo_id": data["photo_id"],
        "file_id": data["file_id"],
        "file_type": data["file_type"],
        "year": "N/A",
        "categories": ["Adult Content"],  # অটো সেট
        "clicks": 0,
        "created_at": datetime.datetime.utcnow()
    })
    
    await c.message.edit_text(f"✅ <b>{data['title']}</b> যুক্ত হয়েছে! (ব্রডকাস্ট ছাড়া)", parse_mode="HTML")
    await c.answer("✅ ডান!")

# ==========================================
# 11. Broadcast Functions
# ==========================================
async def run_movie_broadcast(data, admin_id):
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    web_app_url = APP_URL if APP_URL else "https://t.me/" 
    
    bcast_kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=web_app_url))], 
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link)],
    ]
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = f"🆕 <b>New Video!</b>\n\n🔥 <b>{data['title']}</b>\n\n👇 এখনই দেখুন!"
    
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
        await bot.send_message(admin_id, f"✅ <b>{data['title']}</b> ব্রডকাস্ট শেষ!\n\nসফল: <b>{bcast_success}</b> জন", parse_mode="HTML")
    except: pass

@dp.message(Command("cast"))
async def broadcast_prep(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.set_state(AdminStates.waiting_for_bcast)
    await m.answer("📢 ব্রডকাস্ট মেসেজ পাঠান।\n\n⚠️ বাতিল: /cancel", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_bcast)
async def execute_broadcast(m: types.Message, state: FSMContext):
    if m.text and m.text.startswith("/"):
        await state.clear()
        return
    await state.clear()
    prog_msg = await m.answer("⏳ <b>Broadcasting...</b>", parse_mode="HTML")
    asyncio.create_task(run_manual_broadcast(m, prog_msg, m.from_user.id))

async def run_manual_broadcast(m, prog_msg, admin_id):
    total = await db.users.count_documents({})
    success = 0
    async for u in db.users.find():
        try: 
            await m.copy_to(chat_id=u['user_id'])
            success += 1
            await asyncio.sleep(0.05)
        except: pass
    try: await prog_msg.edit_text(f"✅ <b>Done!</b>\n\n👥 Total: {total}\n✅ Sent: {success}", parse_mode="HTML")
    except:
        try: await bot.send_message(admin_id, f"✅ Broadcast Done! Sent: {success}/{total}", parse_mode="HTML")
        except: pass

@dp.callback_query(F.data.startswith("trx_"))
async def handle_trx_approval(c: types.CallbackQuery):
    if c.from_user.id not in admin_cache: return
    action = c.data.split("_")[1]; pay_id = c.data.split("_")[2]
    payment = await db.payments.find_one({"_id": ObjectId(pay_id)})
    if not payment or payment["status"] != "pending": return await c.answer("⚠️ প্রসেস হয়েছে!", show_alert=True)
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

# Photo ID getter
@dp.message(F.photo, StateFilter(None))
async def get_file_id_for_admin(message: types.Message):
    if message.from_user.id not in admin_cache: return
    await message.answer(f"🖼️ <b>File ID:</b>\n\n<code>{message.photo[-1].file_id}</code>", parse_mode="HTML")

# ==========================================
# 12. Admin Panel
# ==========================================
@app.get("/panel", response_class=HTMLResponse)
async def admin_panel_ui(auth: bool = Depends(verify_admin)):
    html_code = '''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>Admin - BD Viral Box</title><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css"><style>body{font-family:'Segoe UI',sans-serif;background:#0f172a;color:#cbd5e1;margin:0;padding:20px}.header{text-align:center;margin-bottom:30px}.header h1{margin:0;font-size:28px;background:linear-gradient(45deg,#ff416c,#ff4b2b);-webkit-background-clip:text;-webkit-text-fill-color:transparent}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:20px;margin-bottom:40px}.stat-card{background:#1e293b;padding:20px;border-radius:16px;border:1px solid #334155}.stat-card h3{margin:0 0 10px;font-size:13px;color:#94a3b8;text-transform:uppercase}.stat-card .value{font-size:28px;font-weight:800;color:#fff}.table-container{background:#1e293b;border-radius:16px;border:1px solid #334155;overflow-x:auto}.table-header{padding:20px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center}.table-header h2{margin:0;color:#fff;font-size:18px}table{width:100%;border-collapse:collapse;min-width:500px}th{text-align:left;padding:12px;color:#94a3b8;font-size:11px;text-transform:uppercase;border-bottom:1px solid #334155}td{padding:12px;border-bottom:1px solid #334155;font-size:13px;color:#e2e8f0}tr:hover{background:rgba(255,255,255,.03)}.view-badge{background:rgba(239,68,68,.2);color:#f87171;padding:3px 8px;border-radius:10px;font-size:11px;font-weight:600}.delete-btn{background:rgba(239,68,68,.2);color:#f87171;border:1px solid rgba(239,68,68,.3);padding:5px 10px;border-radius:6px;cursor:pointer;font-size:12px}.delete-btn:hover{background:#ef4444;color:#fff}.empty-state{text-align:center;padding:40px;color:#64748b}input[type=text]{padding:6px 10px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#fff;outline:none}</style></head><body><div class="header"><h1><i class="fa-solid fa-shield-halved"></i> Admin Panel</h1><p>BD Viral Box</p></div><div class="stats-grid"><div class="stat-card"><h3>Total Users</h3><div class="value"><i class="fa-solid fa-users" style="color:#3b82f6"></i> <span id="totalUsers">0</span></div></div><div class="stat-card"><h3>Today Users</h3><div class="value"><i class="fa-solid fa-user-plus" style="color:#10b981"></i> <span id="todayUsers">0</span></div></div><div class="stat-card"><h3>Total Clicks</h3><div class="value"><i class="fa-solid fa-eye" style="color:#f59e0b"></i> <span id="totalClicks">0</span></div></div><div class="stat-card"><h3>Live (1m)</h3><div class="value" style="color:#10b981"><i class="fa-solid fa-signal"></i> <span id="activeUsers">0</span></div></div></div><div class="table-container"><div class="table-header"><h2><i class="fa-solid fa-film"></i> Videos</h2><input type="text" id="searchInput" placeholder="🔍 Search..." style="width:140px"></div><table><thead><tr><th>Title</th><th>Views</th><th>Action</th></tr></thead><tbody id="tbody"><tr><td colspan="3" class="empty-state">Loading...</td></tr></tbody></table></div><script>let allM=[];async function loadStats(){try{const r=await fetch("/api/admin/stats");const d=await r.json();document.getElementById("totalUsers").innerText=d.total_users;document.getElementById("todayUsers").innerText=d.today_users;document.getElementById("totalClicks").innerText=d.total_clicks;document.getElementById("activeUsers").innerText=d.active_users}catch(e){}}async function loadMovies(){try{const r=await fetch("/api/admin/movies");allM=await r.json();render(allM)}catch(e){}}function render(arr){const t=document.getElementById("tbody");if(!arr.length){t.innerHTML=\'<tr><td colspan="3" class="empty-state">Empty</td></tr>\';return}t.innerHTML=arr.map(m=>`<tr id="r-${m._id}"><td><b>${m.title}</b></td><td><span class="view-badge">${m.clicks||0}</span></td><td><button class="delete-btn" onclick="del(\'${m._id}\')">🗑 Delete</button></td></tr>`).join("")}document.getElementById("searchInput").addEventListener("input",function(e){const s=e.target.value.toLowerCase();render(allM.filter(m=>(m.title||"").toLowerCase().includes(s)))});async function del(id){if(!confirm("Delete?"))return;try{const r=await fetch("/api/admin/movie/"+id,{method:"DELETE"});const d=await r.json();if(d.ok){document.getElementById("r-"+id).remove();loadStats()}}catch(e){}}loadStats();loadMovies();setInterval(loadStats,60000)</script></body></html>'''
    return HTMLResponse(html_code)

@app.get("/api/admin/stats")
async def admin_stats(auth: bool = Depends(verify_admin)):
    now = datetime.datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    total_users = await db.users.count_documents({})
    today_users = await db.users.count_documents({"joined_at": {"$gte": today_start}})
    active_users = await db.users.count_documents({"last_active": {"$gte": now - datetime.timedelta(minutes=1)}})
    total_clicks_res = await db.movies.aggregate([{"$group": {"_id": None, "total": {"$sum": "$clicks"}}}]).to_list(1)
    total_clicks = total_clicks_res[0]["total"] if total_clicks_res else 0
    return {"total_users": total_users, "today_users": today_users, "active_users": active_users, "total_clicks": total_clicks, "today_clicks": 0}

@app.get("/api/movies/trending")
async def get_trending_movies():
    try:
        now = datetime.datetime.utcnow()
        movies = await db.movies.find({"created_at": {"$gte": now - datetime.timedelta(days=30)}}).sort("clicks", -1).limit(10).to_list(10)
        for m in movies: m["_id"] = str(m["_id"])
        return movies
    except:
        return []

@app.get("/api/movies/recent")
async def get_recent_movies():
    try:
        movies = await db.movies.find({}).sort("created_at", -1).limit(10).to_list(10)
        for m in movies: m["_id"] = str(m["_id"])
        return movies
    except:
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
    raise HTTPException(status_code=404, detail="Not found")

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

# ==========================================
# 13. 🎬 Web App UI - Welcome Animation সহ!
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui():
    dl_cfg = await db.settings.find_one({"id": "direct_links"})
    direct_links = dl_cfg.get('links', []) if dl_cfg else []
    dl_json = json.dumps(direct_links)
    
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"})
    adult_direct_links = adl_cfg.get('links', []) if adl_cfg else []
    adl_json = json.dumps(adult_direct_links)

    html_code = '''<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no"><title>BD Viral Box</title><script src="https://telegram.org/js/telegram-web-app.js"></script><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"><style>@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');*{margin:0;padding:0;box-sizing:border-box}body{background:#0f172a;font-family:'Inter',sans-serif;color:#fff;overscroll-behavior-y:none}

/* ========== WELCOME SCREEN WITH ANIMATION ========== */
#welcomeScreen{position:fixed;top:0;left:0;width:100%;height:100%;background:linear-gradient(135deg,#0f172a 0%,#1a0a0a 50%,#0f172a 100%);display:flex;flex-direction:column;align-items:center;justify-content:center;z-index:9999;transition:opacity .6s ease,transform .6s ease}
#welcomeScreen.hide{opacity:0;transform:scale(1.2);pointer-events:none}

.welcome-logo{width:130px;height:130px;border-radius:35px;background:linear-gradient(135deg,#ff416c,#ff4b2b);display:flex;align-items:center;justify-content:center;font-size:55px;margin-bottom:30px;box-shadow:0 10px 50px rgba(255,65,108,.5);animation:logoPulse 2s ease-in-out infinite,logoFloat 3s ease-in-out infinite}
@keyframes logoPulse{0%,100%{box-shadow:0 10px 50px rgba(255,65,108,.5)}50%{box-shadow:0 10px 80px rgba(255,65,108,.8),0 0 120px rgba(255,75,43,.3)}}
@keyframes logoFloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}

.welcome-title{font-size:38px;font-weight:900;background:linear-gradient(45deg,#ff416c,#ff4b2b,#ff6b6b);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:10px;letter-spacing:-1px;animation:titleSlide .8s ease-out}
@keyframes titleSlide{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}

.welcome-tagline{font-size:15px;color:#94a3b8;margin-bottom:45px;animation:tagFade 1s ease-out .3s both}
@keyframes tagFade{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}

.welcome-btn{background:linear-gradient(135deg,#ff416c,#ff4b2b);color:#fff;border:none;padding:16px 55px;border-radius:50px;font-size:17px;font-weight:800;cursor:pointer;box-shadow:0 8px 30px rgba(255,65,108,.5);transition:transform .2s,box-shadow .2s;animation:btnPop 1s ease-out .5s both;letter-spacing:.5px}
.welcome-btn:active{transform:scale(.93)!important;box-shadow:0 4px 15px rgba(255,65,108,.4)}
@keyframes btnPop{from{opacity:0;transform:scale(.8)}to{opacity:1;transform:scale(1)}}

.welcome-bot{position:absolute;bottom:35px;color:#475569;font-size:13px;animation:botFade 1s ease-out .7s both}
@keyframes botFade{from{opacity:0}to{opacity:1}}

/* ========== MAIN APP ========== */
#mainApp{display:none;padding-bottom:80px}
.app-header{padding:16px 20px;display:flex;align-items:center;justify-content:space-between;background:rgba(15,23,42,.95);backdrop-filter:blur(10px);position:sticky;top:0;z-index:100;border-bottom:1px solid rgba(255,255,255,.05)}
.app-logo{font-size:20px;font-weight:800;background:linear-gradient(45deg,#ff416c,#ff4b2b);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.search-container{padding:12px 20px}
.search-box{display:flex;align-items:center;background:#1e293b;border-radius:12px;padding:12px 16px;border:1px solid #334155}
.search-box i{color:#64748b;margin-right:12px}
.search-box input{flex:1;background:none;border:none;outline:none;color:#fff;font-size:14px;font-family:'Inter',sans-serif}
.search-box input::placeholder{color:#64748b}
.category-section{padding:8px 20px 16px}
.category-btn{background:linear-gradient(135deg,#ff416c,#ff4b2b);color:#fff;border:none;padding:10px 28px;border-radius:25px;font-size:13px;font-weight:700;cursor:pointer;box-shadow:0 4px 15px rgba(255,65,108,.3)}
.category-btn:active{transform:scale(.95)}
.section-title{padding:10px 20px;font-size:18px;font-weight:800;display:flex;align-items:center;gap:10px}
.section-title .fire{color:#ff416c}
.movie-grid{padding:0 20px;display:flex;flex-direction:column;gap:14px}
.movie-card{display:flex;gap:14px;background:#1e293b;border-radius:16px;overflow:hidden;border:1px solid #334155;cursor:pointer;transition:transform .2s,border-color .2s;position:relative}
.movie-card:active{transform:scale(.98);border-color:#ff416c}
.movie-card-rank{position:absolute;top:10px;left:10px;background:rgba(0,0,0,.8);color:#ff416c;font-size:11px;font-weight:800;padding:3px 8px;border-radius:6px;z-index:2}
.movie-card-badge{position:absolute;top:10px;right:10px;background:#ef4444;color:#fff;font-size:10px;font-weight:800;padding:3px 8px;border-radius:6px;z-index:2;animation:badgePulse 2s infinite}
@keyframes badgePulse{0%,100%{opacity:1}50%{opacity:.7}}
.movie-poster{width:110px;min-height:150px;object-fit:cover;flex-shrink:0;background:#334155}
.movie-info{flex:1;padding:14px 14px 14px 0;display:flex;flex-direction:column;justify-content:center}
.movie-title{font-size:14px;font-weight:700;margin-bottom:8px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.movie-meta{font-size:12px;color:#94a3b8;margin-bottom:10px;display:flex;gap:10px}
.movie-meta span{display:flex;align-items:center;gap:4px}
.movie-play-btn{display:inline-flex;align-items:center;gap:6px;background:linear-gradient(135deg,#ff416c,#ff4b2b);color:#fff;border:none;padding:8px 18px;border-radius:20px;font-size:12px;font-weight:700;cursor:pointer;width:fit-content}

/* Detail Page */
#detailPage{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:#0f172a;z-index:500;overflow-y:auto;padding-bottom:100px}
.detail-back{position:sticky;top:0;z-index:10;background:rgba(15,23,42,.95);backdrop-filter:blur(10px);padding:16px 20px;display:flex;align-items:center;gap:12px;border-bottom:1px solid rgba(255,255,255,.05);cursor:pointer}
.detail-back i{font-size:20px;color:#ff416c}
.detail-poster{width:100%;max-height:400px;object-fit:cover}
.detail-info{padding:20px}
.detail-title{font-size:22px;font-weight:800;margin-bottom:10px;line-height:1.3}
.detail-meta{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap}
.detail-meta span{background:#1e293b;padding:6px 14px;border-radius:20px;font-size:12px;color:#94a3b8;border:1px solid #334155}
.detail-download-btn{display:flex;align-items:center;justify-content:center;gap:10px;background:linear-gradient(135deg,#ff416c,#ff4b2b);color:#fff;border:none;padding:16px;border-radius:16px;font-size:16px;font-weight:800;width:100%;cursor:pointer;box-shadow:0 5px 25px rgba(255,65,108,.4);margin-bottom:16px;transition:transform .2s}
.detail-download-btn:active{transform:scale(.98)}
.detail-ad-link{display:block;background:#1e293b;border:1px solid #334155;border-radius:12px;padding:14px 16px;color:#60a5fa;text-decoration:none;font-size:13px;font-weight:600;margin-bottom:10px;text-align:center}

/* Bottom Nav */
.bottom-nav{position:fixed;bottom:0;left:0;width:100%;background:rgba(15,23,42,.98);backdrop-filter:blur(10px);display:flex;border-top:1px solid rgba(255,255,255,.05);z-index:200;padding:8px 0}
.nav-item{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;color:#64748b;font-size:10px;font-weight:600;cursor:pointer;padding:6px 0;transition:color .2s}
.nav-item.active{color:#ff416c}
.nav-item i{font-size:20px}

.loading{text-align:center;padding:40px;color:#64748b}
.loading i{font-size:30px;animation:spin 1s linear infinite;margin-bottom:10px;display:block}
@keyframes spin{100%{transform:rotate(360deg)}}
.no-results{text-align:center;padding:60px 20px;color:#64748b}
.no-results i{font-size:50px;margin-bottom:15px;display:block;color:#334155}
::-webkit-scrollbar{width:0}</style></head><body>

<!-- WELCOME SCREEN -->
<div id="welcomeScreen">
    <div class="welcome-logo">🎬</div>
    <div class="welcome-title">BD Viral Box</div>
    <div class="welcome-tagline">খারাপ দুনিয়ায় সাগরম</div>
    <button class="welcome-btn" onclick="enterApp()">▶ Enter Now</button>
    <div class="welcome-bot">@MovieBoxx_bot</div>
</div>

<!-- MAIN APP -->
<div id="mainApp">
    <div class="app-header"><div class="app-logo">BD Viral Box</div></div>
    <div class="search-container"><div class="search-box"><i class="fa-solid fa-magnifying-glass"></i><input type="text" id="searchInput" placeholder="Search videos..." oninput="handleSearch()"></div></div>
    <div class="category-section"><button class="category-btn active" onclick="loadHome()">🏠 HOME</button></div>
    <div id="contentArea">
        <div class="section-title"><span class="fire">🔥</span> Trending Now</div>
        <div class="movie-grid" id="movieGrid"><div class="loading"><i class="fa-solid fa-spinner"></i>Loading...</div></div>
    </div>
    <div class="bottom-nav">
        <div class="nav-item active" onclick="loadHome()" id="navHome"><i class="fa-solid fa-house"></i><span>Home</span></div>
        <div class="nav-item" onclick="focusSearch()" id="navSearch"><i class="fa-solid fa-magnifying-glass"></i><span>Search</span></div>
    </div>
</div>

<!-- DETAIL PAGE -->
<div id="detailPage">
    <div class="detail-back" onclick="closeDetail()"><i class="fa-solid fa-arrow-left"></i><span>Back</span></div>
    <img class="detail-poster" id="detailPoster" src="" alt="">
    <div class="detail-info">
        <div class="detail-title" id="detailTitle"></div>
        <div class="detail-meta" id="detailMeta"></div>
        <button class="detail-download-btn" id="detailDownloadBtn" onclick="handleDownload()"><i class="fa-solid fa-download"></i> Download Now</button>
        <div id="detailAds"></div>
    </div>
</div>

<script>
const tg=window.Telegram&&window.Telegram.WebApp;
if(tg){tg.ready();tg.expand()}
let userId=null,allMovies=[],currentMovie=null,clickCount=0;
let directLinks=''' + dl_json + ''';
let adultDirectLinks=''' + adl_json + ''';

function enterApp(){
    document.getElementById("welcomeScreen").classList.add("hide");
    setTimeout(()=>{
        document.getElementById("welcomeScreen").style.display="none";
        document.getElementById("mainApp").style.display="block";
    },600);
    initUser();loadMovies();
}

async function initUser(){
    if(tg&&tg.initDataUnsafe&&tg.initDataUnsafe.user){
        userId=tg.initDataUnsafe.user.id;
        try{await fetch("/api/user/init",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:userId,first_name:tg.initDataUnsafe.user.first_name||"",username:tg.initDataUnsafe.user.username||"",init_data:tg.initData})})}catch(e){}
        setInterval(()=>{fetch("/api/user/ping",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:userId})}).catch(()=>{})},60000);
    }
}

async function loadMovies(){
    try{const r=await fetch("/api/movies/recent");allMovies=await r.json();renderMovies(allMovies)}
    catch(e){document.getElementById("movieGrid").innerHTML='<div class="no-results"><i class="fa-solid fa-triangle-exclamation"></i>Failed</div>'}
}

function renderMovies(movies){
    const g=document.getElementById("movieGrid");
    if(!movies||!movies.length){g.innerHTML='<div class="no-results"><i class="fa-solid fa-film"></i>No videos</div>';return}
    g.innerHTML=movies.map((m,i)=>'<div class="movie-card" onclick="openDetail(\\''+m._id+'\\')"><div class="movie-card-rank">'+String(i+1).padStart(2,"0")+'</div><div class="movie-card-badge">18+</div><img class="movie-poster" src="https://api.telegram.org/file/bot' + (TOKEN||"") + '/" onerror="this.style.background=\\'#334155\\'"><div class="movie-info"><div class="movie-title">'+m.title+'</div><div class="movie-meta"><span><i class="fa-solid fa-eye"></i> '+(m.clicks||0)+'</span></div><div class="movie-play-btn"><i class="fa-solid fa-play"></i> Watch</div></div></div>').join("");
}

function handleSearch(){const q=document.getElementById("searchInput").value.toLowerCase();if(!q){renderMovies(allMovies);return}renderMovies(allMovies.filter(m=>(m.title||"").toLowerCase().includes(q)))}
function focusSearch(){document.querySelectorAll(".nav-item").forEach(n=>n.classList.remove("active"));document.getElementById("navSearch").classList.add("active");document.getElementById("searchInput").focus()}
function loadHome(){document.querySelectorAll(".nav-item").forEach(n=>n.classList.remove("active"));document.getElementById("navHome").classList.add("active");document.getElementById("searchInput").value="";renderMovies(allMovies)}

async function openDetail(id){
    const m=allMovies.find(x=>x._id===id);if(!m)return;currentMovie=m;
    document.getElementById("detailTitle").innerText=m.title;
    document.getElementById("detailPoster").src="https://api.telegram.org/file/bot"+(TOKEN||"")+"/"+m.photo_id;
    document.getElementById("detailMeta").innerHTML='<span><i class="fa-solid fa-eye"></i> '+(m.clicks||0)+' views</span><span>18+</span>';
    const ads=adultDirectLinks;
    document.getElementById("detailAds").innerHTML=ads.map(l=>'<a class="detail-ad-link" href="'+l+'" target="_blank">'+l+'</a>').join("");
    document.getElementById("detailPage").style.display="block";
    document.getElementById("mainApp").style.display="none";
    clickCount=0;
    document.getElementById("detailDownloadBtn").innerHTML='<i class="fa-solid fa-download"></i> Download Now';
    document.getElementById("detailDownloadBtn").style.background="linear-gradient(135deg,#ff416c,#ff4b2b)";
    document.getElementById("detailDownloadBtn").style.pointerEvents="auto";
    try{await fetch("/api/movie/click/"+id,{method:"POST"})}catch(e){}
}

function closeDetail(){document.getElementById("detailPage").style.display="none";document.getElementById("mainApp").style.display="block";currentMovie=null}

async function handleDownload(){
    if(!currentMovie)return;clickCount++;const btn=document.getElementById("detailDownloadBtn");
    if(clickCount===1){
        btn.innerHTML='<i class="fa-solid fa-arrow-up-right-from-square"></i> Open Ad First';
        btn.style.background="linear-gradient(135deg,#f59e0b,#d97706)";
        if(adultDirectLinks.length>0)window.open(adultDirectLinks[0],"_blank");
    }else{
        btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Preparing...';btn.style.pointerEvents="none";
        try{
            const r=await fetch("/api/movie/unlock/"+currentMovie._id,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({user_id:userId})});
            const d=await r.json();
            if(d.file_url){window.open(d.file_url,"_blank");btn.innerHTML='<i class="fa-solid fa-check"></i> Opened!';btn.style.background="linear-gradient(135deg,#10b981,#059669)"}
            else{btn.innerHTML='<i class="fa-solid fa-triangle-exclamation"></i> '+(d.error||"Retry");btn.style.background="linear-gradient(135deg,#ef4444,#dc2626)";btn.style.pointerEvents="auto";clickCount=0}
        }catch(e){btn.innerHTML='<i class="fa-solid fa-triangle-exclamation"></i> Error';btn.style.background="linear-gradient(135deg,#ef4444,#dc2626)";btn.style.pointerEvents="auto";clickCount=0}
    }
}
</script></body></html>'''
    return HTMLResponse(html_code)

# ==========================================
# 14. API Endpoints
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
                "user_id": user_id, "first_name": body.get("first_name", ""),
                "username": body.get("username", ""), "joined_at": now,
                "refer_count": 0, "coins": 0,
                "last_checkin": now - datetime.timedelta(days=2),
                "vip_until": now - datetime.timedelta(days=1)
            })
        else:
            await db.users.update_one({"user_id": user_id}, {"$set": {"last_active": now}})
        is_vip = user["vip_until"] > now if user else False
        return {"ok": True, "is_vip": is_vip}
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
            return {"error": "Not found"}
        existing = await db.user_unlocks.find_one({"user_id": user_id, "movie_id": movie_id})
        if existing:
            return {"file_url": f"https://api.telegram.org/file/bot{TOKEN}/{movie['file_id']}"}
        await db.user_unlocks.insert_one({"user_id": user_id, "movie_id": movie_id, "unlocked_at": datetime.datetime.utcnow()})
        return {"file_url": f"https://api.telegram.org/file/bot{TOKEN}/{movie['file_id']}"}
    except Exception as e:
        return {"error": str(e)}

# ==========================================
# 15. Run
# ==========================================
if __name__ == "__main__":
    print("🚀 Starting BD Viral Box...")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
