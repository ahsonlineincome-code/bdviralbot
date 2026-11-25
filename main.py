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
import random
import re

# ==========================================
# 🛑 FIX FOR EVENT LOOP ERROR
# ==========================================
try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
# ==========================================

from fastapi import FastAPI, Body, Request, Depends, HTTPException, status, Response
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware  # 💰 ব্যান্ডউইথ-সেভ
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramRetryAfter, TelegramAPIError, TelegramForbiddenError

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
BOT_USER = os.getenv("BOT_USERNAME", "bdviralboxx_bot")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003904328439") 
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123") 
BOT_USERNAME = "@bdviralboxx_bot" 

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
# 💰 ব্যান্ডউইথ-সেভ: HTML/JSON রেসপন্স gzip দিয়ে ৭৫-৮৫% ছোট হয়ে যায় — Render-এর ৫GB কম কাটে
app.add_middleware(GZipMiddleware, minimum_size=1024)

client = AsyncIOMotorClient(MONGO_URL)
db = client['movie_database']

admin_cache = set([OWNER_ID]) 
banned_cache = set() 

# কিউ সিস্টেম যোগ করা হলো (একসাথে একাধিক ব্রডকাস্ট রান করে বট যেন হ্যাং না করে)
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

# ==========================================
# প্রি-সেট অ্যাড কনফিগ (মালিকের লিংক/ব্যানার — ডিপ্লয়ের সাথে সাথেই চালু, কমান্ড লাগবে না)
# ==========================================
# 🎲 অ্যাডাল্ট (18+) অ্যাড লিংক পুল — এই ৫টা লিংক মিনি অ্যাপ গেট/সাকসেস অফার/ব্রডকাস্টে র‍্যান্ডমলি চলবে
# (মালিক /addadultlink দিয়ে যত খুশি নতুন লিংক যোগ করতে পারবেন — সবগুলোর উপর থেকেই র‍্যান্ডম আসবে)
DEFAULT_ADULT_LINKS = [
    "https://auctionr.org/4/1b6df569ebc094e3e8e592d2fd017b4f",
    "https://auctionr.org/4/ae2d07d4c0488d06e6aec709d40d437f",
    "https://auctionr.org/4/ef5b10414fe8fdd3f602e74bcdd4225c",
    "https://auctionr.org/4/9a420739bd309e36f6535c4bcad2d3c9",
    "https://auctionr.org/4/b51d4b4d19155b12b384cdd6f2b31daa",
]
DEFAULT_BANNER_CODE = """<script>
  atOptions = {
    'key' : 'f1ac37c4ab0a5dab60781b14dd5696bb',
    'format' : 'iframe',
    'height' : 250,
    'width' : 300,
    'params' : {}
  };
</script>
<script src="https://brijmohan.org/22/f1ac37c4ab0a5dab60781b14dd5696bb"></script>"""
DEFAULT_AD_COUNT = 2
DEFAULT_AD_WAIT = 10  # 🛡️ গেটে অ্যাড পেজে কাটাতে হবে এমন সেকেন্ড (/setadwait বা প্যানেল থেকে বদলানো যায়)

async def init_db():
    await db.movies.create_index([("title", "text")])
    await db.movies.create_index("title")
    await db.movies.create_index("created_at")
    await db.movies.create_index("categories")
    await db.auto_delete.create_index("delete_at")
    await db.users.create_index("joined_at")
    await db.users.create_index("last_active")
    await db.payments.create_index("trx_id", unique=True)
    # অ্যাড ক্লিক ট্র্যাকিং (কোন স্লটে/লিংকে ক্লিক হচ্ছে তার হিসাব)
    await db.ad_clicks.create_index([("user_id", 1), ("clicked_at", -1)])
    await db.ad_clicks.create_index("clicked_at")
    # 🛡️ সার্ভার-সাইড অ্যাড গেট (Movie Box BD-এর সেম সিস্টেম): গেট রেকর্ড + ৭-দিনের স্ট্যাটস
    await db.ad_gates.create_index([("uid", 1), ("started_at", -1)])
    await db.ad_gates.create_index([("movie_id", 1), ("status", 1), ("verified_at", -1)])
    try: await db.ad_gates.drop_index("started_at_1")
    except Exception: pass
    # ৭ দিন পর মঙ্গো-নিজেও অটো-ক্লিন (ব্যাকআপ; প্যানেল খুললে অ্যাপ-লেভেল ক্লিনাপও চলে)
    await db.ad_gates.create_index("started_at", expireAfterSeconds=60*60*24*7)
    if not await db.settings.find_one({"id": "ad_wait"}):
        await db.settings.update_one({"id": "ad_wait"}, {"$set": {"value": DEFAULT_AD_WAIT}}, upsert=True)
    # প্রি-সেট সিড: ডাটাবেজে সেটিং না থাকলে মালিকের লিংক/ব্যানার/গেট সংখ্যা বসিয়ে দাও
    # (পরে কমান্ড দিয়ে চেঞ্জ করলে ডাটাবেজের মানই চলে — প্রি-সেটের উপরে লেখে)
    # প্রি-সেট সিড + এক-বারের মাইগ্রেশন: ডাটাবেজে যা আছে তার সাথে ডিফল্ট ৫টা লিংক মার্জ হবে (ডুপ্লিকেট বাদ)
    # এরপর মালিক /addadultlink দিয়ে যত খুশি লিংক যোগ / /deladultlink দিয়ে বাদ করলে সেটাই চলবে —
    # রিস্টার্টে আর কখনো অটো-বদল হবে না (একবারই মার্জ, তারপর মালিকের লিস্টই ফাইনাল)
    _adl_cfg = await db.settings.find_one({"id": "adult_direct_links"})
    _seed_cfg = await db.settings.find_one({"id": "adult_links_seed_v2"})
    if not _adl_cfg:
        await db.settings.update_one({"id": "adult_direct_links"}, {"$set": {"links": DEFAULT_ADULT_LINKS}}, upsert=True)
    elif not _seed_cfg:
        _cur_links = [u for u in (_adl_cfg.get("links", []) or []) if u]
        _merged = list(_cur_links)
        for _u in DEFAULT_ADULT_LINKS:
            if _u not in _merged:
                _merged.append(_u)
        await db.settings.update_one({"id": "adult_direct_links"}, {"$set": {"links": _merged}})
    if not _seed_cfg:
        await db.settings.update_one({"id": "adult_links_seed_v2"}, {"$set": {"status": True}}, upsert=True)
    if not await db.settings.find_one({"id": "ad_count"}):
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": DEFAULT_AD_COUNT}}, upsert=True)
    for _zid in ("banner_home", "banner_detail", "banner_detail_bottom", "banner_success", "banner_profile"):
        if not await db.settings.find_one({"id": _zid}):
            await db.settings.update_one({"id": _zid}, {"$set": {"code": DEFAULT_BANNER_CODE}}, upsert=True)

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

def tg_user_from_init(init_data: str):
    """🪪 initData থেকে (user_id, first_name) বের করা — ভুয়া uid দিয়ে স্প্যাম আটকাতে"""
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data or ""))
        u = json.loads(parsed.get("user", "{}"))
        return int(u.get("id") or 0), (u.get("first_name") or "User")
    except Exception:
        return 0, "User"

async def get_setting(key, default=None):
    doc = await db.settings.find_one({"id": key})
    if not doc: return default
    for k in ("value", "count", "minutes", "status", "code", "url"):
        if k in doc: return doc[k]
    return default

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect", headers={"WWW-Authenticate": "Basic"})
    return True

# ==========================================
# 4.5 🛡️ সার্ভার-সাইড অ্যাড গেট (Movie Box BD-এর সেম সিস্টেম — 🔞 adult-only)
# ==========================================
# ❌ আগের সমস্যা: পুরো গেট ছিল শুধু ক্লায়েন্ট-সাইড JS। সার্ভার (/api/send) কখনো চেকই করত না
#    ইউজার সত্যিই অ্যাড দেখেছে কি না — তাই PC/DevTools (কনসোলে এক লাইনের fetch) দিয়ে যেকোনো
#    ডিভাইস থেকে অ্যাড না দেখেই ফাইল নেওয়া যেত → ইম্প্রেশন হারাত, ইনকাম পড়ে যেত।
# ✅ এখন: প্রতিটি গেট সার্ভারে রেকর্ড হয় — লিংক সার্ভারই র‍্যান্ডম বেছে দেয় (no-repeat),
#    আসল সময় (away-time) সার্ভারে জমা হয় (beacon), verify-এ সার্ভারই সিদ্ধান্ত নেয়,
#    আর /api/send ভেরিফাইড গেট ছাড়া ফাইল দেয় না। VIP গেট-ফ্রি।
# ফ্লো: /api/ad/start (লিংক+gate_id) → ইউজার অ্যাডে যায় → beacon(away/back) → /api/ad/verify
#      → সব রাউন্ড শেষে /api/send (ভেরিফাইড গেট গুনে দেখে)

class GateStartModel(BaseModel):
    initData: str; movieId: str; adult: bool = True

class GateBeatModel(BaseModel):
    gateId: str; initData: str; phase: str  # 'away' | 'back'

class GateVerifyModel(BaseModel):
    gateId: str; initData: str

def gate_settings():
    """⚙️ গেট সেটিংস (প্যানেল/কমান্ড থেকে বদলানো যায়): অপেক্ষা-সেকেন্ড, রাউন্ড
    (TTL বাদ — ইউজার-ফ্রেন্ডলি: সেকেন্ড পার করলেই গেট ভেরিফাইড, আর কোনো এক্সপায়ার নেই)"""
    async def _inner():
        aw = await get_setting("ad_wait", 10)
        ac = await get_setting("ad_count", 1)
        try: aw = max(5, min(120, int(aw or 10)))
        except Exception: aw = 10
        try: ac = max(1, min(5, int(ac or 1)))
        except Exception: ac = 1
        return aw, ac
    return _inner

async def gate_get_settings():
    return await gate_settings()()

async def pick_link_for_user(uid: int, adult: bool):
    """🎲 সার্ভার-সাইড র‍্যান্ডম লিংক পিক — পুল থেকে র‍্যান্ডম, কিন্তু এই ইউজারের সর্বশেষ
    সার্ভ করা লিংকটা পরপর দুইবার যাবে না। 🔞 এই বট ১৮+ অনলি — সবসময় অ্যাডাল্ট পুল।
    রিটার্ন: (লিংক, পুল-সাইজ)"""
    key = "adult_direct_links"  # 🔞 adult-only বট: নরমাল পুল নেই, সব গেট অ্যাডাল্ট পুল থেকে
    cfg = await db.settings.find_one({"id": key})
    links = (cfg or {}).get("links", []) or []
    if not links: return None, 0
    last = None
    try:
        prev = await db.ad_gates.find_one({"uid": uid, "link": {"$exists": True}},
                                          sort=[("started_at", -1)])
        if prev: last = prev.get("link")
    except Exception:
        pass
    pool = [u for u in links if u != last] or list(links)
    return random.choice(pool), len(links)

async def _gate_uid_guard(init_data: str):
    """🪪 initData যাচাই — আসল টেলিগ্রাম ইউজার আইডি বের করে দেয়; ভুয়া হলে 0"""
    if not validate_tg_data(init_data): return 0
    uid, _name = tg_user_from_init(init_data)
    return uid or 0

# 📳 টাইমার-শেষ নোটিফিকেশন: অ্যাড খোলার wait সেকেন্ড পর বট নিজে থেকে ইউজারকে ছোট মেসেজ দেয় —
# মেসেজ এলেই ফোনে নোটিফিকেশনে ভাইব্রেট/রিং হয় (টেলিগ্রামে সরাসরি "শুধু ভাইব্রেট" করানোর Bot API নেই)।
# ইউজার আগেই ফিরে গেট ভেরিফাই/বাতিল করলে মেসেজ আর যায় না। 💰 খরচ: প্রতি গেটে ১টি ছোট মেসেজ।
async def gate_notify_worker(uid: int, gate_id: str, wait_s: int):
    try:
        await asyncio.sleep(max(5, int(wait_s)))
        g = await db.ad_gates.find_one({"_id": ObjectId(gate_id)}, {"status": 1})
        if not g or g.get("status") != "running": return  # ভেরিফাই/বাতিল/খরচ হয়ে গেলে আর নয়
        try:
            await bot.send_message(uid,
                f"📳 বিপ! আপনার {wait_s} সেকেন্ডের অ্যাড-টাইম শেষ ✅\n"
                "এখন বিজ্ঞাপন থেকে Back দিয়ে বটে ফিরে আসুন — ফাইলটি নিয়ে নিন। 🎬")
        except Exception:
            pass  # ইউজার বট ব্লক করে থাকলেও গেট চলবে — শুধু মেসেজ যাবে না
    except Exception as e:
        print(f"gate_notify_worker error: {e}")

@app.post("/api/ad/start")
async def ad_gate_start(d: GateStartModel):
    """🎬 গেট শুরু: সার্ভার র‍্যান্ডম লিংক ঠিক করে gate_id দেয় — ক্লায়েন্ট শুধু খুলবে,
    লিংক বদলানো/টাইমার চালানোর সুযোগ নেই। রিটার্ন: {gate_id, link, wait, count, pool}"""
    try:
        uid = await _gate_uid_guard(d.initData)
        if uid == 0 or uid in banned_cache: return {"ok": False, "msg": "auth"}
        # 🚦 রেট-লিমিট: ঘণ্টায় সর্বোচ্চ ৬০টা গেট শুরু (স্প্যাম/ফার্মিং আটকাতে)
        hour_ago = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        started_cnt = await db.ad_gates.count_documents({"uid": uid, "started_at": {"$gte": hour_ago}})
        if started_cnt >= 60: return {"ok": False, "msg": "rate"}
        aw, ac = await gate_get_settings()
        link, pool_size = await pick_link_for_user(uid, True)
        if not link: return {"ok": False, "msg": "no_link"}
        now = datetime.datetime.utcnow()
        res = await db.ad_gates.insert_one({
            "uid": uid, "movie_id": d.movieId, "adult": True, "link": link,
            "started_at": now, "away_ms": 0, "away_start": None, "beats": 0,
            "status": "running", "verified_at": None, "consumed_at": None})
        # 📳 টাইমার-শেষ নোটিফিকেশন: wait সেকেন্ড পর বট মেসেজ দেবে (ফোন ভাইব্রেট/রিং হবে)
        asyncio.create_task(gate_notify_worker(uid, str(res.inserted_id), aw))
        return {"ok": True, "gate_id": str(res.inserted_id), "link": link,
                "wait": aw, "count": ac, "pool": pool_size}
    except Exception as e:
        print(f"ad_gate_start error: {e}")
        return {"ok": False, "msg": "error"}

@app.post("/api/ad/beat")
async def ad_gate_beat(d: GateBeatModel):
    """📡 অ্যাওয়ে-বিকন: মিনি অ্যাপ লুকালে (অ্যাড খুলল) 'away', ফিরলে 'back' — সার্ভার আসল
    অ্যাড-দেখার সময় হিসাব করে। ক্লায়েন্ট টাইমার বদলেও এই হিসাব বদলানো যায় না।"""
    try:
        uid = await _gate_uid_guard(d.initData)
        if uid == 0: return {"ok": False}
        try: gid = ObjectId(d.gateId)
        except Exception: return {"ok": False}
        gate = await db.ad_gates.find_one({"_id": gid, "uid": uid, "status": "running"})
        if not gate: return {"ok": False}
        now = datetime.datetime.utcnow()
        if d.phase == "away":
            if gate.get("away_start") is None:
                await db.ad_gates.update_one({"_id": gid}, {"$set": {"away_start": now}, "$inc": {"beats": 1}})
        elif d.phase == "back":
            a_start = gate.get("away_start")
            if a_start is not None:
                away_add = int((now - a_start).total_seconds() * 1000)
                await db.ad_gates.update_one({"_id": gid}, {"$inc": {"away_ms": max(0, away_add), "beats": 1},
                                                           "$set": {"away_start": None}})
        return {"ok": True}
    except Exception as e:
        print(f"ad_gate_beat error: {e}")
        return {"ok": False}

@app.post("/api/ad/verify")
async def ad_gate_verify(d: GateVerifyModel):
    """✅ গেট যাচাই — সার্ভারের হিসাবই চূড়ান্ত (TTL এক্সপায়ার নেই — ইউজার-ফ্রেন্ডলি):
    • beacon এলে: সত্যিকারের অ্যাডে-থাকার সময় (away_ms) ≥ wait হতে হবে
    • beacon না এলে (PC-তে blur/focus দিয়ে বিকন আসে; তবুও না এলে): আসল সময় (start→verify) ≥ wait
    → কনসোল দিয়ে চেষ্টা করলেও অন্তত wait সেকেন্ড আসল সময় না গেলে ভেরিফাই হবে না।"""
    try:
        uid = await _gate_uid_guard(d.initData)
        if uid == 0: return {"ok": False, "msg": "auth"}
        try: gid = ObjectId(d.gateId)
        except Exception: return {"ok": False, "msg": "bad_gate"}
        gate = await db.ad_gates.find_one({"_id": gid, "uid": uid})
        if not gate: return {"ok": False, "msg": "bad_gate"}
        aw, _ac = await gate_get_settings()
        now = datetime.datetime.utcnow()
        if gate["status"] == "verified": return {"ok": True, "done": True, "remain": 0}
        if gate["status"] != "running": return {"ok": False, "msg": "expired"}
        started = gate["started_at"]
        elapsed_ms = int((now - started).total_seconds() * 1000)
        away_start = gate.get("away_start")
        away_ms = int(gate.get("away_ms") or 0) + (int((now - away_start).total_seconds() * 1000) if away_start else 0)
        beats = int(gate.get("beats") or 0)
        # বিকন-ভিত্তিক হিসাব প্রাধান্য পায়; বিকন না থাকলে আসল elapsed (ফলব্যাক)
        effective_ms = away_ms if beats > 0 else elapsed_ms
        remain_ms = max(0, aw * 1000 - effective_ms)
        if remain_ms <= 1200:  # ১.২ সেকেন্ড নেটওয়ার্ক-গ্রেস
            await db.ad_gates.update_one({"_id": gid}, {"$set": {"status": "verified", "verified_at": now, "suspect": beats == 0}})
            return {"ok": True, "done": True, "remain": 0}
        return {"ok": True, "done": False, "remain": int(remain_ms / 1000) + 1}
    except Exception as e:
        print(f"ad_gate_verify error: {e}")
        return {"ok": False, "msg": "error"}

@app.post("/api/ad/abort")
async def ad_gate_abort(d: GateVerifyModel):
    """🚪 ইউজার গেট বন্ধ করে দিলে (TRY AGAIN/মোডাল ক্লোজ) — চলমান গেট বাতিল"""
    try:
        uid = await _gate_uid_guard(d.initData)
        if uid == 0: return {"ok": False}
        try: gid = ObjectId(d.gateId)
        except Exception: return {"ok": False}
        await db.ad_gates.update_one({"_id": gid, "uid": uid, "status": "running"}, {"$set": {"status": "aborted"}})
        return {"ok": True}
    except Exception:
        return {"ok": False}

async def gate_check_and_consume(uid: int, movie_id: str, is_vip: bool):
    """🔐 /api/send-এর গেট-প্রহরী: VIP ছাড়া ভেরিফাইড গেট সংখ্যা < ad_count হলে ফাইল না।
    লিংক-পুল খালি হলে গেট বাইপাস (ইউজার আটকে থাকবে না)। রিটার্ন: (চলবে কি না, ভেরিফাইড গেট, দরকার)"""
    if is_vip: return True, 0, 0
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"})
    if not ((adl_cfg or {}).get("links", []) or []): return True, 0, 0  # পুল খালি → গেট নেই
    _, ac = await gate_get_settings()
    since = datetime.datetime.utcnow() - datetime.timedelta(minutes=15)
    cnt = await db.ad_gates.count_documents({"uid": uid, "movie_id": movie_id,
                                            "status": "verified", "verified_at": {"$gte": since}})
    return cnt >= ac, cnt, ac

async def gate_consume(uid: int, movie_id: str):
    """🧾 ডেলিভারি হয়ে গেলে ভেরিফাইড গেটগুলো consumed মার্ক — একবারের গেট = একবারের ফাইল (রিপ্লে আটকায)"""
    _, ac = await gate_get_settings()
    since = datetime.datetime.utcnow() - datetime.timedelta(minutes=15)
    gates = await db.ad_gates.find({"uid": uid, "movie_id": movie_id, "status": "verified",
                                    "verified_at": {"$gte": since}}).sort("verified_at", -1).to_list(ac)
    for g in gates:
        await db.ad_gates.update_one({"_id": g["_id"]}, {"$set": {"status": "consumed", "consumed_at": datetime.datetime.utcnow()}})

# 🧹 ৭-দিনের স্ট্যাটস সিস্টেম (মালিকের নির্দেশ): শুধু এই বটের গেট-কাউন্টই এখানে জমা হয়,
# ৭ দিনের ডাটা দেখানো হয়, আর ৭ দিন পার হলে পুরনো রেকর্ড নিজে নিজে মুছে ফ্রেশ শুরু হয়।
_last_gate_cleanup = 0.0  # ঘণ্টায় একবারই ক্লিন-আপ চালানোর থ্রটল
async def cleanup_old_gates():
    """🧹 ৭ দিনের পুরনো গেট-স্ট্যাট ডিলিট (মঙ্গো-TTL-এর সাথে ডাবল-নিরাপত্তা; TTL ব্যাকগ্রাউন্ডে
    একটু দেরি করলেও প্যানেল খুললেই এই ফাংশন ফ্রেশ করে দেয়)"""
    global _last_gate_cleanup
    now_ts = datetime.datetime.utcnow().timestamp()
    if now_ts - _last_gate_cleanup < 3600: return  # ঘণ্টায় একবারই যথেষ্ট
    _last_gate_cleanup = now_ts
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    try: await db.ad_gates.delete_many({"started_at": {"$lt": cutoff}})
    except Exception as e: print(f"cleanup_old_gates error: {e}")

