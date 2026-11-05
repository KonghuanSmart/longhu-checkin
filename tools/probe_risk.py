"""对照实验：同一账号、同一时间，三种 X-LF-DXRisk-Token 各打一次 clock。

目的：判断「8040012 需要过滑块」是常态，还是只针对某个被标记的指纹。
"""
import sys
sys.path.insert(0, "/Users/zhaoyueyin/WorkBuddy/2026-10-28-16-14-53/longhu-app")

from app import longhu

TOKEN = "002e3524c6c64d5e93ea2595ce1ac5ec"
FP_APP = "6ae93a6cUDMfhHgK6eYXWjqtu1qMG9bAJdij8Er1"
BODY = {"activity_no": longhu.APP_ACTIVITY_NO}

base = {"token": TOKEN, "bu_code": "L00602", "channel": "L0"}

cases = [
    ("App指纹", FP_APP),
    ("udid新造", None),   # None 表示现取
    ("空指纹", ""),
]

for name, fp in cases:
    if fp is None:
        acct = dict(base)
        fp = longhu.risk_token(acct, force=True)
        print(f"\n=== {name}: {fp or '(取不到)'}")
    acct = dict(base, dx_token=fp)
    h = longhu._task_headers(acct)
    # page-info 先验证头部可用
    pi = longhu._request("POST", f"{longhu.BASE}/lmarketing-task-api-mvc-prod/openapi/task/v1/signature/page-info", h, BODY)
    print(f"  page-info -> {pi.get('code')} {pi.get('message')}")
    res = longhu._request("POST", f"{longhu.BASE}/lmarketing-task-api-mvc-prod/openapi/task/v1/signature/clock", h, BODY)
    print(f"  clock     -> {pi and ''}{res.get('code')} {res.get('message')} data={str(res.get('data'))[:120]}")
