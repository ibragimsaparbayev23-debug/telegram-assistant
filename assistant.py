"""Personal Telegram assistant. Run: python assistant.py setup | run."""
import asyncio
import getpass
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CONFIG = DATA / "config.json"
MODEL = "gpt-4.1-mini"


def save_config(cfg):
    DATA.mkdir(mode=0o700, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG)


def eligible(chat_id, is_private, is_group, mentioned, reply_to_owner, cfg):
    if cfg.get("paused", False) or str(chat_id) not in cfg["chats"]:
        return False
    return is_private or (is_group and (mentioned or reply_to_owner))


def api_reply(cfg, prompt, text):
    payload = {"model": cfg["model"], "instructions": prompt,
               "input": text, "max_output_tokens": 500, "store": False}
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + cfg["openai_key"],
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not print URLs, headers, API keys or response bodies.
        raise RuntimeError("ИИ: HTTP " + str(exc.code)) from None
    except (OSError, ValueError):
        raise RuntimeError("Не удалось получить ответ ИИ") from None
    if result.get("status") != "completed":
        raise RuntimeError("ИИ не завершил ответ")
    output = "\n".join(part.get("text", "")
        for item in result.get("output", []) if item.get("type") == "message"
        for part in item.get("content", []) if part.get("type") == "output_text").strip()
    if not output:
        raise RuntimeError("ИИ вернул пустой ответ")
    return output


def make_client(cfg):
    from telethon import TelegramClient
    return TelegramClient(str(DATA / "account"), cfg["api_id"], cfg["api_hash"],
                          device_model="Shokhrukh Assistant", catch_up=False,
                          flood_sleep_threshold=0, request_retries=0)


async def setup():
    print("Настройка. Все ключи и код входа вводятся только на этом компьютере.")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    if not cfg:
        print("Получите API ID и API hash: https://my.telegram.org -> API development tools")
        cfg["api_id"] = int(input("API ID: ").strip())
        cfg["api_hash"] = getpass.getpass("API hash (ввод скрыт): ").strip()
    if not cfg.get("openai_key"):
        print("Ключ ИИ: https://platform.openai.com/api-keys")
        cfg["openai_key"] = getpass.getpass("OpenAI API key (ввод скрыт): ").strip()
    if not cfg["api_hash"] or not cfg["openai_key"]:
        raise ValueError("Ключи не могут быть пустыми")
    cfg.setdefault("model", MODEL)
    DATA.mkdir(mode=0o700, exist_ok=True)
    client = make_client(cfg)
    try:
        await client.start(phone=lambda: input("Номер Telegram с кодом страны: "),
                           code_callback=lambda: getpass.getpass("Код из Telegram: "),
                           password=lambda: getpass.getpass("Пароль двухэтапной защиты: "))
        me = await client.get_me()
        if me.bot:
            raise ValueError("Нужен личный аккаунт, а не бот")
        cfg["owner_id"] = me.id
        dialogs = []
        async for d in client.iter_dialogs():
            if d.id != me.id and (d.is_group or (d.is_user and not getattr(d.entity, "bot", False))):
                dialogs.append(d)
        print("\nВыберите только чаты, участники которых согласились на обработку сообщений ИИ.")
        print("Сообщение будет отправляться в OpenAI. Автоответы идут от вашего аккаунта.")
        for i, d in enumerate(dialogs, 1):
            print(f"{i}. {'Группа' if d.is_group else 'Личный чат'}: {d.name}")
        choices = {int(x.strip()) for x in input("Номера через запятую: ").split(",") if x.strip()}
        if not choices or min(choices) < 1 or max(choices) > len(dialogs):
            raise ValueError("Нужно выбрать хотя бы один чат из списка")
        if input("В выбранных чатах получено согласие участников? Введите ДА: ").strip().upper() != "ДА":
            raise ValueError("Настройка отменена; чаты не сохранены")
        cfg["chats"] = {str(dialogs[i-1].id): dialogs[i-1].name for i in sorted(choices)}
        cfg["paused"] = False
        cfg["daily_limit"] = 100
        save_config(cfg)
        print("Готово. Запустите START.bat. В группах: только упоминания и ответы вам.")
    finally:
        await client.disconnect()


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen(chat INTEGER, msg INTEGER, PRIMARY KEY(chat,msg))")
        self.db.execute("CREATE TABLE IF NOT EXISTS quota(day TEXT PRIMARY KEY, n INTEGER)")
        self.db.commit()

    def claim(self, chat, msg):
        try:
            self.db.execute("INSERT INTO seen VALUES (?,?)", (chat, msg))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def reserve(self, limit):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        row = self.db.execute("SELECT n FROM quota WHERE day=?", (day,)).fetchone()
        if row and row[0] >= limit:
            return False
        self.db.execute("INSERT INTO quota VALUES (?,1) ON CONFLICT(day) DO UPDATE SET n=n+1", (day,))
        self.db.commit()
        return True