@app.get("/api/admin/funnel")
async def admin_funnel(auth: bool = Depends(verify_admin)):
    try:
        await cleanup_old_gates()  # 🧹 ৭ দিন পার হলে পুরনো রেকর্ড নিজেই মুছে ফেলো
        now = datetime.datetime.utcnow()
        d7 = now - datetime.timedelta(days=7)
        out = {}
        for name, since in (("d7", d7), ("all", None)):
            # 🛡️ গেট consume হলেও verified_at থেকে যায় — তাই ভেরিফাই/ডেলিভারি গোনা হয়
            # টাইমস্ট্যাম্প দিয়ে (স্ট্যাটাস দিয়ে নয়), ফানেলে কেউ হারায় না
            starts = await db.ad_gates.count_documents({"started_at": {"$gte": since}} if since else {})
            verified = await db.ad_gates.count_documents({"verified_at": {"$gte": since}} if since else {"verified_at": {"$exists": True}})
            delivered = await db.ad_gates.count_documents({"consumed_at": {"$gte": since}} if since else {"status": "consumed"})
            suspect = await db.ad_gates.count_documents({"suspect": True, "verified_at": {"$gte": since}} if since else {"suspect": True})
            out[name] = {"starts": starts, "verified": verified, "delivered": delivered,
                         "suspect": suspect,
                         "complete_pct": round(delivered * 100.0 / starts, 1) if starts else 0.0}
        # 🔗 লিংক-ভিত্তিক পারফরম্যান্স (৭ দিন): কোন লিংক কতবার খুলেছে, কতটায় পুরো দেখা হয়েছে —
        # শুধু এই বটের মিনি-অ্যাপ থেকে খোলা গেটই এখানে আসে (অন্য-জায়গার কাউন্ট মেশে না)
        per_link = await db.ad_gates.aggregate([
            {"$match": {"started_at": {"$gte": d7}}},
            {"$group": {"_id": {"link": "$link"}, "starts": {"$sum": 1},
                        "verified": {"$sum": {"$cond": [{"$in": ["$status", ["verified", "consumed"]]}, 1, 0]}}}},
            {"$sort": {"starts": -1}}, {"$limit": 25}]).to_list(25)
        links_out = [{"link": r["_id"]["link"], "adult": True,
                      "starts": r["starts"], "verified": r["verified"]} for r in per_link]
        # 👥 ইউজার-স্প্লিট: ৭ দিনে কত ইউনিক ইউজার গেটে ডাকা খেয়েছে
        uniq_users = len(await db.ad_gates.distinct("uid", {"started_at": {"$gte": d7}}))
        return {"ok": True, "d7": out.get("d7"), "h24": out.get("d7"), "all": out.get("all"),
                "links": links_out, "unique_users_7d": uniq_users, "unique_users_24h": uniq_users}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

# ⚙️ অ্যাডমিন কনফিগ API — প্যানেল থেকে নিয়ন্ত্রণ (টাইমিং, ব্যানার, 🔞 অ্যাডাল্ট লিংক পুল)
@app.get("/api/admin/config")
async def admin_get_config(auth: bool = Depends(verify_admin)):
    adl = await db.settings.find_one({"id": "adult_direct_links"}); alinks = (adl or {}).get("links", []) or []
    aw, ac = await gate_get_settings()
    banners = await get_setting("banners_enabled", False)
    return {"ok": True, "adult_links": alinks, "links": [],
            "ad_wait": aw, "ad_count": ac, "banners_enabled": bool(banners)}

class AdminConfigModel(BaseModel):
    ad_wait: int | None = None
    ad_count: int | None = None
    banners_enabled: bool | None = None

@app.post("/api/admin/config")
async def admin_set_config(d: AdminConfigModel, auth: bool = Depends(verify_admin)):
    try:
        if d.ad_wait is not None:
            v = max(5, min(120, int(d.ad_wait)))
            await db.settings.update_one({"id": "ad_wait"}, {"$set": {"value": v}}, upsert=True)
        if d.ad_count is not None:
            v = max(1, min(5, int(d.ad_count)))
            await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": v}}, upsert=True)
        if d.banners_enabled is not None:
            await db.settings.update_one({"id": "banners_enabled"}, {"$set": {"status": bool(d.banners_enabled)}}, upsert=True)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

class AdminLinkModel(BaseModel):
    pool: str = "adult"  # 🔞 adult-only: শুধু অ্যাডাল্ট পুল
    action: str  # 'add' | 'del'
    url: str

@app.post("/api/admin/links")
async def admin_links(d: AdminLinkModel, auth: bool = Depends(verify_admin)):
    try:
        if d.pool != "adult": return {"ok": False, "msg": "adult_only"}  # 🔞 নরমাল পুল এই বটে নেই
        url = (d.url or "").strip()
        if not url.startswith(("http://", "https://")): return {"ok": False, "msg": "bad_url"}
        if d.action == "add":
            await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        elif d.action == "del":
            await db.settings.update_one({"id": "adult_direct_links"}, {"$pull": {"links": url}})
        else: return {"ok": False, "msg": "bad_action"}
        cfg = await db.settings.find_one({"id": "adult_direct_links"})
        return {"ok": True, "links": (cfg or {}).get("links", []) or []}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

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
            # ২৪ ঘণ্টা আগের সময় বের করা হচ্ছে
            expire_time = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
            
            # ডেটাবেস থেকে ২৪ ঘণ্টা আগে আনলক করা মুভিগুলো ডিলিট করা হচ্ছে
            result = await db.user_unlocks.delete_many({"unlocked_at": {"$lte": expire_time}})
            
            # কনসোলে লগ দেখানোর জন্য (প্রয়োজন না হলে রাখতে পারেন)
            if result.deleted_count > 0:
                print(f"🔒 Auto-locked {result.deleted_count} movies (24 hrs expired).")
        except Exception as e:
            print(f"Auto-lock worker error: {e}")
            
        # প্রতি ১ ঘণ্টা পর পর চেক করবে
        await asyncio.sleep(3600)

# নতুন কিউ ওয়ার্কার (ব্রডকাস্ট একটি একটি করে পাঠাবে)
async def broadcast_queue_worker():
    while True:
        try:
            # কিউ থেকে ডেটা নিবে (আগের ব্রডকাস্ট শেষ না হলে এখানে অপেক্ষা করবে)
            task_data = await broadcast_queue.get()
            await run_movie_broadcast(task_data['data'], task_data['admin_id'], task_data.get('movie_id'))
            broadcast_queue.task_done()
        except Exception as e:
            print(f"Queue Worker Error: {e}")
            await asyncio.sleep(5)

# স্টার্টআপ ইভেন্ট
@app.on_event("startup")
async def on_startup():
    global BOT_USER
    await init_db()
    await load_admins()
    await load_banned_users()
    # বটের আসল username নিয়ে নিই (শেয়ার ডিপ-লিংকের জন্য)
    try:
        me = await bot.get_me()
        if me and me.username: BOT_USER = me.username
    except Exception as e: print(f"Bot username fetch failed: {e}")
    asyncio.create_task(auto_delete_worker())
    asyncio.create_task(broadcast_queue_worker())
    asyncio.create_task(auto_lock_worker())
    asyncio.create_task(self_pinger())  # ✅ অ্যান্টি-স্লিপ: রেন্ডার যেন ১৫ মিনিটে ঘুমাতে না পারে
    # 🖼️ বট-প্রোফাইলের খালি "Description" লাইনে পরিষ্কার বর্ণনা (১ বার, স্টার্টআপে)
    # 📌 নোট: "X monthly users" ব্যাজটা টেলিগ্রাম নিজেই অটো দেখায় — কোড দিয়ে সেট/সরানোর API নেই;
    #     আসল অ্যাক্টিভ ইউজার বাড়লে টেলিগ্রাম নিজেই নম্বর বাড়াবে (বড় বটগুলোতেও একই সিস্টেম)।
    try:
        await bot.set_my_description("🔥 BD Viral Box — সব কনটেন্ট এক জায়গায়! নিচের Open App চাপুন অথবা START দিন।")
        await bot.set_my_short_description("🔥 সব কনটেন্ট এক জায়গায় — Open App / START চাপুন!")
    except Exception as e:
        print(f"Description set failed: {e}")

# ==========================================
# 6. Telegram Bot Commands
# ==========================================
@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    if uid in banned_cache:
        return await message.answer("🚫 আপনাকে ব্যান করা হয়েছে।", parse_mode="HTML")
        
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

    # 🔥 শেয়ার ডিপ-লিংক ব্রিজ: t.me/<bot>?start=<movieId>
    # এই লিংক শুধু অ্যাপের গেট-পাস করা নতুন ইউজারের জন্য (অ্যাপ /api/send থেকে 403 পেলে ইউজারকে এখানে পাঠায়)।
    # গেট আগেই পাস, তাই START চাপতেই ফাইল সাথে সাথে ডেলিভারি — ইউজার হাতে ফাইল, আর সে রেজিস্টার্ড+কাউন্টেড।
    # শেয়ার বাটনগুলো startapp লিংক বহন করে — পরের মানুষও অ্যাপ+গেট দিয়ে যাবে, অ্যাড-বাইপাস লিক নেই।
    args_s = message.text.split(" ")
    payload_s = args_s[1].strip() if len(args_s) > 1 else ""
    mv_s = None
    if payload_s and len(payload_s) == 24:
        try:
            mv_s = await db.movies.find_one({"_id": ObjectId(payload_s)})
        except Exception:
            mv_s = None
    if mv_s:
        try:
            parts_s = await db.movies.find({"title": mv_s.get("title")}).sort("created_at", 1).to_list(10)
            total_s = len(parts_s) or 1
            protect_cfg_s = await db.settings.find_one({"id": "protect_content"})
            is_protected_s = protect_cfg_s.get("status", False) if protect_cfg_s else False
            time_cfg_s = await db.settings.find_one({"id": "del_time"})
            del_minutes_s = time_cfg_s['minutes'] if time_cfg_s else 60
            u_doc_s = await db.users.find_one({"user_id": uid})
            is_vip_s = bool(u_doc_s and u_doc_s.get("vip_until", now - datetime.timedelta(days=1)) > now)
            f_rows_s = [[types.InlineKeyboardButton(text="🔗 শেয়ার লিংক — যে কেউ ক্লিক করলে মুভির পেজ খুলবে", url=f"https://t.me/{BOT_USER}?startapp={str(mv_s['_id'])}")]]
            if APP_URL:
                f_rows_s.append([types.InlineKeyboardButton(text="🎬 আরো মুভি দেখুন", web_app=types.WebAppInfo(url=APP_URL))])
            f_kb_s = types.InlineKeyboardMarkup(inline_keyboard=f_rows_s)
            for idx_s, p_s in enumerate(parts_s, 1):
                cap_s = f"🎥 <b>{p_s.get('title')} [{p_s.get('quality', '')}]</b>"
                if total_s > 1: cap_s += f"\n📦 Part {idx_s}/{total_s}"
                cap_s += "\n\n✅ ডাউনলোড সফল! ফাইল চলে এসেছে — উপভোগ করো! 🔥"
                if is_vip_s:
                    cap_s += "\n\n💎 VIP সুবিধা: এই ফাইলটি কখনো ডিলিট হবে না!"
                else:
                    cap_s += f"\n\n⏳ সতর্কতা: সিকিউরিটির জন্য এই ভিডিওটি {del_minutes_s} মিনিট পর অটোমেটিক ডিলিট হয়ে যাবে!"
                try:
                    if p_s.get("file_type") == "video":
                        sent_s = await bot.send_video(uid, p_s["file_id"], caption=cap_s, parse_mode="HTML", protect_content=is_protected_s, reply_markup=f_kb_s)
                    else:
                        sent_s = await bot.send_document(uid, p_s["file_id"], caption=cap_s, parse_mode="HTML", protect_content=is_protected_s, reply_markup=f_kb_s)
                    await db.movies.update_one({"_id": p_s["_id"]}, {"$inc": {"clicks": 1}})
                    await db.user_unlocks.update_one({"user_id": uid, "movie_id": str(p_s["_id"])}, {"$set": {"unlocked_at": now}}, upsert=True)
                    if not is_vip_s and sent_s:
                        await db.auto_delete.insert_one({"chat_id": uid, "message_id": sent_s.message_id, "delete_at": now + datetime.timedelta(minutes=del_minutes_s)})
                except TelegramRetryAfter as e_s:
                    await asyncio.sleep(e_s.retry_after + 1)
                except Exception as send_err_s:
                    print(f"Start-delivery error: {send_err_s}")
            # 🛡️ গেট-ফানেল: ওয়েব অ্যাপে ভেরিফাই হওয়া গেটগুলো এই ডেলিভারিতেই খরচ হিসেবে ধরা হলো
            # (মিনি অ্যাপের /api/send থেকে start_first পেয়ে ইউজার ?start=<movieId> লিংকে এসেছিল)
            try: await gate_consume(uid, payload_s)
            except Exception: pass
            return  # ফাইলই মূল ডেলিভারি — ওয়েলকাম স্কিপ
        except Exception as e_s:
            print(f"Start payload error: {e_s}")

    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"

    kb = [
        [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=APP_URL))],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link), types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)]
    ]
    markup = types.InlineKeyboardMarkup(inline_keyboard=kb)
    
    text = f"👋 <b>স্বাগতম {message.from_user.first_name}!</b>\n\n🎬 BD Viral Box জগতে আপনাকে স্বাগতম। নিচের বাটনে ক্লিক করে মুভি উপভোগ করুন।"
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
        await m.answer("⚠️ দুঃখিত! আমি শুধুমাত্র টেক্সট মেসেজ গ্রহণ করি।\n\n🎬 মুভি দেখতে নিচের 'Watch Now' বাটনে ক্লিক করুন।", parse_mode="HTML")
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
    await c.message.answer("✍️ আপনার মেসেজ লিখুন (রিপ্লাই দেওয়ার জন্য):")
    await c.answer()

@dp.message(AdminStates.waiting_for_reply)
async def send_reply_to_user(m: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = data.get("reply_user_id")
    await state.clear()
    if user_id:
        try:
            await m.copy_to(chat_id=user_id)
            await m.answer("✅ রিপ্লাই পাঠানো হয়েছে!")
        except:
            await m.answer("❌ রিপ্লাই পাঠাতে ব্যর্থ হয়েছে।")

# ==========================================
# 7. Admin Commands & Movie Upload
# ==========================================
@dp.message(Command("cancel"))
async def cancel_cmd(m: types.Message, state: FSMContext):
    if m.from_user.id not in admin_cache: return
    await state.clear()
    await m.answer("❌ বর্তমান প্রসেস বাতিল করা হয়েছে!", parse_mode="HTML")

@dp.message(Command("protect"))
async def toggle_protect(m: types.Message):
    if m.from_user.id not in admin_cache: return
    cfg = await db.settings.find_one({"id": "protect_content"})
    current = cfg.get("status", False) if cfg else False
    new_status = not current
    await db.settings.update_one({"id": "protect_content"}, {"$set": {"status": new_status}}, upsert=True)
    status_text = "অন 🔒" if new_status else "অফ 🔓"
    await m.answer(f"✅ ফরোয়ার্ড প্রোটেকশন এখন <b>{status_text}</b>", parse_mode="HTML")

@dp.message(Command("setadcount"))
async def set_ad_count(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        count = int(m.text.split()[1])
        await db.settings.update_one({"id": "ad_count"}, {"$set": {"count": count}}, upsert=True)
        if count <= 0:
            await m.answer("✅ অ্যাড গেট অফ করা হয়েছে। এখন ডাউনলোডের আগে কোনো বিজ্ঞাপন দেখা লাগবে না।", parse_mode="HTML")
        else:
            await m.answer(f"✅ অ্যাড গেট <b>{count}</b> এ সেট হয়েছে। এখন প্রতি ডাউনলোডের আগে ইউজারকে <b>{count} টি</b> বিজ্ঞাপন দেখতে হবে (২-৩ সুপারিশকৃত)।", parse_mode="HTML")
    except: await m.answer("⚠️ /setadcount 2", parse_mode="HTML")

@dp.message(Command("setadwait"))
async def set_ad_wait(m: types.Message):
    """⏱️ গেটে অ্যাড পেজে কাটাতে হবে এমন সেকেন্ড (Movie Box BD-এর সেম কমান্ড)।
    সার্ভার-সাইড যাচাই — ইউজার কম সময়ে ফিরে এলে ফাইল পাবে না।"""
    if m.from_user.id not in admin_cache: return
    try:
        sec = int(m.text.split()[1])
        if not (5 <= sec <= 120):
            await m.answer("⚠️ ৫ থেকে ১২০ সেকেন্ডের মধ্যে দাও।", parse_mode="HTML"); return
        await db.settings.update_one({"id": "ad_wait"}, {"$set": {"value": sec}}, upsert=True)
        await m.answer(f"✅ অ্যাড গেট-অপেক্ষা <b>{sec} সেকেন্ড</b> সেট হয়েছে। এখন ইউজারকে অ্যাড পেজে অন্তত <b>{sec} সেকেন্ড</b> থাকতে হবে — সময়ের আগে ফিরলে <b>আবার চেষ্টা করো</b> মেসেজ দেখাবে আর ফাইল পাবে না।", parse_mode="HTML")
    except: await m.answer("⚠️ /setadwait 10", parse_mode="HTML")

@dp.message(Command("settime"))
async def set_delete_time(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        minutes = int(m.text.split()[1])
        await db.settings.update_one({"id": "del_time"}, {"$set": {"minutes": minutes}}, upsert=True)
        await m.answer(f"✅ অটো-ডিলিট টাইম <b>{minutes} মিনিট</b> এ সেট করা হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settime 60 (মিনিট লিখুন)", parse_mode="HTML")

@dp.message(Command("addadultlink"))
async def add_adult_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "adult_direct_links"}, {"$addToSet": {"links": url}}, upsert=True)
        await m.answer("✅ ১৮+ অ্যাড লিংক অ্যাড হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /addadultlink url", parse_mode="HTML")

# ==========================================
# 6.5 Ad Management Commands (নতুন অ্যাড সিস্টেম)
# ==========================================
BANNER_ZONES = {"home": "banner_home", "detail": "banner_detail", "detail_bottom": "banner_detail_bottom", "success": "banner_success", "profile": "banner_profile"}

@dp.message(Command("deladultlink"))
async def del_adult_link_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        url = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "adult_direct_links"}, {"$pull": {"links": url}})
        await m.answer("✅ ১৮+ অ্যাড লিংক ডিলিট হয়েছে।")
    except: await m.answer("⚠️ /deladultlink url", parse_mode="HTML")

@dp.message(Command("listlinks"))
async def list_links_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); adls = adl_cfg.get("links", []) if adl_cfg else []
    sp_cfg = await db.settings.find_one({"id": "sponsor_buttons"}); sps = sp_cfg.get("buttons", []) if sp_cfg else []
    ac_cfg = await db.settings.find_one({"id": "ad_count"}); ac = ac_cfg.get("count", 1) if ac_cfg else 1
    lines = [f"📢 বর্তমান অ্যাড সেটআপ (সব অ্যাড ১৮+)", f"🔢 ডাউনলোড গেট (ad_count): {ac}", "", f"🔴 অ্যাড লিংক ({len(adls)} টি):"]
    lines += [f"  {i+1}. {u}" for i, u in enumerate(adls)] or ["  (খালি)"]
    lines += ["", f"🔥 প্রোফাইল স্পনসর বাটন ({len(sps)} টি):"]
    lines += [f"  {i+1}. {b.get('label')} → {b.get('url')}" for i, b in enumerate(sps)] or ["  (খালি)"]
    # ব্যানার জোন স্ট্যাটাস — /setbanner দিয়ে বসানো HTML/স্ক্রিপ্ট কোন জায়গায় আছে তা এখানেই দেখা যাবে
    lines += ["", "🖼 ব্যানার জোন (অ্যাপের ৫টা জায়গা):"]
    for zname, zid in BANNER_ZONES.items():
        zdoc = await db.settings.find_one({"id": zid})
        zcode = (zdoc.get("code", "") if zdoc else "").strip()
        lines.append(f"  {zname}: {'✅ বসানো আছে (' + str(len(zcode)) + ' অক্ষর)' if zcode else '❌ খালি → /setbanner ' + zname + ' <কোড>'}")
    lines += ["", "💡 এক কমান্ডে সব জায়গায় বসাতে: /setbanner all <কোড>"]
    await m.answer("\n".join(lines))

@dp.message(Command("setbanner"))
async def set_banner_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        parts = m.text.split(" ", 2)
        zone = parts[1].strip().lower()
        code = parts[2].strip()
        if zone == "all":
            for zid in BANNER_ZONES.values():
                await db.settings.update_one({"id": zid}, {"$set": {"code": code}}, upsert=True)
            return await m.answer("✅ সব ৫টা জায়গায় ব্যানার বসানো হয়েছে!\n\n🖼 হোম উপরে + ডিটেইল বক্স + ডাউনলোড বাটনের নিচে + সাকসেস স্ক্রিন + প্রোফাইল\n\nওয়েব অ্যাপ বন্ধ করে আবার খুলে দেখুন।")
        if zone not in BANNER_ZONES:
            return await m.answer("⚠️ ভুল জোন! জোন হতে হবে: all / home / detail / detail_bottom / success / profile")
        await db.settings.update_one({"id": BANNER_ZONES[zone]}, {"$set": {"code": code}}, upsert=True)
        await m.answer(f"✅ '{zone}' জোনে ব্যানার/স্ক্রিপ্ট বসানো হয়েছে! ওয়েব অ্যাপ বন্ধ করে আবার খুলে দেখুন।")
    except: await m.answer("⚠️ ফরম্যাট: /setbanner all <HTML বা JS কোড>\n\n'all' = এক কমান্ডে সব ৫টা জায়গায় বসবে (সবচেয়ে সহজ!)। আলাদা বসাতে: home / detail / detail_bottom / success / profile")

@dp.message(Command("delbanner"))
async def del_banner_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        zone = m.text.split(" ", 1)[1].strip().lower()
        if zone == "all":
            for zid in BANNER_ZONES.values():
                await db.settings.update_one({"id": zid}, {"$set": {"code": ""}}, upsert=True)
            return await m.answer("✅ সব ৫টা জায়গার ব্যানার মুছে ফেলা হয়েছে।")
        if zone not in BANNER_ZONES:
            return await m.answer("⚠️ ভুল জোন! জোন: all / home / detail / detail_bottom / success / profile")
        await db.settings.update_one({"id": BANNER_ZONES[zone]}, {"$set": {"code": ""}}, upsert=True)
        await m.answer(f"✅ '{zone}' জোনের ব্যানার মুছে ফেলা হয়েছে।")
    except: await m.answer("⚠️ /delbanner home", parse_mode="HTML")

@dp.message(Command("addspbtn"))
async def add_sp_btn_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        parts = m.text.split(" ", 2)
        label = parts[1].strip()
        url = parts[2].strip()
        await db.settings.update_one({"id": "sponsor_buttons"}, {"$pull": {"buttons": {"label": label}}})
        await db.settings.update_one({"id": "sponsor_buttons"}, {"$push": {"buttons": {"label": label, "url": url}}}, upsert=True)
        await m.answer(f"✅ প্রোফাইলে স্পনসর বাটন '{label}' যোগ হয়েছে।")
    except: await m.answer('⚠️ ফরম্যাট: /addspbtn বাটনের_নাম url\nউদাহরণ: /addspbtn Hot Videos https://your-ad-link.com', parse_mode="HTML")

@dp.message(Command("delspbtn"))
async def del_sp_btn_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        label = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "sponsor_buttons"}, {"$pull": {"buttons": {"label": label}}})
        await m.answer(f"✅ স্পনসর বাটন '{label}' মুছে ফেলা হয়েছে।")
    except: await m.answer("⚠️ /delspbtn বাটনের_নাম", parse_mode="HTML")

