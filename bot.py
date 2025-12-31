import os
import asyncio
import json
import time
import secrets
import hashlib
from urllib.parse import urlencode, quote
import re
from aiohttp import web, client
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command


bot_token = os.getenv("bot_token")
bot_username = os.getenv("bot_username", "")

if not bot_token:
  raise RuntimeError("bot_token is not set")

dp = Dispatcher()
tg_bot = Bot(token=bot_token)

base_dir = os.path.dirname(os.path.abspath(__file__))
users_path = os.path.join(base_dir, "users.json")
items_path = os.path.join(base_dir, "items.json")

lock = asyncio.Lock()


def load_json(path, default):
  if not os.path.exists(path):
    save_json(path, default)
    return default
  try:
    with open(path, "r", encoding="utf-8") as f:
      return json.load(f)
  except Exception:
    # если файл битый — сохраним backup и создадим новый
    try:
      bad_path = path + f".bad-{int(time.time())}"
      os.replace(path, bad_path)
    except Exception:
      pass
    save_json(path, default)
    return default


def save_json(path, data):
  tmp_path = path + ".tmp"

  try:
    payload = json.dumps(data, ensure_ascii=False, indent=2)

    print("save_json begin")
    print("save_json path:", path)
    print("save_json tmp:", tmp_path)
    print("save_json bytes:", len(payload))

    with open(tmp_path, "w", encoding="utf-8") as f:
      f.write(payload)
      f.flush()
      os.fsync(f.fileno())

    os.replace(tmp_path, path)

    size = os.path.getsize(path)
    print("save_json ok")
    print("save_json size:", size)

  except Exception as e:
    print("save_json fail:", repr(e))
    raise




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


steam_timeout = client.ClientTimeout(total=10)
steam_base_headers = {
  "user-agent": "steam-track-n-buy/1.0",
  "accept": "application/json,text/plain,*/*",
  "accept-language": "en-US,en;q=0.9"
}
steam_session = None


async def get_steam_session():
  global steam_session
  if steam_session is None or steam_session.closed:
    steam_session = client.ClientSession(timeout=steam_timeout, headers=steam_base_headers)
  return steam_session


def parse_price_str(price_str):
  s = (
    str(price_str)
      .replace(" ", "")
      .replace("\xa0", "")
  )
  s = "".join(ch for ch in s if ch.isdigit() or ch in ".,")
  if not s:
    return None

  last_dot = s.rfind(".")
  last_comma = s.rfind(",")

  if last_dot != -1 and last_comma != -1:
    if last_comma > last_dot:
      dec = ","
      thou = "."
    else:
      dec = "."
      thou = ","
    s = s.replace(thou, "")
    s = s.replace(dec, ".")
  else:
    sep = "." if last_dot != -1 else ("," if last_comma != -1 else None)
    if sep:
      parts = s.split(sep)
      if len(parts) > 2:
        decimal = parts[-1]
        int_part = "".join(parts[:-1])
        s = int_part + "." + decimal
      else:
        s = s.replace(sep, ".")

  try:
    return float(s)
  except Exception:
    return None

def parse_price(price_text):
  if price_text is None:
    return None

  text = str(price_text)
  text = text.replace("\xa0", " ")
  text = text.strip()

  if text == "":
    return None

  m = re.search(r"\d[\d\s\.,]*\d", text)
  if not m:
    return None

  cleaned = m.group(0)
  cleaned = cleaned.replace(" ", "")

  has_comma = "," in cleaned
  has_dot = "." in cleaned

  if has_comma and has_dot:
    last_comma = cleaned.rfind(",")
    last_dot = cleaned.rfind(".")

    if last_comma > last_dot:
      cleaned = cleaned.replace(".", "")
      cleaned = cleaned.replace(",", ".")
    else:
      cleaned = cleaned.replace(",", "")

  elif has_comma and not has_dot:
    cleaned = cleaned.replace(",", ".")

  try:
    return float(cleaned)
  except Exception:
    return None


