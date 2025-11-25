import os
import asyncio
import json
import time
import secrets
import hashlib

from aiohttp import web, client
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command


bot_token = os.getenv("bot_token")
bot_username = os.getenv("bot_username", "")

if not bot_token:
  raise RuntimeError("bot_token is not set")

dp = Dispatcher()
tg_bot = Bot(token=bot_token)

users_path = "users.json"
items_path = "items.json"

lock = asyncio.Lock()


def load_json(path, default):
  if not os.path.exists(path):
    return default
  try:
    with open(path, "r", encoding="utf-8") as f:
      return json.load(f)
  except Exception:
    return default


def save_json(path, data):
  with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)


users = load_json(users_path, [])
items = load_json(items_path, [])


def hash_password(password, salt):
  s = (salt + password).encode("utf-8")
  return hashlib.sha256(s).hexdigest()


def find_user_by_login(tg_username):
  for u in users:
    if u["tg_login"].lower() == tg_username.lower():
      return u
  return None


def find_user_by_token(token):
  for u in users:
    if u.get("token") == token:
      return u
  return None


def find_user_by_chat(chat_id):
  for u in users:
    if u.get("tg_chat_id") == chat_id:
      return u
  return None


def now_sec():
  return int(time.time())


async def fetch_price(appid, hash_name, currency_code):
  url = (
    "https://steamcommunity.com/market/priceoverview/"
    f"?appid={appid}&market_hash_name={hash_name}"
    f"&currency={currency_code}&format=json"
  )

  async with client.ClientSession() as session:
    async with session.get(url) as r:
      j = await r.json()

  if not j or not j.get("success"):
    return None

  price_str = j.get("lowest_price") or j.get("median_price")
  if not price_str:
    return None

  cleaned = (
    price_str
      .replace(" ", "")
      .replace("\xa0", "")
      .replace(",", ".")
  )

  digits = "".join(ch for ch in cleaned if ch.isdigit() or ch == ".")
  try:
    return float(digits)
  except Exception:
    return None


def make_item_id(appid, hash_name, user_token):
  return f"{user_token}|{appid}|{hash_name}"


@web.middleware
async def cors_middleware(request, handler):
  if request.method == "OPTIONS":
    resp = web.Response(text="ok")
  else:
    resp = await handler(request)

  resp.headers["Access-Control-Allow-Origin"] = "*"
  resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
  resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
  return resp


# ---------- TELEGRAM /start ----------

@dp.message(CommandStart())
async def start_handler(message: types.Message):
  args = message.text.split(maxsplit=1)
  code = args[1].strip() if len(args) == 2 else None

  if not code:
    await message.answer(
      "Hi! Press Start using the link from the browser extension 🙂"
    )
    return

  async with lock:
    user = None
    for u in users:
      if u.get("pair_code") == code and not u.get("tg_chat_id"):
        user = u
        break

    if not user:
      await message.answer("Code is invalid or already used.")
      return

    user["tg_chat_id"] = message.chat.id
    user["tg_username_real"] = (
      "@" + message.from_user.username
      if message.from_user.username else user["tg_login"]
    )
    user["pair_code"] = None

    save_json(users_path, users)

  await message.answer(
    "✅ Steam Track n Buy is now connected.\n"
    "All alerts will be sent here."
  )


# ---------- TELEGRAM /items /list ----------

@dp.message(Command("items"))
@dp.message(Command("list"))
async def list_items_handler(message: types.Message):
  chat_id = message.chat.id

  async with lock:
    user = find_user_by_chat(chat_id)

    if not user:
      await message.answer(
        "First link Telegram via the browser extension (Connect Telegram)."
      )
      return

    user_token = user["token"]
    my_items = [it for it in items if it["user_token"] == user_token]
    cur = user["settings"]["currency_label"]

  if len(my_items) == 0:
    await message.answer("You don't have any tracked items yet.")
    return

  lines = []
  for i, it in enumerate(my_items, start=1):
    last_price = it.get("last_price")
    last_price_str = "?" if last_price is None else f"{last_price} {cur}"

    direction = it.get("direction")
    dir_str = (
      "waiting for price drop (buy)"
      if direction == "buy"
      else "waiting for price rise (sell)"
    )

    lines.append(
      f"{i}. {it['hash_name']}\n"
      f"   now: {last_price_str}\n"
      f"   target: {it['target_price']} {cur}\n"
      f"   mode: {dir_str}"
    )

  text = "📌 Your tracked items:\n\n" + "\n\n".join(lines)
  await message.answer(text)


