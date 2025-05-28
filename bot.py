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
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.utils import executor
import uvicorn

load_dotenv()

BOT_TOKEN           = os.getenv('TELEGRAM_TOKEN')
WFP_ACCOUNT         = os.getenv('WFP_MERCHANT_ACCOUNT')
WFP_SECRET          = os.getenv('WFP_SECRET_KEY')
WFP_DOMAIN          = os.getenv('WFP_DOMAIN')
WFP_SERVICE_URL     = os.getenv('WFP_CALLBACK_URL')
RETURN_URL          = os.getenv('WFP_RETURN_URL')  # куди користувач вернеться після оплати

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())
app = FastAPI()

USERS_FILE = 'users.json'

# ======================
# БЛОК 2: Стани FSM
# ======================
class Session(StatesGroup):
    waiting_amount = State()

# ====================================
# БЛОК 3: Робота з JSON (storage)
# ====================================
def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def ensure_user(user_id, referrer=None):
    data = load_users()
    uid = str(user_id)
    if uid not in data:
        data[uid] = {
            'balance': 0,
            'referral_link': f"{WFP_DOMAIN}/?ref={uid}",
            'referrer': referrer or ''
        }
        save_users(data)
    return data[uid]

def change_balance(user_id, amount):
    data = load_users()
    uid = str(user_id)
    data[uid]['balance'] += amount
    save_users(data)

# ============================================
# БЛОК 4: Генерація WayForPay-підписів & URL
# ============================================
def make_signature(fields: list) -> str:
    """HMAC_MD5 від конкатенації полів через ';'"""
    data = ';'.join(str(f) for f in fields)
    return hmac.new(WFP_SECRET.encode(), data.encode('utf-8'), hashlib.md5).hexdigest()

def create_payment_link(user_id: int, amount: float):
    order_ref   = str(uuid4())
    order_date  = int(time.time())
    # формуємо параметри
    params = {
        'merchantAccount':       WFP_ACCOUNT,
        'merchantDomainName':    WFP_DOMAIN,
        'orderReference':        order_ref,
        'orderDate':             order_date,
        'amount':                amount,
        'currency':              'UAH',
        'productName[]':         ['Top-up balance'],
        'productCount[]':        [1],
        'productPrice[]':        [amount],
        'serviceUrl':            WFP_SERVICE_URL,
        'returnUrl':             RETURN_URL
    }
    # підпис
    signature = make_signature([
        params['merchantAccount'], params['merchantDomainName'],
        params['orderReference'], params['orderDate'],
        params['amount'], params['currency'],
        *params['productName[]'], *params['productCount[]'], *params['productPrice[]']
    ])
    params['merchantSignature'] = signature
    # генеруємо HTML-форму
    inputs = '\n'.join(
        f'<input type="hidden" name="{k}" value="{v}"/>' for k, v in params.items()
    )
    form = f"""
    <html><body onload="document.forms[0].submit()">
      <form method="post" action="https://secure.wayforpay.com/pay" accept-charset="utf-8">
        {inputs}
      </form>
    </body></html>
    """
    return form

# =============================================
# БЛОК 5: FastAPI-ендпоінт для serviceUrl
# =============================================
@app.post("/wfp-callback")
async def wfp_callback(req: Request):
    data = await req.json()
    # перевірка підпису від WayForPay
    signature = make_signature([
        data['merchantAccount'], data['orderReference'],
        data['amount'], data['currency'],
        data['authCode'], data['cardPan'],
        data['transactionStatus'], data['reasonCode']
    ])
    if signature != data.get('merchantSignature') or data['transactionStatus'] != 'Approved':
        return {'orderReference': data['orderReference'], 'status': 'reject', 'time': int(time.time()), 'signature': make_signature([data['orderReference'], 'reject', int(time.time())])}
    # успішний платіж
    # знаходимо user_id за orderReference — для простоти припускаємо, що orderReference == user_id
    user_id = int(data['orderReference'].split('-')[0])  # або інша логіка
    amount = float(data['amount'])
    # нарахування користувачу
    change_balance(user_id, amount)
    # бонус рефереру
    users = load_users()
    ref = users[str(user_id)]['referrer']
    if ref:
        bonus = amount * 0.1
        change_balance(int(ref), bonus)
    return {'orderReference': data['orderReference'], 'status': 'accept', 'time': int(time.time()), 'signature': make_signature([data['orderReference'], 'accept', int(time.time())])}

# ======================================
# БЛОК 6: Клавіатури меню
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
    # перевіримо, чи є реферер
    ref = None
    if msg.get_args().startswith("ref="):
        ref = msg.get_args().split("ref=")[1]
    ensure_user(msg.from_user.id, referrer=ref)
    await msg.answer("Ласкаво просимо! Оберіть дію:", reply_markup=kb_main())

@dp.message_handler(lambda m: m.text == "💰 Мій баланс")
async def show_balance(msg: types.Message):
    user = load_users()[str(msg.from_user.id)]
    await msg.answer(f"Ваш баланс: {user['balance']} грн", reply_markup=kb_balance())

@dp.message_handler(lambda m: m.text == "🔙 Назад")
async def back_to_main(msg: types.Message):
    await msg.answer("Головне меню:", reply_markup=kb_main())

@dp.message_handler(lambda m: m.text == "➕ Поповнити баланс")
async def topup_start(msg: types.Message):
    await Session.waiting_amount.set()
    await msg.answer("Введіть суму для поповнення (UAH):", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(state=Session.waiting_amount)
async def process_amount(msg: types.Message, state: FSMContext):
    try:
        amount = float(msg.text)
    except ValueError:
        return await msg.answer("Будь ласка, введіть коректну числову суму.")
    # згенеруємо форму і відправимо
    html = create_payment_link(msg.from_user.id, amount)
    await msg.answer("Перейдіть за посиланням для оплати:", reply_markup=kb_back())
    await msg.answer(html, parse_mode='HTML')
    await state.finish()

@dp.message_handler(lambda m: m.text == "🤝 Реферальна програма")
async def referral(msg: types.Message):
    user = load_users()[str(msg.from_user.id)]
    link = user['referral_link']
    text = (
        "Ваша реферальна програма:\n"
        f"{link}\n\n"
        "Запрошуйте друзів — отримуйте 10% від кожного їх поповнення!"
    )
    await msg.answer(text, reply_markup=kb_main())

@dp.message_handler(lambda m: m.text == "🔙 Повернутися в меню")
async def back_from_payment(msg: types.Message):
    await msg.answer("Головне меню:", reply_markup=kb_main())

# ===========================================
# БЛОК 8: Запуск Aiogram і FastAPI разом
# ===========================================
if __name__ == "__main__":
    # одночасно запускаємо FastAPI (uvicorn) і Aiogram
    import threading
    def run_api():
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
    threading.Thread(target=run_api).start()
    executor.start_polling(dp, skip_updates=True)
