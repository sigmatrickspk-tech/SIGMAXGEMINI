"""
Telegram Bot — Crypto Payments + PixVerify Google One Verification
Ready for deployment on Pella.app
"""
import os, sys, json, time, uuid, logging, asyncio, sqlite3
from pathlib import Path
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, CallbackQuery, Message
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ─── ENVIRONMENT VARIABLES ───
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8448367676:AAFSFYQAwb6vLcSj1GkDqxrYwx0vsayfQck")
CRYPTO_PAY_TOKEN = os.environ.get("CRYPTO_PAY_TOKEN", "607964:AA802op9FGK4cgT6ucCEPaJtG7tLb1q1OTy")
PIXVERIFY_API_KEY = os.environ.get("PIXVERIFY_API_KEY", "pk_live_ed6979e455bdaacdcb786978901670d745581fa14a809e8f")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8278238550"))
PIXVERIFY_BASE = os.environ.get("PIXVERIFY_BASE", "https://pixverify.shop/api/v1")
CRYPTO_PAY_BASE = os.environ.get("CRYPTO_PAY_BASE", "https://pay.crypt.bot/api")
SUPPORTED_ASSETS = os.environ.get("SUPPORTED_ASSETS", "USDT,TON,BTC,ETH,BNB,TRX").split(",")
PRICE_VIP = float(os.environ.get("PRICE_VIP", "3.50"))
PRICE_NORMAL = float(os.environ.get("PRICE_NORMAL", "2.50"))

# ─── LOGGING ───
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("CryptoBot")

