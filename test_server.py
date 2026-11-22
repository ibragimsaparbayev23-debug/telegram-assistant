import asyncio
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

import assistant as core
import server

SECRET = "test-password-with-at-least-24-characters"


class SecurityTests(unittest.TestCase):
    def test_session_signature_and_expiry(self):
        cookie = server.signed_cookie(SECRET, 100)
        self.assertTrue(server.valid_cookie(SECRET, cookie, 101))
        self.assertFalse(server.valid_cookie(SECRET, cookie, 100 + 8*3600))
        self.assertFalse(server.valid_cookie(SECRET, cookie, 99))
        self.assertFalse(server.valid_cookie(SECRET, cookie[:-1] + "x", 101))
        self.assertFalse(server.valid_cookie("different", cookie, 101))

    def test_csrf_bound_to_session(self):
        self.assertNotEqual(server.csrf_for(SECRET, "one"), server.csrf_for(SECRET, "two"))

    def test_persistent_storage_required_on_railway(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {"RAILWAY_ENVIRONMENT_ID": "test", "DATA_DIR": temp,
                   "ADMIN_PASSWORD": SECRET, "TELEGRAM_API_ID": "1",
                   "TELEGRAM_API_HASH": "test", "OPENAI_API_KEY": "test"}
            with patch.dict(os.environ, env, clear=True), patch.object(core, "DATA", Path(temp)), \
                    patch.object(core, "CONFIG", Path(temp)/"config.json"):
                ctl = server.Controller()
                self.assertIn("Volume", ctl.problem)

    def test_login_headers_and_cross_site_block(self):
        class C:
            secret = SECRET
        http = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_class(C(), None))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{http.server_port}"
        body = urllib.parse.urlencode({"password": SECRET}).encode()
        try:
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(urllib.request.Request(base+"/login", data=body,
                    headers={"Origin": "https://other.example"}))
            self.assertEqual(err.exception.code, 403)
            # Avoid following redirect so the issued cookie can be inspected.
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args): return None
            opener = urllib.request.build_opener(NoRedirect)
            with self.assertRaises(urllib.error.HTTPError) as err:
                opener.open(urllib.request.Request(base+"/login", data=body, headers={"Origin": base}))
            self.assertEqual(err.exception.code, 303)
            cookie = err.exception.headers["Set-Cookie"]
            self.assertIn("HttpOnly", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("SameSite=Strict", cookie)
            token = cookie.split(";",1)[0].split("=",1)[1]
            self.assertTrue(server.valid_cookie(SECRET, token))
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(urllib.request.Request(base+"/activate", data=b"csrf=wrong",
                    headers={"Origin": base, "Cookie": "admin="+token}))
            self.assertEqual(err.exception.code, 403)
            with urllib.request.urlopen(base+"/") as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn(SECRET.encode(), response.read())
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


class ConsentTests(unittest.IsolatedAsyncioTestCase):
    async def test_activation_needs_consent_and_known_chats(self):
        class Client:
            async def is_user_authorized(self): return True
        ctl = object.__new__(server.Controller)
        ctl.problem = ""
        ctl.wait_until = 0
        async def connection(): return Client()
        async def dialogs(client): return Obj(id=1), {"-10": "test"}
        ctl.connection, ctl.dialogs = connection, dialogs
        self.assertIn("согласие", await ctl.action("/activate", {"chat": ["-10"]}))
        self.assertIn("списка", await ctl.action("/activate", {"chat": ["-20"], "consent": ["yes"]}))


if __name__ == "__main__":
    unittest.main()