# ---------- HTTP API ----------

async def healthz(request):
  return web.Response(text="ok")


# registration: tg_username + password
async def api_register(request):
  data = await request.json()

  tg_username = (data.get("tg_username") or "").strip()
  password = (data.get("password") or "").strip()

  if len(tg_username) < 2 or len(password) < 4 or not tg_username.startswith("@"):
    return web.json_response({ "ok": False, "error": "bad_input" })

  async with lock:
    if find_user_by_login(tg_username):
      return web.json_response({ "ok": False, "error": "exists" })

    salt = secrets.token_hex(8)
    pw_hash = hash_password(password, salt)
    token = secrets.token_hex(16)

    users.append({
      "tg_login": tg_username,
      "tg_username_real": None,
      "salt": salt,
      "pw_hash": pw_hash,
      "token": token,
      "tg_chat_id": None,
      "pair_code": None,
      "settings": {
        "currency_label": "RUB",
        "currency_code": 5,
        "language": "en",
        "interval_min": 10
      }
    })

    save_json(users_path, users)

  return web.json_response({ "ok": True })


# login: tg_username + password
async def api_login(request):
  data = await request.json()

  tg_username = (data.get("tg_username") or "").strip()
  password = (data.get("password") or "").strip()

  async with lock:
    user = find_user_by_login(tg_username)

    if not user:
      return web.json_response({ "ok": False, "error": "not_found" })

    pw_hash = hash_password(password, user["salt"])
    if pw_hash != user["pw_hash"]:
      return web.json_response({ "ok": False, "error": "wrong_pass" })

    if not user.get("token"):
      user["token"] = secrets.token_hex(16)
      save_json(users_path, users)

  return web.json_response({
    "ok": True,
    "token": user["token"]
  })


async def api_state(request):
  token = request.query.get("token")

  async with lock:
    user = find_user_by_token(token)
    if not user:
      return web.json_response({ "ok": False, "error": "no_auth" })

    user_token = user["token"]
    my_items = [it for it in items if it["user_token"] == user_token]

    return web.json_response({
      "ok": True,
      "tg_username": user.get("tg_username_real") or user.get("tg_login"),
      "settings": user["settings"],
      "items": my_items,
      "bot_username": bot_username,
      "tg_connected": bool(user.get("tg_chat_id"))
    })


async def api_pair_start(request):
  data = await request.json()
  token = data.get("token")

  async with lock:
    user = find_user_by_token(token)
    if not user:
      return web.json_response({ "ok": False, "error": "no_auth" })

    code = str(secrets.randbelow(900000) + 100000)
    user["pair_code"] = code
    save_json(users_path, users)

  return web.json_response({
    "ok": True,
    "code": code,
    "bot_username": bot_username
  })


async def api_settings(request):
  data = await request.json()
  token = data.get("token")

  async with lock:
    user = find_user_by_token(token)
    if not user:
      return web.json_response({ "ok": False, "error": "no_auth" })

    settings = user["settings"]

    if data.get("currency_label") in ["RUB", "USD"]:
      settings["currency_label"] = data["currency_label"]
      settings["currency_code"] = 5 if data["currency_label"] == "RUB" else 1

    interval_min = data.get("interval_min")
    try:
      interval = int(interval_min)
    except (TypeError, ValueError):
      interval = settings.get("interval_min", 10)

    if interval < 1:
      interval = 1

    settings["interval_min"] = interval
    settings["language"] = "en"

    save_json(users_path, users)

  return web.json_response({ "ok": True })


async def api_use(request):
  data = await request.json()
  token = data.get("token")
  action = (data.get("action") or "use").strip()

  async with lock:
    user = find_user_by_token(token)
    if not user:
      return web.json_response({ "ok": False })

    chat_id = user.get("tg_chat_id")

  if chat_id:
    text = f"🟦 Steam Track n Buy used: {action}"
    await tg_bot.send_message(chat_id, text)

  return web.json_response({ "ok": True })