@dp.message(Command("adstats"))
async def ad_stats_cmd(m: types.Message):
    """📊 অ্যাড গেট ফানেল — শেষ ৭ দিন (শুধু এই বটের মিনি-অ্যাপ থেকে খোলা গেটই গোনা হয়)।
    ৭ দিন পার হলে পুরনো রেকর্ড নিজে নিজে মুছে ফ্রেশ শুরু হয়। লিংক শেষ ৮ ডিজিট দেখানো হয়।"""
    if m.from_user.id not in admin_cache: return
    await cleanup_old_gates()  # 🧹 ৭ দিন পার হলে পুরনো রেকর্ড মুছে ফেলো
    now = datetime.datetime.utcnow()
    d7 = now - datetime.timedelta(days=7)
    starts = await db.ad_gates.count_documents({"started_at": {"$gte": d7}})
    verified = await db.ad_gates.count_documents({"verified_at": {"$gte": d7}})
    delivered = await db.ad_gates.count_documents({"consumed_at": {"$gte": d7}})
    uniq_users = len(await db.ad_gates.distinct("uid", {"started_at": {"$gte": d7}}))
    lines = [
        "📊 <b>অ্যাড গেট ফানেল — শেষ ৭ দিন</b> (শুধু এই বটের কাউন্ট)", "",
        f"🎯 গেট খুলেছে (অ্যাড লিংক গেছে): <b>{starts}</b>",
        f"✅ পুরো সময় দেখে ভেরিফাই: <b>{verified}</b>",
        f"📥 ফাইল ডেলিভারি: <b>{delivered}</b>",
        f"👥 ইউনিক ইউজার: <b>{uniq_users}</b>",
        f"📈 কমপ্লিট রেট: <b>{round(delivered * 100.0 / starts, 1) if starts else 0}%</b>", "",
        "🔗 <b>লিংক পারফরম্যান্স (শেষ ৮ ডিজিট):</b>"
    ]
    per_link = await db.ad_gates.aggregate([
        {"$match": {"started_at": {"$gte": d7}}},
        {"$group": {"_id": "$link", "starts": {"$sum": 1},
                    "verified": {"$sum": {"$cond": [{"$in": ["$status", ["verified", "consumed"]]}, 1, 0]}}}},
        {"$sort": {"starts": -1}}, {"$limit": 10}]).to_list(10)
    if not per_link:
        lines.append("· এখনো কোনো গেট খোলা হয়নি")
    else:
        for r in per_link:
            rate = round(r["verified"] * 100.0 / r["starts"]) if r["starts"] else 0
            lines.append(f"· <code>#{(r['_id'] or '')[-8:]}</code> — খুলা {r['starts']} | ভেরিফাই {r['verified']} | {rate}%")
    lines += ["", "💡 খুলেছে অনেক কিন্তু ভেরিফাই কম = ইউজার সময়ের আগেই ফিরে আসছে (লিংক লোড ধীর বা কনটেন্ট দুর্বল)।",
              "🧹 ডাটা ৭ দিনের — প্রতি ৭ দিনে নিজে নিজে মুছে ফ্রেশ শুরু হয়।"]
    await m.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("settg"))
