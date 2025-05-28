# bot.py

# ==============================
# БЛОК 1: Імпорти та конфігурація
# ==============================
import os
import json
import time
import hmac
import hashlib
from uuid import uuid4
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
)
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
import uvicorn

load_dotenv()

# Telegram
BOT_TOKEN = os.getenv('TELEGRAM_TOKEN')

# WayForPay & ваш домен
WFP_ACCOUNT     = os.getenv('WFP_MERCHANT_ACCOUNT')
WFP_SECRET      = os.getenv('WFP_SECRET_KEY')
WFP_DOMAIN      = os.getenv('WFP_DOMAIN')            # https://aiphoto-bot.onrender.com
WFP_CALLBACK    = os.getenv('WFP_CALLBACK_URL')      # https://…/wfp-callback
RETURN_URL      = os.getenv('WFP_RETURN_URL')        # https://…/return

# Зберігання
USERS_FILE    = os.getenv('USERS_FILE_PATH',    '/data/users.json')
PAYMENTS_FILE = os.getenv('PAYMENTS_FILE_PATH', '/data/payments.json')

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(bot, storage=MemoryStorage())
app = FastAPI()

# ======================
# БЛОК 2: FSM-стани
# ======================
class Session(StatesGroup):
    waiting_amount = State()

# ====================================
# БЛОК 3: Робота з JSON (users + payments)
# ====================================
def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# — users.json —
def ensure_user(uid, referrer=None):
    users = load_json(USERS_FILE)
    if uid not in users:
        users[uid] = {
            'balance': 0,
            'referral_link': f"{WFP_DOMAIN}/?ref={uid}",
            'referrer': referrer or ''
        }
        save_json(USERS_FILE, users)
    return users[uid]

def change_balance(uid, delta):
    users = load_json(USERS_FILE)
    users[uid]['balance'] += delta
    save_json(USERS_FILE, users)

# — payments.json —
def record_payment(order_ref, uid, amount):
    pays = load_json(PAYMENTS_FILE)
    pays[order_ref] = {'user_id': uid, 'amount': amount}
    save_json(PAYMENTS_FILE, pays)

def pop_payment(order_ref):
    pays = load_json(PAYMENTS_FILE)
    rec = pays.pop(order_ref, None)
    save_json(PAYMENTS_FILE, pays)
    return rec

# ============================================
# БЛОК 4: WayForPay-підписи
# ============================================
def make_signature(fields: list) -> str:
    data = ';'.join(str(f) for f in fields)
    return hmac.new(WFP_SECRET.encode(), data.encode('utf-8'), hashlib.md5).hexdigest()

# =============================================
# БЛОК 5: FastAPI-ендпоінти
# =============================================
@app.get("/pay", response_class=HTMLResponse)
async def pay_page(order_ref: str, amount: float):
    """Сторінка WebApp: автосабміт форми WayForPay"""
    params = {
        'merchantAccount':    WFP_ACCOUNT,
        'merchantDomainName': WFP_DOMAIN,
        'orderReference':     order_ref,
        'orderDate':          int(time.time()),
        'amount':             amount,
        'currency':           'UAH',
        'productName[]':      ['Top-up'],
        'productCount[]':     [1],
        'productPrice[]':     [amount],
        'serviceUrl':         WFP_CALLBACK,
        'returnUrl':          RETURN_URL
    }
    sig = make_signature([
        params['merchantAccount'], params['merchantDomainName'],
        params['orderReference'], params['orderDate'],
        params['amount'], params['currency'],
        *params['productName[]'],
        *params['productCount[]'],
        *params['productPrice[]']
    ])
    params['merchantSignature'] = sig

    inputs = "\n".join(
        f'<input type="hidden" name="{k}" value="{v}"/>' for k,v in params.items()
    )
    html = f"""
    <html><body onload="document.forms[0].submit()">
      <form method="post" action="https://secure.wayforpay.com/pay" accept-charset="utf-8">
        {inputs}
      </form>
    </body></html>
    """
    return html

