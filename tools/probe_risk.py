"""对照实验：同一账号、同一时间，三种 X-LF-DXRisk-Token 各打一次 clock。

⚠️ 不要再在正式账号上跑这个脚本。

它故意用「现造指纹」和「空指纹」去打签到接口，会显著拉低该账号的风控评分。
2026-11-04 跑过一轮之后，该账号连着两天首签都被要求过滑块——而其他人的
实测是「换了新指纹只有第一次弹，之后连续几天都不弹」。换句话说：
滑块是风控评分的产物，而这个脚本本身就是在给自己扣分。

留着仅作原理说明：指纹不会导致接口失败（8040012 是「要人机验证」，不是
「指纹无效」），但当天的「已验证」状态确实存在。
"""
import os
import sys
sys.path.insert(0, "/Users/zhaoyueyin/WorkBuddy/2026-10-28-16-14-53/longhu-app")

# 硬性闸门：这个脚本会主动给账号扣信任分，禁止顺手跑。
if os.environ.get("I_KNOW_THIS_BURNS_TRUST") != "1":
    print(__doc__)
    print("已拒绝运行。确实要跑（仅限废号）请先设置 I_KNOW_THIS_BURNS_TRUST=1")
    raise SystemExit(1)

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