async def set_tg_link(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        link = m.text.split(" ", 1)[1].strip()
        await db.settings.update_one({"id": "tg_link"}, {"$set": {"url": link}}, upsert=True)
        await m.answer("✅ টেলিগ্রাম চ্যানেল লিংক আপডেট হয়েছে।", parse_mode="HTML")
    except: await m.answer("⚠️ /settg https://t.me/...", parse_mode="HTML")

@dp.message(Command("delmovie"))
async def del_movie_cmd(m: types.Message):
    if m.from_user.id not in admin_cache: return
    try:
        title = m.text.split(" ", 1)[1].strip()
        result = await db.movies.delete_many({"title": title})
        if result.deleted_count > 0: await m.answer(f"✅ '<b>{title}</b>' ডিলিট হয়েছে!", parse_mode="HTML")
        else: await m.answer("⚠️ পাওয়া যায়নি")
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
        await m.answer(f"✅ <code>{target_uid}</code> কে {days} দিনের VIP দেওয়া হয়েছে!", parse_mode="HTML")
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
    await m.answer(f"🌟 <b>{data['title']}</b> আপকামিং লিস্টে যুক্ত হয়েছে!", parse_mode="HTML")

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
    await state.update_data(file_id=fid, file_type=ftype)
    await m.answer("✅ ফাইল পেয়েছি! এবার <b>পোস্টার</b> পাঠান।", parse_mode="HTML")

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
    await m.answer("✅ এবার <b>এপিসোড বা কোয়ালিটি</b> লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_title)
async def fallback_title(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>মুভির নাম (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_quality, F.text)
async def receive_movie_quality(m: types.Message, state: FSMContext):
    await state.update_data(quality=m.text.strip())
    
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 New Movie (Broadcast & Log)", callback_data="action_new_bcast")
    builder.button(text="➕ Add File Only (No Broadcast)", callback_data="action_add_file")
    builder.adjust(1)
    await m.answer(
        "✅ সব তথ্য নেওয়া হয়েছে!\n\n👇 এখন আপনি কি করতে চান তা নিচের যেকোনো একটি বাটনে ক্লিক করে নির্বাচন করুন:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )

@dp.message(AdminStates.waiting_for_quality)
async def fallback_quality(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>কোয়ালিটি (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_year)
async def fallback_year(m: types.Message):
    await m.answer("⚠️ দয়া করে <b>রিলিজ সাল (টেক্সট)</b> লিখুন। অথবা /cancel লিখুন।", parse_mode="HTML")

# নতুন মুভি হিসেবে ব্রডকাস্ট করার ফাংশন
@dp.callback_query(F.data == "action_new_bcast")
async def action_new_broadcast(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    ins_result = await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "categories": [], "clicks": 0, "created_at": datetime.datetime.utcnow()})
    movie_id = str(ins_result.inserted_id)
    
    await c.message.edit_text(f"🎉 <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n⏳ <b>ব্রডকাস্ট কিউতে যোগ করা হয়েছে...</b>\nআপনি চাইলে আরও মুভি আপলোড করতে পারেন, বট একটি একটি করে ইউজারদের কাছে মেসেজ পাঠাবে।", parse_mode="HTML")
    
    if LOG_CHANNEL_ID:
        try:
            log_kb = [
                [types.InlineKeyboardButton(text="🎬 Watch Now", url=f"https://t.me/{BOT_USER}?startapp={movie_id}")],
                [types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")],
                [types.InlineKeyboardButton(text="📝 Request Movie", url="https://t.me/requestmoviebox")]
            ]
            log_markup = types.InlineKeyboardMarkup(inline_keyboard=log_kb)
            log_text = f"🎬 <b>New Movie Uploaded</b>\n\n🏷 Title: <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n\n👤 Uploaded by Admin"
            await bot.send_photo(LOG_CHANNEL_ID, photo=data["photo_id"], caption=log_text, parse_mode="HTML", reply_markup=log_markup)
        except: pass

    await broadcast_queue.put({"data": data, "admin_id": c.from_user.id, "movie_id": movie_id})
    await c.answer("🚀 ব্রডকাস্ট শুরু হচ্ছে...")


# শুধু ফাইল অ্যাড করার ফাংশন (কোনো ব্রডকাস্ট বা লগ হবে না)
@dp.callback_query(F.data == "action_add_file")
async def action_add_file_only(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    await db.movies.insert_one({"title": data["title"], "quality": data["quality"], "photo_id": data["photo_id"], "file_id": data["file_id"], "file_type": data["file_type"], "categories": [], "clicks": 0, "created_at": datetime.datetime.utcnow()})
    
    await c.message.edit_text(f"✅ <b>{data['title']} [{data['quality']}]</b> সফলভাবে যুক্ত হয়েছে!\n\n❌ কোনো ব্রডকাস্ট বা লগ পোস্ট করা হয়নি। ফাইলটি শুধুমাত্র ওয়েব অ্যাপে যুক্ত হয়েছে।", parse_mode="HTML")
    await c.answer("✅ ফাইল অ্যাড হয়েছে!")

async def run_movie_broadcast(data, admin_id, movie_id=None):
    bcast_success = 0
    tg_cfg = await db.settings.find_one({"id": "tg_link"})
    tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
    link_18 = "https://t.me/+W5V9-mn08jMyYTE1"
    web_app_url = APP_URL if APP_URL else "https://t.me/" 
    # ডিপ-লিংক: Watch Now এ ক্লিক করতেই সরাসরি এই মুভির ডাউনলোড পেজ খুলবে (হোম পেজে না)
    if movie_id and BOT_USER:
        watch_btn = [types.InlineKeyboardButton(text="🎬 Watch Now", url=f"https://t.me/{BOT_USER}?startapp={movie_id}")]
    else:
        watch_btn = [types.InlineKeyboardButton(text="🎬 Watch Now", web_app=types.WebAppInfo(url=web_app_url))]
    bcast_kb = [
        watch_btn, 
        [types.InlineKeyboardButton(text="📥 ডাউনলোড কিভাবে করবেন", url="https://t.me/SakibMovieBox/62")],
        [types.InlineKeyboardButton(text="🚀 Join Channel", url=tg_link)],
        [types.InlineKeyboardButton(text="🔴 18+ Channel", url=link_18)],
        [types.InlineKeyboardButton(text="📝 Request Movie", url="https://t.me/requestmoviebox")],
    ]
    # 🔥 স্পনসর্ড বাটন: ১৮+ অ্যাড লিংক থাকলে প্রতিটি নিউ-মুভি ব্রডকাস্টে যুক্ত হবে
    adl_cfg_b = await db.settings.find_one({"id": "adult_direct_links"})
    bcast_links = adl_cfg_b.get("links", []) if adl_cfg_b else []
    if bcast_links and APP_URL:
        sp_url = f"{APP_URL.rstrip('/')}/api/ad/go?u={urllib.parse.quote(secrets.choice(bcast_links))}&slot=bcast"
        bcast_kb.append([types.InlineKeyboardButton(text="🔥 Today's Special Offer", url=sp_url)])
    bcast_markup = types.InlineKeyboardMarkup(inline_keyboard=bcast_kb)
    bcast_text = f"🆕 <b>New Movie Alert!</b>\n\n🎬 <b>{data['title']}</b>\n📺 Quality: <b>{data['quality']}</b>\n\n👇 এখনই দেখুন!"
    
    now = datetime.datetime.utcnow()
    # ২৪ ঘণ্টা (১ দিন) পর ডিলিট হবে
    delete_at = now + datetime.timedelta(days=1) 
    
    async for u in db.users.find():
        try:
            sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
            await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
            bcast_success += 1
            await asyncio.sleep(0.05) # ১ লাখ ইউজারের জন্য স্পিড বাড়ানো হয়েছে
        except TelegramRetryAfter as e:
            # টেলিগ্রাম যদি বল কিছুক্ষন অপেক্ষা করতে, তাহলে অপেক্ষা করবে
            await asyncio.sleep(e.retry_after + 1)
            try:
                sent_msg = await bot.send_photo(u['user_id'], photo=data["photo_id"], caption=bcast_text, reply_markup=bcast_markup, parse_mode="HTML")
                await db.auto_delete.insert_one({"chat_id": u['user_id'], "message_id": sent_msg.message_id, "delete_at": delete_at})
                bcast_success += 1
            except: pass
        except: pass
        
    try:
        await bot.send_message(admin_id, f"✅ <b>{data['title']}</b> এর ব্রডকাস্ট শেষ!\n\nসফলভাবে পাঠানো হয়েছে: <b>{bcast_success}</b> জনকে।\n⏳ নোটিফিকেশনগুলো <b>২৪ ঘণ্টা</b> পর অটো-ডিলিট হবে।", parse_mode="HTML")
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
        await m.answer("⚠️ ব্রডকাস্ট বাতিল হয়েছে।", parse_mode="HTML")
        return
    if m.reply_to_message:
        await state.clear()
        await m.answer("⚠️ ব্রডকাস্ট বাতিল করা হয়েছে কারণ আপনি রিপ্লাই করেছেন!", parse_mode="HTML")
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
    if not payment or payment["status"] != "pending": return await c.answer("⚠️ প্রসেস করা হয়েছে!", show_alert=True)
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
@app.get("/admin_panel", response_class=HTMLResponse)
async def admin_panel_ui(request: Request, auth: bool = Depends(verify_admin)):
    html_code = r'''
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>BD Viral Box — Admin Panel v2</title>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
        <style>
            :root { --bg:#0B0F1F; --card:#151B33; --card2:#10142A; --line:#232B4A; --txt:#C9D2F5; --mut:#8494b5;
                    --acc:#00E5A0; --acc2:#7C6CFF; --ok:#10b981; --warn:#f59e0b; --bad:#ef4444; --sky:#38bdf8; }
            * { margin:0; padding:0; box-sizing:border-box; }
            body { font-family:'Segoe UI',Tahoma,sans-serif; background:var(--bg); color:var(--txt); min-height:100vh; }
            .topbar { position:sticky; top:0; z-index:100; background:rgba(11,15,31,.92); backdrop-filter:blur(10px);
                      border-bottom:1px solid var(--line); padding:14px 20px; display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; }
            .brand { font-size:20px; font-weight:900; background:linear-gradient(90deg,#00E5A0,#7C6CFF); -webkit-background-clip:text; -webkit-text-fill-color:transparent; display:flex; align-items:center; gap:8px; }
            .brand small { font-size:11px; color:var(--mut); -webkit-text-fill-color:var(--mut); font-weight:600; }
            .tabs { display:flex; gap:6px; flex-wrap:wrap; }
            .tab-btn { border:1px solid var(--line); background:var(--card); color:var(--mut); padding:9px 16px; border-radius:10px;
                       cursor:pointer; font-weight:700; font-size:13px; transition:.2s; }
            .tab-btn:hover { color:#fff; border-color:#33456e; }
            .tab-btn.active { background:linear-gradient(90deg,#00E5A0,#7C6CFF); color:#04121B; border-color:transparent; box-shadow:0 4px 14px rgba(0,229,160,.35); }
            main { max-width:1180px; margin:0 auto; padding:22px 16px 60px; }
            .sec { display:none; } .sec.active { display:block; animation:fadeIn .25s ease; }
            @keyframes fadeIn { from{opacity:0; transform:translateY(6px);} to{opacity:1; transform:none;} }
            h2.sec-title { font-size:17px; font-weight:800; color:#fff; margin:4px 0 14px; display:flex; align-items:center; gap:8px; }
            h2.sec-title i { color:var(--acc); }
            .grid { display:grid; gap:14px; }
            .g5 { grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); }
            .g3 { grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); }
            .g2 { grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); }
            .card { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:18px; }
            .card h3 { font-size:12px; color:var(--mut); text-transform:uppercase; letter-spacing:1px; margin-bottom:10px; display:flex; align-items:center; gap:8px; }
            .card .big { font-size:30px; font-weight:900; color:#fff; }
            .card .sub { font-size:12px; color:var(--mut); margin-top:6px; }
            .ic { width:34px; height:34px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:15px; }
            .ic.blue { background:rgba(56,189,248,.15); color:var(--sky); } .ic.green { background:rgba(16,185,129,.15); color:var(--ok); }
            .ic.orange { background:rgba(245,158,11,.15); color:var(--warn); } .ic.red { background:rgba(239,68,68,.15); color:var(--bad); }
            .ic.pink { background:rgba(255,93,122,.15); color:#ff5d7a; }
            .stat-top { display:flex; align-items:center; justify-content:space-between; margin-bottom:8px; }
            /* Funnel */
            .funnel { display:flex; align-items:stretch; gap:10px; flex-wrap:wrap; }
            .f-step { flex:1; min-width:150px; background:var(--card2); border:1px solid var(--line); border-radius:14px; padding:14px; text-align:center; position:relative; }
            .f-step .num { font-size:26px; font-weight:900; color:#fff; }
            .f-step .lbl { font-size:12px; color:var(--mut); margin-top:4px; }
            .f-arrow { align-self:center; color:var(--mut); font-size:18px; }
            .f-bar { height:8px; border-radius:6px; background:#0e1729; overflow:hidden; margin-top:8px; }
            .f-bar > div { height:100%; border-radius:6px; background:linear-gradient(90deg,#00E5A0,#7C6CFF); transition:width .5s; }
            /* Tables */
            .tbl-wrap { background:var(--card); border:1px solid var(--line); border-radius:16px; overflow:auto; }
            table { width:100%; border-collapse:collapse; min-width:620px; }
            th { text-align:left; padding:13px 15px; font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:1px; border-bottom:1px solid var(--line); }
            td { padding:12px 15px; font-size:13px; border-bottom:1px solid rgba(36,50,82,.5); color:var(--txt); }
            tr:last-child td { border-bottom:none; } tr:hover td { background:rgba(255,255,255,.02); }
            .badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:11px; font-weight:700; }
            .b-blue { background:rgba(56,189,248,.15); color:var(--sky); } .b-pink { background:rgba(255,93,122,.15); color:#ff5d7a; }
            .b-green { background:rgba(16,185,129,.15); color:var(--ok); } .b-red { background:rgba(239,68,68,.15); color:var(--bad); }
            .link-cell { max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--sky); font-size:12px; }
            .tail-badge { display:inline-block; background:var(--card2); border:1px solid var(--line); border-radius:6px; padding:1px 6px; font-size:10px; color:var(--acc2); font-family:Consolas,monospace; margin-left:6px; }
            /* Forms */
            .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
            input[type=text], input[type=number], select {
                background:#0e1729; border:1px solid var(--line); color:#fff; padding:10px 13px; border-radius:10px; font-size:13px; outline:none; }
            input:focus, select:focus { border-color:var(--acc); }
            .btn { border:none; cursor:pointer; font-weight:800; font-size:13px; padding:10px 16px; border-radius:10px; transition:.2s; color:#fff; }
            .btn-grad { background:linear-gradient(135deg,#7C6CFF,#5A3FE0); box-shadow:0 4px 14px rgba(124,108,255,.3); }
            .btn-grad:hover { transform:translateY(-1px); }
            .btn-ghost { background:var(--card2); border:1px solid var(--line); color:var(--txt); }
            .btn-ghost:hover { border-color:#33456e; }
            .btn-mini { padding:6px 11px; font-size:12px; border-radius:8px; }
            .btn-del { background:rgba(239,68,68,.15); color:var(--bad); border:1px solid rgba(239,68,68,.3); }
            .btn-del:hover { background:var(--bad); color:#fff; }
            /* Link pool items */
            .pool-item { display:flex; align-items:center; justify-content:space-between; gap:10px; background:var(--card2);
                         border:1px solid var(--line); border-radius:12px; padding:10px 12px; margin-bottom:8px; }
            .pool-item .url { font-size:12px; color:var(--sky); word-break:break-all; }
            .pool-item .meta { font-size:11px; color:var(--mut); margin-top:3px; }
            .empty { text-align:center; padding:26px; color:var(--mut); font-size:13px; }
            /* Toggle switch */
            .switch { position:relative; display:inline-block; width:52px; height:28px; }
            .switch input { opacity:0; width:0; height:0; }
            .slider { position:absolute; cursor:pointer; inset:0; background:#243252; border-radius:28px; transition:.3s; }
            .slider:before { content:""; position:absolute; height:20px; width:20px; left:4px; top:4px; background:#8494b5; border-radius:50%; transition:.3s; }
            .switch input:checked + .slider { background:linear-gradient(90deg,#00E5A0,#7C6CFF); }
            .switch input:checked + .slider:before { transform:translateX(24px); background:#fff; }
            /* Range slider */
            input[type=range] { width:100%; accent-color:#7C6CFF; }
            .range-val { font-size:26px; font-weight:900; color:#fff; min-width:70px; text-align:center; background:var(--card2); border:1px solid var(--line); border-radius:12px; padding:6px 10px; }
            .note { font-size:12px; color:var(--mut); background:var(--card2); border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin-top:12px; line-height:1.7; }
            .toast { position:fixed; bottom:20px; left:50%; transform:translateX(-50%) translateY(80px); background:#1a2540; color:#fff;
                     border:1px solid var(--line); padding:12px 20px; border-radius:12px; font-size:13px; font-weight:700; opacity:0; transition:.3s; z-index:999; box-shadow:0 8px 24px rgba(0,0,0,.4); }
            .toast.show { transform:translateX(-50%) translateY(0); opacity:1; }
            .toast.ok { border-color:rgba(16,185,129,.5); } .toast.err { border-color:rgba(239,68,68,.5); }
            @media (max-width:640px) { .tab-btn { padding:8px 12px; font-size:12px; } .card .big { font-size:24px; } }
        </style>
    </head>
    <body>
        <div class="topbar">
            <div class="brand"><i class="fa-solid fa-fire-flame-curved"></i> BD Viral Box <small>ADMIN v2</small></div>
            <div class="tabs">
                <button class="tab-btn active" data-tab="overview" onclick="showTab('overview', this)"><i class="fa-solid fa-chart-pie"></i> ওভারভিউ</button>
                <button class="tab-btn" data-tab="ads" onclick="showTab('ads', this)"><i class="fa-solid fa-bullhorn"></i> অ্যাডস ম্যানেজার</button>
                <button class="tab-btn" data-tab="gate" onclick="showTab('gate', this)"><i class="fa-solid fa-shield-halved"></i> গেট সেটিংস</button>
                <button class="tab-btn" data-tab="movies" onclick="showTab('movies', this)"><i class="fa-solid fa-film"></i> মুভি</button>
            </div>
        </div>
        <main>
            <!-- ═══════════════ 📊 OVERVIEW ═══════════════ -->
            <section id="sec-overview" class="sec active">
                <h2 class="sec-title"><i class="fa-solid fa-chart-pie"></i> লাইভ ওভারভিউ</h2>
                <div class="grid g5" style="margin-bottom:16px;">
                    <div class="card"><div class="stat-top"><h3>Total Users</h3><span class="ic blue"><i class="fa-solid fa-users"></i></span></div><div class="big" id="totalUsers">0</div></div>
                    <div class="card"><div class="stat-top"><h3>আজকের নতুন</h3><span class="ic green"><i class="fa-solid fa-user-plus"></i></span></div><div class="big" id="todayUsers">0</div></div>
                    <div class="card"><div class="stat-top"><h3>Total Views</h3><span class="ic orange"><i class="fa-solid fa-eye"></i></span></div><div class="big" id="totalClicks">0</div></div>
                    <div class="card"><div class="stat-top"><h3>আজকের Views</h3><span class="ic red"><i class="fa-solid fa-chart-line"></i></span></div><div class="big" id="todayClicks">0</div></div>
                    <div class="card"><div class="stat-top"><h3>লাইভ (১ মিনিট)</h3><span class="ic green"><i class="fa-solid fa-signal"></i></span></div><div class="big" id="activeUsers">0</div></div>
                </div>

                <h2 class="sec-title"><i class="fa-solid fa-filter"></i> অ্যাড গেট ফানেল — শেষ ৭ দিন (শুধু এই বটের কাউন্ট)</h2>
                <div class="card" style="margin-bottom:16px;">
                    <div class="funnel">
                        <div class="f-step"><div class="num" id="fStarts">0</div><div class="lbl">🎯 গেট খুলেছে (অ্যাড লিংক গেছে)</div><div class="f-bar"><div id="fStartsBar" style="width:100%"></div></div></div>
                        <div class="f-arrow"><i class="fa-solid fa-angles-right"></i></div>
                        <div class="f-step"><div class="num" id="fVerified">0</div><div class="lbl">✅ পুরো সময় দেখে ভেরিফাই</div><div class="f-bar"><div id="fVerifiedBar" style="width:0%"></div></div></div>
                        <div class="f-arrow"><i class="fa-solid fa-angles-right"></i></div>
                        <div class="f-step"><div class="num" id="fDelivered">0</div><div class="lbl">📥 ফাইল ডেলিভারি</div><div class="f-bar"><div id="fDeliveredBar" style="width:0%"></div></div></div>
                    </div>
                    <div class="row" style="margin-top:14px; gap:20px; flex-wrap:wrap;">
                        <span class="badge b-green" id="fComplete">কমপ্লিট: 0%</span>
                        <span class="badge b-red" id="fSuspect">সন্দেহজনক (বিকন ছাড়া): 0</span>
                        <span class="badge b-blue" id="fUnique">ইউনিক ইউজার (৭ দিন): 0</span>
                    </div>
                    <div class="note">💡 <b>কীভাবে পড়বেন:</b> এখানে শুধু <b>এই বটের</b> গেট-কাউন্ট দেখানো হয় (অ্যাপ/অন্য-জায়গার কাউন্ট মেশে না)। "গেট খুলেছে" = অ্যাড লিংক ওপেন হয়েছে (এটাই আপনার ইম্প্রেশনের সুযোগ)। "ভেরিফাই" = পুরো সময় অ্যাড পেজে ছিল (রিয়েল ইম্প্রেশন)। খুলেছে অনেক কিন্তু ভেরিফাই কম = ইউজার আগেই ফিরে আসছে (লিংক লোড ধীর বা কনটেন্ট দুর্বল)। খুলেছেই কম = গেটে পৌঁছানোর আগেই ইউজার ঝেড়ে যাচ্ছে। <b>🧹 ডাটা ৭ দিনের — প্রতি ৭ দিনে নিজে নিজে মুছে ফ্রেশ শুরু হয়।</b></div>
                </div>

                <h2 class="sec-title"><i class="fa-solid fa-link"></i> লিংক পারফরম্যান্স (শেষ ৭ দিন)</h2>
                <div class="tbl-wrap"><table><thead><tr><th>লিংক</th><th>টাইপ</th><th>খোলা হয়েছে</th><th>ভেরিফাই</th><th>রেট</th></tr></thead>
                <tbody id="linkTable"><tr><td colspan="5" class="empty">লোড হচ্ছে...</td></tr></tbody></table></div>
            </section>

            <!-- ═══════════════ 📢 ADS MANAGER (🔞 ১৮+ অনলি — নরমাল পুল এই বটে নেই) ═══════════════ -->
            <section id="sec-ads" class="sec">
                <h2 class="sec-title"><i class="fa-solid fa-bullhorn"></i> অ্যাডস ম্যানেজার — 🔞 অ্যাডাল্ট পুল অনলি, প্যানেল থেকেই ফুল কন্ট্রোল</h2>
                <div class="grid g2">
                    <div class="card">
                        <h3><i class="fa-solid fa-fire" style="color:#ff5d7a"></i> অ্যাডাল্ট ডাইরেক্ট লিংক পুল (<span id="adultCount">0</span>)</h3>
                        <div class="row" style="margin-bottom:12px;">
                            <input type="text" id="adultUrl" placeholder="https://... (Adsterra/Monetag direct link)" style="flex:1; min-width:180px;">
                            <button class="btn btn-grad" onclick="addLink()"><i class="fa-solid fa-plus"></i> যোগ</button>
                        </div>
                        <div id="adultPool"><div class="empty">লোড হচ্ছে...</div></div>
                        <div class="note">🔞 এই বট ১৮+ অনলি — সব গেট/স্পনসর অ্যাড এই পুল থেকেই আসে। লিংক যোগ/বাদ করলে সাথে সাথে সব ইউজারের জন্য চালু হয়।</div>
                    </div>
                    <div class="card">
                        <h3><i class="fa-solid fa-rectangle-ad" style="color:var(--warn)"></i> ব্যানার অ্যাড</h3>
                        <div class="row" style="justify-content:space-between;">
                            <div><b style="font-size:14px;">সব ব্যানার (হোম/ডিটেইল/সাকসেস)</b><div class="sub">মিনি অ্যাপের ব্যানার জোন</div></div>
                            <label class="switch"><input type="checkbox" id="bannersToggle" onchange="saveBanners(this.checked)"><span class="slider"></span></label>
                        </div>
                        <div class="note" style="margin-top:14px;">⚠️ টেলিগ্রামের ইন-অ্যাপ ব্রাউজারে ব্যানার প্রায়ই ব্লক হয় — অনেক সময় লোডই হয় না (ডেড রিকোয়েস্ট → CPM পড়ে যায়)। ব্যানারের বদলে গেটের ডাইরেক্ট লিংকই আসল ইনকাম। <b>সাজেস্টেড: OFF</b> রেখে ২-৩ দিন ফানেল দেখুন — কমপ্লিট রেট বাড়লে প্যানেল থেকেই ফিরে ON করতে পারবেন।</div>
                    </div>
                </div>
            </section>

            <!-- ═══════════════ 🛡️ GATE SETTINGS ═══════════════ -->
            <section id="sec-gate" class="sec">
                <h2 class="sec-title"><i class="fa-solid fa-shield-halved"></i> গেট সেটিংস — Movie Box BD-এর সেম সার্ভার-সাইড সিস্টেম</h2>
                <div class="grid g2">
                    <div class="card">
                        <h3><i class="fa-solid fa-hourglass-half" style="color:var(--sky)"></i> অ্যাড ওয়াচ টাইম</h3>
                        <div class="row" style="margin-top:6px;">
                            <input type="range" id="adWaitRange" min="5" max="60" step="1" value="10" oninput="document.getElementById('adWaitVal').innerText=this.value+'s'">
                            <div class="range-val" id="adWaitVal">10s</div>
                        </div>
                        <div class="sub" style="margin-top:8px;">প্রতি গেটে ইউজার কত সেকেন্ড অ্যাড পেজে থাকবে (৫–৬০ সেকেন্ড)। সার্ভার নিজেই টাইম যাচাই করে — PC/DevTools দিয়ে ফাঁকি অসম্ভব।</div>
                    </div>
                    <div class="card">
                        <h3><i class="fa-solid fa-layer-group" style="color:var(--ok)"></i> অ্যাড রাউন্ড (প্রতি ফাইলে)</h3>
                        <div class="row" style="margin-top:6px;">
                            <input type="range" id="adCountRange" min="1" max="5" step="1" value="1" oninput="document.getElementById('adCountVal').innerText=this.value+'টি'">
                            <div class="range-val" id="adCountVal">1টি</div>
                        </div>
                        <div class="sub" style="margin-top:8px;">একটা ফাইল পেতে কতটা অ্যাড দেখতে হবে (১–৫)। প্রতিটি রাউন্ডে fresh র‍্যান্ডম লিংক (একই লিংক পরপর দুইবার যায় না)।</div>
                    </div>
                </div>
                <div class="row" style="margin-top:16px;">
                    <button class="btn btn-grad" onclick="saveGate()"><i class="fa-solid fa-floppy-disk"></i> সেটিংস সেভ করুন</button>
                    <span class="sub" style="font-size:12px; color:var(--mut);">বট কমান্ড /setadwait, /setadcount-এর সাথে সিঙ্ক করা — যেকোনো জায়গা থেকে বদলান</span>
                </div>
                <div class="note">🛡️ <b>সিস্টেমটা যেভাবে চলে (Movie Box BD-এর মতো):</b> প্রতিটি ডাউনলোড ক্লিকে fresh গেট → সার্ভার র‍্যান্ডম লিংক দেয় → ইউজার অ্যাড পেজে থাকার সময় সার্ভারে জমা হয় (visibility + blur-বিকন, PC-তেও কাজ করে) → ফিরে এসে সার্ভার যাচাই করে → ভেরিফাইড গেট ছাড়া /api/send ফাইল দেয় না। গেটে কোনো টাইম-লিমিট নেই — যখন ভেরিফাই করবে তখনই। সময়ের আগে ফিরলে "আবার চেষ্টা করুন" মেসেজ। VIP ইউজার গেট-ফ্রি। আনলক ১৫ মিনিট valid — একবার ডেলিভারির পর গেট খরচ হয়ে যায় (রিপ্লে বন্ধ)।</div>
            </section>

            <!-- ═══════════════ 🎬 MOVIES ═══════════════ -->
            <section id="sec-movies" class="sec">
                <h2 class="sec-title"><i class="fa-solid fa-film"></i> আপলোড করা মুভি</h2>
                <div class="tbl-wrap">
                    <div style="padding:14px 15px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap;">
                        <b style="color:#fff;"><i class="fa-solid fa-film" style="color:var(--acc)"></i> সব মুভি (<span id="movieTotal">0</span>)</b>
                        <input type="text" id="movieSearchInput" placeholder="🔍 মুভি সার্চ..." style="min-width:170px;">
                    </div>
                    <table><thead><tr><th>টাইটেল</th><th>কোয়ালিটি</th><th>Views</th><th>অ্যাকশন</th></tr></thead>
                    <tbody id="movieTableBody"><tr><td colspan="4" class="empty">লোড হচ্ছে...</td></tr></tbody></table>
                </div>
            </section>
        </main>

        <!-- Edit Modal -->
        <div id="editModal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,.7); z-index:2000; align-items:center; justify-content:center;">
            <div style="background:var(--card); padding:24px; border-radius:16px; width:92%; max-width:420px; border:1px solid var(--line);">
                <h3 style="color:var(--acc); margin-bottom:16px;"><i class="fa-solid fa-pen"></i> মুভি এডিট</h3>
                <input type="hidden" id="editId">
                <div style="margin-bottom:12px;"><label style="display:block; font-size:12px; color:var(--mut); margin-bottom:5px;">টাইটেল</label><input type="text" id="editTitle" style="width:100%;"></div>
                <div style="margin-bottom:12px;"><label style="display:block; font-size:12px; color:var(--mut); margin-bottom:5px;">পোস্টার File ID</label><input type="text" id="editPhoto" style="width:100%;" placeholder="নতুন Telegram File ID"></div>
                <div style="margin-bottom:12px;"><label style="display:block; font-size:12px; color:var(--mut); margin-bottom:5px;">কোয়ালিটি</label><input type="text" id="editQuality" style="width:100%;"></div>
                <div style="margin-bottom:12px;"><label style="display:block; font-size:12px; color:var(--mut); margin-bottom:5px;">সাল</label><input type="text" id="editYear" style="width:100%;"></div>
                <div class="row">
                    <button class="btn btn-grad" style="flex:1;" onclick="saveMovieEdit()"><i class="fa-solid fa-floppy-disk"></i> সেভ</button>
                    <button class="btn btn-ghost" style="flex:1;" onclick="document.getElementById('editModal').style.display='none'">বাতিল</button>
                </div>
            </div>
        </div>
        <div class="toast" id="toast"></div>

        <script>
            function showToast(msg, ok) { const t = document.getElementById('toast'); t.innerText = msg; t.className = 'toast show ' + (ok ? 'ok' : 'err'); setTimeout(() => t.classList.remove('show'), 2600); }
            function showTab(name, btn) {
                document.querySelectorAll('.sec').forEach(s => s.classList.remove('active'));
                document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('sec-' + name).classList.add('active');
                btn.classList.add('active');
                if (name === 'overview') { fetchStats(); fetchFunnel(); }
                if (name === 'ads') fetchConfig();
                if (name === 'gate') fetchConfig();
            }

            /* ═══ 📊 STATS + FUNNEL ═══ */
            async function fetchStats() { try { const res = await fetch('/api/admin/stats'); const d = await res.json();
                document.getElementById('totalUsers').innerText = d.total_users; document.getElementById('todayUsers').innerText = d.today_users;
                document.getElementById('totalClicks').innerText = d.total_clicks; document.getElementById('todayClicks').innerText = d.today_clicks;
                document.getElementById('activeUsers').innerText = d.active_users; } catch(e) {} }
            async function fetchFunnel() { try {
                const res = await fetch('/api/admin/funnel'); const d = await res.json(); if (!d.ok) return;
                const f = d.d7 || d.h24;
                document.getElementById('fStarts').innerText = f.starts; document.getElementById('fVerified').innerText = f.verified;
                document.getElementById('fDelivered').innerText = f.delivered;
                document.getElementById('fVerifiedBar').style.width = (f.starts ? Math.min(100, f.verified * 100 / f.starts) : 0) + '%';
                document.getElementById('fDeliveredBar').style.width = (f.starts ? Math.min(100, f.delivered * 100 / f.starts) : 0) + '%';
                document.getElementById('fComplete').innerText = 'কমপ্লিট: ' + f.complete_pct + '%';
                document.getElementById('fSuspect').innerText = 'সন্দেহজনক (বিকন ছাড়া): ' + f.suspect;
                document.getElementById('fUnique').innerText = 'ইউনিক ইউজার (৭ দিন): ' + ((d.unique_users_7d != null ? d.unique_users_7d : d.unique_users_24h) || 0);
                const tb = document.getElementById('linkTable');
                if (!d.links || !d.links.length) { tb.innerHTML = '<tr><td colspan="5" class="empty">এখনো কোনো গেট খোলা হয়নি (শেষ ৭ দিনে)</td></tr>'; return; }
                tb.innerHTML = d.links.map(l => {
                    const rate = l.starts ? Math.round(l.verified * 100 / l.starts) : 0;
                    const host = (() => { try { return new URL(l.link).hostname; } catch(e) { return l.link; } })();
                    // 🔖 লিংকের শেষ ৮ অক্ষর — একই ডোমেইনের একাধিক লিংক আলাদা করে চেনার জন্য
                    const tail = (l.link || '').slice(-8);
                    return '<tr><td class="link-cell" title="' + l.link + '"><a href="' + l.link + '" target="_blank" rel="noreferrer" style="color:var(--sky);text-decoration:none;">' + host + '</a><span class="tail-badge">#' + tail + '</span></td>' +
                        '<td><span class="badge b-pink">অ্যাডাল্ট</span></td>' +
                        '<td><b>' + l.starts + '</b></td><td><b style="color:var(--ok)">' + l.verified + '</b></td>' +
                        '<td><span class="badge ' + (rate >= 60 ? 'b-green' : (rate >= 30 ? 'b-blue' : 'b-red')) + '">' + rate + '%</span></td></tr>';
                }).join('');
            } catch(e) {} }

            /* ═══ 📢 ADS CONFIG (🔞 adult-only) ═══ */
            async function fetchConfig() { try {
                const res = await fetch('/api/admin/config'); const d = await res.json(); if (!d.ok) return;
                document.getElementById('adultCount').innerText = (d.adult_links || []).length;
                renderPool('adultPool', d.adult_links || []);
                document.getElementById('bannersToggle').checked = !!d.banners_enabled;
                document.getElementById('adWaitRange').value = d.ad_wait; document.getElementById('adWaitVal').innerText = d.ad_wait + 's';
                document.getElementById('adCountRange').value = d.ad_count; document.getElementById('adCountVal').innerText = d.ad_count + 'টি';
            } catch(e) {} }
            function renderPool(elId, links) {
                const el = document.getElementById(elId);
                if (!links.length) { el.innerHTML = '<div class="empty">পুল খালি — উপরে লিংক যোগ করুন</div>'; return; }
                el.innerHTML = links.map(u => {
                    let host = u; try { host = new URL(u).hostname; } catch(e) {}
                    // 🔖 শেষ ৮ অক্ষরের ব্যাজ — পারফরম্যান্স টেবিলের #ব্যাজের সাথে মিলিয়ে চেনার জন্য
                    return '<div class="pool-item"><div><div class="url">' + u + '</div><div class="meta">' + host + ' <span class="tail-badge">#' + u.slice(-8) + '</span></div></div>' +
                        '<button class="btn btn-del btn-mini" onclick="delLink(this)" data-url="' + u.replace(/"/g, '&quot;') + '"><i class="fa-solid fa-trash"></i></button></div>';
                }).join('');
            }
            async function addLink() {
                const input = document.getElementById('adultUrl');
                const url = (input.value || '').trim();
                if (!url) { showToast('লিংক লিখুন', false); return; }
                try { const res = await fetch('/api/admin/links', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pool: 'adult', action: 'add', url: url}) });
                    const d = await res.json(); if (d.ok) { input.value = ''; fetchConfig(); showToast('✅ লিংক যোগ হয়েছে', true); } else showToast('❌ ' + (d.msg || 'ব্যর্থ'), false);
                } catch(e) { showToast('❌ এরর', false); } }
            async function delLink(btn) {
                const url = btn.getAttribute('data-url');
                if (!confirm('এই লিংক মুছে ফেলবেন?')) return;
                try { const res = await fetch('/api/admin/links', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pool: 'adult', action: 'del', url: url}) });
                    const d = await res.json(); if (d.ok) { fetchConfig(); showToast('🗑️ মুছে ফেলা হয়েছে', true); } else showToast('❌ ব্যর্থ', false);
                } catch(e) { showToast('❌ এরর', false); } }
            async function saveBanners(on) {
                try { const res = await fetch('/api/admin/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({banners_enabled: on}) });
                    const d = await res.json(); if (d.ok) showToast(on ? '📢 ব্যানার চালু হয়েছে' : '🚫 ব্যানার বন্ধ হয়েছে', true); else showToast('❌ ব্যর্থ', false);
                } catch(e) { showToast('❌ এরর', false); } }

            /* ═══ 🛡️ GATE SETTINGS ═══ */
            async function saveGate() {
                const body = { ad_wait: parseInt(document.getElementById('adWaitRange').value),
                               ad_count: parseInt(document.getElementById('adCountRange').value) };
                try { const res = await fetch('/api/admin/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
                    const d = await res.json(); if (d.ok) showToast('✅ গেট সেটিংস সেভ হয়েছে — এখনই সব ইউজারের জন্য চালু', true); else showToast('❌ ব্যর্থ', false);
                } catch(e) { showToast('❌ এরর', false); } }

            /* ═══ 🎬 MOVIES ═══ */
            let allMovies = [];
            async function fetchMovies() { try { const res = await fetch('/api/admin/movies'); allMovies = await res.json();
                document.getElementById('movieTotal').innerText = allMovies.length; renderMovies(allMovies); } catch(e) {} }
            function renderMovies(list) {
                const tbody = document.getElementById('movieTableBody');
                if (!list.length) { tbody.innerHTML = '<tr><td colspan="4" class="empty">কোনো মুভি নেই</td></tr>'; return; }
                tbody.innerHTML = list.map(m => '<tr id="row-' + m._id + '">' +
                    '<td><strong>' + (m.title || '') + '</strong><br><small style="color:var(--mut)">ID: ' + m._id + '</small></td>' +
                    '<td>' + (m.quality || 'N/A') + '</td>' +
                    '<td><span class="badge b-blue"><i class="fa-solid fa-eye"></i> ' + (m.clicks || 0) + '</span></td>' +
                    '<td><button class="btn btn-ghost btn-mini" onclick="openEditModal(\'' + m._id + '\')" style="margin-right:6px;"><i class="fa-solid fa-pen"></i> এডিট</button>' +
                    '<button class="btn btn-del btn-mini" onclick="deleteMovie(\'' + m._id + '\')"><i class="fa-solid fa-trash"></i></button></td></tr>').join(''); }
            document.getElementById('movieSearchInput').addEventListener('input', function(e) {
                const q = e.target.value.toLowerCase();
                renderMovies(allMovies.filter(m => (m.title||'').toLowerCase().includes(q) || (m.quality||'').toLowerCase().includes(q))); });
            async function deleteMovie(id) { if (!confirm('এই ফাইল মুছবেন?')) return;
                try { const res = await fetch('/api/admin/movie/' + id, { method: 'DELETE' }); const d = await res.json();
                    if (d.ok) { const r = document.getElementById('row-' + id); if (r) r.remove(); allMovies = allMovies.filter(m => m._id !== id); document.getElementById('movieTotal').innerText = allMovies.length; fetchStats(); showToast('🗑️ মুছে ফেলা হয়েছে', true); } } catch(e) {} }
            function openEditModal(id) { const m = allMovies.find(x => x._id === id); if (!m) return;
                document.getElementById('editId').value = m._id; document.getElementById('editTitle').value = m.title || '';
                document.getElementById('editPhoto').value = m.photo_id || ''; document.getElementById('editQuality').value = m.quality || '';
                document.getElementById('editYear').value = m.year || '';
                document.getElementById('editModal').style.display = 'flex'; }
            async function saveMovieEdit() {
                const id = document.getElementById('editId').value;
                const data = { title: document.getElementById('editTitle').value, photo_id: document.getElementById('editPhoto').value,
                    quality: document.getElementById('editQuality').value, year: document.getElementById('editYear').value };
                try { const res = await fetch('/api/admin/movie/' + id, { method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data) });
                    const r = await res.json();
                    if (r.ok) { showToast('✅ মুভি আপডেট হয়েছে', true); document.getElementById('editModal').style.display = 'none'; fetchMovies(); }
                    else showToast('❌ ' + (r.detail || 'এরর'), false); } catch(e) { showToast('❌ এরর', false); } }

            /* ═══ BOOT ═══ */
            fetchStats(); fetchFunnel(); fetchConfig(); fetchMovies();
            setInterval(fetchStats, 60000); setInterval(fetchFunnel, 120000);
        </script>
    </body></html>'''
    # 💰 প্যানেলেও ETag-ক্যাশ — বারবার খুললেও ৩০৪ (খালি রেসপন্স), ব্যান্ডউইথ প্রায়-জিরো
    etag = '"' + hashlib.sha256(html_code.encode("utf-8")).hexdigest()[:32] + '"'
    _cc = "public, max-age=600, must-revalidate"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": _cc})
    return HTMLResponse(html_code, headers={"ETag": etag, "Cache-Control": _cc})

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
        # গত ৩০ দিনের মধ্যে যেগুলো বেশি দেখা হয়েছে (Top 10)
        thirty_days_ago = now - datetime.timedelta(days=30)
        movies = await db.movies.find({"created_at": {"$gte": thirty_days_ago}}).sort("clicks", -1).limit(10).to_list(10)
        
        for m in movies:
            m["_id"] = str(m["_id"])
        return movies
    except Exception as e:
        return []

@app.get("/api/movies/recent")
async def get_recent_movies():
    try:
        # সবশেষে আপলোড করা ১০টি মুভি (created_at অনুযায়ী সর্ট করা)
        movies = await db.movies.find({}).sort("created_at", -1).limit(10).to_list(10)
        for m in movies:
            m["_id"] = str(m["_id"])
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
    # খালি ভ্যালু ফিল্টার করা
    update_data = {k: v for k, v in movie_data.items() if v is not None and v != ""}
    
    # ডাটাবেস আপডেট
    result = await db.movies.update_one({"_id": ObjectId(movie_id)}, {"$set": update_data})
    
    if result.modified_count > 0:
        return {"ok": True, "message": "Movie updated successfully"}
    raise HTTPException(status_code=400, detail="Failed to update movie")

# ==========================================
# Get Photo ID for Admin Panel
# ==========================================
@dp.message(F.photo, StateFilter(None))
async def get_file_id_for_admin(message: types.Message):
    if message.from_user.id not in admin_cache: 
        return

    file_id = message.photo[-1].file_id
    
    await message.answer(
        f"🖼️ <b>New Photo File ID:</b>\n\n<code>{file_id}</code>\n\n✅ এই আইডিটি কপি করে Admin Panel এর 'Poster Photo ID' বক্সে পেস্ট করুন।", 
        parse_mode="HTML"
    )

# ==========================================
# 9. Main Web App UI
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def web_ui(request: Request):
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); adult_direct_links = adl_cfg.get('links', []) if adl_cfg else []; adl_json = json.dumps(adult_direct_links)
    # নতুন অ্যাড সিস্টেম: ব্যানার জোন, স্পনসর বাটন ও অ্যাড গেট কাউন্ট
    ac_cfg = await db.settings.find_one({"id": "ad_count"}); ad_count = ac_cfg.get("count", 1) if ac_cfg else 1
    # 🛡️ গেট-অপেক্ষার সেকেন্ড (সার্ভার-সাইড যাচাই — /setadwait বা প্যানেল থেকে বদলানো যায়)
    aw_cfg = await db.settings.find_one({"id": "ad_wait"}); ad_wait = aw_cfg.get("value", DEFAULT_AD_WAIT) if aw_cfg else DEFAULT_AD_WAIT
    try: ad_wait = max(5, min(120, int(ad_wait)))
    except Exception: ad_wait = DEFAULT_AD_WAIT
    bh_cfg = await db.settings.find_one({"id": "banner_home"}); banner_home = bh_cfg.get("code", "") if bh_cfg else ""
    bd_cfg = await db.settings.find_one({"id": "banner_detail"}); banner_detail = bd_cfg.get("code", "") if bd_cfg else ""
    bp_cfg = await db.settings.find_one({"id": "banner_profile"}); banner_profile = bp_cfg.get("code", "") if bp_cfg else ""
    bdb_cfg = await db.settings.find_one({"id": "banner_detail_bottom"}); banner_detail_bottom = bdb_cfg.get("code", "") if bdb_cfg else ""
    bs_cfg = await db.settings.find_one({"id": "banner_success"}); banner_success = bs_cfg.get("code", "") if bs_cfg else ""
    # 🎛️ অ্যাডমিন প্যানেলের ব্যানার-টগল: OFF থাকলে সব ব্যানার-স্লট খালি যাবে (ডেড ইম্প্রেশন বন্ধ)
    try:
        _banners_on = bool(await get_setting("banners_enabled", False))
    except Exception:
        _banners_on = True
    if not _banners_on:
        banner_home = banner_detail = banner_profile = banner_detail_bottom = banner_success = ""
    sp_cfg = await db.settings.find_one({"id": "sponsor_buttons"}); sponsor_buttons = sp_cfg.get("buttons", []) if sp_cfg else []

    html_code = '''
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>BD Viral Box</title>
        <script src="https://telegram.org/js/telegram-web-app.js"></script>
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap');
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { background: #0f172a; font-family: 'Inter', sans-serif; color: #fff; overscroll-behavior-y: none; transition: background 0.3s; } 
            body.oled-mode { background: #000000; }
            #welcomeScreen { position: fixed; top:0; left:0; width:100%; height:100%; background: #0f172a; z-index: 99999; display: flex; flex-direction: column; align-items: center; justify-content: center; transition: opacity 0.8s ease; }
            #welcomeScreen.hide { opacity: 0; visibility: hidden; }
            .ws-brand { font-size: 48px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; animation: pulse 1.5s infinite; }
            .ws-bn { font-size: 18px; color: #94a3b8; margin-top: 10px; opacity: 0; animation: fadeUp 1s 0.5s forwards; }
            @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.05); } 100% { transform: scale(1); } }
            @keyframes fadeUp { to { opacity: 1; transform: translateY(-10px); } }
            header { display: flex; justify-content: center; align-items: center; padding: 15px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); z-index: 1000; cursor: pointer; }
            body.oled-mode header { background: rgba(0, 0, 0, 0.95); border-color: #1a1a1a; }
            .logo { display: flex; align-items: center; font-size: 24px; font-weight: 900; background: linear-gradient(45deg, #ff416c, #ff4b2b); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
            .page-section { display: none; padding-bottom: 80px; }
            .page-section.active { display: block; }
            .cat-row { display: flex; flex-wrap: wrap; gap: 8px; padding: 15px; }
            .cat-chip { background: #1e293b; padding: 8px 16px; border-radius: 20px; white-space: nowrap; cursor: pointer; border: 1px solid #ef4444; font-weight: 600; font-size: 12px; transition: 0.3s; color: #cbd5e1; }
            .cat-chip.active { background: linear-gradient(45deg, #ef4444, #dc2626); border-color: #ef4444; color: white; box-shadow: 0 0 12px rgba(239, 68, 68, 0.4); }
            .movie-list { padding: 0 15px; display: flex; flex-direction: column; gap: 15px; }
            
            /* ✅ STATIC RAINBOW BORDER STYLE (No Animation) */
            .movie-card { 
                display: flex; 
                flex-direction: column; 
                background: #0f172a; 
                border-radius: 16px; 
                overflow: hidden; 
                cursor: pointer; 
                transition: 0.3s; 
                position: relative; 
                z-index: 1;
                border: none; 
            }
            body.oled-mode .movie-card { background: #000; }
            .movie-card:active { transform: scale(0.98); }
            
            /* Static Beautiful Gradient Border */
            .movie-card::before {
                content: "";
                position: absolute;
                inset: -3px; /* Border thickness */
                z-index: -1;
                /* Theme matching Gradient (Red to Orange to Purple) */
                background: linear-gradient(45deg, #ff416c, #ff4b2b, #ff8c00, #b91c1c);
                border-radius: 18px; /* Slightly larger than card radius */
            }

            .movie-card img { width: 100%; aspect-ratio: 16/9; object-fit: cover; display: block; }
            .movie-overlay { position: absolute; bottom: 0; left: 0; width: 100%; background: linear-gradient(to top, rgba(0,0,0,0.95) 0%, transparent 100%); padding: 40px 10px 10px 10px; }
            .movie-title { font-size: 14px; font-weight: 700; color: #fff; margin-bottom: 5px; text-shadow: 0 2px 4px rgba(0,0,0,0.8); white-space: normal; line-height: 1.4; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
            .movie-cats { display: flex; flex-wrap: wrap; gap: 5px; }
            .movie-cat-tag { background: rgba(239, 68, 68, 0.8); padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 600; color: #fff; }
            .fav-btn { position: absolute; top: 10px; right: 10px; background: rgba(0,0,0,0.6); border: none; width: 30px; height: 30px; border-radius: 50%; color: white; font-size: 14px; cursor: pointer; display: flex; align-items: center; justify-content: center; z-index: 10; }
            .fav-btn.active { color: #ef4444; }
            .admin-view-badge {
                position: absolute;
                top: 10px;
                left: 10px;
                background: rgba(0,0,0,0.8);
                color: #fbbf24; /* Gold */
                padding: 2px 6px;
                border-radius: 4px;
                font-size: 10px;
                font-weight: 800;
                z-index: 10;
                border: 1px solid #fbbf24;
                display: flex;
                align-items: center;
                gap: 3px;
            }
            .adult-lock-overlay { position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); display: flex; align-items: center; justify-content: center; color: #ef4444; font-size: 40px; z-index: 5; }
            .floating-btn { position: fixed; right: 15px; width: 50px; height: 50px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 20px; z-index: 500; cursor: pointer; box-shadow: 0 4px 15px rgba(0,0,0,0.5); border: 2px solid white; text-decoration: none; color: white; }
            .btn-tg { bottom: 160px; background: linear-gradient(45deg, #24A1DE, #1b7ba8); }
            .btn-18 { bottom: 100px; background: linear-gradient(45deg, #ef4444, #b91c1c); font-weight: bold; }
            .bottom-nav { position: fixed; bottom: 0; left: 0; width: 100%; background: rgba(15, 23, 42, 0.95); backdrop-filter: blur(10px); border-top: 1px solid #1e293b; display: flex; justify-content: space-around; padding: 10px 0; z-index: 1000; }
            body.oled-mode .bottom-nav { background: rgba(0, 0, 0, 0.95); border-color: #1a1a1a; }
            .nav-item { display: flex; flex-direction: column; align-items: center; color: #64748b; font-size: 11px; font-weight: 600; cursor: pointer; border: none; background: none; }
            .nav-item i { font-size: 20px; margin-bottom: 3px; }
            .nav-item.active { color: #ef4444; }
            .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); display: none; align-items: flex-end; justify-content: center; z-index: 3000; }
            .modal-content { background: #1e293b; width: 100%; max-width: 400px; padding: 25px; border-radius: 20px 20px 0 0; max-height: 90vh; overflow-y: auto; position: relative; }
            body.oled-mode .modal-content { background: #000000; }
            .detail-img { width: 100%; height: 250px; object-fit: cover; border-radius: 12px; margin-bottom: 15px; }
            .detail-title { font-size: 22px; font-weight: 800; margin-bottom: 5px; }
            .detail-meta { color: #94a3b8; font-size: 14px; margin-bottom: 15px; }
            .close-icon { position: absolute; top: 12px; right: 15px; width: 32px; height: 32px; border-radius: 50%; background: rgba(0,0,0,0.6); color: #fff; font-size: 18px; display: flex; align-items: center; justify-content: center; cursor: pointer; border: none; }
            .dl-file-btn { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 15px; background: #0f172a; border: 1px solid #334155; color: white; font-weight: 700; border-radius: 10px; margin-bottom: 10px; cursor: pointer; }
            body.oled-mode .dl-file-btn { background: #050505; border-color: #1a1a1a; }
            .dl-file-btn i { color: #ef4444; font-size: 18px; }
            .dl-file-btn.unlocked i { color: #10b981; }
            .age-box { text-align: center; }
            .age-btn { width: 100%; padding: 15px; border-radius: 12px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-top: 15px; }
            .age-yes { background: #ef4444; color: white; }
            .age-no { background: #334155; color: white; }
            .ad-box { text-align: center; padding: 20px; }
            .ad-icon { font-size: 60px; margin-bottom: 10px; color: #fbbf24; }
            .ad-title { color: #fbbf24; font-size: 20px; font-weight: 800; margin-bottom: 15px; }
            .ad-box-orange { background: #ea580c; color: white; padding: 12px; border-radius: 8px; margin-bottom: 10px; font-weight: 600; }
            .ad-box-black { background: #000000; color: #e2e8f0; padding: 12px; border-radius: 8px; margin-bottom: 20px; font-size: 14px; }
            /* 📳 ভাইব্রেট-নোট: চোখে পড়ে এমন সোনালি (amber) বক্স — ইউজার আগেই জেনে নেবে টাইমার শেষে বট মেসেজ দেবে */
            .ad-vibe-note { background: linear-gradient(135deg, #b45309, #f59e0b); color: #fff; padding: 11px 12px; border-radius: 8px; margin-bottom: 12px; font-size: 13px; line-height: 1.7; text-align: left; box-shadow: 0 4px 14px rgba(245,158,11,.35); }
            .ad-action-btn { width: 100%; padding: 15px; border-radius: 8px; font-weight: 700; border: none; font-size: 16px; cursor: pointer; margin-bottom: 10px; }
            .btn-ad-open { background: #ea580c; color: white; }
            .btn-ad-unlock { background: #10b981; color: white; }
            .btn-ad-tryagain { background: #ef4444; color: white; }
            .banner-slot { margin: 10px 15px 5px 15px; text-align: center; overflow: hidden; border-radius: 12px; }
            .banner-slot iframe, .banner-slot img { max-width: 100%; border: none; }
            .ad-progress { background: #334155; color: #fbbf24; padding: 10px; border-radius: 8px; margin-bottom: 10px; font-weight: 700; }
            .search-box { padding: 0 15px 15px; }
            .search-input { width: 100%; padding: 14px; border-radius: 12px; border: none; outline: none; background: #1e293b; color: #fff; font-size: 15px; border: 1px solid #334155; }
            body.oled-mode .search-input { background: #0a0a0a; border-color: #1a1a1a; }
            .profile-card { background: #1e293b; margin: 15px; border-radius: 16px; padding: 20px; border: 1px solid #334155; }
            body.oled-mode .profile-card { background: #0a0a0a; border-color: #1a1a1a; }
            .profile-action-btn { display: block; width: 100%; padding: 14px; border-radius: 12px; font-weight: 700; text-align: center; margin-bottom: 10px; border: none; color: white; text-decoration: none; cursor: pointer; font-size: 15px; transition: 0.2s; }
            .profile-action-btn:active { transform: scale(0.97); }
            .btn-dark-mode { background: #334155; display: flex; align-items: center; justify-content: center; gap: 10px; }
            .btn-fb { background: #1877F2; }
            .btn-main-ch { background: #24A1DE; }
            .btn-18-ch { background: #ef4444; }
            .btn-sax-grp { background: #8B5CF6; }
            .skeleton { background: #1e293b; border-radius: 12px; height: 160px; position: relative; overflow: hidden; }
            .skeleton::after { content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent); animation: shimmer 1.5s infinite; }
            @keyframes shimmer { 0% { transform: translateX(-100%); } 100% { transform: translateX(100%); } }
            .join-channel-btn { display: block; width: 100%; padding: 15px; border-radius: 12px; background: #24A1DE; color: white; font-weight: 700; text-decoration: none; font-size: 16px; text-align: center; margin-top: 15px; margin-bottom: 10px; box-shadow: 0 4px 10px rgba(36, 161, 222, 0.3); }
            
            /* ✅ Pagination Styles */
            .pagination-container { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 20px 15px 80px 15px; }
            .page-btn { background: #1e293b; color: #cbd5e1; border: 1px solid #334155; padding: 10px 15px; border-radius: 10px; font-weight: 700; cursor: pointer; transition: 0.2s; font-size: 14px; }
            body.oled-mode .page-btn { background: #0a0a0a; border-color: #1a1a1a; }
            .page-btn:hover { background: #334155; color: white; }
            .page-btn.active { background: linear-gradient(45deg, #ef4444, #dc2626); color: white; border-color: #ef4444; box-shadow: 0 0 8px rgba(239, 68, 68, 0.3); }
            .page-btn:disabled { background: #1e293b; color: #475569; cursor: not-allowed; border-color: #1e293b; }

           /* নতুন মেটা রো (Year এবং Category এর জন্য) */
           .movie-meta-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 5px;
           }
         .year-badge {
          background-color: #22B8FF;
          color: #fff;
          padding: 3px 8px;
          border-radius: 4px;
          font-size: 11px;
          font-weight: 700;
         }
         .cat-small-tag {
         background: rgba(255, 255, 255, 0.1);
         color: #cbd5e1;
         padding: 2px 6px;
         border-radius: 4px;
         font-size: 10px;
         margin-left: 4px;
         display: inline-block;
         }

         /* ========================= */
         /* ✅ TRENDING SLIDER STYLES */
         /* ========================= */
         .trending-section-wrapper {
            background: rgba(30, 41, 59, 0.4);
            margin: 10px 15px 20px 15px;
            padding: 15px;
            border-radius: 16px;
            border: 1px solid #334155;
         }
         body.oled-mode .trending-section-wrapper { background: #0a0a0a; border-color: #1a1a1a; }

         .section-header { 
            padding-bottom: 10px; 
            font-size: 18px; 
            font-weight: 800; 
            color: #fff; 
            display: flex; 
            align-items: center; 
            gap: 8px; 
            margin-bottom: 10px;
         }
         .section-header i { color: #ff4b2b; }

         .trending-slider-container {
            overflow-x: auto;
            display: flex;
            gap: 15px;
            scroll-behavior: smooth;
            padding-bottom: 5px;
            -ms-overflow-style: none;  
            scrollbar-width: none;  
         }
         .trending-slider-container::-webkit-scrollbar { display: none; }

         .trending-card { 
            flex: 0 0 auto;
            width: 240px; /* Wide Thumbnail Size */
            background: #0f172a;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 4px 10px rgba(0,0,0,0.5);
            border: 2px solid #334155;
            cursor: pointer;
            position: relative;
            transition: transform 0.2s;
         }
         body.oled-mode .trending-card { background: #000; border-color: #1a1a1a; }
         .trending-card:active { transform: scale(0.95); }

         .poster-slider { 
            width: 100%; 
            aspect-ratio: 16/9; 
            position: relative; 
         }
         .poster-slider img { 
            width: 100%; 
            height: 100%; 
            object-fit: cover; 
            display: block; 
         }

         .badge-18-slider { 
            position: absolute; 
            top: 5px; 
            left: 5px; 
            background: #ef4444; 
            color: white; 
            font-size: 9px; 
            font-weight: 800; 
            padding: 2px 6px; 
            border-radius: 4px; 
            z-index: 2; 
            box-shadow: 0 2px 4px rgba(0,0,0,0.5);
         }

         .rank-badge {
            position: absolute;
            top: 5px;
            right: 5px;
            background: rgba(0, 0, 0, 0.7);
            color: #fbbf24;
            width: 24px;
            height: 24px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
            font-weight: 800;
            z-index: 2;
            border: 1px solid #fbbf24;
         }

         .info-slider { 
            padding: 8px; 
         }
         .movie-title-slider { 
            font-size: 12px; 
            color: #fff; 
            font-weight: 600; 
            white-space: nowrap; 
            overflow: hidden; 
            text-overflow: ellipsis; 
            margin-bottom: 2px;
         }
         .movie-meta-slider { 
            font-size: 10px; 
            color: #94a3b8; 
            display: flex; 
            justify-content: space-between;
         }

        </style>
    </head>
    <body>
        <div id="welcomeScreen"><div class="ws-brand">BD Viral Box</div><div class="ws-bn"> ভাইরাল বক্স জগতে স্বাগতম</div></div>
        <header onclick="switchTab('home')"><div class="logo">BD Viral Box</div></header>

        <div id="tabHome" class="page-section active">
            <div class="search-box"><input type="text" id="searchInput" class="search-input" placeholder="🔍 খুঁজুন..."></div>
            <div class="banner-slot" id="bannerHome">__BANNER_HOME__</div>
            <div class="cat-row">
                <div class="cat-chip active" onclick="filterCat('Home', this)">HOME</div>
            </div>
            
            <!-- ✅ TRENDING NOW SLIDER -->
            <div class="trending-section-wrapper">
                <div class="section-header"><i class="fa-solid fa-fire"></i> Trending Now</div>
                <div class="trending-slider-container" id="trendingSlider">
                    <div style="width: 100%; text-align: center; color: #64748b; padding: 20px;">Loading...</div>
                </div>
            </div>

            <!-- ✅ RECENTLY ADDED HEADER -->
            <div style="padding: 0 15px 10px 15px;">
                <div class="section-header" style="margin-bottom: 5px; margin-left: 0px;">
                    <i class="fa-solid fa-clock"></i> Recently Added
                </div>
            </div>

            <div class="movie-list" id="movieListHome"><div class="skeleton"></div><div class="skeleton"></div></div>
            <div id="paginationHome" class="pagination-container"></div>
        </div>

        <div id="tabSearch" class="page-section"><div class="search-box" style="padding-top:15px;"><input type="text" id="searchInputMain" class="search-input" placeholder="🔍 সার্চ..." oninput="searchMovies()"></div><div class="movie-list" id="movieListSearch"></div><div id="paginationSearch" class="pagination-container"></div></div>
        <div id="tabFav" class="page-section"><h3 style="padding: 15px; color: #fbbf24;">❤️ ফেভারিট</h3><div class="movie-list" id="movieListFav"></div></div>
        
        <div id="tabSurprise" class="page-section">
            <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 60vh; text-align: center; padding: 20px;">
                <div style="font-size: 80px; margin-bottom: 20px; animation: pulse 1.5s infinite;">🎲</div>
                <h2 style="margin-bottom: 15px; color: #fbbf24;">মুভি রুলেট!</h2>
                <p style="color: #94a3b8; margin-bottom: 30px;">কী দেখবেন ঠিক করতে পারছেন না? বট আপনার জন্য একটি মুভি বেছে নিচ্ছে!</p>
                <button onclick="loadSurprise()" style="padding: 15px 40px; background: linear-gradient(45deg, #ff416c, #ff4b2b); color: white; border: none; border-radius: 30px; font-size: 18px; font-weight: 800; cursor: pointer; box-shadow: 0 0 20px rgba(255, 65, 108, 0.5);">🎲 Surprise Me!</button>
            </div>
        </div>

        <div id="tabProfile" class="page-section">
            <div class="profile-card">
                <div style="text-align: center; margin-bottom: 20px;"><h2 id="profileName">User</h2></div>
                <button class="profile-action-btn btn-dark-mode" onclick="toggleOledMode()">🌙 ডার্ক মোড (OLED) <span id="darkModeStatus">OFF</span></button>
                <a href="https://facebook.com/" class="profile-action-btn btn-fb" target="_blank">📘 Facebook Group</a>
                <a href="https://t.me/addlist/MwbWNafSFK4yZjhl" class="profile-action-btn btn-main-ch" target="_blank">🚀 Main Channel</a>
                <a href="https://t.me/+W5V9-mn08jMyYTE1" class="profile-action-btn btn-18-ch" target="_blank">🔴 18+ Channel</a>
                <a href="#" class="profile-action-btn btn-sax-grp" target="_blank">🔥 Sax Group</a>
                <div id="sponsorBtns"></div>
                <!-- 🧠 CPM: প্রোফাইল ব্যানার খালি — Profile ট্যাব খোলার মুহূর্তে JS দিয়ে মাউন্ট হয় (mountBannerSlot)।
                     পেজ-লোডে লুকানো সেকশনে ব্যানার লোড হলে ইউজার কিছু না দেখেই "মৃত" ইম্প্রেশন জমে, CPM পড়ে যায়। -->
                <div class="banner-slot" id="bannerProfile"></div>
            </div>
        </div>

        <a href="https://t.me/addlist/MwbWNafSFK4yZjhl" class="floating-btn btn-tg"><i class="fa-brands fa-telegram"></i></a>
        <a href="https://t.me/+W5V9-mn08jMyYTE1" class="floating-btn btn-18">18+</a>

        <div class="bottom-nav">
            <button class="nav-item active" onclick="switchTab('home', this)"><i class="fa-solid fa-house"></i>Home</button>
            <button class="nav-item" onclick="switchTab('search', this)"><i class="fa-solid fa-magnifying-glass"></i>Search</button>
            <button class="nav-item" onclick="switchTab('fav', this)"><i class="fa-solid fa-heart"></i>Favorites</button>
            <button class="nav-item" onclick="switchTab('surprise', this)"><i class="fa-solid fa-dice"></i>Surprise</button>
            <button class="nav-item" onclick="switchTab('profile', this)"><i class="fa-solid fa-user"></i>Profile</button>
        </div>

        <div id="ageModal" class="modal"><div class="modal-content age-box"><h2 style="color:#ef4444;">⚠️ বয়স সীমাবদ্ধতা</h2><p style="color:#cbd5e1; margin:15px 0;">আপনার বয়স কি ১৮ বছরের বেশি?</p><button class="age-btn age-yes" onclick="access18()">হ্যাঁ, আমি ১৮+</button><button class="age-btn age-no" onclick="closeModal('ageModal')">না</button></div></div>

        <div id="startModal" class="modal"><div class="modal-content age-box"><h2 style="color:#f59e0b;">✅ অ্যাড সম্পন্ন — এক ধাপ বাকি!</h2><p style="color:#cbd5e1; margin:15px 0;">নিচের বাটনে টেপ করো → বট চ্যাটে <b style="color:#22c55e;">START</b> চাপো → ফাইল <b style="color:#22c55e;">সাথে সাথে</b> তোমার চ্যাটে চলে আসবে! (Telegram-এর নিয়ম: একবার START দিতে হয়)</p><button class="age-btn age-yes" onclick="openStartChat()">▶ এখন START দিয়ে ফাইল নিন</button><button class="age-btn age-no" onclick="closeModal('startModal')">পরে দেখবো</button></div></div>

        <div id="detailModal" class="modal">
            <div class="modal-content">
                <button class="close-icon" onclick="closeModal('detailModal')"><i class="fa-solid fa-xmark"></i></button>
                <img id="detailImg" class="detail-img" src="">
                <h2 id="detailTitle" class="detail-title"></h2>
                <div id="detailMeta" class="detail-meta"></div>
                <div id="detailCats" style="margin-bottom: 15px;"></div>
                <!-- 🧠 CPM: ডিটেইলস ব্যানার খালি — মুভি খোলার মুহূর্তে JS দিয়ে মাউন্ট (লুকানো মোডালে পেজ-লোডে লোড = মৃত ইম্প্রেশন) -->
                <div class="banner-slot" id="bannerDetail"></div>
                <div id="fileButtonsContainer"></div>
                <button class="dl-file-btn unlocked" style="background: linear-gradient(45deg, #2563eb, #1e40af);" onclick="shareCurrentMovie()"><span><i class="fa-solid fa-share-nodes"></i> 🔗 লিংক কপি করে শেয়ার করুন</span></button>
                <div class="banner-slot" id="bannerDetailBottom"></div>
            </div>
        </div>

        <div id="adModal" class="modal">
            <div class="modal-content ad-box">
                <div class="ad-icon">📺</div>
                <h2 class="ad-title">সতর্কতা!</h2>
                <div id="adProgress" class="ad-progress"></div>
                <div class="ad-box-orange">ডাউনলোড করতে হলে অবশ্যই বিজ্ঞাপন দেখুন!</div>
                <div class="ad-box-black" id="adBoxBlack">প্রতিটি বিজ্ঞাপন লিংকে ক্লিক করে কমপক্ষে <b id="adWaitText">__AD_WAIT__ সেকেন্ড</b> থাকুন, তারপর ফিরে এসে বাটনে ক্লিক করুন।</div>
                <div class="ad-vibe-note">📳 <b>সহজ নিয়ম:</b> <span id="adVibeText">বিজ্ঞাপন খুলে __AD_WAIT__ সেকেন্ড থাকলে বট নিজে থেকেই আপনাকে মেসেজ দেবে — তখন ফোন ভাইব্রেট/রিং করবে 🔔। মেসেজ পেলেই Back দিয়ে ফিরে এসে ফাইল নিন, আর কিছু করতে হবে না!</span></div>
                <button class="ad-action-btn btn-ad-open" id="adClickBtn" onclick="openAdLink()">বিজ্ঞাপন খুলুন</button>
                <button class="ad-action-btn btn-ad-unlock" id="adVerifyBtn" onclick="checkAdWatched()" style="display:none;">✅ অ্যাড দেখে ফিরে এসেছি</button>
                <button class="ad-action-btn btn-ad-tryagain" id="adTryAgainBtn" onclick="resetAdModal()" style="display:none;">🔄 আবার চেষ্টা করুন</button>
            </div>
        </div>

        <div id="successModal" class="modal">
            <div class="modal-content" style="text-align: center; padding-top: 40px;">
                <i class="fa-solid fa-circle-check" style="font-size:70px; color:#4ade80; margin-bottom:20px;"></i>
                <h2>ফাইল পাঠানো হয়েছে!</h2>
                <p style="color:#94a3b8; margin-top:10px;">বট চেক করুন। নতুন মুভি আপডেট পেতে চ্যানেলে জয়েন করুন!</p>
                <a href="https://t.me/addlist/MwbWNafSFK4yZjhl" target="_blank" class="join-channel-btn">🚀 Join Channel</a>
                <button id="successOfferBtn" class="ad-action-btn btn-ad-open" style="background: linear-gradient(45deg, #d97706, #b45309); margin-top: 15px;" onclick="openSuccessOffer()">🔥 আজকের স্পেশাল অফার দেখুন!</button>
                <button class="dl-file-btn unlocked" onclick="closeModal('successModal'); tg.close();"><i class="fa-solid fa-check"></i> বটে যান</button>
                <!-- 🧠 CPM: সাকসেস মোডালের ব্যানার খালি — ফাইল পাঠানোর পর মোডাল খুলবে তখনই JS দিয়ে মাউন্ট হয় -->
                <div class="banner-slot" id="bannerSuccess"></div>
            </div>
        </div>

        <script>
            // Live Active Ping
            try {
                const tgPingUser = window.Telegram?.WebApp?.initDataUnsafe?.user;
                if (tgPingUser && tgPingUser.id) {
                    fetch('/api/user/ping', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({user_id: tgPingUser.id})
                    });
                    setInterval(() => {
                        fetch('/api/user/ping', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({user_id: tgPingUser.id})
                        });
                    }, 30000);
                }
            } catch(e) {}

            let tg = window.Telegram.WebApp; tg.expand();
            const ADULT_DIRECT_LINKS = __ADL_JSON__; const SPONSOR_BTNS = __SPONSOR_JSON__; const AD_COUNT = __AD_COUNT__; const AD_WAIT = __AD_WAIT__; const INIT_DATA = tg.initData || "";
            let uid = tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : 0; 
            let isUserVip = false; 
            let isAdmin = false; // Admin Variable
            let activeCat = "Home"; let userFavs = []; let active18Btn = null; let activeFileId = null; let activeIsAdult = false; let adStartTime = 0; let currentViewMovies = []; let BOT_USER = "__BOT_USER__"; let currentDetailId = "";
            // 🛡️ সার্ভার-সাইড গেট স্টেট (Movie Box BD-এর সেম সিস্টেম): gate_id/away-time সার্ভারেই আসল
            let adSettings = { ad_wait: __AD_WAIT__, ad_count: __AD_COUNT__ };
            let currentGateId = null; let gateOpening = false; let adRound = 0; let adBuzzDone = false;  // 📳 টাইমার-শেষ হ্যাপটিক একবারই
            let adAwayMs = 0; let adAwayStart = null; let adWasHidden = false; let adCountdownTimer = null; let lastGateLink = '';
            let homeCurrentPage = 1;
            let searchCurrentPage = 1;
            let trendingMovies = []; 

            setTimeout(function() { document.getElementById('welcomeScreen').classList.add('hide'); }, 2500);
            if(tg.initDataUnsafe && tg.initDataUnsafe.user) { document.getElementById('profileName').innerText = tg.initDataUnsafe.user.first_name; }
            
            async function fetchUserInfo() { 
                try { 
                    const res = await fetch('/api/user/' + uid); 
                    const data = await res.json(); 
                    isUserVip = data.vip; 
                    isAdmin = data.is_admin || false; // Set Admin Status
                } catch(e) {} 
            }
            
            function switchTab(tabName, btnEl) { document.querySelectorAll('.page-section').forEach(function(el) { el.classList.remove('active'); }); document.querySelectorAll('.nav-item').forEach(function(el) { el.classList.remove('active'); }); if(tabName === 'home') { activeCat = 'Home'; homeCurrentPage = 1; document.querySelectorAll('.cat-chip').forEach(function(el) { el.classList.remove('active'); }); var fc = document.querySelector('.cat-chip'); if(fc) fc.classList.add('active'); } document.getElementById('tab' + tabName.charAt(0).toUpperCase() + tabName.slice(1)).classList.add('active'); if(btnEl) btnEl.classList.add('active'); if(tabName === 'home') loadHomeMovies(1); if(tabName === 'fav') loadFavorites(); if(tabName === 'profile') mountBannerSlot('bannerProfile'); window.scrollTo({top:0, behavior:'smooth'}); }
            function filterCat(cat, btnEl) { activeCat = cat; homeCurrentPage = 1; document.querySelectorAll('.cat-chip').forEach(function(el) { el.classList.remove('active'); }); btnEl.classList.add('active'); loadHomeMovies(1); }
            
            function verify18(btnEl) { active18Btn = btnEl; if(localStorage.getItem('isAdult')) { if(btnEl) filterCat('Adult Content', btnEl); } else { document.getElementById('ageModal').style.display = 'flex'; } }
            function access18() { localStorage.setItem('isAdult', 'true'); closeModal('ageModal'); if(active18Btn) { filterCat('Adult Content', active18Btn); } else { loadHomeMovies(1); } }
            
            function closeModal(id) { document.getElementById(id).style.display = 'none'; }
            function toggleOledMode() { document.body.classList.toggle('oled-mode'); let sEl = document.getElementById('darkModeStatus'); if(document.body.classList.contains('oled-mode')) { sEl.innerText = 'ON'; localStorage.setItem('oledMode', 'true'); } else { sEl.innerText = 'OFF'; localStorage.setItem('oledMode', 'false'); } }
            if(localStorage.getItem('oledMode') === 'true') { document.body.classList.add('oled-mode'); document.getElementById('darkModeStatus').innerText = 'ON'; }
            
            // ✅ TRENDING SLIDER LOAD FUNCTION
            async function loadTrending() {
                try {
                    const res = await fetch('/api/movies/trending');
                    const movies = await res.json();
                    trendingMovies = movies;
                    const container = document.getElementById('trendingSlider');
                    
                    if (movies.length === 0) {
                        document.querySelector('.trending-section-wrapper').style.display = 'none';
                        return;
                    }

                    container.innerHTML = movies.map((m, index) => {
                        const badgeHtml = m.categories && m.categories.includes('Adult Content') 
                            ? '<div class="badge-18-slider">18+</div>' 
                            : '';
                        
                        const imgUrl = `/api/image/${m.photo_id}`;
                        const rank = index + 1;

                        return `
                        <div class="trending-card" onclick="openTrendingDetail(${index})">
                            <div class="poster-slider">
                                <img src="${imgUrl}" loading="lazy" alt="${m.title}">
                                ${badgeHtml}
                                <div class="rank-badge">${rank}</div>
                            </div>
                            <div class="info-slider">
                                <div class="movie-title-slider">${m.title}</div>
                                <div class="movie-meta-slider">
                                    <span>${m.quality || 'HD'}</span>
                                    <span>${m.year || 'N/A'}</span>
                                </div>
                            </div>
                        </div>
                        `;
                    }).join('');

                    startAutoSlider('trendingSlider');
                    
                } catch (error) {
                    console.error("Trending Error:", error);
                    document.querySelector('.trending-section-wrapper').style.display = 'none';
                }
            }

            // Auto Scroll Function (Updated)
            function startAutoSlider(sliderId) {
                const slider = document.getElementById(sliderId);
                if(!slider) return;

                const intervalId = setInterval(() => {
                    const cardWidth = 260; // 240px card + 20px gap
                    const maxScroll = slider.scrollWidth - slider.clientWidth;
                    
                    if (slider.scrollLeft >= maxScroll) {
                        slider.scrollTo({ left: 0, behavior: 'smooth' });
                    } else {
                        slider.scrollBy({ left: cardWidth, behavior: 'smooth' });
                    }
                }, 3000); 
            }

            function openTrendingDetail(index) {
                const originalView = currentViewMovies;
                currentViewMovies = trendingMovies;
                openDetail(index);
                currentViewMovies = originalView;
            }

            // ✅ Pagination Functions
            async function loadHomeMovies(page = 1) { 
                homeCurrentPage = page;
                const list = document.getElementById('movieListHome'); 
                list.innerHTML = '<div class="skeleton"></div>'; 
                try { 
                    const res = await fetch('/api/list?cat='+activeCat+'&uid='+uid+'&page='+page); 
                    const data = await res.json(); 
                    currentViewMovies = data.movies || []; 
                    let cardsHtml = currentViewMovies.map(function(m, index) { return createMovieCard(m, index); }); if(cardsHtml.length > 4) cardsHtml.splice(4, 0, sponsoredCardHtml()); if(cardsHtml.length > 11) cardsHtml.splice(11, 0, sponsoredCardHtml()); list.innerHTML = cardsHtml.length > 0 ? cardsHtml.join('') : '<p style="text-align:center; color:#64748b; padding:30px;">কোনো মুভি পাওয়া যায়নি!</p>'; 
                    renderPagination(data.total_pages, homeCurrentPage, 'paginationHome', 'loadHomeMovies'); 
                } catch(e) {} 
            }

            async function searchMovies(page = 1) { 
                const q = document.getElementById('searchInputMain').value.trim(); 
                const list = document.getElementById('movieListSearch'); 
                if(!q) { list.innerHTML = ''; document.getElementById('paginationSearch').innerHTML = ''; return; } 
                searchCurrentPage = page;
                try { 
                    const res = await fetch('/api/list?q='+encodeURIComponent(q)+'&uid='+uid+'&page='+page); 
                    const data = await res.json(); 
                    currentViewMovies = data.movies || []; 
                    let cardsHtmlS = currentViewMovies.map(function(m, index) { return createMovieCard(m, index); }); if(cardsHtmlS.length > 4) cardsHtmlS.splice(4, 0, sponsoredCardHtml()); list.innerHTML = cardsHtmlS.length > 0 ? cardsHtmlS.join('') : '<p style="text-align:center; color:#64748b;">খুঁজে পাওয়া যায়নি!</p>'; 
                    renderPagination(data.total_pages, searchCurrentPage, 'paginationSearch', 'searchMovies'); 
                } catch(e) {} 
            }

            function renderPagination(totalPages, currentPage, containerId, functionName) {
                const container = document.getElementById(containerId);
                if(totalPages <= 1) { container.innerHTML = ''; return; }
                let html = '';
                html += `<button class="page-btn" onclick="${functionName}(${currentPage - 1})" ${currentPage === 1 ? 'disabled' : ''}><i class="fa-solid fa-chevron-left"></i> Prev</button>`;
                let startPage = Math.max(1, currentPage - 1);
                let endPage = Math.min(totalPages, currentPage + 1);
                if(startPage > 1) { html += `<button class="page-btn" onclick="${functionName}(1)">1</button>`; if(startPage > 2) html += `<span style="color:#64748b;">...</span>`; }
                for(let i = startPage; i <= endPage; i++) { html += `<button class="page-btn ${i === currentPage ? 'active' : ''}" onclick="${functionName}(${i})">${i}</button>`; }
                if(endPage < totalPages) { if(endPage < totalPages - 1) html += `<span style="color:#64748b;">...</span>`; html += `<button class="page-btn" onclick="${functionName}(${totalPages})">${totalPages}</button>`; }
                html += `<button class="page-btn" onclick="${functionName}(${currentPage + 1})" ${currentPage === totalPages ? 'disabled' : ''}>Next <i class="fa-solid fa-chevron-right"></i></button>`;
                container.innerHTML = html;
                window.scrollTo({top:0, behavior:'smooth'});
            }

            function createMovieCard(m, index) { 
                let isFav = userFavs.includes(m._id); 
                let isAdult = m.categories && m.categories.includes("Adult Content");
                let isVerified = localStorage.getItem('isAdult') === 'true';
                let catsHtml = (m.categories || []).map(function(c) { return `<span class="movie-cat-tag">${c}</span>`; }).join(''); 
                let imgSrc = (isAdult && !isVerified) ? 'https://via.placeholder.com/300x169/1e293b/ef4444?text=18%2B+🔒' : `/api/image/${m.photo_id}`;
                let lockOverlay = (isAdult && !isVerified) ? `<div class="adult-lock-overlay"><i class="fa-solid fa-lock"></i></div>` : '';
                let clickAction = (isAdult && !isVerified) ? `onclick="verify18(null)"` : `onclick="openDetail(${index})"`;

                // Admin View Count Badge
                let adminViewBadge = '';
                if(isAdmin) {
                    adminViewBadge = `<div class="admin-view-badge"><i class="fa-solid fa-eye"></i> ${m.clicks || 0}</div>`;
                }
                
    return `<div class="movie-card" ${clickAction}>
                <div style="position: relative; flex-shrink: 0;">
                    <img src="${imgSrc}" style="width: 100%; aspect-ratio: 16/9; object-fit: cover;">
                    ${lockOverlay}
                    ${adminViewBadge}
                </div>
                <div class="movie-info">
                    <div class="movie-title">${m.title}</div>
                    <div class="movie-meta-row">
                        <div class="left-meta">
                            <span class="year-badge">${m.year || 'N/A'}</span>
                        </div>
                        <div class="right-meta">
                            ${(m.categories || []).map(function(c) { return `<span class="cat-small-tag">${c}</span>`; }).join('')}
                        </div>
                    </div>
                </div>
                <button class="fav-btn ${isFav ? 'active' : ''}" onclick="event.stopPropagation(); toggleFav('${m._id}', this)"><i class="fa-solid fa-heart"></i></button>
            </div>`; 
            }

            // ✅ FIXED openDetail to handle Single File and Multi File
            function openDetail(index) { 
                let m = currentViewMovies[index]; 
                if(!m) return; 
                currentDetailId = (m.files && m.files[0] && m.files[0].id) ? m.files[0].id : (m._id || '');
                document.getElementById('detailImg').src = `/api/image/${m.photo_id}`; 
                // Fix for "undefined" title
                document.getElementById('detailTitle').innerText = m.title || 'Unknown Movie'; 
                document.getElementById('detailMeta').innerHTML = `<span>${m.year || 'N/A'}</span>`; 
                document.getElementById('detailCats').innerHTML = (m.categories || []).map(function(c) { return `<span class="movie-cat-tag">${c}</span>`; }).join(' '); 
                
                let isAdult = m.categories && m.categories.includes("Adult Content");
                let btnsHtml = "";

                // Check if it is a multi-file structure (files array exists)
                if (m.files && Array.isArray(m.files) && m.files.length > 0) {
                    btnsHtml = m.files.map(function(f) { 
                        let isFree = f.is_unlocked || isUserVip; 
                        return `<button class="dl-file-btn ${isFree ? 'unlocked' : ''}" onclick="handleFileClick('${f.id}', ${isFree ? 'true' : 'false'}, ${isAdult ? 'true' : 'false'})"><span><i class="fa-solid fa-${isFree ? 'lock-open' : 'lock'}"></i> Download ${f.quality}</span></button>`; 
                    }).join('');
                } else {
                    // Handle Single File (The current bot structure)
                    let isFree = isUserVip; 
                    btnsHtml = `<button class="dl-file-btn ${isFree ? 'unlocked' : ''}" onclick="handleFileClick('${m._id}', ${isFree ? 'true' : 'false'}, ${isAdult ? 'true' : 'false'})"><span><i class="fa-solid fa-${isFree ? 'lock-open' : 'lock'}"></i> Download ${m.quality || 'Full Movie'}</span></button>`;
                }
                
                document.getElementById('fileButtonsContainer').innerHTML = btnsHtml;
                // 🧠 CPM: মোডাল খোলার মুহূর্তেই ব্যানার মাউন্ট — ইউজার দেখার মতো ব্যানারই ইম্প্রেশন খাবে
                mountBannerSlot('bannerDetail'); mountBannerSlot('bannerDetailBottom');
                document.getElementById('detailModal').style.display = 'flex';
            }
            
            // শেয়ার ডিপ-লিংক: t.me/<bot>?startapp=<movieId> খুললেই সরাসরি ওই মুভির ডাউনলোড পেজ
            function openDetailById(mid) {
                fetch('/api/movie/' + mid + '?uid=' + uid).then(function(r){ return r.json(); }).then(function(res) {
                    if(!res || !res.ok || !res.movie) { tg.showAlert('😢 মুভিটা খুঁজে পাওয়া যায়নি! হোম পেজে খুঁজে দেখো।'); return; }
                    currentViewMovies = [res.movie];
                    openDetail(0);
                }).catch(function(e) {});
            }
            function shareCurrentMovie() {
                if(!currentDetailId) return;
                let shareLink = 'https://t.me/' + BOT_USER + '?startapp=' + currentDetailId;
                const done = function(){ try { tg.showAlert('✅ লিংক কপি হয়েছে! 🔥\\n\\nবন্ধুদের পাঠাও — সে লিংকে ক্লিক করতেই সরাসরি এই মুভির ডাউনলোড পেজে চলে আসবে!'); } catch(e) {} };
                if(navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(shareLink).then(done, function(){ tg.showAlert(shareLink); }); }
                else { tg.showAlert(shareLink); }
            }
            
            function handleFileClick(fileId, isFree, isAdult) {
                activeFileId = fileId; activeIsAdult = true;  // 🔞 এই বট ১৮+ অনলি — সব গেট অ্যাডাল্ট পুল
                // 🎯 ফিক্স: আগে সরাসরি অ্যাড-মোডাল খুলত — এখন আগে /api/send চেষ্টা: সদ্য ভেরিফাই হওয়া গেট/আনলক থাকলে
                // ফাইল সাথে সাথে যাবে (রিলোড/ডিপ-লিংক ফ্লোতে দ্বিতীয়বার অ্যাড দেখতে হবে না)। সার্ভার gate_required
                // ফিরিয়ে দিলে (কোনো ভেরিফাইড গেট নেই) তখনই অ্যাড-মোডাল দেখাবে। 💎 VIP/গেট-অফ সরাসরি ফাইল।
                if(isFree || AD_COUNT <= 0) { sendFileRequest(fileId); return; }
                if(!(ADULT_DIRECT_LINKS && ADULT_DIRECT_LINKS.length)) { sendFileRequest(fileId); return; }
                sendFileRequest(fileId);
            }
            function showStartPrompt() { document.getElementById('startModal').style.display = 'flex'; }
            function openStartChat() { closeModal('startModal'); try { tg.openTelegramLink('https://t.me/' + BOT_USER + '?start=' + (activeFileId || '')); } catch(e) {} }
            // 🧠 CPM ফিক্স: লুকানো মোডাল/সেকশনের ব্যানারগুলো (bannerDetail, bannerDetailBottom, bannerSuccess,
            // bannerProfile) এখন খালি — যে মুহূর্তে ইউজারের চোখে সত্যিই দেখা যাবে, ঠিক তখনই JS দিয়ে srcdoc-iframe
            // মাউন্ট হয়। আগে পেজ-লোডেই ৪টা ব্যানার লুকানো অবস্থায় লোড হয়ে মৃত ইম্প্রেশন জমত — বিজ্ঞাপনদাতা ওগুলোর
            // জন্য টাকা দেয় না, তাই ইম্প্রেশন বাড়লেও রেভিনিউ না বাড়িয়ে CPM (রেভিনিউ ÷ ইম্প্রেশন) প্রতিদিন পড়ে যেত।
            // একই স্লটে আগে বানানো iframe ক্যাশে থেকে রিইউজ হয় (move করলে reload হয় না) → এক সেশনে এক স্লট = এক ইম্প্রেশন।
            const LAZY_BANNERS = __LAZY_BANNERS_JSON__;
            const adIframeCache = {};
            function mountBannerSlot(slotId) {
                let slot = document.getElementById(slotId); if (!slot) return;
                let code = LAZY_BANNERS[slotId];
                if (!code || !String(code).trim()) { slot.innerHTML = ''; return; }
                let cacheKey = 'banner::' + slotId;
                let f = adIframeCache[cacheKey];
                if (!f) {
                    f = document.createElement('iframe');
                    f.setAttribute('scrolling', 'no'); f.setAttribute('frameborder', '0');
                    f.style.cssText = 'border:0;overflow:hidden;width:100%;height:50px;max-width:100%;';
                    f.srcdoc = "<html><body style='margin:0;padding:0;background:transparent'>" + code + "</body></html>";
                    // অ্যাড স্ক্রিপ্ট লোড হওয়ার পর ভেতরের ব্যানারের আসল উচ্চতা অনুযায়ী iframe বসানো
                    let tries = 0;
                    let rt = setInterval(function() {
                        tries++;
                        try { let d = f.contentDocument; if (d && d.body && d.body.scrollHeight > 20) f.style.height = (d.body.scrollHeight + 2) + 'px'; } catch(e) {}
                        if (tries > 20) clearInterval(rt);
                    }, 300);
                    adIframeCache[cacheKey] = f;
                }
                if (slot.firstChild !== f) { slot.innerHTML = ''; slot.appendChild(f); }
            }
            function pickAdLink() {
                // সব অ্যাড ১৮+: শুধু এডাল্ট লিংক লিস্ট ব্যবহার হয়
                if(ADULT_DIRECT_LINKS && ADULT_DIRECT_LINKS.length > 0) return ADULT_DIRECT_LINKS[Math.floor(Math.random() * ADULT_DIRECT_LINKS.length)];
                return null;
            }
            async function logAdClick(slot, url) { try { await fetch('/api/ad/click', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, slot: slot, url: url, initData: INIT_DATA})}); } catch(e) {} }
            function updateAdProgress() { const el = document.getElementById('adProgress'); if(!el) return; const total = Math.max(adSettings.ad_count, 1); el.innerHTML = `📺 বিজ্ঞাপন স্টেপ: <b>${Math.min(adRound + 1, total)} / ${total}</b>`; }
            // 💰 অ্যাড রাউন্ড সিস্টেম: ad_count সেটিং অনুযায়ী ১টির বেশি অ্যাড দেখানো যায় + সার্ভার-সাইড লিংক রোটেশন
            function resetAdModal() {
                adStartTime = 0; adRound = 0; adAwayMs = 0; adAwayStart = null; adWasHidden = false; gateOpening = false; adBuzzDone = false;
                clearGateLS();  // 🔧 রিকভারি-রেকর্ডও বাতিল
                // 🛡️ চলমান সার্ভার-গেট বাতিল (TRY AGAIN / আগে ফিরে এলে পুরনো গেট আর গোনা হবে না)
                if (currentGateId) { try { fetch('/api/ad/abort', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({gateId: currentGateId, initData: INIT_DATA}), keepalive: true}).catch(function(){}); } catch(e) {} currentGateId = null; }
                if (adCountdownTimer) { clearInterval(adCountdownTimer); adCountdownTimer = null; }
                let wEl = document.getElementById('adWaitText'); if (wEl) wEl.innerText = adSettings.ad_wait + ' সেকেন্ড';
                // 📳 ভাইব্রেট-নোট আপডেট: সেটিং-এর আসল সেকেন্ড সংখ্যা দিয়ে
                let vibeEl = document.getElementById('adVibeText');
                if (vibeEl) vibeEl.innerHTML = `বিজ্ঞাপন খুলে <b>${adSettings.ad_wait} সেকেন্ড</b> থাকলে বট নিজে থেকেই আপনাকে মেসেজ দেবে — তখন ফোন ভাইব্রেট/রিং করবে 🔔। মেসেজ পেলেই Back দিয়ে ফিরে এসে ফাইল নিন, আর কিছু করতে হবে না!`;
                let stepEl = document.getElementById('adProgress');
                let warn = ' ⚠️ সময়ের আগে ফিরে এলে আবার শুরু থেকে দেখতে হবে!';
                if (stepEl) { stepEl.innerHTML = adSettings.ad_count > 1 ? `আপনাকে মোট <b>${adSettings.ad_count}টি</b> বিজ্ঞাপন দেখতে হবে। প্রতিটি লিংকে ক্লিক করে কমপক্ষে <b>${adSettings.ad_wait} সেকেন্ড</b> অপেক্ষা করে ফিরে আসুন.${warn}` : `লিংকে ক্লিক করে বিজ্ঞাপনটি দেখুন এবং কমপক্ষে <b>${adSettings.ad_wait} সেকেন্ড</b> অপেক্ষা করে ফিরে এসে নিচের বাটনে ক্লিক করুন.${warn}`; }
                document.getElementById('adClickBtn').innerText = 'বিজ্ঞাপন খুলুন';
                document.getElementById('adClickBtn').style.display = 'block';
                document.getElementById('adVerifyBtn').style.display = 'none';
                document.getElementById('adTryAgainBtn').style.display = 'none';
            }

            // 📡 সার্ভার-বিকন: মিনি অ্যাপ লুকালে/ফোকাস হারালে (অ্যাড খুলল) 'away', ফিরলে 'back' — সার্ভার আসল অ্যাড-দেখার
            // সময় নিজে জমা রাখে। কনসোল থেকে টাইমার বদলালেও সার্ভারের হিসাব বদলায় না (PC বাইপাস বন্ধ)।
            function sendGateBeat(phase) {
                if (!currentGateId) return;
                try {
                    const blob = new Blob([JSON.stringify({gateId: currentGateId, initData: INIT_DATA, phase: phase})], {type: 'application/json'});
                    if (navigator.sendBeacon && navigator.sendBeacon('/api/ad/beat', blob)) return;
                } catch(e) {}
                try { fetch('/api/ad/beat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({gateId: currentGateId, initData: INIT_DATA, phase: phase}), keepalive: true}).catch(function(){}); } catch(e) {}
            }

            // 🔧 রিলোড-রিকভারি (ফাইল-না-দেওয়ার বাগ ফিক্স): মোবাইলে অ্যাড থেকে ফিরলে কখনো কখনো মিনি-অ্যাপ
            // রিলোড হয়ে যায় — তখন চলমান গেটের gate_id JS-স্টেটের সাথে মুছে যেত → ইউজার ১০ সেকেন্ড
            // পুরো অ্যাড দেখে ফিরেও ফাইল পেত না। এখন gate_id localStorage-এ সেভ হয় — পেজ লোডে
            // সার্ভারকে জিজ্ঞেস করে: ওই গেটের সময় পুরো হয়ে থাকলে অটো-ভেরিফাই করে ফাইল ডেলিভারি!
            function saveGateLS(gid, fid) { try { localStorage.setItem('vb_pending_gate', JSON.stringify({g: gid, f: fid, t: Date.now()})); } catch(e) {} }
            function clearGateLS() { try { localStorage.removeItem('vb_pending_gate'); } catch(e) {} }
            async function recoverPendingGate() {
                let rec = null; try { rec = JSON.parse(localStorage.getItem('vb_pending_gate') || 'null'); } catch(e) {}
                if (!rec || !rec.g) return;
                // 🔧 বাগ-ফিক্স: আগে যাচাইয়ের আগেই রেকর্ড মুছে ফেলত — নেটওয়ার্ক একটু নাড়লেই ১০ সেকেন্ডের অ্যাড মানা গেট হারিয়ে যেত।
                // এখন: ৩০ মিনিট পুরনো হলে বাদ, নাহলে যাচাই সফল/চূড়ান্ত-ব্যর্থ হলেই রেকর্ড মোছা হবে —
                // নেটওয়ার্ক-ফেল হলে রেকর্ড থেকে যায়, পরের পেজ-লোডে আবার চেষ্টা হয় → অ্যাড দেখা ইউজার কখনোই ফাইল হারাবে না
                if (Date.now() - (rec.t || 0) > 30*60*1000) { clearGateLS(); return; }
                try {
                    const res = await fetch('/api/ad/verify', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({gateId: rec.g, initData: INIT_DATA})});
                    const v = await res.json();
                    if (v && v.ok && v.done && rec.f) { clearGateLS(); sendFileRequest(rec.f, true); }  // ✅ আগের অ্যাড পুরো দেখা ছিল → ফাইল অটো-ডেলিভারি
                    else if (v && v.ok === false && v.msg && v.msg !== 'error') { clearGateLS(); }  // গেট আর নেই (বাতিল/খরচ) → রেকর্ড বাদ
                } catch(e) {}  // নেটওয়ার্ক সমস্যা → রেকর্ড রেখে দাও, পরে আবার হবে
            }

            // 🛡️ অ্যান্টি-বাইপাস হেল্পার: শুধু অ্যাড পেজে (মিনি অ্যাপ লুকানো থাকা অবস্থায়) কাটানো সময়ই গোনা হয়;
            // অ্যাপে ফিরে বসে থাকলে টাইমার এগোবে না — আগে ফিরলে আবার শুরু থেকে অ্যাড দেখতে হবে
            function adTotalAway() { return adAwayMs + (adAwayStart !== null ? (Date.now() - adAwayStart) : 0); }

            async function openAdLink() {
                if (gateOpening) return; gateOpening = true;
                // 📳 টেপ দিলেই ফোন ভাইব্রেট করবে (Telegram হ্যাপটিক ইঞ্জিন + ব্রাউজার ফলব্যাক)
                try { tg.HapticFeedback.impactOccurred('medium'); } catch (e) {}
                try { if (navigator.vibrate) navigator.vibrate(80); } catch (e) {}
                document.getElementById('adClickBtn').style.display = 'none';
                try {
                    // 🛡️ লিংক + gate_id সার্ভার থেকেই আসে (র‍্যান্ডম, no-repeat) — ক্লায়েন্টে লিংক বদলানোর সুযোগ নেই
                    const res = await fetch('/api/ad/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({initData: INIT_DATA, movieId: String(activeFileId || ''), adult: true})});
                    const g = await res.json();
                    if (!g.ok || !g.link) {
                        // লিংক পুল খালি/সমস্যা → ইউজার যেন আটকে না থাকে
                        gateOpening = false; finishAdRounds(); return;
                    }
                    currentGateId = g.gate_id; lastGateLink = g.link;
                    saveGateLS(g.gate_id, String(activeFileId || ''));  // 🔧 রিলোড-রিকভারি: gate_id সেভ
                    if (g.wait) adSettings.ad_wait = g.wait;
                    if (g.count) adSettings.ad_count = Math.max(1, g.count);
                    tg.openLink(g.link);
                    adStartTime = Date.now();
                    // 🛡️ অ্যাড খুলল — থাকার সময় visibility/blur বিকনে সার্ভারে জমা হবে;
                    //    বাটনের কাউন্টডাউন এখন ওয়াল-ক্লক — PC-তে এক্সটার্নাল ব্রাউজারে অ্যাড খুললেও আর আটকে থাকবে না
                    adAwayMs = 0; adAwayStart = null; adWasHidden = false;
                    let vBtn = document.getElementById('adVerifyBtn');
                    vBtn.style.display = 'block';
                    vBtn.disabled = true;
                    vBtn.innerText = `⏳ অপেক্ষা করুন ${adSettings.ad_wait}s`;
                    if (adCountdownTimer) clearInterval(adCountdownTimer);
                    adCountdownTimer = setInterval(() => {
                        // ⏱️ ওয়াল-ক্লক কাউন্টডাউন (০.২৫ সে টিক — থ্রটল হলেও ফিরে এসে সঠিক সময় দেখাবে)
                        const remain = Math.max(0, Math.ceil((adSettings.ad_wait * 1000 - (Date.now() - adStartTime)) / 1000));
                        if (remain <= 0) { 
                            vBtn.disabled = false; vBtn.innerText = '✅ অ্যাড দেখে ফিরে এসেছি'; 
                            // 📳 অ্যাপে থাকলে (PC/ফিরে-এসে) টাইমার শেষ হলেই এক ঝটকা ভাইব্রেট/বিপ — একবারই
                            if (!adBuzzDone) { adBuzzDone = true; try { tg.HapticFeedback.notificationOccurred('success'); } catch(e) {} try { if (navigator.vibrate) navigator.vibrate([120,60,120]); } catch(e) {} }
                        }
                        else { vBtn.innerText = `⏳ অপেক্ষা করুন ${remain}s`; }
                    }, 250);
                } catch(e) {
                    gateOpening = false;
                    document.getElementById('adClickBtn').style.display = 'block';
                }
            }

            async function checkAdWatched() {
                if (adStartTime === 0 || !currentGateId) return;
                let vBtn = document.getElementById('adVerifyBtn');
                vBtn.disabled = true; vBtn.innerText = '🔍 সার্ভারে যাচাই হচ্ছে...';
                try {
                    // 🛡️ সার্ভার-ভেরিফাই: আসল সময় সার্ভারের হিসাব — ক্লায়েন্ট শুধু ফল দেখায়
                    const res = await fetch('/api/ad/verify', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({gateId: currentGateId, initData: INIT_DATA})});
                    const v = await res.json();
                    if (v.ok && v.done) {
                        currentGateId = null; gateOpening = false;
                        adRound++;
                        if (adRound >= adSettings.ad_count) {
                            finishAdRounds();
                        } else {
                            // পরের অ্যাড রাউন্ড
                            tg.showAlert(`✅ দারুণ! এবার পরের বিজ্ঞাপনটি দেখুন (${adRound + 1}/${adSettings.ad_count})`);
                            document.getElementById('adVerifyBtn').style.display = 'none';
                            let oBtn = document.getElementById('adClickBtn');
                            oBtn.style.display = 'block';
                            oBtn.innerText = `বিজ্ঞাপন ${adRound + 1}/${adSettings.ad_count} খুলুন`;
                        }
                    } else {
                        let remaining = (v && typeof v.remain === 'number') ? v.remain : adSettings.ad_wait;
                        if (v.ok === false) {
                            // auth/error/bad_gate — যাচাই-ই হয়নি, স্পষ্ট মেসেজ
                            tg.showAlert('⚠️ যাচাই করা গেল না — TRY AGAIN চেপে আবার চেষ্টা করুন।');
                        } else {
                            // ⏱️ সার্ভার বলল সময় বাকি — স্পষ্ট "আবার চেষ্টা করুন" মেসেজ
                            tg.showAlert(`⚠️ আপনি সময়ের আগেই ফিরে এসেছেন! বিজ্ঞাপন পুরো ${adSettings.ad_wait} সেকেন্ড থাকতে হবে। (${remaining}s বাকি ছিল) — TRY AGAIN চেপে আবার অ্যাড খুলুন।`);
                        }
                        document.getElementById('adVerifyBtn').style.display = 'none';
                        document.getElementById('adTryAgainBtn').style.display = 'block';
                    }
                } catch(e) {
                    vBtn.disabled = false; vBtn.innerText = '✅ অ্যাড দেখে ফিরে এসেছি';
                }
            }

            function finishAdRounds() {
                adStartTime = 0; adAwayMs = 0; adAwayStart = null; gateOpening = false;
                clearGateLS();  // 🔧 গেট শেষ — রিকভারি-রেকর্ড মুছে দাও (ডুপ্লিকেট যোগাযোগ বন্ধ)
                if (adCountdownTimer) { clearInterval(adCountdownTimer); adCountdownTimer = null; }
                closeModal('adModal');
                sendFileRequest(activeFileId, true);
            }

            // 🛡️ অ্যান্টি-বাইপাস মূল চেক: ইউজার অ্যাড থেকে অ্যাপে ফিরে এলেই চেক হয় (visibility + focus — দুই সংকেতেই) —
            // পুরো wait সেকেন্ড অ্যাডে থাকলে ভেরিফাই চালু হয়, আগে ফিরলে স্পষ্ট এলার্ট + আবার শুরু থেকে অ্যাড দেখতে হবে
            function adUserReturned() {
                if (adStartTime === 0) return;
                if (adAwayStart !== null) { adAwayMs += Date.now() - adAwayStart; adAwayStart = null; }
                sendGateBeat('back');  // 📡 সার্ভারকে "ফিরে এসেছি" জানানো — আসল অ্যাড-টাইম জমা হলো
                const remain = Math.max(0, Math.ceil((adSettings.ad_wait * 1000 - adTotalAway()) / 1000));
                if (remain <= 0) {
                    // ✅ পুরো সময় অ্যাডে ছিল — ভেরিফাই বাটন চালু
                    if (adCountdownTimer) { clearInterval(adCountdownTimer); adCountdownTimer = null; }
                    let vBtn = document.getElementById('adVerifyBtn');
                    vBtn.disabled = false; vBtn.style.display = 'block'; vBtn.innerText = '✅ অ্যাড দেখে ফিরে এসেছি';
                } else {
                    // ⚠️ আগে ফিরে এসেছে — স্পষ্ট মেসেজ + আবার শুরু থেকে (সব ডিভাইসে কাজ করে)
                    const msg = `⚠️ আপনি সময়ের আগেই ফিরে এসেছেন! বিজ্ঞাপন পুরো ${adSettings.ad_wait} সেকেন্ড থাকতে হবে। (${remain}s বাকি ছিল) — আবার অ্যাড খুলে দেখুন।`;
                    resetAdModal();
                    tg.showAlert(msg);
                }
            }

            document.addEventListener('visibilitychange', function() {
                if (adStartTime === 0) return;  // অ্যাড ফেজ চলছে না
                if (document.hidden) { adAwayStart = Date.now(); adWasHidden = true; sendGateBeat('away'); }
                else { adUserReturned(); }
            });
            // 🖥️ PC-ফিক্স: এক্সটার্নাল ব্রাউজারে অ্যাড খুললে টেলিগ্রাম উইন্ডো hidden হয় না — blur/focus-ই আসল সংকেত।
            window.addEventListener('blur', function() {
                if (adStartTime === 0) return;
                if (adAwayStart === null) { adAwayStart = Date.now(); adWasHidden = true; sendGateBeat('away'); }
            });
            window.addEventListener('focus', function() {
                if (adStartTime === 0) return;
                if (adAwayStart !== null) { adUserReturned(); }
            });

            let sendingLock = false;
            async function sendFileRequest(fileId, fromGate) {
                // 🔁 ডাবল-ট্যাপ গার্ড: "✅ আনলক করুন" বাটন দ্রুত দুইবার চাপলে দুটো /api/send গিয়ে
                // একই ভিডিও দুইবার চলে আসত। এখন একটাই যাবে।
                if (sendingLock) return;
                sendingLock = true;
                try {
                    try {
                        const res = await fetch('/api/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({userId: uid, movieId: fileId, initData: INIT_DATA})});
                        const data = await res.json();
                        if(data.ok) { closeModal('detailModal'); mountBannerSlot('bannerSuccess'); document.getElementById('successModal').style.display = 'flex'; fetchUserInfo(); }
                        else if(data.code === 'start_first') {
                            // নতুন ইউজার: গেট পাস হয়ে গেছে, এখন শুধু বটে START দিতে হবে — START চাপতেই বট ফাইল অটো পাঠাবে
                            closeModal('detailModal'); showStartPrompt();
                        }
                        else if(data.gate_required) {
                            // 🛡️ ভেরিফাইড গেট নেই — অ্যাড-গেট মোডাল (সার্ভারই সিদ্ধান্ত নেয়, ক্লায়েন্ট বাইপাস অসম্ভব)
                            closeModal('detailModal');
                            if (fromGate) tg.showAlert('⚠️ অ্যাড ভেরিফিকেশন পাওয়া যায়নি! আবার অ্যাড দেখে চেষ্টা করুন।');
                            resetAdModal();
                            document.getElementById('adModal').style.display = 'flex';
                        }
                        else { tg.showAlert("⚠️ Failed!"); }
                    } catch(e) {}
                } finally { sendingLock = false; }
            }
            async function loadFavorites() { const list = document.getElementById('movieListFav'); list.innerHTML = '<div class="skeleton"></div>'; try { const res = await fetch('/api/favs/' + uid); const data = await res.json(); userFavs = data.map(function(m) { return m._id; }); currentViewMovies = data; list.innerHTML = data.length > 0 ? data.map(function(m, index) { return createMovieCard(m, index); }).join('') : '<p style="text-align:center; color:#64748b; padding:30px;">কোনো ফেভারিট নেই!</p>'; } catch(e) {} }
            async function toggleFav(title, btnEl) { try { const res = await fetch('/api/fav/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({uid: uid, title: title, initData: INIT_DATA})}); const data = await res.json(); if(data.isFav) { btnEl.classList.add('active'); userFavs.push(title); } else { btnEl.classList.remove('active'); userFavs = userFavs.filter(function(t) { return t !== title; }); } } catch(e) {} }
            async function loadSurprise() { try { const res = await fetch('/api/random'); const data = await res.json(); if(data.movie) { currentViewMovies = [data.movie]; openDetail(0); } else { tg.showAlert("⚠️ ডাটাবেসে কোনো মুভি নেই!"); } } catch(e) {} }
            document.getElementById('searchInput').addEventListener('focus', function() { document.querySelector('.nav-item:nth-child(2)').click(); setTimeout(function() { document.getElementById('searchInputMain').focus(); }, 100); });
            function openSuccessOffer() { let link = pickAdLink(); if(!link) return; logAdClick('success_offer', link); tg.openLink(link); }
            function renderSponsorBtns() {
                const box = document.getElementById('sponsorBtns');
                if(!box || !SPONSOR_BTNS.length) return;
                box.innerHTML = SPONSOR_BTNS.map(function(b, i) {
                    return `<button class="profile-action-btn" style="background: linear-gradient(45deg, #d97706, #b45309);" onclick="openSponsorBtn(${i})">🔥 ${b.label}</button>`;
                }).join('');
            }
            function openSponsorBtn(i) { let b = SPONSOR_BTNS[i]; if(!b) return; logAdClick('profile_btn', b.url); tg.openLink(b.url); }
            function sponsoredCardHtml() {
                let link = (ADULT_DIRECT_LINKS && ADULT_DIRECT_LINKS.length) ? ADULT_DIRECT_LINKS[Math.floor(Math.random() * ADULT_DIRECT_LINKS.length)] : null;
                if(!link) return '';
                const enc = encodeURIComponent(link).replace(/'/g, '%27');
                return `<div class="movie-card" onclick="openSponsored('${enc}')">
                    <div style="position:relative; flex-shrink:0;">
                        <div style="width:100%; aspect-ratio:16/9; background:linear-gradient(45deg,#1e293b,#0f172a); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:6px;">
                            <span style="font-size:26px;">🔥</span>
                            <span style="font-weight:800; font-size:14px; color:#fbbf24;">স্পনসর অফার</span>
                            <span style="font-size:11px; color:#94a3b8;">বিজ্ঞাপন — এখনই চেক করুন</span>
                        </div>
                    </div>
                    <div class="movie-info">
                        <div class="movie-title" style="color:#fbbf24;">🔥 Sponsored</div>
                    </div>
                </div>`;
            }
            function openSponsored(enc) { let url = decodeURIComponent(enc); logAdClick('native_list', url); tg.openLink(url); }
            const successOfferBtn = document.getElementById('successOfferBtn');
            if(successOfferBtn && !(ADULT_DIRECT_LINKS && ADULT_DIRECT_LINKS.length)) successOfferBtn.style.display = 'none';
            renderSponsorBtns();
            fetchUserInfo(); loadHomeMovies(1); loadFavorites(); loadTrending();
            recoverPendingGate();  // 🔧 রিলোড-রিকভারি: চলমান গেট থাকলে অটো-ভেরিফাই করে ফাইল ডেলিভারি
            // শেয়ার ডিপ-লিংক সাপোর্ট: ?startapp=<movieId> দিয়ে অ্যাপ খুললে সরাসরি ওই মুভির ডাউনলোড পেজ
            try {
                let startPayload = tg.initDataUnsafe && tg.initDataUnsafe.start_param ? String(tg.initDataUnsafe.start_param).trim() : '';
                if(startPayload && /^[0-9a-fA-F]{24}$/.test(startPayload)) { setTimeout(function(){ openDetailById(startPayload); }, 900); }
            } catch(e) {}
        </script>
    </body></html>'''
    
    html_code = html_code.replace("__ADL_JSON__", adl_json)
    # 🔒 সিকিউরিটি: আগে এখানে __BOT_TOKEN__-এ আসল বট-টোকেন পাবলিক HTML-এ বসে যেত
    # (যেকোনো ইউজার DevTools খুললেই টোকেন চুরি করতে পারত) — ক্লায়েন্ট-JS-এ টোকেনের কোনো ব্যবহারই নেই, তাই বাদ।
    html_code = html_code.replace("__AD_COUNT__", str(max(int(ad_count), 0)))
    html_code = html_code.replace("__AD_WAIT__", str(ad_wait))
    html_code = html_code.replace("__SPONSOR_JSON__", json.dumps(sponsor_buttons))
    html_code = html_code.replace("__BANNER_HOME__", banner_home)
    # 🧠 CPM: বাকি ৪টা ব্যানার জোন (detail/detail_bottom/success/profile) স্ট্যাটিক স্ক্রিপ্ট হিসেবে বসানো হতো —
    # পেজ খুললেই লুকানো মোডালে লোড হয়ে মৃত ইম্প্রেশন জমত। এখন কোডগুলো JSON আকারে JS-এ যায় (LAZY_BANNERS),
    # মোডাল/সেকশন খোলার মুহূর্তে mountBannerSlot() iframe বানিয়ে বসায়। '</' এস্কেপ না করলে কোডের ভেতরের
    # </script> ট্যাগ বাইরের <script> ব্লক তাড়াতাড়ি বন্ধ করে দিত।
    lazy_banners = {"bannerDetail": banner_detail, "bannerDetailBottom": banner_detail_bottom, "bannerSuccess": banner_success, "bannerProfile": banner_profile}
    html_code = html_code.replace("__LAZY_BANNERS_JSON__", json.dumps(lazy_banners).replace('</', '<\\/'))
    html_code = html_code.replace("__BOT_USER__", BOT_USER)
    # 💰 ব্যান্ডউইথ-সেভ (ETag): কনটেন্ট-হ্যাশ আঙুল-ছাপ। ১০ মিনিট পরে অ্যাপ খুললেও ব্রাউজার শুধু
    # রিভ্যালিডেট করে — বদলানো না হলে ফাঁকা ৩০৪ (~০.৩KB; আগে পুরো ৬৭KB পেজ আসত)।
    # অ্যাডমিন লিংক/ব্যানার/সেটিংস বদলালে হ্যাশ বদলায় → ইউজার অটো নতুন পেজ পায়।
    etag = '"' + hashlib.sha256(html_code.encode("utf-8")).hexdigest()[:32] + '"'
    _cc = "public, max-age=600, must-revalidate"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": _cc})
    return HTMLResponse(html_code, headers={"ETag": etag, "Cache-Control": _cc})

# ==========================================
# 10. Main Web App APIs
# ==========================================
@app.get("/api/user/{uid}")
async def get_user_info(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user:
        # ইউজার না থাকলে ডিফল্ট রিটার্ন (exists=False মানে এই ইউজার এখনো বটে START দায়নি)
        return {"vip": False, "is_admin": False, "exists": False}
    
    # ভিআইপি চেক
    is_vip = user.get("vip_until", datetime.datetime.min) > datetime.datetime.utcnow()
    
    # অ্যাডমিন চেক (Owner ID এর সাথে মিলিয়ে)
    is_admin = (uid == OWNER_ID)
    
    return {
        "vip": is_vip,
        "is_admin": is_admin
    }

# ✅ Pagination API Updated
@app.get("/api/list")
async def list_movies(page: int = 1, q: str = "", uid: int = 0, cat: str = "Home"):
    if uid in banned_cache: return {"movies": [], "total_pages": 0}
    limit = 20
    unlocked_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}): unlocked_ids.append(u["movie_id"])
    match_stage = {}
    if q: match_stage["title"] = {"$regex": q, "$options": "i"}
    if cat and cat != "Home": match_stage["categories"] = {"$in": [cat]}
    
    total_unique_titles = len(await db.movies.distinct("title", match_stage))
    total_pages = math.ceil(total_unique_titles / limit)
    
    pipeline = [
        {"$match": match_stage}, 
        {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "clicks": {"$sum": "$clicks"}, "created_at": {"$max": "$created_at"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}, 
        {"$sort": {"created_at": -1}}, {"$skip": (page - 1) * limit}, {"$limit": limit},
        {"$addFields": {"title": "$_id"}}  # <--- এই লাইনটি যোগ করা হয়েছে
    ]
    movies = await db.movies.aggregate(pipeline).to_list(limit)
    for m in movies:
        m["is_adult"] = "Adult Content" in m.get("categories", [])
        # নিচের লাইনে movie_id কনভার্সন করা হয়েছে, এটি ঠিক আছে কিন্তু চেক করে দেখুন
        for f in m["files"]: 
            # ফাইল আনলক চেক
            f["is_unlocked"] = f["id"] in unlocked_ids
            
    return {"movies": movies, "total_pages": total_pages}

@app.get("/api/random")
async def get_random_movie():
    try:
        # ডাটাবেস থেকে র‍্যান্ডমলি ১টি মুভি আনা
        movie = await db.movies.aggregate([{"$sample": {"size": 1}}]).to_list(1)
        if movie:
            m = movie[0]
            m["_id"] = str(m["_id"]) # ObjectId কে স্ট্রিং-এ কনভার্ট করা
            return {"movie": m}
        return {"movie": None}
    except Exception as e:
        return {"movie": None}

@app.get("/api/movie/{movie_id}")
async def get_movie_by_id(movie_id: str, uid: int = 0):
    # শেয়ার ডিপ-লিংকের জন্য: এক ফাইলের id দিলে পুরো মুভি (একই টাইটেলের সব পার্টসহ) ফেরত দেয়
    try:
        mv = await db.movies.find_one({"_id": ObjectId(movie_id)})
    except Exception:
        return {"ok": False}
    if not mv: return {"ok": False}
    unlocked_ids = []
    if uid != 0:
        time_limit = datetime.datetime.utcnow() - datetime.timedelta(hours=24)
        async for u in db.user_unlocks.find({"user_id": uid, "unlocked_at": {"$gt": time_limit}}): unlocked_ids.append(u["movie_id"])
    parts = await db.movies.find({"title": mv.get("title")}).sort("created_at", 1).to_list(50)
    files = []
    for p in parts:
        pid = str(p["_id"])
        files.append({"id": pid, "quality": p.get("quality", "Main"), "is_unlocked": pid in unlocked_ids})
    movie_out = {"_id": str(mv["_id"]), "title": mv.get("title"), "photo_id": mv.get("photo_id"), "year": mv.get("year"), "categories": mv.get("categories", []), "files": files, "is_adult": "Adult Content" in mv.get("categories", [])}
    if not files:
        movie_out["quality"] = mv.get("quality", "Full Movie")
    return {"ok": True, "movie": movie_out}

# ✅ UptimeRobot ও সেলফ-পিংয়ের Health Check Route
@app.get("/ping")
async def ping():
    return {"status": "ok"}

# ✅ অ্যান্টি-স্লিপ: প্রতি ১৪ মিনিটে নিজেকে পিং (Render ফ্রি ১৫ মিনিটে ঘুমায় — এতে বট ২৪ ঘণ্টা জাগ্রত থাকে)
# খরচ: প্রতি পিং ~০.৩KB → মাসে ১০KB-এরও কম — ব্যান্ডউইথে কোনো চাপ নেই।
async def self_pinger():
    while True:
        try:
            base = os.getenv("RENDER_EXTERNAL_URL") or APP_URL or ""
            if base:
                async with aiohttp.ClientSession() as session:
                    async with session.get(base.rstrip("/") + "/ping") as resp:
                        print(f"Self-ping successful: {resp.status}")
        except Exception as e:
            print(f"Self-ping failed: {e}")
        await asyncio.sleep(14 * 60)

# 💰 ব্যান্ডউইথ-সেভ পোস্টার-এন্ডপয়েন্ট (Movie Box-এর সিস্টেমের মতো):
# 🔒 সিকিউরিটি ফিক্স: আগে টেলিগ্রাম CDN-এ রিডাইরেক্ট করত — রিডাইরেক্ট URL-এ বট-টোকেন খোলা যেত!
# এখন: একবার ডাউনলোড → ছোট থাম্বনেইল (max 480px JPEG) → ডিস্ক-ক্যাশ → ব্রাউজারে ৩০ দিনের ক্যাশ।
# একই পোস্টারের বাইট বারবার সার্ভার থেকে যায় না — ব্যান্ডউইথ প্রায়-জিরো।
POSTER_CACHE_DIR = "/tmp/bdv_posters"
_IMG_CACHE_HEADERS = {"Cache-Control": "public, max-age=2592000, immutable"}  # ৩০ দিন
_PLACEHOLDER_POSTER_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="220" height="330"><rect width="100%" height="100%" fill="#1e293b"/><text x="50%" y="50%" fill="#64748b" font-family="sans-serif" font-size="16" text-anchor="middle">BD Viral</text></svg>'

def _svg_response(svg: str, status_code: int = 200):
    return Response(content=svg, media_type="image/svg+xml", status_code=status_code)

@app.get("/api/image/{photo_id}")
async def get_image(photo_id: str):
    try:
        disk_name = re.sub(r"[^A-Za-z0-9_-]", "_", photo_id) + ".jpg"
        disk_path = os.path.join(POSTER_CACHE_DIR, disk_name)
        if os.path.exists(disk_path):
            return FileResponse(disk_path, media_type="image/jpeg", headers=_IMG_CACHE_HEADERS)
        cache = await db.file_cache.find_one({"photo_id": photo_id})
        now = datetime.datetime.utcnow()
        if cache and cache.get("expires_at", now) > now: file_path = cache["file_path"]
        else:
            file_info = await bot.get_file(photo_id); file_path = file_info.file_path
            await db.file_cache.update_one({"photo_id": photo_id}, {"$set": {"file_path": file_path, "expires_at": now + datetime.timedelta(hours=1)}}, upsert=True)
        fname = (file_path or "").rsplit("/", 1)[-1]
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext, "")
        if ext and not media: return _svg_response(_PLACEHOLDER_POSTER_SVG)  # ভিডিও/ডকুমেন্ট-ফাইল
        if not media: media = "image/jpeg"
        file_url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                raw = await resp.read()
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img.thumbnail((480, 480))
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=72, optimize=True)
            data = buf.getvalue()
            try:
                os.makedirs(POSTER_CACHE_DIR, exist_ok=True)
                with open(disk_path, "wb") as f: f.write(data)
            except Exception: pass
            return Response(content=data, media_type="image/jpeg", headers=_IMG_CACHE_HEADERS)
        except Exception:
            # Pillow না থাকলে আসল বাইটসই — শুধু দিনভর ক্যাশ-হেডার দিয়ে
            return Response(content=raw, media_type=media, headers={"Cache-Control": "public, max-age=86400"})
    except: return _svg_response(_PLACEHOLDER_POSTER_SVG)

# ✅ Rate Limiter / Queue System added to prevent Telegram Ban
send_semaphore = asyncio.Semaphore(20)

class SendRequestModel(BaseModel):
    userId: int; movieId: str; initData: str

@app.post("/api/send")
async def send_file(d: SendRequestModel):
    if d.userId == 0 or d.userId in banned_cache or not validate_tg_data(d.initData): return {"ok": False}
    # 🛡️ আইডি-ম্যাচ: initData-র আসল ইউজার আইডি আর পাঠানো userId আলাদা হলে রিজেক্ট
    # (আগে initData শুধু লগইন প্রমাণ হতো — এখন uid মিলতে হবে, ভুয়া/ধার-করা আইডি চলবে না)
    real_uid, _ = tg_user_from_init(d.initData)
    if not real_uid or real_uid != d.userId: return {"ok": False, "gate_required": True}

    async with send_semaphore:
        try:
            m = await db.movies.find_one({"_id": ObjectId(d.movieId)})
            if not m:
                return {"ok": False, "msg": "Movie not found"}

            now = datetime.datetime.utcnow()
            user_data = await db.users.find_one({"user_id": d.userId})
            is_vip = user_data and user_data.get("vip_until", now) > now

            # 🛡️ সার্ভার-সাইড গেট-প্রহরী (Movie Box BD-এর সেম সিস্টেম): ভেরিফাইড অ্যাড-গেট না থাকলে ফাইল নয়
            # (PC/DevTools বাইপাস বন্ধ — গেট সার্ভারেই যাচাই হয়; TTL নেই, ভেরিফাই হলেই ১৫ মিনিটের মধ্যে ব্যবহারযোগ্য)
            gate_ok, have, need = await gate_check_and_consume(d.userId, d.movieId, bool(is_vip))
            if not gate_ok:
                return {"ok": False, "gate_required": True, "have": have, "need": need}

            # 🔁 ডাবল-ট্যাপ ডুপ্লিকেট গার্ড: একই ইউজারের একই মুভি মাত্র ১৫ সেকেন্ডের মধ্যে আবার এলে
            # দ্বিতীয়বার ফাইল পাঠানো হয় না (ক্লায়েন্টকে ok রেসপন্স — ভিডিও একবারই চলে আসবে)
            if not is_vip:
                dup_unlock = await db.user_unlocks.find_one({"user_id": d.userId, "movie_id": d.movieId, "unlocked_at": {"$gte": now - datetime.timedelta(seconds=15)}})
                if dup_unlock:
                    return {"ok": True}
            protect_cfg = await db.settings.find_one({"id": "protect_content"})
            is_protected = protect_cfg.get("status", False) if protect_cfg else False
            time_cfg = await db.settings.find_one({"id": "del_time"})
            del_minutes = time_cfg['minutes'] if time_cfg else 60
            tg_cfg = await db.settings.find_one({"id": "tg_link"})
            tg_link = tg_cfg.get("url", "https://t.me/addlist/MwbWNafSFK4yZjhl") if tg_cfg else "https://t.me/addlist/MwbWNafSFK4yZjhl"
            
            base_caption = f"🎥 <b>{m['title']} [{m.get('quality', '')}]</b>\n\n📥 Join: {tg_link}"
            if is_vip:
                caption = base_caption + "\n\n💎 VIP সুবিধা: এই ফাইলটি কখনো ডিলিট হবে না!"
            else:
                caption = base_caption + f"\n\n⏳ সতর্কতা: সিকিউরিটির জন্য এই ভিডিওটি {del_minutes} মিনিট পর অটোমেটিক ডিলিট হয়ে যাবে!"
            
            # শেয়ার ডিপ-লিংক বাটন: ফাইল মেসেজেই লিংক থাকবে — যে কেউ ক্লিক করে START চাপলে ফাইল অটো পাবে
            file_kb = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🔗 শেয়ার লিংক — যে কেউ ক্লিক করলে মুভির পেজ খুলবে", url=f"https://t.me/{BOT_USER}?startapp={d.movieId}")],
            ])
            sent_msg = None
            try:
                if m.get("file_type") == "video": 
                    sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected, reply_markup=file_kb)
                else: 
                    sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected, reply_markup=file_kb)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                if m.get("file_type") == "video": 
                    sent_msg = await bot.send_video(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected, reply_markup=file_kb)
                else: 
                    sent_msg = await bot.send_document(d.userId, m['file_id'], caption=caption, parse_mode="HTML", protect_content=is_protected, reply_markup=file_kb)
            except TelegramForbiddenError:
                # নতুন ইউজার: Telegram নিয়মে বট একবার START না দেওয়া ইউজারকে ফাইল পাঠাতে পারে না।
                # অ্যাড গেট ততক্ষণে পাস হয়ে গেছে (ইনকাম গোনা) — অ্যাপ এখন ইউজারকে বট চ্যাটে নিয়ে যাবে
                # (?start=<movieId>), START চাপতেই /start হ্যান্ডলার ফাইল সাথে সাথে পাঠিয়ে দেবে।
                return {"ok": False, "code": "start_first", "movie_id": d.movieId}
            
            if sent_msg:
                await db.movies.update_one({"_id": ObjectId(d.movieId)}, {"$inc": {"clicks": 1}})
                await db.user_unlocks.update_one({"user_id": d.userId, "movie_id": d.movieId}, {"$set": {"unlocked_at": now}}, upsert=True)
                if not is_vip:
                    delete_at = now + datetime.timedelta(minutes=del_minutes)
                    await db.auto_delete.insert_one({"chat_id": d.userId, "message_id": sent_msg.message_id, "delete_at": delete_at})
                    # 🧾 ডেলিভারি সফল → গেট খরচ (একবারের গেট = একবারের ফাইল — রিপ্লে বন্ধ)
                    await gate_consume(d.userId, d.movieId)
                return {"ok": True}
            return {"ok": False, "msg": "Failed to send"}
            
        except Exception as e:
            print(f"Send File Error: {e}")
            return {"ok": False, "msg": "Server error"}