@app.post("/wfp-callback")
async def wfp_callback(req: Request):
    data = await req.json()
    # перевірка підпису
    sig = make_signature([
        data['merchantAccount'], data['orderReference'],
        data['amount'], data['currency'],
        data['authCode'], data['cardPan'],
        data['transactionStatus'], data['reasonCode']
    ])
    ok = sig == data.get('merchantSignature') and data['transactionStatus']=='Approved'
    status = 'accept' if ok else 'reject'
    # формуємо відповідь WayForPay
    answer = {
        'orderReference': data['orderReference'],
        'status': status,
        'time': int(time.time()),
        'signature': make_signature([data['orderReference'], status, int(time.time())])
    }

    if not ok:
        return answer

    # успішно: знаходимо замовлення
    rec = pop_payment(data['orderReference'])
    if not rec:
        return answer  # замовлення не знайдено

    uid    = str(rec['user_id'])
    amount = float(rec['amount'])
    # нараховуємо баланс
    change_balance(uid, amount)
    # бонус рефереру
    users = load_json(USERS_FILE)
    ref = users[uid]['referrer']
    if ref:
        change_balance(str(ref), amount * 0.1)

    return answer

# ======================================
# БЛОК 6: Клавіатури
# ======================================
def kb_main():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("📸 Почати фотосесію"))
    kb.add(KeyboardButton("💰 Мій баланс"))
    kb.add(KeyboardButton("🤝 Реферальна програма"))
    return kb

def kb_balance():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("➕ Поповнити баланс"))
    kb.add(KeyboardButton("🔙 Назад"))
    return kb

def kb_back():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("🔙 Повернутися в меню"))
    return kb

# =====================================
# БЛОК 7: Telegram-хендлери
# =====================================
@dp.message_handler(commands=['start'])
async def cmd_start(msg: types.Message):
    ref = None
    if msg.get_args().startswith("ref="):
        ref = msg.get_args().split("ref=")[1]
    ensure_user(str(msg.from_user.id), referrer=ref)
    await msg.answer("Ласкаво просимо! Оберіть дію:", reply_markup=kb_main())

@dp.message_handler(lambda m: m.text=="💰 Мій баланс")
async def show_balance(msg: types.Message):
    u = load_json(USERS_FILE)[str(msg.from_user.id)]
    await msg.answer(f"Ваш баланс: {u['balance']} грн", reply_markup=kb_balance())

@dp.message_handler(lambda m: m.text=="🔙 Назад")
async def back_to_main(msg: types.Message):
    await msg.answer("Головне меню:", reply_markup=kb_main())

@dp.message_handler(lambda m: m.text=="➕ Поповнити баланс")
async def topup_start(msg: types.Message):
    await Session.waiting_amount.set()
    await msg.answer("Введіть суму для поповнення (UAH):", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=Session.waiting_amount)
async def process_amount(msg: types.Message, state: FSMContext):
    try:
        amount = float(msg.text)
    except ValueError:
        return await msg.answer("Будь ласка, введіть коректну суму.")
    order_ref = str(uuid4())
    # зберігаємо замовлення
    record_payment(order_ref, msg.from_user.id, amount)
    # лінк на WebApp
    wa_url = f"{WFP_DOMAIN}/pay?order_ref={order_ref}&amount={amount}"
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("Оплатити баланс", web_app=WebAppInfo(url=wa_url))
    )
    await msg.answer("Натисніть кнопку, щоб відкрити оплату:", reply_markup=kb)
    await state.finish()

@dp.message_handler(lambda m: m.text=="🤝 Реферальна програма")
async def referral(msg: types.Message):
    u = load_json(USERS_FILE)[str(msg.from_user.id)]
    await msg.answer(
        f"Ваша реферальна програма:\n{u['referral_link']}\n\n"
        "Запрошуйте друзів — отримуйте 10% від кожного їх поповнення!",
        reply_markup=kb_main()
    )

@dp.message_handler(lambda m: m.text=="🔙 Повернутися в меню")
async def back_from_payment(msg: types.Message):
    await msg.answer("Головне меню:", reply_markup=kb_main())

# ===========================================
# БЛОК 8: Запуск Aiogram + FastAPI
# ===========================================
if __name__ == "__main__":
    import threading
    def run_api():
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
    threading.Thread(target=run_api).start()
    executor.start_polling(dp, skip_updates=True)
