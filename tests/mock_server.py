"""本地 mock 服务器:1:1 模拟 bigmodel.cn 用量页与三个官方接口。

用途:让程序在完全不接触真实网站的情况下进行端到端测试。
支持的场景开关(state.scenario):
- logged_out: /usage 重定向到 /login(模拟未登录)
- fail_use_once: 第一次 POST use 返回业务失败"指定的重置次数不可用"
- hang_use: POST use 挂起 60s(模拟超时,前端 15s 中止 -> 结果未知)
- invalid_use_record: use 请求的 recordId 不在可用列表 -> 业务失败
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USAGE_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>用量统计(mock)</title></head>
<body><h1>用量统计</h1><div id="app">每5小时使用额度 / 每周使用额度 / 用量重置额度</div>
<script>/* mock page */</script></body></html>"""

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>登录(mock)</title></head>
<body><h1>完成登录/注册</h1></body></html>"""


class MockState:
    def __init__(self):
        self.lock = threading.Lock()
        # 额度:5小时 36%(724/2000),周 83%(8349/10000) —— 与真实观察值同构
        self.limits = [
            {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "usage": 2000,
             "currentValue": 724, "remaining": 1275, "percentage": 36,
             "nextResetTime": 1792954587445},
            {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "usage": 10000,
             "currentValue": 8349, "remaining": 1650, "percentage": 83,
             "nextResetTime": 1793106806999},
        ]
        self.five_hour_resets = [
            {"recordId": 101, "grantType": "DIRECT", "expireTime": "2026-10-27 10:00:00", "available": True},
            {"recordId": 102, "grantType": "DIRECT", "expireTime": "2026-10-28 14:56:05", "available": True},
            {"recordId": 103, "grantType": "DIRECT", "expireTime": "2026-10-26 23:00:00", "available": False},
        ]
        self.week_resets = [
            {"recordId": 201, "grantType": "DIRECT", "expireTime": "2026-10-28 14:56:05", "available": True},
            {"recordId": 202, "grantType": "DIRECT", "expireTime": "2026-10-27 08:00:00", "available": True},
            {"recordId": 203, "grantType": "GLOBAL", "expireTime": "2026-12-22 23:59:59", "available": True},
        ]
        self.scenario = {}          # 见模块注释
        self.use_requests = []      # 记录每次 POST use 的完整 payload
        self.served_paths = []      # 记录请求路径(便于断言没有多余调用)
        # 初始可用性快照:reset_cards 恢复到这里(103 号卡初始即不可用)
        self._initial_avail = {
            "fh": [c["available"] for c in self.five_hour_resets],
            "wk": [c["available"] for c in self.week_resets],
        }
        self.consumed = 0           # 通过 use 接口实际消耗的卡数

    # -------------------------------------------------- helpers for tests
    def set_percent(self, five_hour=None, week=None):
        with self.lock:
            if five_hour is not None:
                self.limits[0]["currentValue"] = round(self.limits[0]["usage"] * five_hour / 100)
                self.limits[0]["percentage"] = int(five_hour)
            if week is not None:
                self.limits[1]["currentValue"] = round(self.limits[1]["usage"] * week / 100)
                self.limits[1]["percentage"] = int(week)

    def reset_cards(self):
        with self.lock:
            for c, a in zip(self.five_hour_resets, self._initial_avail["fh"]):
                c["available"] = a
            for c, a in zip(self.week_resets, self._initial_avail["wk"]):
                c["available"] = a
            self.consumed = 0

    def used_count(self):
        with self.lock:
            return self.consumed


def make_handler(state: MockState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 静默
            pass

        # -------------------------------------------------- routing
        def _send_json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html, status=200):
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state.served_paths.append(self.path)
            if self.path.startswith("/usage"):
                if state.scenario.get("logged_out"):
                    self.send_response(302)
                    self.send_header("Location", "/login?redirect=%2Fusage")
                    self.end_headers()
                    return
                self._send_html(USAGE_PAGE)
            elif self.path.startswith("/login"):
                self._send_html(LOGIN_PAGE)
            elif self.path.startswith("/api/monitor/usage/quota/limit"):
                with state.lock:
                    data = {"limits": [dict(l) for l in state.limits], "level": "lite"}
                self._send_json({"code": 200, "msg": "操作成功", "data": data, "success": True})
            elif self.path.startswith("/api/biz/customer-package-reset/list"):
                with state.lock:
                    data = {
                        "customerId": 1, "targetType": "PERSONAL",
                        "organizationId": None, "projectId": None,
                        "lastFiveHourResetTime": "2026-10-25 21:56:22",
                        "lastWeekResetTime": "2026-10-20 21:13:27",
                        "fiveHourResets": [dict(c) for c in state.five_hour_resets],
                        "weekResets": [dict(c) for c in state.week_resets],
                    }
                self._send_json({"code": 200, "msg": "操作成功", "data": data, "success": True})
            else:
                self._send_json({"code": 404, "msg": "not found", "success": False}, 404)

        def do_POST(self):
            state.served_paths.append(self.path)
            if not self.path.startswith("/api/biz/customer-package-reset/use"):
                self._send_json({"code": 404, "msg": "not found", "success": False}, 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            with state.lock:
                state.use_requests.append(payload)

            if state.scenario.get("hang_use"):
                time.sleep(60)  # 前端 15s abort,这里挂住即可
                return

            if state.scenario.get("fail_use_once"):
                state.scenario["fail_use_once"] = False
                self._send_json({"code": 500, "msg": "指定的重置次数不可用，请刷新后重试",
                                 "success": False})
                return

            reset_type = payload.get("resetType")
            record_id = payload.get("recordId")
            with state.lock:
                pool = state.five_hour_resets if reset_type == "FIVE_HOUR" else state.week_resets
                card = next((c for c in pool if c["recordId"] == record_id), None)
                if card is None or not card["available"]:
                    self._send_json({"code": 500, "msg": "指定的重置次数不可用，请刷新后重试",
                                     "success": False})
                    return
                card["available"] = False
                state.consumed += 1
                # 官方语义:周卡同时回满5h;5h卡只重置5h
                state.limits[0]["currentValue"] = 0
                state.limits[0]["percentage"] = 0
                if reset_type == "WEEK":
                    state.limits[1]["currentValue"] = 0
                    state.limits[1]["percentage"] = 0
            self._send_json({"code": 200, "msg": "操作成功", "data": None, "success": True})

    return Handler


class MockServer:
    def __init__(self):
        self.state = MockState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    @property
    def usage_url(self) -> str:
        return f"{self.base}/usage"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":
    # 手动把玩 mock:python tests/mock_server.py
    s = MockServer()
    print("mock server:", s.usage_url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.stop()