async def api_track(request):
  data = await request.json()
  token = data.get("token")
  appid = data.get("appid")
  hash_name = data.get("hash_name")
  target_price = data.get("target_price")

  async with lock:
    user = find_user_by_token(token)
    if not user:
      return web.json_response({ "ok": False, "error": "no_auth" })

    settings = user["settings"]
    chat_id = user.get("tg_chat_id")
    user_token = user["token"]

  if not appid or not hash_name or not target_price:
    return web.json_response({ "ok": False, "error": "bad_input" })

  current_price = await fetch_price(appid, hash_name, settings["currency_code"])
  if current_price is None:
    current_price = 0.0

  direction = "buy"
  if target_price > current_price:
    direction = "sell"

  item_id = make_item_id(appid, hash_name, user_token)

  async with lock:
    exist = None
    for it in items:
      if it["id"] == item_id:
        exist = it
        break

    if exist:
      exist["target_price"] = target_price
      exist["direction"] = direction
      exist["enabled"] = True
    else:
      items.append({
        "id": item_id,
        "user_token": user_token,
        "appid": appid,
        "hash_name": hash_name,
        "target_price": target_price,
        "direction": direction,
        "enabled": True,
        "last_price": current_price,
        "last_checked_at": now_sec(),
        "last_notified_at": 0
      })

    save_json(items_path, items)

  if chat_id:
    cur = settings["currency_label"]
    advise = (
      "target is below current price — I will wait for the price to drop."
      if direction == "buy"
      else "target is above current price — I will wait for the price to rise."
    )

    text = (
      "✅ Added to tracking:\n"
      f"[{hash_name}]\n"
      f"current price: {current_price} {cur}\n"
      f"target: {target_price} {cur}\n"
      f"{advise}"
    )
    await tg_bot.send_message(chat_id, text)

  return web.json_response({
    "ok": True,
    "current_price": current_price,
    "direction": direction
  })


async def api_untrack(request):
  data = await request.json()
  token = data.get("token")
  item_id = data.get("item_id")

  async with lock:
    user = find_user_by_token(token)
    if not user:
      return web.json_response({ "ok": False, "error": "no_auth" })

    user_token = user["token"]

    new_items = []
    for it in items:
      if it["id"] == item_id and it["user_token"] == user_token:
        continue
      new_items.append(it)

    items.clear()
    items.extend(new_items)

    save_json(items_path, items)

  return web.json_response({ "ok": True })


# ---------- price polling ----------

async def polling_loop():
  while True:
    await asyncio.sleep(3)

    async with lock:
      local_users = list(users)
      local_items = list(items)

    interval_min = 10
    for u in local_users:
      interval_min = min(interval_min, u["settings"].get("interval_min", 10))

    now = now_sec()

    for it in local_items:
      if not it.get("enabled"):
        continue

      user = None
      for u in local_users:
        if u["token"] == it["user_token"]:
          user = u
          break

      if not user or not user.get("tg_chat_id"):
        continue

      settings = user["settings"]

      if now - it.get("last_checked_at", 0) < interval_min * 60:
        continue

      price_now = await fetch_price(
        it["appid"],
        it["hash_name"],
        settings["currency_code"]
      )

      it["last_checked_at"] = now
      it["last_price"] = price_now

      if price_now is None:
        continue

      cooldown = 3600
      if now - it.get("last_notified_at", 0) < cooldown:
        continue

      direction = it["direction"]
      target = it["target_price"]
      cur = settings["currency_label"]

      if direction == "buy" and price_now <= target:
        it["last_notified_at"] = now
        text = (
          f"[{it['hash_name']}] is now selling for "
          f"[{price_now} {cur}] — hurry up and buy!"
        )
        await tg_bot.send_message(user["tg_chat_id"], text)

      if direction == "sell" and price_now >= target:
        it["last_notified_at"] = now
        text = (
          f"[{it['hash_name']}] just went up to "
          f"[{price_now} {cur}] — hurry up and sell!"
        )
        await tg_bot.send_message(user["tg_chat_id"], text)

    async with lock:
      id_to_item = {it["id"]: it for it in local_items}
      for i in range(len(items)):
        old_id = items[i]["id"]
        if old_id in id_to_item:
          items[i] = id_to_item[old_id]
      save_json(items_path, items)


async def main():
  app = web.Application(middlewares=[cors_middleware])

  app.router.add_get("/healthz", healthz)
  app.router.add_route("OPTIONS", "/{tail:.*}", healthz)

  app.router.add_post("/api/register", api_register)
  app.router.add_post("/api/login", api_login)
  app.router.add_get("/api/state", api_state)
  app.router.add_post("/api/pair/start", api_pair_start)
  app.router.add_post("/api/settings", api_settings)
  app.router.add_post("/api/use", api_use)
  app.router.add_post("/api/track", api_track)
  app.router.add_post("/api/untrack", api_untrack)

  runner = web.AppRunner(app)
  await runner.setup()

  port = int(os.getenv("PORT", "10000"))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()

  asyncio.create_task(polling_loop())
  await dp.start_polling(tg_bot)


if __name__ == "__main__":
  asyncio.run(main())