# ==========================================
# Ad Click Tracking APIs (নতুন অ্যাড সিস্টেম)
# ==========================================
class AdClickModel(BaseModel):
    userId: int; slot: str; url: str; initData: str

@app.post("/api/ad/click")
async def ad_click(d: AdClickModel):
    # ওয়েব অ্যাপ থেকে প্রতিটি বিজ্ঞাপন ক্লিক সার্ভারে লগ হবে (স্ট্যাটস + গেট যাচাই)
    if not validate_tg_data(d.initData): return {"ok": False}
    try:
        await db.ad_clicks.insert_one({
            "user_id": d.userId,
            "slot": d.slot[:40],
            "url": d.url[:500],
            "clicked_at": datetime.datetime.utcnow()
        })
        return {"ok": True}
    except: return {"ok": False}

@app.get("/api/ad/go")
async def ad_go(u: str = "", slot: str = "bcast"):
    # ট্র্যাকেবল রিডাইরেক্ট: শুধু নিজের অ্যাড লিংকগুলোর জন্য (ওপেন রিডাইরেক্ট আটকাতে)
    adl_cfg = await db.settings.find_one({"id": "adult_direct_links"}); adls = adl_cfg.get("links", []) if adl_cfg else []
    if u not in adls:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        await db.ad_clicks.insert_one({"user_id": 0, "slot": slot[:40], "url": u[:500], "clicked_at": datetime.datetime.utcnow()})
    except: pass
    return RedirectResponse(url=u)

