import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import string
import time
from urllib import parse

from aiohttp import ClientSession
from fake_useragent import UserAgent

_LOGGER = logging.getLogger(__package__)

MANUAL_LOGIN_TIMEOUT = 300


def get_random(length):
    return "".join(random.sample(string.ascii_letters + string.digits, length))


class MiTokenStore:
    def __init__(self, token_path):
        self.token_path = token_path

    def load_token(self):
        if os.path.isfile(self.token_path):
            try:
                with open(self.token_path) as f:
                    return json.load(f)
            except Exception:
                _LOGGER.exception("Exception on load token from %s", self.token_path)
        return None

    def save_token(self, token=None):
        if token:
            try:
                with open(self.token_path, "w") as f:
                    json.dump(token, f, indent=2)
            except Exception:
                _LOGGER.exception("Exception on save token to %s", self.token_path)
        elif os.path.isfile(self.token_path):
            os.remove(self.token_path)


class MiAccount:
    def __init__(self, session: ClientSession, username, password, token_store=None):
        self.session = session
        self.username = username
        self.password = password
        self.token_store = (
            MiTokenStore(token_store) if isinstance(token_store, str) else token_store
        )
        self.token_store_backup = (
            MiTokenStore(token_store + ".back") if isinstance(token_store, str) else None
        )
        self.token = token_store is not None and self.token_store.load_token()
        self.ua = UserAgent()  # 初始化随机 User-Agent 生成器
        self.now_ua = self.ua.random

    async def login(self, sid, _manual_login_attempted=False):
        if not self.token:
            self.token = {"deviceId": get_random(16).upper()}
        try:
            resp = await self._serviceLogin(f"serviceLogin?sid={sid}&_json=true")
            if resp["code"] != 0:
                data = {
                    "_json": "true",
                    "qs": resp["qs"],
                    "sid": resp["sid"],
                    "_sign": resp["_sign"],
                    "callback": resp["callback"],
                    "user": self.username,
                    "hash": hashlib.md5(self.password.encode()).hexdigest().upper(),
                }
                if resp["code"] == 70016 and not _manual_login_attempted:
                    await self._handle_manual_login(resp)
                resp = await self._serviceLogin("serviceLoginAuth2", data)

            self.token["userId"] = resp["userId"]
            self.token["passToken"] = resp["passToken"]

            serviceToken = await self._securityTokenService(
                resp["location"], resp["nonce"], resp["ssecurity"]
            )
            self.token[sid] = (resp["ssecurity"], serviceToken)
            if self.token_store:
                self.token_store.save_token(self.token)
            return True

        except Exception as e:
            self.token = None
            if self.token_store:
                self.token_store.save_token()
            _LOGGER.exception("Exception on login %s: %s", self.username, e)
            return False

    async def _serviceLogin(self, uri, data=None):
        self.now_ua = self.ua.random
        headers = {"User-Agent": self.now_ua}
        cookies = {"sdkVersion": "3.9", "deviceId": self.token["deviceId"]}
        if "passToken" in self.token:
            cookies["userId"] = self.token["userId"]
            cookies["passToken"] = self.token["passToken"]
        else:
            cookies["passToken"] = ""
        url = "https://account.xiaomi.com/pass/" + uri
        async with self.session.request(
            "GET" if data is None else "POST",
            url,
            data=data,
            cookies=cookies,
            headers=headers,
            ssl=False,
        ) as r:
            raw = await r.read()
        resp = json.loads(raw[11:])
        _LOGGER.debug("%s: %s", uri, resp)
        return resp

    async def _securityTokenService(self, location, nonce, ssecurity):
        nsec = "nonce=" + str(nonce) + "&" + ssecurity
        clientSign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
        async with self.session.get(
            location + "&clientSign=" + parse.quote(clientSign)
        ) as r:
            serviceToken = r.cookies["serviceToken"].value
            if not serviceToken:
                raise Exception(await r.text())
        return serviceToken

    async def _handle_manual_login(self, resp):
        location = resp.get("location")
        if not location:
            raise Exception("Manual login required but response missing location")
        callback_url = resp.get("callback")
        _LOGGER.debug("Manual login challenge received: location=%s callback=%s", location, callback_url)
        cookies = await self._manual_login_with_browser(location, callback_url)
        self._store_manual_login_cookies(cookies)

    async def _manual_login_with_browser(self, location, callback_url=None):
        return await asyncio.to_thread(
            self._open_manual_login_browser, location, callback_url, MANUAL_LOGIN_TIMEOUT
        )

    def _store_manual_login_cookies(self, cookies):
        cookie_map = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
        if not cookie_map:
            raise Exception("Manual login did not produce any cookies")
        self.token.setdefault("cookies", {}).update(cookie_map)
        if "userId" in cookie_map:
            self.token["userId"] = cookie_map["userId"]
        if "passToken" in cookie_map:
            self.token["passToken"] = cookie_map["passToken"]
        if self.token_store_backup:
            self.token_store_backup.save_token(cookies)

    def _open_manual_login_browser(self, location, callback_url, timeout):
        try:
            from playwright.sync_api import Error as PlaywrightError, sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Manual login requires playwright; install it with 'pip install playwright'"
            ) from exc

        browser_name = os.environ.get("MI_PLAYWRIGHT_BROWSER", "chromium").lower()
        if browser_name not in {"chromium", "firefox", "webkit"}:
            raise RuntimeError(
                "MI_PLAYWRIGHT_BROWSER must be one of chromium, firefox, or webkit"
            )
        executable_path = os.environ.get("MI_PLAYWRIGHT_EXECUTABLE_PATH")

        launch_options = {
            "headless": False,
            "args": [
                "--disable-extensions",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-browser-side-navigation",
            ],
        }
        if executable_path:
            launch_options["executable_path"] = executable_path
        if browser_name == "chromium":
            launch_options["channel"] = os.environ.get("MI_PLAYWRIGHT_CHANNEL", "chrome")

        with sync_playwright() as playwright:
            launcher = getattr(playwright, browser_name)
            try:
                browser = launcher.launch(**launch_options)
            except PlaywrightError as exc:
                _LOGGER.debug(
                    "PlaywrightError launching browser %s for manual login: %s",
                    browser_name,
                    exc,
                )
                raise RuntimeError(
                    "Unable to start Playwright browser for manual login; ensure a browser is installed "
                    "and MI_PLAYWRIGHT_EXECUTABLE_PATH points to a valid executable"
                ) from exc

            def _log_playwright_error(action, exc=None):
                if exc:
                    _LOGGER.debug("PlaywrightError while %s: %s", action, exc)
                else:
                    _LOGGER.debug("PlaywrightError while %s", action)

            def _log_form_snapshot(target_page):
                try:
                    form = target_page.query_selector("form")
                except PlaywrightError as exc:
                    _log_playwright_error("querying login form", exc)
                    return
                if not form:
                    return
                try:
                    html_snippet = form.inner_html()
                except PlaywrightError as exc:
                    _log_playwright_error("reading login form HTML", exc)
                    return
                if html_snippet:
                    snippet = html_snippet[:1024].replace("\n", " ")
                    _LOGGER.debug("Captured login form HTML snippet: %s", snippet)

            def _try_fill_field(target_page, selectors, value):
                for selector in selectors:
                    try:
                        locator = target_page.locator(selector)
                        if locator.count() > 0:
                            locator.fill(value)
                            return True
                    except PlaywrightError as exc:
                        _log_playwright_error(f"locating field {selector}", exc)
                        continue
                return False

            def _check_agreement_checkbox(target_page):
                agreement_selector = "input[type='checkbox']"
                agreement_keywords = ("agree", "协议", "隐私", "使用协议", "隐私政策")
                try:
                    checkbox = target_page.locator(agreement_selector)
                except PlaywrightError as exc:
                    _log_playwright_error("locating agreement checkbox", exc)
                    return
                count = checkbox.count()
                for idx in range(min(count, 3)):
                    try:
                        element = checkbox.nth(idx)
                        label = element.evaluate("el => Array.from(el.labels || []).map(l => l.innerText).join(' ').toLowerCase()")
                        if any(keyword in (label or "").lower() for keyword in agreement_keywords):
                            if not element.is_checked():
                                element.check()
                            return
                    except PlaywrightError as exc:
                        _log_playwright_error("checking agreement checkbox labels", exc)
                        continue

            def _auto_login_if_possible(target_page):
                if not (self.username and self.password):
                    return
                _LOGGER.debug("Attempting auto login with Playwright snapshot for %s", self.username)
                username_selectors = [
                    "input[placeholder*='邮箱']",
                    "input[placeholder*='手机']",
                    "input[placeholder*='小米ID']",
                    "input[name='user']",
                    "input[name='account']",
                    "input#user",
                    "input#username",
                ]
                password_selectors = [
                    "input[type='password']",
                    "input[name='password']",
                    "input#pwd",
                    "input#password",
                ]
                _try_fill_field(target_page, username_selectors, self.username)
                _try_fill_field(target_page, password_selectors, self.password)
                _LOGGER.debug("Auto login fields filled for %s", self.username)
                _check_agreement_checkbox(target_page)
                submit_selectors = [
                    "button[type='submit']",
                    "button:has-text('登录')",
                    "button:has-text('Login')",
                    "input[type='submit']",
                ]
                for selector in submit_selectors:
                    try:
                        locator = target_page.locator(selector)
                        if locator.count() > 0:
                            locator.first.click(timeout=2000)
                            return
                    except PlaywrightError as exc:
                        _log_playwright_error(f"clicking submit selector {selector}", exc)
                        continue

            try:
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()
                _LOGGER.info(
                    "Manual verification required, opening browser at %s for %s",
                    location,
                    self.username,
                )
                _LOGGER.debug("Waiting for callback url %s", callback_url)
                page.goto(location)
                page.wait_for_timeout(1000)
                _log_form_snapshot(page)
                _auto_login_if_possible(page)
                start_url = page.url or location
                deadline = time.time() + timeout

                while time.time() < deadline:
                    current_url = page.url
                    if current_url:
                        if callback_url and current_url.startswith(callback_url):
                            break
                        if not callback_url and current_url != start_url:
                            break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(
                        "Timed out waiting for manual login callback; please complete the login faster"
                    )

                final_url = page.url
                _LOGGER.info("Manual login finished, redirected to %s", final_url)
                page.goto("https://account.xiaomi.com")
                cookies = context.cookies()
                if not cookies:
                    _LOGGER.warning("Manual login browser session did not produce cookies")
                else:
                    _LOGGER.debug(
                        "Manual login cookies after visiting account page: %s", cookies
                    )
                return cookies
            finally:
                browser.close()

    async def mi_request(self, sid, url, data, headers, relogin=True):
        headers["User-Agent"] = self.now_ua
        if (self.token and sid in self.token) or await self.login(sid):  # Ensure login
            cookies = {
                "userId": self.token["userId"],
                "serviceToken": self.token[sid][1],
            }
            content = data(self.token, cookies) if callable(data) else data
            method = "GET" if data is None else "POST"
            _LOGGER.info("%s %s", url, content)
            async with self.session.request(
                method, url, data=content, cookies=cookies, headers=headers
            ) as r:
                status = r.status
                if status == 200:
                    resp = await r.json(content_type=None)
                    code = resp["code"]
                    if code == 0:
                        return resp
                    if "auth" in resp.get("message", "").lower():
                        status = 401
                else:
                    resp = await r.text()
            if status == 401 and relogin:
                _LOGGER.warn("Auth error on request %s %s, relogin...", url, resp)
                self.token = None  # Auth error, reset login
                return await self.mi_request(sid, url, data, headers, False)
        else:
            resp = "Login failed"
        raise Exception(f"Error {url}: {resp}")