# ─── DATABASE ───
DB_DIR = Path("db")
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "bot_database.sqlite"

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            coins REAL DEFAULT 0.0,
            is_banned INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            invoice_id TEXT UNIQUE,
            asset TEXT,
            amount REAL DEFAULT 0,
            amount_usd REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            payload TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            confirmed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            pix_gen_id INTEGER,
            vtype TEXT NOT NULL,
            email TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            result_url TEXT DEFAULT '',
            credits_used REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS shop_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            order_id INTEGER DEFAULT 0,
            category_name TEXT DEFAULT '',
            quantity INTEGER DEFAULT 1,
            total_cost REAL DEFAULT 0,
            credentials TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS generated_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            result_url TEXT DEFAULT '',
            vtype TEXT DEFAULT '',
            generated_at TEXT DEFAULT (datetime('now'))
        );
    """)
    c.execute("INSERT OR IGNORE INTO users (telegram_id, username, is_admin) VALUES (?, ?, 1)", (ADMIN_ID, "admin"))
    conn.commit()
    conn.close()

def db_exec(sql, params=()):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute(sql, params)
    conn.commit()
    conn.close()

def db_fetch(sql, params=()):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute(sql, params)
    rows = c.fetchall()
    conn.close()
    return rows

def db_fetch_one(sql, params=()):
    rows = db_fetch(sql, params)
    return rows[0] if rows else None

def get_user(tid):
    return db_fetch_one("SELECT * FROM users WHERE telegram_id = ?", (tid,))

def create_user(tid, username="", first_name="", last_name=""):
    db_exec("INSERT OR IGNORE INTO users (telegram_id, username, first_name, last_name) VALUES (?,?,?,?)",
            (tid, username, first_name, last_name))

def add_coins(tid, amount):
    db_exec("UPDATE users SET coins = coins + ? WHERE telegram_id = ?", (amount, tid))
    r = db_fetch_one("SELECT coins FROM users WHERE telegram_id = ?", (tid,))
    return r[0] if r else 0

def deduct_coins(tid, amount):
    r = db_fetch_one("SELECT coins FROM users WHERE telegram_id = ?", (tid,))
    if not r or r[0] < amount:
        return False
    db_exec("UPDATE users SET coins = coins - ? WHERE telegram_id = ?", (amount, tid))
    return True

def set_ban(tid, banned):
    db_exec("UPDATE users SET is_banned = ? WHERE telegram_id = ?", (1 if banned else 0, tid))

def get_all_users():
    return db_fetch("SELECT telegram_id, username, first_name, coins, is_banned, is_admin, created_at FROM users ORDER BY created_at DESC")

def save_payment(tid, invoice_id, asset, amount, amount_usd, payload):
    db_exec("INSERT OR IGNORE INTO payments (telegram_id, invoice_id, asset, amount, amount_usd, payload) VALUES (?,?,?,?,?,?)",
            (tid, invoice_id, asset, amount, amount_usd, payload))

def confirm_payment(invoice_id):
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("UPDATE payments SET status='paid', confirmed_at=datetime('now') WHERE invoice_id=? AND status='pending'", (invoice_id,))
    affected = c.rowcount
    if affected:
        c.execute("SELECT telegram_id, amount_usd FROM payments WHERE invoice_id=?", (invoice_id,))
        row = c.fetchone()
        if row:
            add_coins(row[0], row[1])
    conn.commit()
    conn.close()
    return affected

def save_verification(tid, pix_gen_id, vtype, email):
    db_exec("INSERT INTO verifications (telegram_id, pix_gen_id, vtype, email) VALUES (?,?,?,?)",
            (tid, pix_gen_id, vtype, email))

def update_verification(pix_gen_id, status, result_url=None, credits_used=None):
    if result_url and credits_used:
        db_exec("UPDATE verifications SET status=?, result_url=?, credits_used=? WHERE pix_gen_id=?",
                (status, result_url, credits_used, pix_gen_id))
    else:
        db_exec("UPDATE verifications SET status=? WHERE pix_gen_id=?", (status, pix_gen_id))

def save_shop_purchase(tid, order_id, cat_name, qty, cost, creds):
    db_exec("INSERT INTO shop_purchases (telegram_id, order_id, category_name, quantity, total_cost, credentials) VALUES (?,?,?,?,?,?)",
            (tid, order_id, cat_name, qty, cost, creds))

def save_generated_link(tid, result_url, vtype):
    db_exec("INSERT INTO generated_links (telegram_id, result_url, vtype) VALUES (?,?,?)",
            (tid, result_url, vtype))

# ─── CRYPTO PAY API ───
async def crypto_pay_request(method, http_method="GET", data=None):
    url = f"{CRYPTO_PAY_BASE}/{method}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        if http_method == "GET":
            async with s.get(url, headers=headers, params=data) as r:
                return await r.json()
        else:
            async with s.post(url, headers=headers, json=data) as r:
                return await r.json()

async def create_crypto_invoice(tid, amount_usd, asset="USDT", description=""):
    payload = json.dumps({"user_id": tid, "item": description, "ts": int(time.time()), "uid": str(uuid.uuid4())[:8]})
    return await crypto_pay_request("createInvoice", "POST", {
        "asset": asset,
        "amount": str(round(amount_usd, 2)),
        "description": description or f"Deposit ${amount_usd:.2f}",
        "payload": payload,
        "allow_anonymous": False,
        "expires_in": 1800,
    })

async def get_invoice(invoice_id):
    return await crypto_pay_request("getInvoices", data={"invoice_ids": str(invoice_id)})

# ─── PIXVERIFY API ───
async def pix_request(method, http_method="GET", data=None):
    url = f"{PIXVERIFY_BASE}/{method}"
    headers = {"X-API-Key": PIXVERIFY_API_KEY, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        if http_method == "GET":
            async with s.get(url, headers=headers) as r:
                return await r.json()
        else:
            async with s.post(url, headers=headers, json=data) as r:
                return await r.json()

async def pix_balance():
    return await pix_request("profile")

async def pix_start_verification(vtype, email, password, totp):
    return await pix_request("verifications/generate", "POST", {
        "type": vtype, "email": email, "password": password, "totp_secret": totp
    })

async def pix_poll(gen_id):
    return await pix_request(f"verifications/{gen_id}")

async def pix_shop_categories():
    return await pix_request("shop/categories")

async def pix_buy(category_id, quantity=1):
    return await pix_request("shop/buy", "POST", {"category_id": category_id, "quantity": quantity})

# ─── BOT SETUP ───
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ─── FSM STATES ───
class UserVerify(StatesGroup):
    choosing_type = State()
    entering_email = State()
    entering_password = State()
    entering_totp = State()

class AdminVerify(StatesGroup):
    entering_email = State()
    entering_password = State()
    entering_totp = State()

class AdminStates(StatesGroup):
    waiting_ban = State()
    waiting_unban = State()
    waiting_give_user = State()
    waiting_give_amount = State()
    waiting_gen_user = State()

class ShopBuy(StatesGroup):
    waiting_qty = State()

# ─── KEYBOARDS ───
def main_kb(is_admin=False):
    kb = [
        [KeyboardButton(text="🛒 Buy Verification")],
        [KeyboardButton(text="🛍️ Shop Categories")],
        [KeyboardButton(text="💰 Balance / Deposit")],
        [KeyboardButton(text="📜 My Orders")],
    ]
    if is_admin:
        kb.append([KeyboardButton(text="⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👥 All Users"), KeyboardButton(text="⛔ Ban User")],
        [KeyboardButton(text="✅ Unban User"), KeyboardButton(text="💰 Add Coins")],
        [KeyboardButton(text="💳 Pending Payments"), KeyboardButton(text="📊 PixVerify Balance")],
        [KeyboardButton(text="📋 Gen VIP Link"), KeyboardButton(text="📋 Gen Normal Link")],
        [KeyboardButton(text="◀️ Back to Menu")],
    ], resize_keyboard=True)

def verify_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"VIP — ${PRICE_VIP:.2f}", callback_data="vtype_vip")],
        [InlineKeyboardButton(text=f"Normal — ${PRICE_NORMAL:.2f}", callback_data="vtype_normal")],
        [InlineKeyboardButton(text="◀️ Back", callback_data="back_menu")],
    ])

def asset_kb():
    kb = []
    row = []
    for i, a in enumerate(SUPPORTED_ASSETS):
        row.append(InlineKeyboardButton(text=a, callback_data=f"pay_{a}"))
        if (i+1) % 3 == 0:
            kb.append(row)
            row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="◀️ Back", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_btn():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Back", callback_data="back_menu")]])

# ─── HANDLERS ───

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    u = msg.from_user
    create_user(u.id, u.username or "", u.first_name or "", u.last_name or "")
    ud = get_user(u.id)
    if ud and ud[5]:
        await msg.answer("❌ You are banned.")
        return
    is_adm = u.id == ADMIN_ID
    await msg.answer(
        f"👋 Welcome {u.first_name}!\n\n"
        f"💎 Get Google One verified accounts.\n"
        f"💰 Deposit crypto via @CryptoBot.\n"
        f"🛒 Use coins for verifications & shop.",
        reply_markup=main_kb(is_adm)
    )

@dp.message(F.text == "🛒 Buy Verification")
async def buy_verify(msg: Message):
    ud = get_user(msg.from_user.id)
    if not ud or ud[5]:
        await msg.answer("❌ Banned.")
        return
    await msg.answer("Choose verification type:", reply_markup=verify_type_kb())

@dp.callback_query(F.data.startswith("vtype_"))
async def vtype_cb(cb: CallbackQuery, state: FSMContext):
    if cb.data == "back_menu":
        await state.clear()
        await cb.message.delete()
        ud = get_user(cb.from_user.id)
        await cb.message.answer("Menu:", reply_markup=main_kb(ud and ud[6]))
        await cb.answer()
        return
    vtype = "vip" if cb.data == "vtype_vip" else "normal"
    price = PRICE_VIP if vtype == "vip" else PRICE_NORMAL
    await state.update_data(vtype=vtype, price=price)
    await state.set_state(UserVerify.entering_email)
    await cb.message.edit_text(
        f"🔑 Selected **{'VIP' if vtype=='vip' else 'Normal'}** (${price:.2f}).\n\n"
        "Enter the **Gmail address**:"
    )
    await cb.answer()

@dp.message(UserVerify.entering_email)
async def user_email(msg: Message, state: FSMContext):
    if "@" not in msg.text or "." not in msg.text:
        await msg.answer("❌ Invalid email. Try again:")
        return
    await state.update_data(email=msg.text.strip())
    await state.set_state(UserVerify.entering_password)
    await msg.answer("📧 Enter the **password**:")

@dp.message(UserVerify.entering_password)
async def user_pass(msg: Message, state: FSMContext):
    if len(msg.text.strip()) < 4:
        await msg.answer("❌ Too short. Enter password:")
        return
    await state.update_data(password=msg.text.strip())
    await state.set_state(UserVerify.entering_totp)
    await msg.answer("🔐 Enter **TOTP secret** (Base32):")

@dp.message(UserVerify.entering_totp)
async def user_totp(msg: Message, state: FSMContext):
    totp = msg.text.strip().upper().replace(" ", "")
    if len(totp) < 16:
        await msg.answer("❌ Too short. Enter 32-char Base32 secret:")
        return
    data = await state.get_data()
    vtype, price = data["vtype"], data["price"]
    email, password = data["email"], data["password"]
    uid = msg.from_user.id

    ud = get_user(uid)
    if not ud or ud[3] < price:
        await msg.answer(f"❌ Need {price} coins, you have {ud[3] if ud else 0}. Use Balance/Deposit.")
        await state.clear()
        return
    if not deduct_coins(uid, price):
        await msg.answer("❌ Deduction failed.")
        await state.clear()
        return

    await msg.answer("⏳ Starting verification...")
    result = await pix_start_verification(vtype, email, password, totp)
    if not result.get("success"):
        add_coins(uid, price)
        await msg.answer(f"❌ Error: {result.get('error',{}).get('message','Unknown')}. Refunded ${price}.")
        await state.clear()
        return

    gen_id = result["generation_id"]
    save_verification(uid, gen_id, vtype, email)
    await msg.answer(f"✅ Submitted! Generation ID: `{gen_id}`\n⏳ Polling for result...")
    await state.clear()
    asyncio.create_task(poll_loop(uid, gen_id, vtype))

async def poll_loop(uid, gen_id, vtype):
    price = PRICE_VIP if vtype == "vip" else PRICE_NORMAL
    for _ in range(60):
        await asyncio.sleep(5)
        r = await pix_poll(gen_id)
        if not r.get("success"):
            continue
        st = r.get("status")
        if st == "success":
            url = r.get("result_url", "")
            cu = r.get("credits_used", 0)
            update_verification(gen_id, "success", url, cu)
            save_generated_link(uid, url, vtype)
            await bot.send_message(uid, f"✅ **Done!**\n🔗 `{url}`\n💲 Used: {cu} credits")
            return
        elif st == "failed":
            update_verification(gen_id, "failed")
            nb = add_coins(uid, price)
            await bot.send_message(uid, f"❌ Verification failed. ${price} refunded. Balance: {nb:.2f}")
            return
    update_verification(gen_id, "failed")
    nb = add_coins(uid, price)
    await bot.send_message(uid, f"⏱ Timeout. ${price} refunded. Balance: {nb:.2f}")

@dp.message(F.text == "💰 Balance / Deposit")
@dp.message(Command("deposit"))
async def balance_menu(msg: Message):
    ud = get_user(msg.from_user.id)
    if not ud or ud[5]:
        await msg.answer("❌ Banned.")
        return
    await msg.answer(f"💰 **Balance:** {ud[3]:.2f} coins\n\nChoose asset to deposit:", reply_markup=asset_kb())

@dp.callback_query(F.data.startswith("pay_"))
async def pay_asset_cb(cb: CallbackQuery):
    asset = cb.data.replace("pay_", "")
    await dp.storage.update_data(chat=cb.message.chat.id, user=cb.from_user.id, data={"dep_asset": asset})
    await cb.message.edit_text(f"💳 **{asset}** selected.\n\nEnter **USD amount** (min $1):")
    await cb.answer()

@dp.message(lambda msg: msg.text and msg.text.replace(".","").replace(",","").isdigit() and float(msg.text.replace(",",".")) >= 1)
async def deposit_amt(msg: Message):
    cd = await dp.storage.get_data(chat=msg.chat.id, user=msg.from_user.id)
    asset = cd.get("dep_asset", "USDT")
    usd = float(msg.text.strip().replace(",", "."))
    if usd < 1:
        await msg.answer("❌ Min $1.")
        return
    await msg.answer(f"⏳ Creating {asset} invoice for ${usd:.2f}...")
    r = await create_crypto_invoice(msg.from_user.id, usd, asset, f"Deposit ${usd:.2f}")
    if not r.get("ok") or not r.get("result"):
        await msg.answer(f"❌ Failed: {r}")
        return
    inv = r["result"]
    iid = inv.get("invoice_id")
    pay_url = inv.get("pay_url")
    amt_crypto = inv.get("amount")
    as_asset = inv.get("asset", asset)
    save_payment(msg.from_user.id, str(iid), as_asset, float(amt_crypto or 0), usd, str(iid))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Pay {amt_crypto} {as_asset}", url=pay_url)],
        [InlineKeyboardButton(text="✅ I've Paid", callback_data=f"chk_{iid}")],
    ])
    await msg.answer(
        f"🧾 **Invoice**\n💲 {amt_crypto} {as_asset} (~${usd:.2f})\n🆔 `{iid}`",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith("chk_"))
async def check_pay_cb(cb: CallbackQuery):
    iid = cb.data.replace("chk_", "")
    await cb.message.edit_text("⏳ Checking...")
    r = await get_invoice(int(iid))
    inv = None
    if r.get("ok") and r.get("result"):
        inv = r["result"]
    else:
        all_inv = await crypto_pay_request("getInvoices")
        if all_inv.get("ok") and all_inv.get("result"):
            items = all_inv["result"].get("items", all_inv["result"])
            if isinstance(items, list):
                for x in items:
                    if str(x.get("invoice_id")) == iid:
                        inv = x
                        break
    if not inv:
        await cb.message.edit_text("❌ Not found.", reply_markup=back_btn())
        await cb.answer()
        return
    if inv.get("status") == "paid":
        confirm_payment(iid)
        ud = get_user(cb.from_user.id)
        bal = ud[3] if ud else 0
        await cb.message.edit_text(f"✅ **Paid!** Balance: {bal:.2f} coins")
    else:
        await cb.message.edit_text(
            f"Status: **{inv.get('status','unknown')}**",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Retry", callback_data=f"chk_{iid}")]
            ])
        )
    await cb.answer()

@dp.message(F.text == "🛍️ Shop Categories")
async def shop_cats(msg: Message):
    ud = get_user(msg.from_user.id)
    if not ud or ud[5]:
        await msg.answer("❌ Banned.")
        return
    await msg.answer("⏳ Loading...")
    r = await pix_shop_categories()
    if not r.get("success") or not r.get("categories"):
        await msg.answer("❌ No categories.")
        return
    text = "🛍️ **Shop**\n\n"
    kb = []
    for cat in r["categories"]:
        name = cat["name"]
        price = cat.get("discounted_price", cat.get("price_per_unit", 0))
        stock = cat.get("stock", {}).get("available", 0)
        cid = cat["id"]
        text += f"• **{name}** — {price} coins ({stock} in stock)\n"
        kb.append([InlineKeyboardButton(text=f"🛒 {name} ({price} coins)", callback_data=f"shop_{cid}")])
    kb.append([InlineKeyboardButton(text="◀️ Back", callback_data="back_menu")])
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("shop_"))
async def shop_buy_cb(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.replace("shop_", ""))
    await state.update_data(shop_cid=cid)
    await state.set_state(ShopBuy.waiting_qty)
    await cb.message.edit_text("Enter quantity (1-50):", reply_markup=back_btn())
    await cb.answer()

@dp.message(ShopBuy.waiting_qty)
async def shop_qty(msg: Message, state: FSMContext):
    if not msg.text.isdigit() or not 1 <= int(msg.text) <= 50:
        await msg.answer("❌ 1-50 only.")
        return
    qty = int(msg.text)
    d = await state.get_data()
    await msg.answer("⏳ Buying...")
    r = await pix_buy(d["shop_cid"], qty)
    if not r.get("success"):
        await msg.answer(f"❌ {r.get('error',{}).get('message','Unknown')}")
        await state.clear()
        return
    save_shop_purchase(msg.from_user.id, r.get("order_id",0), r.get("category_name","?"),
                       r.get("quantity",qty), r.get("total_cost",0), json.dumps(r.get("credentials",[])))
    creds = r.get("credentials",[])
    txt = "\n".join(creds[:10])
    await msg.answer(f"✅ **Purchased!**\n📦 {r.get('category_name','?')} x{r.get('quantity',qty)}\n💲 {r.get('total_cost',0):.2f}\n`{txt}`")
    await state.clear()

@dp.message(F.text == "📜 My Orders")
async def my_orders(msg: Message):
    uid = msg.from_user.id
    v = db_fetch("SELECT vtype, email, status, result_url, created_at FROM verifications WHERE telegram_id=? ORDER BY created_at DESC LIMIT 10", (uid,))
    p = db_fetch("SELECT asset, amount, amount_usd, status, created_at FROM payments WHERE telegram_id=? ORDER BY created_at DESC LIMIT 10", (uid,))
    s = db_fetch("SELECT category_name, quantity, total_cost, created_at FROM shop_purchases WHERE telegram_id=? ORDER BY created_at DESC LIMIT 10", (uid,))
    text = "📜 **Orders**\n\n"
    if v:
        text += "🔑 **Verifications:**\n"
        for x in v:
            text += f"{'✅' if x[2]=='success' else '❌' if x[2]=='failed' else '⏳'} {x[0].upper()} — {x[1][:10]}... — {x[2]}\n"
        text += "\n"
    if p:
        text += "💳 **Payments:**\n"
        for x in p:
            text += f"{'✅' if x[3]=='paid' else '⏳'} {x[1]} {x[0]} (${x[2]:.2f})\n"
        text += "\n"
    if s:
        text += "🛍️ **Shop:**\n"
        for x in s:
            text += f"📦 {x[0]} x{x[1]} — {x[2]:.2f}\n"
    if not any([v, p, s]):
        text += "No orders yet."
    await msg.answer(text)

# ─── ADMIN ───
@dp.message(F.text == "⚙️ Admin Panel", F.from_user.id == ADMIN_ID)
async def admin_panel(msg: Message):
    await msg.answer("⚙️ **Admin Panel**", reply_markup=admin_kb())

@dp.message(F.text == "◀️ Back to Menu")
async def back_menu(msg: Message):
    ud = get_user(msg.from_user.id)
    await msg.answer("Menu:", reply_markup=main_kb(ud and ud[6] if ud else False))

@dp.callback_query(F.data == "back_menu")
async def back_cb(cb: CallbackQuery):
    ud = get_user(cb.from_user.id)
    await cb.message.delete()
    await cb.message.answer("Menu:", reply_markup=main_kb(ud and ud[6] if ud else False))
    await cb.answer()

@dp.message(F.text == "👥 All Users", F.from_user.id == ADMIN_ID)
async def admin_users(msg: Message):
    users = get_all_users()
    text = "👥 **Users**\n\n"
    for u in users:
        tid, uname, fname, coins, banned, adm, created = u
        badge = "👑" if adm else ("⛔" if banned else "✅")
        text += f"{badge} **{fname or 'N/A'}** (`{tid}`) — {coins:.2f} coins\n"
    for i in range(0, len(text), 3800):
        await msg.answer(text[i:i+3800])

@dp.message(F.text == "⛔ Ban User", F.from_user.id == ADMIN_ID)
async def admin_ban_start(msg: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_ban)
    await msg.answer("Enter User ID to ban:")

@dp.message(AdminStates.waiting_ban)
async def admin_ban_do(msg: Message, state: FSMContext):
    try:
        tid = int(msg.text.strip())
        set_ban(tid, True)
        await msg.answer(f"✅ `{tid}` banned.")
    except:
        await msg.answer("❌ Invalid ID.")
    await state.clear()

@dp.message(F.text == "✅ Unban User", F.from_user.id == ADMIN_ID)
async def admin_unban_start(msg: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_unban)
    await msg.answer("Enter User ID to unban:")

@dp.message(AdminStates.waiting_unban)
async def admin_unban_do(msg: Message, state: FSMContext):
    try:
        tid = int(msg.text.strip())
        set_ban(tid, False)
        await msg.answer(f"✅ `{tid}` unbanned.")
    except:
        await msg.answer("❌ Invalid ID.")
    await state.clear()

@dp.message(F.text == "💰 Add Coins", F.from_user.id == ADMIN_ID)
async def admin_coins_start(msg: Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_give_user)
    await msg.answer("Enter User ID:")

@dp.message(AdminStates.waiting_give_user)
async def admin_coins_user(msg: Message, state: FSMContext):
    try:
        await state.update_data(give_uid=int(msg.text.strip()))
        await state.set_state(AdminStates.waiting_give_amount)
        await msg.answer("Enter amount:")
    except:
        await msg.answer("❌ Invalid ID.")
        await state.clear()

@dp.message(AdminStates.waiting_give_amount)
async def admin_coins_amt(msg: Message, state: FSMContext):
    try:
        amt = float(msg.text.strip())
        d = await state.get_data()
        nb = add_coins(d["give_uid"], amt)
        await msg.answer(f"✅ Added {amt:.2f}. New balance: {nb:.2f}")
    except:
        await msg.answer("❌ Invalid amount.")
    await state.clear()

@dp.message(F.text == "💳 Pending Payments", F.from_user.id == ADMIN_ID)
async def admin_pending(msg: Message):
    rows = db_fetch("SELECT id, telegram_id, invoice_id, amount, amount_usd, asset, status, created_at FROM payments WHERE status='pending' ORDER BY created_at DESC LIMIT 20")
    if not rows:
        await msg.answer("✅ No pending payments.")
        return
    text = "💳 **Pending**\n\n"
    for r in rows:
        text += f"• `{r[2]}` | User `{r[1]}` | {r[3]} {r[5]} (${r[4]:.2f}) | {r[6]}\n"
    await msg.answer(text)

@dp.message(F.text == "📊 PixVerify Balance", F.from_user.id == ADMIN_ID)
async def admin_pix_bal(msg: Message):
    await msg.answer("⏳ Fetching...")
    r = await pix_balance()
    if r.get("success"):
        p = r["profile"]
        await msg.answer(
            f"📊 **PixVerify**\n\n"
            f"💰 Credit: {p.get('credit_balance',0):.2f}\n"
            f"💳 Topup: {p.get('topup_credit_balance',0):.2f}\n"
            f"🎁 Referral: {p.get('referral_credit_balance',0):.2f}\n"
            f"⚡ Usable: {p.get('api_usable_balance',0):.2f}\n"
            f"🏷️ Discount: {p.get('api_discount_pct',0)}%"
        )
    else:
        await msg.answer(f"❌ {r}")

@dp.message(F.text == "📋 Gen VIP Link", F.from_user.id == ADMIN_ID)
async def admin_gen_vip(msg: Message, state: FSMContext):
    await state.update_data(gen_type="vip")
    await state.set_state(AdminStates.waiting_gen_user)
    await msg.answer("Enter User ID to generate VIP link for:")

@dp.message(F.text == "📋 Gen Normal Link", F.from_user.id == ADMIN_ID)
async def admin_gen_normal(msg: Message, state: FSMContext):
    await state.update_data(gen_type="normal")
    await state.set_state(AdminStates.waiting_gen_user)
    await msg.answer("Enter User ID to generate Normal link for:")

@dp.message(AdminStates.waiting_gen_user)
async def admin_gen_user(msg: Message, state: FSMContext):
    try:
        tid = int(msg.text.strip())
        await state.update_data(gen_uid=tid)
        await state.set_state(AdminVerify.entering_email)
        await msg.answer(f"User `{tid}`. Enter **Gmail**:")
    except:
        await msg.answer("❌ Invalid ID.")
        await state.clear()

@dp.message(AdminVerify.entering_email)
async def admin_verify_email(msg: Message, state: FSMContext):
    if msg.text.lower() == "cancel":
        await state.clear()
        await msg.answer("Cancelled.", reply_markup=admin_kb())
        return
    if "@" not in msg.text or "." not in msg.text:
        await msg.answer("❌ Invalid email:")
        return
    await state.update_data(email=msg.text.strip())
    await state.set_state(AdminVerify.entering_password)
    await msg.answer("Enter **password**: (or 'cancel')")

@dp.message(AdminVerify.entering_password)
async def admin_verify_pass(msg: Message, state: FSMContext):
    if msg.text.lower() == "cancel":
        await state.clear()
        await msg.answer("Cancelled.", reply_markup=admin_kb())
        return
    await state.update_data(password=msg.text.strip())
    await state.set_state(AdminVerify.entering_totp)
    await msg.answer("Enter **TOTP secret**: (or 'cancel')")

@dp.message(AdminVerify.entering_totp)
async def admin_verify_totp(msg: Message, state: FSMContext):
    if msg.text.lower() == "cancel":
        await state.clear()
        await msg.answer("Cancelled.", reply_markup=admin_kb())
        return
    totp = msg.text.strip().upper().replace(" ", "")
    d = await state.get_data()
    vtype = d["gen_type"]
    email, password = d["email"], d["password"]
    uid = d["gen_uid"]
    await msg.answer("⏳ Starting...")
    r = await pix_start_verification(vtype, email, password, totp)
    if not r.get("success"):
        await msg.answer(f"❌ {r.get('error',{}).get('message','Err')}")
        await state.clear()
        return
    gen_id = r["generation_id"]
    save_verification(uid, gen_id, vtype, email)
    await msg.answer(f"✅ Gen ID: `{gen_id}`. Polling...")
    await state.clear()
    asyncio.create_task(poll_loop(uid, gen_id, vtype))

# ─── FALLBACK ───
@dp.message()
async def fallback(msg: Message):
    ud = get_user(msg.from_user.id)
    is_adm = ud and ud[6] if ud else False
    await msg.answer("Use menu:", reply_markup=main_kb(is_adm))

# ─── MAIN ───
async def main():
    log.info(f"DB path: {DB_PATH}")
    init_db()
    log.info("Database ready. Starting polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
        sys.exit(0)
    except Exception as e:
        log.exception("Fatal")
        sys.exit(1)
