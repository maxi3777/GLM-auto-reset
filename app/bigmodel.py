"""bigmodel.cn 官方接口客户端(在已登录页面上下文内调用,与官方前端行为一致)。"""
from __future__ import annotations

from .browser import BrowserController, LoginRequired

QUOTA_PATH = "/api/monitor/usage/quota/limit"
CARDS_PATH = "/api/biz/customer-package-reset/list?targetType=PERSONAL"
USE_PATH = "/api/biz/customer-package-reset/use"


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg


class BigModelClient:
    def __init__(self, browser: BrowserController):
        self._b = browser

    # -------------------------------------------------- helpers
    def _call(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        payload = self._b.fetch_json(path, method=method, body=body)
        if payload.get("code") == 1001 or "Authorization" in str(payload.get("msg") or ""):
            raise LoginRequired(payload.get("msg") or "登录态失效")
        return payload

    # -------------------------------------------------- APIs
    def get_quota_raw(self) -> dict:
        """GET /api/monitor/usage/quota/limit"""
        return self._call(QUOTA_PATH)

    def get_cards_raw(self) -> dict:
        """GET /api/biz/customer-package-reset/list?targetType=PERSONAL"""
        return self._call(CARDS_PATH)

    def use_card(self, reset_type: str, record_id: int, grant_type: str, request_id: str) -> dict:
        """POST /biz/customer-package-reset/use —— 官方前端同款参数,含幂等 requestId。"""
        body = {
            "targetType": "PERSONAL",
            "resetType": reset_type,
            "recordId": record_id,
            "grantType": grant_type or "",
            "requestId": request_id,
        }
        return self._call(USE_PATH, method="POST", body=body)
