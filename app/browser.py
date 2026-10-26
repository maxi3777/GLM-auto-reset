"""Playwright 浏览器控制:持久化登录态、页面管理、无头->有头自动升级。"""
from __future__ import annotations

import threading
from typing import Optional

from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout

PAGE_READY_TIMEOUT_MS = 20000
FETCH_TIMEOUT_MS = 20000


class LoginRequired(Exception):
    """需要用户登录。"""


class CaptchaRequired(Exception):
    """页面出现安全验证(点选汉字等),需要人工处理。"""


class BrowserClosed(Exception):
    pass


_FETCH_JS = """
async ({url, method, body, authHeader}) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    const r = await fetch(url, {
      method, credentials: 'include',
      headers: {
        'Accept': 'application/json',
        ...(body ? {'Content-Type': 'application/json'} : {}),
        ...(authHeader ? {'Authorization': authHeader} : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch (e) {}
    return {status: r.status, json, text: json ? null : text.slice(0, 300)};
  } finally { clearTimeout(timer); }
}
"""


class BrowserController:
    """单线程使用(监控线程)。持久化 context,登录态保存在 profile_dir。"""

    def __init__(self, profile_dir: str, usage_url: str, on_event=None):
        from pathlib import Path
        p = Path(profile_dir)
        if not p.is_absolute():
            # 相对路径一律相对项目根目录,避免受启动时工作目录影响
            p = Path(__file__).resolve().parent.parent / p
        self._profile_dir = str(p)
        self._usage_url = usage_url
        self._on_event = on_event or (lambda *a: None)
        self._lock = threading.Lock()
        self._pw = None
        self._context = None
        self._page = None
        self._headless = True
        self._closed = False

    # -------------------------------------------------- lifecycle
    def start(self, headless: bool = True) -> None:
        """启动浏览器(或重启为指定模式)。"""
        self._shutdown_context()
        self._headless = headless
        self._pw = sync_playwright().start()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if not headless:
            args.append("--window-size=1100,860")
        try:
            self._context = self._pw.chromium.launch_persistent_context(
                self._profile_dir,
                headless=headless,
                channel="msedge",
                args=args,
                viewport={"width": 1100, "height": 860},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
        except Exception:
            # 系统没有 Edge 时退回 Playwright 自带 Chromium
            self._context = self._pw.chromium.launch_persistent_context(
                self._profile_dir,
                headless=headless,
                args=args,
                viewport={"width": 1100, "height": 860},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
        self._context.set_default_timeout(PAGE_READY_TIMEOUT_MS)
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        self._closed = False

    def escalate_headful(self, reason: str) -> None:
        """无头 -> 有头切换(登录/验证码/反复失败时)。"""
        if not self._headless:
            return
        self._on_event("log", f"切换为有头浏览器:{reason}")
        self.start(headless=False)
        self.goto_usage()

    @property
    def headless(self) -> bool:
        return self._headless

    def close(self) -> None:
        self._closed = True
        self._shutdown_context()

    def _shutdown_context(self) -> None:
        ctx, pw = self._context, self._pw
        self._context, self._page, self._pw = None, None, None
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    # -------------------------------------------------- page
    def _ensure(self) -> None:
        if self._closed:
            raise BrowserClosed()
        if self._context is None:
            self.start(headless=True)

    def goto_usage(self) -> str:
        """打开用量页;返回最终 URL(用于登录态判断)。"""
        self._ensure()
        assert self._page is not None
        try:
            self._page.goto(self._usage_url, wait_until="domcontentloaded")
        except PWTimeout:
            pass  # SPA 慢加载可接受,后续 fetch 自会验证
        except PWError as e:
            raise BrowserClosed(str(e))
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return self._page.url or ""

    def current_url(self) -> str:
        if self._page is None:
            return ""
        return self._page.url or ""

    def check_login(self) -> bool:
        """当前页面是否处于已登录的用量页(而非被重定向到登录页)。"""
        from urllib.parse import urlparse
        url = self.current_url()
        if not url:
            return False
        parsed = urlparse(url)
        if not parsed.netloc:
            return False
        return "/login" not in parsed.path

    def detect_captcha(self) -> bool:
        """是否出现腾讯点选验证码浮层(登录场景常见)。"""
        if self._page is None:
            return False
        try:
            return self._page.locator(
                ".tencent-captcha-dy__popup-type, .tencent-captcha__transform"
            ).first.is_visible(timeout=1500)
        except Exception:
            return False

    # -------------------------------------------------- in-page API
    def fetch_json(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        """在已登录页面上下文内调用官方接口(自动带 Authorization)。"""
        self._ensure()
        assert self._page is not None
        token = self._get_token()
        from urllib.parse import urljoin
        base = self.current_url() or self._usage_url
        url = path if path.startswith("http") else urljoin(base, path)
        res = self._page.evaluate(
            _FETCH_JS,
            {"url": url, "method": method, "body": body, "authHeader": token},
        )
        if res is None or not isinstance(res, dict):
            raise RuntimeError("接口无响应")
        if res.get("json") is None:
            raise RuntimeError(f"接口返回非JSON(HTTP {res.get('status')}): {res.get('text')}")
        return res["json"]

    def has_token(self) -> bool:
        """登录令牌 cookie 是否存在(未登录时该 cookie 不存在,比 URL 跳转更可靠)。"""
        return self._get_token() is not None

    def _get_token(self) -> Optional[str]:
        assert self._context is not None
        try:
            for c in self._context.cookies("https://bigmodel.cn"):
                if c["name"] == "bigmodel_token_production":
                    return c["value"]
        except Exception:
            pass
        return None