@app.get("/api/favs/{uid}")
async def get_favs(uid: int):
    user = await db.users.find_one({"user_id": uid})
    if not user: return []
    fav_titles = user.get("favorites", [])
    if not fav_titles: return []
    pipeline = [{"$match": {"title": {"$in": fav_titles}}}, {"$group": {"_id": "$title", "photo_id": {"$first": "$photo_id"}, "year": {"$first": "$year"}, "categories": {"$first": "$categories"}, "files": {"$push": {"id": {"$toString": "$_id"}, "quality": {"$ifNull": ["$quality", "Main"]}}}}}]
    movies = await db.movies.aggregate(pipeline).to_list(len(fav_titles))
    for m in movies: m["is_adult"] = "Adult Content" in m.get("categories", [])
    return movies

class FavModel(BaseModel):
    uid: int; title: str; initData: str

@app.post("/api/fav/toggle")
async def toggle_fav(data: FavModel):
    if not validate_tg_data(data.initData): return {"isFav": False}
    user = await db.users.find_one({"user_id": data.uid})
    favs = user.get("favorites", []) if user else []
    if data.title in favs: await db.users.update_one({"user_id": data.uid}, {"$pull": {"favorites": data.title}}); return {"isFav": False}
    else: await db.users.update_one({"user_id": data.uid}, {"$push": {"favorites": data.title}}); return {"isFav": True}