async def run():
    from telethon import events, errors
    if not CONFIG.exists():
        raise ValueError("Сначала запустите SETUP.bat")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    prompt = (ROOT / "instructions.txt").read_text(encoding="utf-8")
    state = State(DATA / "state.sqlite")
    client = make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise ValueError("Сессия недействительна. Запустите SETUP.bat повторно")
    me = await client.get_me()
    if me.id != cfg["owner_id"] or me.bot:
        await client.disconnect()
        raise ValueError("Сессия принадлежит другому аккаунту")
    started = time.time()
    last_call = {}
    locks = {}
    epoch = 0

    async def notify(text):
        try:
            await client.send_message("me", text, parse_mode=None)
        except Exception:
            print("Не удалось отправить уведомление в Избранное.")

    @client.on(events.NewMessage(outgoing=True))
    async def control(event):
        nonlocal epoch
        if event.chat_id != me.id or event.sender_id != me.id:
            return
        command = event.raw_text.strip().lower()
        if command == ".ai off":
            cfg["paused"] = True
            epoch += 1
            save_config(cfg)
            await notify("Помощник приостановлен.")
        elif command == ".ai on":
            cfg["paused"] = False
            save_config(cfg)
            await notify("Помощник включён для выбранных чатов.")
        elif command == ".ai status":
            await notify("Помощник: " + ("пауза" if cfg["paused"] else "включён") +
                         "\nЧаты: " + ", ".join(cfg["chats"].values()))

    @client.on(events.NewMessage(incoming=True))
    async def incoming(event):
        nonlocal epoch
        # Gate before retrieving reply metadata or transmitting content to AI.
        if cfg["paused"] or str(event.chat_id) not in cfg["chats"]:
            return
        if event.sender_id is None or event.sender_id == me.id or event.fwd_from:
            return
        if not event.raw_text or event.message.action or event.message.via_bot_id:
            return
        if event.date.timestamp() < started or time.time() - event.date.timestamp() > 120:
            return
        try:
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return
            reply_owner = False
            if event.is_group and event.is_reply:
                reply = await event.get_reply_message()
                reply_owner = bool(reply and reply.sender_id == me.id)
            if not eligible(event.chat_id, event.is_private, event.is_group,
                            event.message.mentioned, reply_owner, cfg):
                return
            lock = locks.setdefault(event.chat_id, asyncio.Lock())
            if lock.locked():
                return
            async with lock:
                if time.time() - last_call.get(event.chat_id, 0) < 30:
                    return
                if not state.claim(event.chat_id, event.id) or not state.reserve(cfg["daily_limit"]):
                    return
                last_call[event.chat_id] = time.time()
                generation_epoch = epoch
                answer = await asyncio.to_thread(api_reply, cfg, prompt, event.raw_text[:6000])
                if cfg["paused"] or epoch != generation_epoch:
                    return
                # Recheck edits/deletions before delivering a generated answer.
                current = await client.get_messages(event.chat_id, ids=event.id)
                if not current or current.raw_text != event.raw_text:
                    return
                if answer == "[[HANDOFF]]" or "[[HANDOFF]]" in answer:
                    await notify("Нужен твой ответ: " + cfg["chats"][str(event.chat_id)] +
                                 f"; сообщение {event.id}. Автоответ не отправлен.")
                    return
                if not answer or len(answer) > 3500:
                    raise RuntimeError("Неподходящая длина ответа ИИ")
                await client.send_message(event.chat_id, answer, reply_to=event.id, parse_mode=None)
                print("Отправлен ответ в разрешённый чат.")
        except errors.FloodWaitError as exc:
            cfg["paused"] = True
            epoch += 1
            epoch_note = f"Ограничение Telegram: {exc.seconds} сек. Помощник на паузе."
            save_config(cfg)
            print(epoch_note)
            await notify(epoch_note)
        except Exception as exc:
            # No exception messages: third-party exceptions can contain secrets/content.
            detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            print("Ошибка обработки: " + detail + ". Сообщение не повторяется.")
            await notify("Ошибка помощника: " + type(exc).__name__ +
                         ". Проверь окно программы и ключ ИИ. Автоматического повтора нет.")

    print("Помощник работает. Остановка: Ctrl+C. Пауза: .ai off в Избранном.")
    try:
        await client.run_until_disconnected()
    finally:
        await client.disconnect()
        state.db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    os.umask(0o077)
    try:
        action = sys.argv[1] if len(sys.argv) > 1 else "run"
        if action not in {"setup", "run"}:
            raise ValueError("Используйте setup или run")
        asyncio.run(setup() if action == "setup" else run())
    except KeyboardInterrupt:
        print("Остановлено.")
    except Exception as exc:
        print("Запуск не завершён: " + type(exc).__name__)
        if isinstance(exc, ValueError):
            print(str(exc))
        sys.exit(1)
