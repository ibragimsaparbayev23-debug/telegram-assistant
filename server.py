"""Single-process Railway onboarding UI. Secrets enter via Railway variables."""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
import contextlib
import hashlib
import hmac
from html import escape as esc
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import assistant as core


def signed_cookie(secret, now=None):
    data = str(int(time.time() if now is None else now)) + "." + secrets.token_hex(16)
    return data + "." + hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()


def valid_cookie(secret, token, now=None):
    try:
        stamp, nonce, signature = token.split(".")
        age = (time.time() if now is None else now) - int(stamp)
        expected = hmac.new(secret.encode(), (stamp + "." + nonce).encode(), hashlib.sha256).hexdigest()
        return len(secret) >= 24 and 0 <= age < 8 * 3600 and hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False


def csrf_for(secret, token):
    return hmac.new(secret.encode(), ("csrf:" + token).encode(), hashlib.sha256).hexdigest()


def page(body):
    return ("<!doctype html><html lang=ru><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Помощник Шохруха</title><style>"
        "body{font:17px system-ui;background:#101522;color:#edf0f8;margin:0;padding:28px}"
        "main{max-width:700px;margin:auto}input,button{font:inherit;padding:12px;border-radius:8px}"
        "input:not([type=checkbox]){display:block;box-sizing:border-box;width:100%;margin:12px 0}"
        "button{background:#a3f1cb;color:#13231c;border:0;cursor:pointer;margin:10px 0}"
        "label{display:block;padding:10px 0}p{line-height:1.6}small{color:#acbbce}"
        "a{color:#a3f1cb}</style><main><h1>Помощник Шохруха</h1>" + body + "</main></html>").encode()


def form(action, token, fields, label):
    return (f'<form method=post action="{action}"><input type=hidden name=csrf value="{token}">'
            + fields + f"<button>{label}</button></form>")