async def fetch_price(appid, hash_name, currency_code):
  params = {
    "appid": str(appid),
    "market_hash_name": hash_name,
    "currency": str(currency_code),
    "format": "json"
  }

  url = "https://steamcommunity.com/market/priceoverview/?" + urlencode(
    params,
    quote_via=quote,
    safe=""
  )
  print("price fetch url:", url)

  headers = {
    "user-agent": "steam-track-n-buy/1.0",
    "accept": "application/json,text/plain,*/*",
    "accept-language": "en-US,en;q=0.9,ru;q=0.8",
    "referer": "https://steamcommunity.com/market/"
  }

  timeout = client.ClientTimeout(total=12)

  async with client.ClientSession(timeout=timeout) as session:
    for attempt in range(3):
      try:
        async with session.get(url, headers=headers) as r:
          body_text = await r.text()

          if r.status == 429:
            if attempt < 2:
              wait_sec = 1 + attempt
              print("price fetch rate limited:", r.status, "wait:", wait_sec)
              await asyncio.sleep(wait_sec)
              continue

          if r.status != 200:
            print("price fetch bad status:", r.status)
            print("price fetch body:", body_text[:300])
            return None

      except Exception as e:
        if attempt < 2:
          wait_sec = 1 + attempt
          print("price fetch error retry:", e, "wait:", wait_sec)
          await asyncio.sleep(wait_sec)
          continue

        print("price fetch error:", e)
        return None

      try:
        data = json.loads(body_text)
      except Exception as e:
        print("price fetch json parse error:", e)
        print("price fetch body:", body_text[:300])
        return None

      print("price fetch raw data:", data)

      if not isinstance(data, dict):
        print("price fetch unexpected type:", type(data))
        return None

      if not data.get("success"):
        print("price fetch not success:", data)
        return None

      price_text = data.get("lowest_price") or data.get("median_price")
      print("price fetch price_text:", price_text)

      price_value = parse_price(price_text)
      if price_value is None:
        print("price parse failed:", price_text)
        return None

      return price_value

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

    print("api_register before save")
    print("users_path:", users_path)
    print("users_len:", len(users))

    save_json(users_path, users)

    print("api_register after save")
    print("users_len:", len(users))

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
  appid_raw = data.get("appid")
  hash_name = (data.get("hash_name") or "").strip()
  target_raw = data.get("target_price")
  direction_raw = (str(data.get("direction") or "")).strip().lower()

  async with lock:
    user = find_user_by_token(token)
    if not user:
      return web.json_response({ "ok": False, "error": "no_auth" })

    settings = user["settings"]
    chat_id = user.get("tg_chat_id")
    user_token = user["token"]

  try:
    appid = int(appid_raw)
  except (TypeError, ValueError):
    return web.json_response({ "ok": False, "error": "bad_appid" })

  try:
    target_price = float(target_raw)
  except (TypeError, ValueError):
    return web.json_response({ "ok": False, "error": "bad_price" })

  if not hash_name or target_price <= 0:
    return web.json_response({ "ok": False, "error": "bad_input" })

  if direction_raw not in ("buy", "sell"):
    return web.json_response({ "ok": False, "error": "bad_direction" })

  direction = direction_raw
  now = now_sec()

  current_price = await fetch_price(appid, hash_name, settings["currency_code"])

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
      exist["last_price"] = current_price
      exist["last_checked_at"] = now
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
        "last_checked_at": now,
        "last_notified_at": 0
      })

    save_json(items_path, items)

  if chat_id:
    cur = settings["currency_label"]

    if current_price is None:
      current_price_str = "?"
      advise = "price is unknown — Steam didn't return a price yet."
    else:
      current_price_str = f"{current_price} {cur}"

      if direction == "buy":
        if current_price > target_price:
          advise = "target is below current price — I will wait for the price to drop."
        else:
          advise = "target already reached — you may buy now."
      else:
        if current_price < target_price:
          advise = "target is above current price — I will wait for the price to rise."
        else:
          advise = "target already reached — you may sell now."

    text = (
      "✅ Added to tracking:\n"
      f"[{hash_name}]\n"
      f"current price: {current_price_str}\n"
      f"target: {target_price} {cur}\n"
      f"mode: {direction}\n"
      f"{advise}"
    )

    try:
      await tg_bot.send_message(chat_id, text)
    except Exception as e:
      print("tg send error:", e)

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
      local_users = [
        {
          **u,
          "settings": dict(u.get("settings") or {})
        }
        for u in users
      ]

      local_items = [dict(it) for it in items]

    token_to_user = {}
    for u in local_users:
      token_to_user[u.get("token")] = u

    now = now_sec()

    for it in local_items:
      if not it.get("enabled"):
        continue

      user = token_to_user.get(it.get("user_token"))
      if not user:
        continue

      chat_id = user.get("tg_chat_id")
      if not chat_id:
        continue

      settings = user.get("settings") or {}

      interval_min = settings.get("interval_min", 10)
      try:
        interval_min = int(interval_min)
      except Exception:
        interval_min = 10

      if interval_min < 1:
        interval_min = 1

      if now - it.get("last_checked_at", 0) < interval_min * 60:
        continue

      price_now = await fetch_price(
        it["appid"],
        it["hash_name"],
        settings.get("currency_code", 5)
      )

      it["last_checked_at"] = now
      it["last_price"] = price_now

      if price_now is None:
        continue

      cooldown = 3600
      if now - it.get("last_notified_at", 0) < cooldown:
        continue

      direction = it.get("direction")
      target = it.get("target_price")
      cur = settings.get("currency_label", "RUB")

      if direction == "buy":
        if price_now <= target:
          it["last_notified_at"] = now
          text = (
            f"[{it['hash_name']}] is now selling for "
            f"[{price_now} {cur}] — hurry up and buy!"
          )
          try:
            await tg_bot.send_message(chat_id, text)
          except Exception as e:
            print("tg send error:", e)

      if direction == "sell":
        if price_now >= target:
          it["last_notified_at"] = now
          text = (
            f"[{it['hash_name']}] just went up to "
            f"[{price_now} {cur}] — hurry up and sell!"
          )
          try:
            await tg_bot.send_message(chat_id, text)
          except Exception as e:
            print("tg send error:", e)

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