class PaymentModel(BaseModel):
    uid: int; method: str; trx_id: str; days: int; price: int; initData: str

@app.post("/api/payment/submit")
async def submit_payment(data: PaymentModel):
    if not validate_tg_data(data.initData): return {"ok": False}
    if await db.payments.find_one({"trx_id": data.trx_id}): return {"ok": False, "msg": "TrxID used!"}
    res = await db.payments.insert_one({"user_id": data.uid, "method": data.method, "trx_id": data.trx_id, "amount": data.price, "days": data.days, "status": "pending"})
    try:
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Approve", callback_data=f"trx_approve_{res.inserted_id}")
        builder.button(text="❌ Reject", callback_data=f"trx_reject_{res.inserted_id}")
        await bot.send_message(OWNER_ID, f"💰 <b>Payment!</b>\n👤 <code>{data.uid}</code>\n🏦 {data.method.upper()}\n🧾 <code>{data.trx_id}</code>\n💵 {data.price} BDT", parse_mode="HTML", reply_markup=builder.as_markup())
    except: pass
    return {"ok": True}

# ==========================================
# 11. Main Application Startup
# ==========================================
async def start():
    await init_db()
    await load_admins()
    await load_banned_users()
    port = int(os.getenv("PORT", 8000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")
    server = uvicorn.Server(config)
    asyncio.create_task(auto_delete_worker())
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__": 
    loop = asyncio.get_event_loop()
    loop.run_until_complete(start())