class Controller:
    def __init__(self):
        self.client = None
        self.engine = None
        self.phone = None
        self.phone_hash = None
        self.need_password = False
        self.last_code = 0
        self.wait_until = 0
        self.gate = asyncio.Lock()
        self.secret = os.environ.get("ADMIN_PASSWORD", "")
        self.problem = ""
        # Require persistent storage on Railway, never quietly use an ephemeral session.
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        if os.environ.get("RAILWAY_ENVIRONMENT_ID") and not mount:
            self.problem = "Подключите Volume с путём /data и перезапустите сервис."
        core.DATA = Path(mount or os.environ.get("DATA_DIR", "/data"))
        core.CONFIG = core.DATA / "config.json"
        core.DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
        if len(self.secret) < 24:
            self.problem = "Добавьте ADMIN_PASSWORD длиной от 24 символов в Variables Railway."
        old = self.config()
        try:
            old.update(api_id=int(os.environ.get("TELEGRAM_API_ID", "")),
                       api_hash=os.environ["TELEGRAM_API_HASH"],
                       openai_key=os.environ["OPENAI_API_KEY"],
                       model=os.environ.get("OPENAI_MODEL", core.MODEL))
            if not old["api_hash"] or not old["openai_key"]:
                raise ValueError()
            old.setdefault("chats", {})
            old.setdefault("paused", True)
            old.setdefault("daily_limit", 100)
            core.save_config(old)
        except (ValueError, KeyError):
            self.problem = "Заполните TELEGRAM_API_ID, TELEGRAM_API_HASH и OPENAI_API_KEY в Variables Railway."

    def config(self):
        return json.loads(core.CONFIG.read_text(encoding="utf-8")) if core.CONFIG.exists() else {}

    def running(self):
        return self.engine is not None and not self.engine.done()

    async def start(self):
        if self.running():
            return
        if self.client:
            await self.client.disconnect()
            self.client = None
        self.engine = asyncio.create_task(core.run())
        # Consume background exceptions without logging credentials or a traceback.
        self.engine.add_done_callback(lambda t: None if t.cancelled() else t.exception())

    async def stop(self):
        if self.engine:
            self.engine.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.engine
            self.engine = None

    async def connection(self):
        if self.running():
            raise ValueError("Сначала нажмите «Пауза и выбор чатов».")
        if self.client is None:
            self.client = core.make_client(self.config())
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def dialogs(self, client):
        me = await client.get_me()
        cfg = self.config()
        if me.bot or (cfg.get("owner_id") and cfg["owner_id"] != me.id):
            raise ValueError("Вход выполнен в другой аккаунт. Отзовите сессию в Telegram.")
        choices = {}
        async for d in client.iter_dialogs(limit=200):
            if d.id != me.id and (d.is_group or (d.is_user and not getattr(d.entity, "bot", False))):
                choices[str(d.id)] = d.name
        return me, choices

    async def screen(self, csrf):
        if self.problem:
            return page("<p>" + esc(self.problem) + "</p>")
        cfg = self.config()
        if self.running():
            status = "На паузе" if cfg.get("paused") else "Работает"
            names = ", ".join(cfg.get("chats", {}).values())
            return page(f"<h2>{status}</h2><p>Чаты: {esc(names)}</p>"
                "<p>В группах: упоминания аккаунта и ответы вам. В выбранной личке: новые сообщения."
                "<br>Команды в Избранном: .ai off, .ai on, .ai status.</p>"
                + form("/pause", csrf, "", "Пауза и выбор чатов"))
        client = await self.connection()
        if await client.is_user_authorized():
            me, choices = await self.dialogs(client)
            fields = "".join(f'<label><input type=checkbox name=chat value="{cid}"'
                + (" checked" if cid in cfg["chats"] else "") + f"> {esc(name)}</label>"
                for cid, name in choices.items())
            fields += ('<label><input type=checkbox name=consent value=yes required> '
                'Участники выбранных чатов дали явное согласие на обработку их сообщений ИИ '
                'и передачу текста в OpenAI.</label>')
            return page("<h2>Выберите чаты</h2><p>Начните с тестовой группы. "
                "Показаны первые 200 диалогов. При отзыве согласия исключите чат.</p>"
                + form("/activate", csrf, fields, "Включить ответы"))
        if self.need_password:
            return page("<h2>Двухэтапная защита Telegram</h2>" + form("/password", csrf,
                '<input type=password name=password placeholder="Облачный пароль Telegram" required autocomplete=off>',
                "Подтвердить"))
        if self.phone_hash:
            return page("<h2>Код входа</h2><p>Введите код, который Telegram прислал вам.</p>"
                + form("/code", csrf, '<input name=code inputmode=numeric required autocomplete=off>', "Войти")
                + form("/reset", csrf, "", "Начать вход заново"))
        return page("<h2>Подключить личный Telegram</h2><p>Создаётся дополнительная сессия вашего "
            "аккаунта на сервере. Код и пароль вводите только на этой странице своего сервиса.</p>"
            + form("/phone", csrf, '<input name=phone placeholder="+998…" required autocomplete=tel>', "Получить код"))

    async def action(self, path, values):
        from telethon.errors import SessionPasswordNeededError, FloodWaitError
        if self.problem:
            return self.problem
        if time.time() < self.wait_until:
            return "Telegram просит подождать перед следующей попыткой."
        try:
            if path == "/pause":
                # Cancellation stops in-flight generation before changing config.
                await self.stop()
                cfg = self.config()
                cfg["paused"] = True
                core.save_config(cfg)
                return None
            client = await self.connection()
            if path == "/reset":
                self.phone = self.phone_hash = None
                self.need_password = False
            elif path == "/phone":
                if await client.is_user_authorized():
                    return "Аккаунт уже подключён."
                if time.time() - self.last_code < 60:
                    return "Перед повторным запросом кода подождите минуту."
                phone = values.get("phone", [""])[0].strip()
                if not phone.startswith("+") or not phone[1:].isdigit():
                    return "Введите номер с плюсом и кодом страны, без пробелов."
                self.last_code = time.time()
                sent = await client.send_code_request(phone)
                self.phone, self.phone_hash = phone, sent.phone_code_hash
            elif path == "/code":
                if not self.phone_hash:
                    return "Сначала запросите код входа."
                try:
                    await client.sign_in(phone=self.phone, phone_code_hash=self.phone_hash,
                                         code=values.get("code", [""])[0].replace(" ", ""))
                except SessionPasswordNeededError:
                    self.need_password = True
            elif path == "/password":
                if not self.need_password:
                    return "Сначала подтвердите код входа."
                await client.sign_in(password=values.get("password", [""])[0])
                self.need_password = False
            elif path == "/activate":
                if not await client.is_user_authorized():
                    return "Сначала войдите в Telegram."
                if values.get("consent") != ["yes"]:
                    return "Требуется согласие участников выбранных чатов."
                me, choices = await self.dialogs(client)
                selected = values.get("chat", [])
                if not selected or any(cid not in choices for cid in selected):
                    return "Выберите чаты из списка."
                cfg = self.config()
                cfg.update(owner_id=me.id, chats={cid: choices[cid] for cid in selected}, paused=False)
                core.save_config(cfg)
                self.phone = self.phone_hash = None
                await self.start()
            else:
                return "Неизвестное действие."
            return None
        except FloodWaitError as exc:
            self.wait_until = time.time() + exc.seconds
            return f"Telegram просит подождать {exc.seconds} секунд."
        except Exception as exc:
            # Only type names: Telegram exceptions may otherwise include sensitive inputs.
            return "Операция не завершена: " + type(exc).__name__ + ". Проверьте данные и повторите."

    async def dispatch(self, path, values, csrf):
        async with self.gate:
            if path == "/":
                return await self.screen(csrf)
            error = await self.action(path, values)
            if error:
                return page("<p>" + esc(error) + '</p><a href="/">Назад</a>')
            return None


