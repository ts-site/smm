
import asyncio
import logging
import time
import re
import math
import aiohttp
import aiosqlite
import os
from flask import Flask
from threading import Thread
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# =====================================================================
# ⚙️ НАСТРОЙКИ (БЕЗОПАСНЫЙ ИМПОРТ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ RENDER)
# =====================================================================
# Если переменные BOT_TOKEN и OWNER_ID настроены в Render, они возьмутся оттуда.
# Если нет — бот будет использовать резервные значения, указанные ниже.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8969739360:AAG-0Pt5Xbl_A5sk01AZMx2CEmtAvx6LWm8")
OWNER_ID = int(os.environ.get("OWNER_ID", 7888286588))

DEFAULT_API_KEY = "XVxwJNlgJr8zcuKv0us99lXa82ugRbCuFOSTT6GBUabWwXi2ucADvVaTSYvp"
DEFAULT_MARGIN = 2.0         # Наценка по умолчанию (коэффициент)
MIN_ORDER_COST = 5.0         # Минимальная стоимость заказа в рублях (базовая)
DEFAULT_WALLET = "41001XXXXXXXXXXXX"  # Твой кошелек ЮMoney
DEFAULT_YOOTOKEN = "ТВОЙ_ТОКЕН_ЮМОНИ"
DEFAULT_SUPPORT = "https://t.me/твой_юзернейм"  # Ссылка на поддержку
# =====================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
DB_PATH = "booster_v24.db"

# =====================================================================
# 🌐 ВЕБ-СЕРВЕР FLASK ДЛЯ RENDER (ФУНКЦИЯ ALWAYS-ON)
# =====================================================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Telegram Bot is alive!", 200

def run_flask_app():
    # Render сам выдает порт через переменную PORT. Если её нет, используем 8080.
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- API КЛИЕНТ TWIBOOST ---
class SmmAPI:
    def __init__(self):
        self.url = "https://twiboost.com/api/v2"

    async def request(self, data: dict) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key='api_key'")
            res = await cur.fetchone()
            api_key = res[0] if res else DEFAULT_API_KEY
            
        data["key"] = api_key
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.url, data=data, timeout=15) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return {"error": f"HTTP status {resp.status}"}
        except Exception as e:
            return {"error": str(e)}

    async def get_balance(self) -> str:
        res = await self.request({"action": "balance"})
        if res and "balance" in res:
            return f"{res['balance']} {res.get('currency', 'RUB')}"
        return f"Ошибка: {res.get('error', 'неизвестно')}"

api = SmmAPI()

# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, 
                username TEXT, 
                balance REAL DEFAULT 0, 
                is_blocked INTEGER DEFAULT 0,
                role TEXT DEFAULT 'user'
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                user_id INTEGER, 
                api_order_id INTEGER, 
                service_id INTEGER,
                service_name TEXT, 
                link TEXT,
                quantity INTEGER,
                cost REAL, 
                status TEXT DEFAULT 'Pending',
                timestamp INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS promo (code TEXT PRIMARY KEY, amount REAL, max_uses INTEGER, uses INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS promo_uses (user_id INTEGER, code TEXT);
            CREATE TABLE IF NOT EXISTS payments (label TEXT PRIMARY KEY, user_id INTEGER, amount REAL, status TEXT DEFAULT 'pending');
        """)
        sets = [
            ("api_key", DEFAULT_API_KEY), 
            ("margin", str(DEFAULT_MARGIN)), 
            ("wallet", DEFAULT_WALLET), 
            ("yoo_token", DEFAULT_YOOTOKEN), 
            ("support_link", DEFAULT_SUPPORT), 
            ("maintenance", "0"),
            ("min_cost", str(MIN_ORDER_COST))
        ]
        for k, v in sets:
            await db.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))
        
        await db.execute("INSERT OR IGNORE INTO users (user_id, role) VALUES (?, 'owner')", (OWNER_ID,))
        await db.execute("UPDATE users SET role='owner' WHERE user_id=?", (OWNER_ID,))
        await db.commit()

async def get_conf(key):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        res = await cur.fetchone()
        return res[0] if res else ""

# --- СОСТОЯНИЯ (FSM) ---
class FSM(StatesGroup):
    plat = State()
    cat = State()
    serv = State()
    link = State()
    qty = State()
    refill = State()
    promo = State()
    
    # Расширенные состояния админки
    broadcast = State()
    adm_u_search = State()
    adm_u_bal = State()
    adm_u_msg = State()
    adm_o_search = State()
    adm_o_status_man = State()
    adm_p_code = State()
    adm_p_amt = State()
    adm_p_uses = State()
    adm_st_edit = State()

# --- ПРОВЕРКА ДОСТУПА И БЛОКИРОВОК ---
async def check_access(event):
    u_id = event.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        u = await (await db.execute("SELECT is_blocked, role FROM users WHERE user_id=?", (u_id,))).fetchone()
        if u and u[0] == 1:
            if isinstance(event, Message):
                await event.answer("🚫 Ваш аккаунт заблокирован.")
            else:
                await event.answer("🚫 Ваш аккауйн заблокирован.", show_alert=True)
            return False
            
        maint = await get_conf("maintenance")
        if maint == "1" and (not u or u[1] == 'user'):
            if isinstance(event, Message):
                await event.answer("⚠️ В боте ведутся технические работы. Пожалуйста, зайдите позже.")
            else:
                await event.answer("⚠️ В боте ведутся технические работы. Пожалуйста, зайдите позже.", show_alert=True)
            return False
    return u[1] if u else 'user'

# --- ГЛАВНОЕ МЕНЮ ---
async def main_kb(role):
    kb = [
        [InlineKeyboardButton(text="🚀 ЗАКАЗАТЬ НАКРУТКУ", callback_data="start_order")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"), InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="refill")],
        [InlineKeyboardButton(text="📋 Мои Заказы", callback_data="my_orders"), InlineKeyboardButton(text="🎫 Промокод", callback_data="use_promo")],
        [InlineKeyboardButton(text="🆘 Поддержка", url=await get_conf("support_link"))]
    ]
    if role in ['admin', 'owner']:
        kb.append([InlineKeyboardButton(text="👑 АДМИН-ПАНЕЛЬ", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


# =====================================================================
# 🚀 ОФОРМЛЕНИЕ ЗАКАЗА
# =====================================================================

@router.callback_query(F.data == "start_order")
async def order_1_plat(call: CallbackQuery, state: FSMContext):
    if not await check_access(call): 
        return
    await call.message.edit_text("⏳ Загружаем список платформ...")
    res = await api.request({"action": "services"})
    if not res or "error" in res:
        role = await check_access(call)
        return await call.message.edit_text("❌ Не удалось получить список услуг. Попробуйте позже.", reply_markup=await main_kb(role))
    
    platforms = set()
    for s in res:
        try: 
            rate = float(s.get('rate', 0))
        except Exception: 
            rate = 0
        if rate <= 0: 
            continue
        
        cat = s['category']
        parts = re.split(r'[\s\-|/|:|\|]+', cat)
        first_word = parts[0].strip() if parts else cat.split()[0].strip()
        
        if first_word.upper() not in ["API", "GMAIL", "SERVICES"] and len(first_word) > 1:
            platforms.add(first_word)
            
    sorted_plats = sorted(list(platforms))
    kb = []
    for i in range(0, len(sorted_plats), 2):
        row = [InlineKeyboardButton(text=f"🌐 {sorted_plats[i]}", callback_data=f"pl_{i}")]
        if i + 1 < len(sorted_plats):
            row.append(InlineKeyboardButton(text=f"🌐 {sorted_plats[i+1]}", callback_data=f"pl_{i+1}"))
        kb.append(row)
        
    kb.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="to_main")])
    await state.update_data(all_servs=res, platforms=sorted_plats)
    await call.message.edit_text("<b>Выберите социальную сеть:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(FSM.plat)

@router.callback_query(FSM.plat, F.data.startswith("pl_"))
async def order_2_cat(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.replace("pl_", ""))
    data = await state.get_data()
    plat = data['platforms'][idx]
    cats = sorted(list(set([s['category'] for s in data['all_servs'] if s['category'].lower().startswith(plat.lower()) and float(s.get('rate', 0)) > 0])))
    
    kb = []
    for c_idx, c in enumerate(cats):
        clean_name = c.replace(plat, "").replace("-", "").replace("|", "").replace("/", "").strip()
        if not clean_name: 
            clean_name = "Общие услуги"
        kb.append([InlineKeyboardButton(text=clean_name, callback_data=f"ct_{c_idx}")])
        
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="start_order")])
    await state.update_data(cur_cats=cats, sel_plat=plat)
    await call.message.edit_text(f"<b>{plat}</b> — Выберите направление:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(FSM.cat)

@router.callback_query(FSM.cat, F.data.startswith("ct_"))
async def order_3_serv(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.replace("ct_", ""))
    data = await state.get_data()
    cat_name = data['cur_cats'][idx]
    margin = float(await get_conf("margin"))
    servs = [s for s in data['all_servs'] if s['category'] == cat_name and float(s.get('rate', 0)) > 0]
    
    kb = []
    for s_idx, s in enumerate(servs[:25]):
        price = float(s['rate']) * margin
        kb.append([InlineKeyboardButton(text=f"{s['name']} | {price:.2f}₽", callback_data=f"sr_{s_idx}")])
        
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="start_order")])
    await state.update_data(cur_servs=servs)
    await call.message.edit_text(f"<b>{cat_name}:</b>\nВыберите тариф:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(FSM.serv)

@router.callback_query(FSM.serv, F.data.startswith("sr_"))
async def order_4_link(call: CallbackQuery, state: FSMContext):
    idx = int(call.data.replace("sr_", ""))
    data = await state.get_data()
    s = data['cur_servs'][idx]
    price_1k = float(s['rate']) * float(await get_conf("margin"))
    min_limit = max(int(s.get('min', 10)), 10)
    max_limit = int(s.get('max', 100000))
    
    await state.update_data(sel=s, price_1k=price_1k, min_limit=min_limit, max_limit=max_limit)
    await call.message.edit_text(f"📥 <b>Введите ссылку:</b>\n\n"
                                 f"Тариф: <code>{s['name']}</code>\n"
                                 f"Минимум: <code>{min_limit}</code> шт. | Максимум: <code>{max_limit}</code> шт.", 
                                 parse_mode="HTML", 
                                 disable_web_page_preview=True,
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="to_main")]]))
    await state.set_state(FSM.link)

@router.message(FSM.link)
async def order_5_qty(msg: Message, state: FSMContext):
    if not msg.text.startswith("http"): 
        return await msg.answer("❌ Введите корректную ссылку, начинающуюся с http:// или https://:")
    await state.update_data(link=msg.text)
    data = await state.get_data()
    await msg.answer(f"🔢 <b>Введите количество (от {data['min_limit']} до {data['max_limit']}):</b>", parse_mode="HTML")
    await state.set_state(FSM.qty)

@router.message(FSM.qty)
async def order_6_conf(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): 
        return await msg.answer("❌ Введите целое число:")
    qty = int(msg.text)
    data = await state.get_data()
    s = data['sel']
    cost = round((data['price_1k'] / 1000) * qty, 2)
    
    min_order_cost = float(await get_conf("min_cost") or MIN_ORDER_COST)
    if cost < min_order_cost:
        needed_qty = math.ceil((min_order_cost * 1000) / data['price_1k'])
        if needed_qty < data['min_limit']: 
            needed_qty = data['min_limit']
        return await msg.answer(
            f"❌ <b>Минимальная сумма заказа — {min_order_cost:.2f} руб.</b>\n"
            f"Для {qty} шт. цена составила всего {cost:.2f} руб.\n\n"
            f"Увеличьте заказ. На этом тарифе нужно заказать минимум: <b>{needed_qty} шт.</b>",
            parse_mode="HTML"
        )
        
    if qty < data['min_limit'] or qty > data['max_limit']:
        return await msg.answer(f"❌ Ошибка. Лимит: от {data['min_limit']} до {data['max_limit']} шт. Повторите ввод:")
        
    await state.update_data(qty=qty, cost=cost)
    kb = [
        [InlineKeyboardButton(text=f"✅ Оплатить {cost:.2f}₽", callback_data="buy_ok")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="to_main")]
    ]
    await msg.answer(f"📋 <b>Детали вашего заказа:</b>\n\n"
                     f"🔹 Услуга: <code>{s['name']}</code>\n"
                     f"🔗 Ссылка: {data['link']}\n"
                     f"🔢 Количество: {qty} шт.\n"
                     f"💵 К оплате: <b>{cost:.2f} руб.</b>", 
                     parse_mode="HTML", 
                     disable_web_page_preview=True, 
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "buy_ok")
async def order_7_final(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    u_id = call.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        bal = (await (await db.execute("SELECT balance FROM users WHERE user_id=?", (u_id,))).fetchone())[0]
        if bal < d['cost']: 
            return await call.answer("❌ Недостаточно средств!", show_alert=True)
        
        await db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (d['cost'], u_id))
        
        res = await api.request({"action": "add", "service": d['sel']['service'], "link": d['link'], "quantity": d['qty']})
        
        if res and "order" in res and int(res.get('order', 0)) > 0:
            api_id = int(res['order'])
            status = "Pending"
            msg_text = f"🚀 <b>Заказ №{api_id} успешно запущен в работу!</b>"
        else:
            api_id = 0
            status = "Awaiting Refill"
            msg_text = "🚀 <b>Заказ принят и поставлен в очередь на обработку!</b>"
            try:
                err_text = res.get('error', 'Unknown / Low Balance') if res else 'No Connection'
                await bot.send_message(
                    OWNER_ID, 
                    f"🚨 <b>СРОЧНО ПОПОЛНИ БАЛАНС TWIBOOST!</b>\n\n"
                    f"Юзер <code>{u_id}</code> оплатил заказ на {d['cost']}₽, но система не смогла запустить его автоматически.\n"
                    f"Ошибка API: <code>{err_text}</code>\n"
                    f"Заказ ожидает ручного пополнения счета и ручного пуша из админки.", 
                    parse_mode="HTML"
                )
            except Exception: 
                pass
            
        await db.execute("INSERT INTO orders (user_id, api_order_id, service_id, service_name, link, quantity, cost, status, timestamp) VALUES (?,?,?,?,?,?,?,?,?)", 
                         (u_id, api_id, d['sel']['service'], d['sel']['name'], d['link'], d['qty'], d['cost'], status, int(time.time())))
        await db.commit()
        
    role = await check_access(call)
    await call.message.edit_text(msg_text, parse_mode="HTML", reply_markup=await main_kb(role))
    await state.clear()


# =====================================================================
# 👤 ЛИЧНЫЙ КАБИНЕТ, ПРОМОКОДЫ И ОПЛАТА
# =====================================================================

@router.callback_query(F.data == "to_main")
async def to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    role = await check_access(call)
    if role: 
        await call.message.edit_text("<b>Главное меню:</b>", parse_mode="HTML", reply_markup=await main_kb(role))

@router.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    role = await check_access(call)
    if not role: 
        return
    async with aiosqlite.connect(DB_PATH) as db:
        b = (await (await db.execute("SELECT balance FROM users WHERE user_id=?", (call.from_user.id,))).fetchone())[0]
    await call.message.edit_text(f"👤 <b>Личный кабинет:</b>\n\n"
                                 f"🆔 ID: <code>{call.from_user.id}</code>\n"
                                 f"💰 Баланс: <b>{b:.2f} рублей</b>", 
                                 parse_mode="HTML", reply_markup=await main_kb(role))

@router.callback_query(F.data == "my_orders")
async def my_orders(call: CallbackQuery):
    role = await check_access(call)
    if not role: 
        return
    async with aiosqlite.connect(DB_PATH) as db:
        ords = await (await db.execute("SELECT api_order_id, service_name, cost, status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (call.from_user.id,))).fetchall()
        
    if not ords: 
        return await call.message.edit_text("📋 У вас еще нет заказов.", reply_markup=await main_kb(role))
    txt = "📋 <b>Последние 5 заказов:</b>\n\n"
    for o in ords:
        st = "В очереди" if o[3] == "Awaiting Refill" else o[3]
        txt += f"📦 <b>Заказ №{o[0] if o[0] > 0 else '---'}</b>\n🏷 Тариф: {o[1]}\n💰 Цена: {o[2]}₽ | Статус: {st}\n\n"
    await call.message.edit_text(txt, parse_mode="HTML", reply_markup=await main_kb(role))

@router.callback_query(F.data == "use_promo")
async def promo_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call): 
        return
    await call.message.edit_text("🎫 Введите промокод:")
    await state.set_state(FSM.promo)

@router.message(FSM.promo)
async def promo_use(msg: Message, state: FSMContext):
    c = msg.text.strip().upper()
    u = msg.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        p = await (await db.execute("SELECT amount, max_uses, uses FROM promo WHERE code=?", (c,))).fetchone()
        if not p: 
            return await msg.answer("❌ Такого промокода нет.")
        if p[2] >= p[1]: 
            return await msg.answer("❌ Промокод полностью израсходован.")
        
        used = await (await db.execute("SELECT 1 FROM promo_uses WHERE user_id=? AND code=?", (u, c))).fetchone()
        if used: 
            return await msg.answer("❌ Вы уже активировали этот промокод!")
        
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (p[0], u))
        await db.execute("UPDATE promo SET uses = uses + 1 WHERE code = ?", (c,))
        await db.execute("INSERT INTO promo_uses VALUES (?, ?)", (u, c))
        await db.commit()
    
    role = await check_access(msg)
    await msg.answer(f"🎉 Промокод активирован! Зачислено {p[0]}₽.", reply_markup=await main_kb(role))
    await state.clear()

@router.callback_query(F.data == "refill")
async def refill_start(call: CallbackQuery, state: FSMContext):
    if not await check_access(call): 
        return
    await call.message.edit_text("💳 Введите сумму пополнения баланса (целое число в рублях):")
    await state.set_state(FSM.refill)

@router.message(FSM.refill)
async def refill_pay(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): 
        return await msg.answer("❌ Пожалуйста, введите корректное целое число:")
    amt = int(msg.text)
    if amt <= 0: 
        return await msg.answer("❌ Сумма должна быть больше нуля!")
    
    lbl = f"{msg.from_user.id}_{int(time.time())}"
    wallet = await get_conf("wallet")
    url = f"https://yoomoney.ru/quickpay/confirm.xml?receiver={wallet}&quickpay-form=shop&targets=Refill&paymentType=SB&sum={amt}&label={lbl}"
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO payments VALUES (?,?,?, 'pending')", (lbl, msg.from_user.id, amt))
        await db.commit()
        
    kb = [[InlineKeyboardButton(text="💳 Ссылка на оплату", url=url)], [InlineKeyboardButton(text="✅ Проверить платеж", callback_data=f"chk_{lbl}")]]
    await msg.answer(f"💵 Счёт на {amt}₽ создан.\nПроизведите оплату и нажмите кнопку:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.clear()

@router.callback_query(F.data.startswith("chk_"))
async def refill_check(call: CallbackQuery):
    lbl = call.data.replace("chk_", "")
    tok = await get_conf("yoo_token")
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/x-www-form-urlencoded"}
    async with aiohttp.ClientSession() as s:
        async with s.post("https://yoomoney.ru/api/operation-history", headers=headers, data={"label": lbl}) as r:
            try:
                d = await r.json()
                if d.get("operations") and d["operations"][0]["status"] == "success":
                    async with aiosqlite.connect(DB_PATH) as db:
                        p = await (await db.execute("SELECT amount, user_id, status FROM payments WHERE label=?", (lbl,))).fetchone()
                        if p and p[2] == 'pending':
                            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (p[0], p[1]))
                            await db.execute("UPDATE payments SET status='success' WHERE label=?", (lbl,))
                            await db.commit()
                            return await call.message.edit_text(f"✅ Баланс пополнен на {p[0]}₽!")
            except Exception: 
                pass
            await call.answer("❌ Оплата не обнаружена. Попробуйте еще раз позже.", show_alert=True)


# =====================================================================
# 👑 АДМИН-ПАНЕЛЬ
# =====================================================================

@router.callback_query(F.data == "admin_panel")
async def adm_menu(call: CallbackQuery, state: FSMContext):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    await state.clear()
    kb = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats"), InlineKeyboardButton(text="🔌 Статус API", callback_data="adm_api_status")],
        [InlineKeyboardButton(text="👥 Юзеры", callback_data="adm_u_hub"), InlineKeyboardButton(text="📦 Заказы", callback_data="adm_o_hub")],
        [InlineKeyboardButton(text="⏳ Зависшие заказы", callback_data="adm_stuck_hub"), InlineKeyboardButton(text="🎫 Промокоды", callback_data="adm_promo_hub")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_bc_hub"), InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm_st_hub")],
        [InlineKeyboardButton(text="💾 База данных", callback_data="adm_db_hub"), InlineKeyboardButton(text="◀️ В меню", callback_data="to_main")]
    ]
    await call.message.edit_text("👑 <b>ГЛАВНОЕ МЕНЮ АДМИНИСТРАТОРА</b>\nВыберите раздел для управления:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# 📊 1. Модуль Статистики и Аналитики
@router.callback_query(F.data == "adm_stats")
async def adm_stats(call: CallbackQuery):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    async with aiosqlite.connect(DB_PATH) as db:
        users_count = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        blocked_count = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1")).fetchone())[0]
        new_users_24h = (await (await db.execute("SELECT COUNT(DISTINCT user_id) FROM orders WHERE timestamp > ?", (int(time.time()) - 86400,))).fetchone())[0]
        
        orders_count = (await (await db.execute("SELECT COUNT(*) FROM orders")).fetchone())[0]
        orders_stuck = (await (await db.execute("SELECT COUNT(*) FROM orders WHERE status='Awaiting Refill'")).fetchone())[0]
        orders_refunded = (await (await db.execute("SELECT COUNT(*) FROM orders WHERE status='Refunded'")).fetchone())[0]
        
        total_revenue = (await (await db.execute("SELECT SUM(cost) FROM orders WHERE status != 'Refunded'")).fetchone())[0] or 0.0
        margin = float(await get_conf("margin") or 2.0)
        net_profit = total_revenue * (1 - (1 / margin)) if margin > 1 else 0.0

    await call.message.edit_text(
        f"📊 <b>ПОЛНАЯ СТАТИСТИКА БОТА</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"  └ Всего в базе: <code>{users_count}</code>\n"
        f"  └ Новых за 24ч (активных): <code>{new_users_24h}</code>\n"
        f"  └ Заблокировано: <code>{blocked_count}</code>\n\n"
        f"📦 <b>Заказы:</b>\n"
        f"  └ Всего оформлено: <code>{orders_count}</code>\n"
        f"  └ Зависло (без баланса): <b>{orders_stuck}</b>\n"
        f"  └ Сделано возвратов: <code>{orders_refunded}</code>\n\n"
        f"💵 <b>Финансы:</b>\n"
        f"  └ Суммарный оборот: <code>{total_revenue:.2f}₽</code>\n"
        f"  └ <b>Чистая прибыль (прибл.): {net_profit:.2f}₽</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]])
    )

# 🔌 2. Модуль API Статуса
@router.callback_query(F.data == "adm_api_status")
async def adm_api_status(call: CallbackQuery):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    bal = await api.get_balance()
    
    kb = [
        [InlineKeyboardButton(text="💳 Пополнить Twiboost (Сайт)", url="https://twiboost.com/addfunds")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
    ]
    
    await call.message.edit_text(
        f"🔌 <b>СТАТУС И СИНХРОНИЗАЦИЯ API</b>\n\n"
        f"🔗 Сервер API: <code>twiboost.com</code>\n"
        f"💰 Текущий баланс на Twiboost: <b>{bal}</b>\n\n"
        f"<i>При нулевом балансе заказы пользователей автоматически уходят в 'Awaiting Refill'. Пополните счет на сайте, а затем воспользуйтесь разделом зависших заказов.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )

# 👥 3. Модуль Пользователей (Хаб)
@router.callback_query(F.data == "adm_u_hub")
async def adm_u_hub(call: CallbackQuery, state: FSMContext):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    await call.message.edit_text("🔍 <b>УПРАВЛЕНИЕ ЮЗЕРАМИ</b>\n\nВведите Telegram ID пользователя для детального поиска:", 
                                 parse_mode="HTML", 
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]]))
    await state.set_state(FSM.adm_u_search)

@router.message(FSM.adm_u_search)
async def adm_u_search_res(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): 
        return await msg.answer("❌ ID должен быть числовым:")
    async with aiosqlite.connect(DB_PATH) as db:
        u = await (await db.execute("SELECT balance, is_blocked, role, username FROM users WHERE user_id=?", (msg.text,))).fetchone()
    if not u: 
        return await msg.answer("❌ Пользователь не найден в базе данных.")
    
    await state.update_data(t_id=msg.text, t_role=u[2])
    status_text = "🚫 ЗАБАНЕН" if u[1] == 1 else "🟢 Активен"
    kb = [
        [InlineKeyboardButton(text="💰 Изменить Баланс", callback_data="u_bal"), InlineKeyboardButton(text="🚫 Бан / Разбан", callback_data="u_ban")],
        [InlineKeyboardButton(text="✉️ Написать юзеру", callback_data="u_msg"), InlineKeyboardButton(text="👑 Дать/Забрать Админа", callback_data="u_role")],
        [InlineKeyboardButton(text="📋 Последние 5 заказов", callback_data="u_orders_view")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_panel")]
    ]
    await msg.answer(f"👤 <b>Пользователь:</b> <code>{msg.text}</code>\n"
                     f"Никнейм: @{u[3] or 'нет'}\n"
                     f"💰 Баланс: {u[0]:.2f}₽\n"
                     f"🚦 Статус: {status_text}\n"
                     f"🎭 Роль: {u[2]}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "u_ban")
async def adm_u_ban(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    t_id = d.get('t_id')
    if not t_id: 
        return await call.answer("Сессия истекла", show_alert=True)
    if int(t_id) == OWNER_ID: 
        return await call.answer("Нельзя забанить создателя!", show_alert=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_blocked = 1 - is_blocked WHERE user_id=?", (t_id,))
        await db.commit()
    await call.answer("Статус бана успешно обновлен!")
    await call.message.delete()
    await adm_u_search_res(Message(from_user=call.from_user, text=t_id, chat=call.message.chat), state)


@router.callback_query(F.data == "u_role")
async def adm_u_role(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID: 
        return await call.answer("Роли может менять только Создатель бота!", show_alert=True)
    d = await state.get_data()
    t_id = d.get('t_id')
    cur_role = d.get('t_role')
    if not t_id: 
        return await call.answer("Сессия истекла", show_alert=True)
    if int(t_id) == OWNER_ID: 
        return await call.answer("Нельзя изменить роль создателя!", show_alert=True)
    
    new_role = 'admin' if cur_role == 'user' else 'user'
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET role = ? WHERE user_id=?", (new_role, t_id))
        await db.commit()
    await call.answer(f"Роль успешно обновлена на: {new_role}!")
    await call.message.delete()
    await adm_u_search_res(Message(from_user=call.from_user, text=t_id, chat=call.message.chat), state)

@router.callback_query(F.data == "u_bal")
async def adm_u_bal_start(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if 't_id' not in data:
        return await call.answer("❌ Ошибка: сессия поиска истекла. Найдите пользователя заново.", show_alert=True)
    
    await call.message.answer(f"💰 <b>Изменение баланса пользователя:</b> <code>{data['t_id']}</code>\n"
                                f"Введите сумму, на которую изменится баланс (например, 100 или -100):", parse_mode="HTML")
    await state.set_state(FSM.adm_u_bal)

@router.message(FSM.adm_u_bal)
async def adm_u_bal_save(msg: Message, state: FSMContext):
    data = await state.get_data()
    t_id = data.get('t_id')
    
    if not t_id:
        await msg.answer("❌ Ошибка: сессия потеряна.")
        return await state.clear()

    try:
        amount_text = msg.text.replace(',', '.')
        val = float(amount_text)
        
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (t_id,))
            if not await cur.fetchone():
                await msg.answer("❌ Пользователь не найден в БД.")
                return await state.clear()
                
            await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (val, t_id))
            await db.commit()
            
        await msg.answer(f"✅ Баланс пользователя <code>{t_id}</code> успешно изменен на <b>{val}₽</b>", parse_mode="HTML")
        
        try:
            await bot.send_message(t_id, f"💰 Ваш баланс был изменен на <b>{val:+.2f}₽</b> администратором.", parse_mode="HTML")
        except Exception:
            pass
            
    except ValueError:
        await msg.answer("❌ Ошибка! Введите числовое значение (например: 150 или -50):")
    except Exception as e:
        await msg.answer(f"❌ Ошибка базы данных: {e}")
    
    await state.clear()

@router.callback_query(F.data == "u_msg")
async def adm_u_msg_start(call: CallbackQuery, state: FSMContext):
    await call.message.answer("Введите текст сообщения для отправки этому пользователю в ЛС:")
    await state.set_state(FSM.adm_u_msg)

@router.message(FSM.adm_u_msg)
async def adm_u_msg_send(msg: Message, state: FSMContext):
    d = await state.get_data()
    try:
        await bot.send_message(d['t_id'], f"✉️ <b>Сообщение от поддержки:</b>\n\n{msg.text}", parse_mode="HTML")
        await msg.answer("✅ Сообщение успешно доставлено пользователю!")
    except Exception as e:
        await msg.answer(f"❌ Не удалось отправить: {e}")
    await state.clear()

@router.callback_query(F.data == "u_orders_view")
async def adm_u_orders_view(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        ords = await (await db.execute("SELECT api_order_id, service_name, cost, status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (d['t_id'],))).fetchall()
    if not ords: 
        return await call.answer("У этого пользователя нет заказов", show_alert=True)
    
    txt = f"📦 <b>Последние 5 заказов юзера {d['t_id']}:</b>\n\n"
    for o in ords:
        txt += f"Order ID: {o[0]} | {o[1]} | {o[2]}₽ | {o[3]}\n"
    await call.message.answer(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]]))

# 📦 4. Модуль Заказов (Хаб)
@router.callback_query(F.data == "adm_o_hub")
async def adm_o_hub(call: CallbackQuery, state: FSMContext):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    await call.message.edit_text("🔍 <b>УПРАВЛЕНИЕ ЗАКАЗАМИ</b>\n\nВведите Внутренний ID или API ID заказа для поиска:", 
                                 parse_mode="HTML", 
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]]))
    await state.set_state(FSM.adm_o_search)

@router.message(FSM.adm_o_search)
async def adm_o_search_res(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): 
        return await msg.answer("❌ ID заказа должен быть числом:")
    async with aiosqlite.connect(DB_PATH) as db:
        o = await (await db.execute("SELECT id, user_id, api_order_id, service_name, link, quantity, cost, status FROM orders WHERE id=? OR api_order_id=?", (msg.text, msg.text))).fetchone()
        
    if not o: 
        return await msg.answer("❌ Заказ не найден.")
    await state.update_data(o_id=o[0], o_u=o[1], o_api=o[2], o_cost=o[6], o_serv=o[3], o_link=o[4], o_qty=o[5])
    
    kb = [
        [InlineKeyboardButton(text="💸 Сделать Манибэк", callback_data="o_refund")],
        [InlineKeyboardButton(text="🚦 Изменить Статус", callback_data="o_status_edit"), InlineKeyboardButton(text="🔄 Перезапустить API", callback_data="o_retry")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_panel")]
    ]
    await msg.answer(f"📦 <b>Заказ #{o[0]} (API: {o[2]})</b>\n\n"
                     f"👤 Юзер ID: <code>{o[1]}</code>\n"
                     f"🏷 Услуга: {o[3]}\n"
                     f"🔗 Ссылка: {o[4]}\n"
                     f"🔢 Кол-во: {o[5]} шт.\n"
                     f"💰 Стоимость: {o[6]}₽\n"
                     f"🚦 Статус: <b>{o[7]}</b>", parse_mode="HTML", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "o_refund")
async def adm_o_refund(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        st = await (await db.execute("SELECT status FROM orders WHERE id=?", (d['o_id'],))).fetchone()
        if st and st[0] == 'Refunded': 
            return await call.answer("❌ Манибэк уже был выполнен!", show_alert=True)
        
        await db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (d['o_cost'], d['o_u']))
        await db.execute("UPDATE orders SET status = 'Refunded' WHERE id = ?", (d['o_id'],))
        await db.commit()
        
    await call.message.edit_text(f"✅ Успешно! {d['o_cost']}₽ возвращены пользователю {d['o_u']} за заказ #{d['o_id']}.")
    try:
        await bot.send_message(d['o_u'], f"💸 <b>Манибэк заказа!</b>\nСумма {d['o_cost']}₽ за заказ #{d['o_id']} была возвращена на баланс.")
    except Exception: 
        pass
    await state.clear()

@router.callback_query(F.data == "o_status_edit")
async def adm_o_status_edit_start(call: CallbackQuery):
    kb = [
        [InlineKeyboardButton(text="Pending", callback_data="st_Pending"), InlineKeyboardButton(text="Processing", callback_data="st_Processing")],
        [InlineKeyboardButton(text="Completed", callback_data="st_Completed"), InlineKeyboardButton(text="Cancelled", callback_data="st_Cancelled")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
    ]
    await call.message.edit_text("выберите новый статус для заказа:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("st_"))
async def adm_o_status_edit_save(call: CallbackQuery, state: FSMContext):
    new_status = call.data.replace("st_", "")
    d = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status=? WHERE id=?", (new_status, d['o_id']))
        await db.commit()
    await call.answer(f"Статус успешно изменен на {new_status}!")
    await call.message.delete()
    await state.clear()

@router.callback_query(F.data == "o_retry")
async def adm_o_retry(call: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await call.message.edit_text("⏳ Повторный запуск через API...")
    res = await api.request({"action": "add", "service": d['o_serv'], "link": d['o_link'], "quantity": d['o_qty']})
    
    if res and "order" in res and int(res.get('order', 0)) > 0:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("UPDATE orders SET api_order_id=?, status='Pending' WHERE id=?", (res['order'], d['o_id']))
            await db.commit()
        await call.message.edit_text(f"✅ Успешно перезапущен! Новый API ID: {res['order']}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В админку", callback_data="admin_panel")]]))
    else:
        err = res.get('error', 'API Reject') if res else 'No Connect'
        await call.message.edit_text(f"❌ Ошибка перезапуска API: {err}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]]))
    await state.clear()

# ⏳ 5. Модуль Управления Очередью Зависших Заказов
@router.callback_query(F.data == "adm_stuck_hub")
async def adm_stuck_hub(call: CallbackQuery):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    async with aiosqlite.connect(DB_PATH) as db:
        stuck = await (await db.execute("SELECT id, user_id, service_id, link, quantity, cost FROM orders WHERE status='Awaiting Refill'")).fetchall()
        
    if not stuck:
        return await call.message.edit_text("⏳ <b>ОЧЕРЕДЬ ЗАВИСШИХ ЗАКАЗОВ</b>\n\nНа данный момент зависших заказов нет. Все заказы успешно ушли на API!", 
                                     parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]]))
                                     
    txt = f"⏳ <b>ОЧЕРЕДЬ ЗАВИСШИХ ЗАКАЗОВ (Всего: {len(stuck)}):</b>\n\n"
    for o in stuck[:10]:
        txt += f"ID: {o[0]} | Юзер: {o[1]} | Сумма: {o[5]}₽ | Линк: {o[3][:25]}...\n"
    if len(stuck) > 10: 
        txt += "...и другие."
    
    kb = [
        [InlineKeyboardButton(text="🔄 Протолкнуть ВСЕ заказы", callback_data="stuck_push_all")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
    ]
    await call.message.edit_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "stuck_push_all")
async def stuck_push_all(call: CallbackQuery):
    await call.message.edit_text("⏳ Запущен процесс проталкивания зависших заказов. Пожалуйста, подождите...")
    async with aiosqlite.connect(DB_PATH) as db:
        stuck = await (await db.execute("SELECT id, user_id, service_id, link, quantity, cost FROM orders WHERE status='Awaiting Refill'")).fetchall()
    
    pushed = 0
    failed = 0
    for o in stuck:
        res = await api.request({"action": "add", "service": o[2], "link": o[3], "quantity": o[4]})
        if res and "order" in res and int(res.get('order', 0)) > 0:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE orders SET api_order_id = ?, status = 'Pending' WHERE id = ?", (res['order'], o[0]))
                await db.commit()
            pushed += 1
            try: 
                await bot.send_message(o[1], f"✅ Ваш зависший заказ №{o[0]} успешно ушел в работу!")
            except Exception: 
                pass
        else: 
            failed += 1
        await asyncio.sleep(0.5)
        
    await call.message.answer(f"🚀 Процесс завершен!\n\nУспешно протолкнуто: {pushed}\nОсталось зависшими: {failed}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]]))

# 🎫 6. Модуль Промокодов (Хаб)
@router.callback_query(F.data == "adm_promo_hub")
async def adm_promo_hub(call: CallbackQuery):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    kb = [
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="promo_create"), InlineKeyboardButton(text="📋 Список активных", callback_data="promo_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
    ]
    await call.message.edit_text("🎫 <b>МЕНЕДЖЕР ПРОМОКОДОВ</b>\nУправление бонусными купонами:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data == "promo_create")
async def promo_create_1(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text("🎫 Введите буквенный код для промокода (символы):")
    await state.set_state(FSM.adm_p_code)

@router.message(FSM.adm_p_code)
async def promo_create_2(msg: Message, state: FSMContext):
    await state.update_data(p_code=msg.text.strip().upper())
    await msg.answer("💰 Введите сумму активации промокода (в рублях):")
    await state.set_state(FSM.adm_p_amt)

@router.message(FSM.adm_p_amt)
async def promo_create_3(msg: Message, state: FSMContext):
    try:
         amt = float(msg.text)
         await state.update_data(p_amt=amt)
         await msg.answer("🔢 Лимит количества активаций (всего):")
         await state.set_state(FSM.adm_p_uses)
    except Exception:
         await msg.answer("❌ Введите число.")

@router.message(FSM.adm_p_uses)
async def promo_create_final(msg: Message, state: FSMContext):
    if not msg.text.isdigit(): 
        return await msg.answer("❌ Введите целое число:")
    d = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO promo VALUES (?,?,?,0)", (d['p_code'], d['p_amt'], int(msg.text)))
        await db.commit()
    await msg.answer(f"✅ Промокод <code>{d['p_code']}</code> на {d['p_amt']}₽ успешно создан!", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="admin_panel")]]))
    await state.clear()

@router.callback_query(F.data == "promo_list")
async def promo_list(call: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        p = await (await db.execute("SELECT code, amount, max_uses, uses FROM promo")).fetchall()
    if not p: 
        return await call.message.edit_text("Нет активных промокодов.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]]))
    
    txt = "🎫 <b>АКТИВНЫЕ ПРОМОКОДЫ В СИСТЕМЕ:</b>\n\n"
    kb = []
    for pr in p:
        txt += f"🔑 Код: <code>{pr[0]}</code> | Номинал: {pr[1]}₽ | Использовано: {pr[3]}/{pr[2]}\n"
        kb.append([InlineKeyboardButton(text=f"❌ Удалить {pr[0]}", callback_data=f"delp_{pr[0]}")])
        
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")])
    await call.message.edit_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("delp_"))
async def promo_del(call: CallbackQuery):
    code = call.data.replace("delp_", "")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM promo WHERE code=?", (code,))
        await db.commit()
    await call.answer(f"Промокод {code} успешно удален!")
    await promo_list(call)

# 📢 7. Модуль Массовой Рассылки
@router.callback_query(F.data == "adm_bc_hub")
async def adm_bc_hub(call: CallbackQuery, state: FSMContext):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    await call.message.edit_text("📢 <b>МАССОВАЯ РАССЫЛКА ПОЛЬЗОВАТЕЛЯМ</b>\n\nНапишите текст вашего сообщения:", 
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]]))
    await state.set_state(FSM.broadcast)

@router.message(FSM.broadcast)
async def adm_bc_exec(msg: Message, state: FSMContext):
    await msg.answer("⏳ Рассылка запущена, пожалуйста подождите...")
    async with aiosqlite.connect(DB_PATH) as db:
        users = await (await db.execute("SELECT user_id FROM users")).fetchall()
        
    sent = 0
    for u in users:
        try:
            await bot.send_message(u[0], msg.text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception: 
            pass
        
    await msg.answer(f"📢 Рассылка успешно завершена!\n\nОтправлено сообщений: {sent} из {len(users)}.")
    await state.clear()

# ⚙️ 8. Настройки Системы (Хаб)
@router.callback_query(F.data == "adm_st_hub")
async def adm_st_hub(call: CallbackQuery):
    role = await check_access(call)
    if role not in ['admin', 'owner']: 
        return
    api_key = await get_conf("api_key")
    margin = await get_conf("margin")
    wallet = await get_conf("wallet")
    support = await get_conf("support_link")
    min_cost = await get_conf("min_cost") or str(MIN_ORDER_COST)
    
    kb = [
        [InlineKeyboardButton(text="🔑 API Ключ", callback_data="set_api_key"), InlineKeyboardButton(text="📈 Наценка (коэфф.)", callback_data="set_margin")],
        [InlineKeyboardButton(text="👛 ЮMoney кошелек", callback_data="set_wallet"), InlineKeyboardButton(text="🔐 ЮMoney токен", callback_data="set_yoo_token")],
        [InlineKeyboardButton(text="🆘 Линк поддержки", callback_data="set_support_link"), InlineKeyboardButton(text="💵 Минималка заказа", callback_data="set_min_cost")],
        [InlineKeyboardButton(text="⚠️ Режим тех. работ", callback_data="adm_maint")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel")]
    ]
    await call.message.edit_text(f"⚙️ <b>СИСТЕМНЫЕ НАСТРОЙКИ:</b>\n\n"
                                 f"🔑 SMM API Key: <code>{api_key[:4]}...{api_key[-4:] if len(api_key)>8 else ''}</code>\n"
                                 f"📈 Наценка: <b>{margin}x</b>\n"
                                 f"👛 Кошелек ЮMoney: <code>{wallet}</code>\n"
                                 f"💵 Минимальная цена заказа: <b>{min_cost}₽</b>\n"
                                 f"🆘 Ссылка на саппорт: {support}", 
                                 parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("set_"))
async def adm_st_start_edit(call: CallbackQuery, state: FSMContext):
    key = call.data.replace("set_", "")
    await state.update_data(editing_key=key)
    await call.message.edit_text(f"Введите новое значение для параметра <code>{key}</code>:", parse_mode="HTML")
    await state.set_state(FSM.adm_st_edit)

@router.message(FSM.adm_st_edit)
async def adm_st_save(msg: Message, state: FSMContext):
    d = await state.get_data()
    key = d['editing_key']
    val = msg.text.strip()
    
    if key == 'margin' or key == 'min_cost':
        try: 
            float(val)
        except Exception: 
            return await msg.answer("❌ Ошибка ввода. Значение должно быть числом:")
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET value=? WHERE key=?", (val, key))
        await db.commit()
    await msg.answer("✅ Настройка сохранена!")
    await state.clear()
    await adm_st_hub(msg)

@router.callback_query(F.data == "adm_maint")
async def adm_maint_toggle(call: CallbackQuery):
    current_maint_status = await get_conf("maintenance")
    new_maint_status = "1" if current_maint_status == "0" else "0"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE settings SET value=? WHERE key='maintenance'", (new_maint_status,))
        await db.commit()
    await call.answer(f"Тех работы: {'ВКЛЮЧЕНЫ' if new_maint_status == '1' else 'ВЫКЛЮЧЕНЫ'}", show_alert=True)
    await adm_st_hub(call)


# 💾 9. Утилита выгрузки базы данных
@router.callback_query(F.data == "adm_db_hub")
async def adm_db_hub(call: CallbackQuery):
    role = await check_access(call)
    if role != 'owner': 
        return await call.answer("Только Создатель имеет доступ к файлу БД!", show_alert=True)
    await call.message.answer_document(FSInputFile(DB_PATH), caption="💾 Файл базы данных SQLite.")


# =====================================================================
# 🚀 СТАРТ И ОБЩИЕ КОМАНДЫ
# =====================================================================

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    
    role = await check_access(msg)
    if not role: 
        return
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (msg.from_user.id, msg.from_user.username))
        await db.execute("UPDATE users SET username=? WHERE user_id=?", (msg.from_user.username, msg.from_user.id))
        await db.commit()
        
    await msg.answer("🔥 <b>Booster Bot</b> — Сервис продвижения №1!\n\nИспользуйте меню для работы:", parse_mode="HTML", reply_markup=await main_kb(role))

# =====================================================================
# 🏁 ГЛАВНЫЙ ЗАПУСК
# =====================================================================
async def main():
    # 1. Запуск Flask-сервера в параллельном потоке, чтобы Render не усыплял бота
    logging.info("Starting Flask server for Render...")
    Thread(target=run_flask_app, daemon=True).start()
    
    # 2. Инициализация базы данных и старт бота в Telegram
    await init_db()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    
    logging.info("Starting Telegram bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