def handler_class(controller, loop):
    class Handler(BaseHTTPRequestHandler):
        attempts = []
        def log_message(self, *args): pass

        def respond(self, status, body=b"", extra=None):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # Preserve Origin on same-site form POSTs without sending a referrer off-site.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            for key, value in (extra or {}).items(): self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def cookie(self):
            try:
                jar = SimpleCookie(self.headers.get("Cookie", ""))
                return jar["admin"].value if "admin" in jar else ""
            except Exception:
                return ""

        def do_GET(self):
            if self.path == "/health":
                return self.respond(200, b"ok")
            if self.path != "/":
                return self.respond(404)
            token = self.cookie()
            if not valid_cookie(controller.secret, token):
                body = page('<h2>Вход в управление</h2><form method=post action="/login">'
                    '<input type=password name=password placeholder="Пароль управления из Railway" required autocomplete=current-password>'
                    '<button>Войти</button></form>')
                return self.respond(200, body)
            self.application("/", {}, token)

        def do_POST(self):
            # Reject cross-site form submissions including login CSRF.
            origin = self.headers.get("Origin", "")
            if not origin or urlsplit(origin).netloc != self.headers.get("Host", ""):
                return self.respond(403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16384:
                    return self.respond(413)
                values = parse_qs(self.rfile.read(size).decode("utf-8"), max_num_fields=250)
            except (ValueError, UnicodeError):
                return self.respond(400)
            if self.path == "/login":
                now = time.time()
                Handler.attempts = [t for t in Handler.attempts if now-t < 60]
                if len(Handler.attempts) >= 10:
                    return self.respond(429, page("<p>Слишком много попыток. Подождите минуту.</p>"))
                Handler.attempts.append(now)
                password = values.get("password", [""])[0]
                if len(controller.secret) < 24:
                    return self.respond(503, page('<p>В настройках сервиса требуется ADMIN_PASSWORD длиной от 24 символов.</p><a href="/">Назад</a>'))
                if not hmac.compare_digest(password.encode(), controller.secret.encode()):
                    return self.respond(403, page('<p>Неверный пароль.</p><a href="/">Назад</a>'))
                token = signed_cookie(controller.secret)
                return self.respond(303, extra={"Location": "/", "Set-Cookie":
                    "admin=" + token + "; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=28800"})
            token = self.cookie()
            if not valid_cookie(controller.secret, token):
                return self.respond(403)
            expected = csrf_for(controller.secret, token)
            if not hmac.compare_digest(values.get("csrf", [""])[0], expected):
                return self.respond(403)
            self.application(self.path, values, token)

        def application(self, path, values, token):
            future = asyncio.run_coroutine_threadsafe(
                controller.dispatch(path, values, csrf_for(controller.secret, token)), loop)
            try:
                body = future.result(timeout=55)
                if body is None:
                    return self.respond(303, extra={"Location": "/"})
                return self.respond(200, body)
            except FutureTimeout:
                future.cancel()
                return self.respond(504, page('<p>Операция заняла слишком долго. Откройте главную страницу.</p>'))
            except Exception:
                return self.respond(500, page('<p>Ошибка подключения. Проверьте Variables и откройте главную страницу повторно.</p>'))
    return Handler


async def main():
    logging.basicConfig(level=logging.CRITICAL)
    os.umask(0o077)
    controller = Controller()
    cfg = controller.config()
    if not controller.problem and cfg.get("owner_id") and cfg.get("chats") and not cfg.get("paused"):
        await controller.start()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))),
                                 handler_class(controller, asyncio.get_running_loop()))
    server.daemon_threads = True
    try:
        await asyncio.to_thread(server.serve_forever)
    finally:
        await controller.stop()
        if controller.client:
            await controller.client.disconnect()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
