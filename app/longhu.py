"""龙湖天街 API 客户端。

封装了三件事：
1. refresh_token —— 用 token2key + key2token 把 token 有效期滚动重置为 30 天
2. checkin       —— 每日签到，严格区分「成功 / 今日已签 / 鉴权失败」
3. fetch_profile —— 拉取成长值、等级、珑珠余额

所有请求强制直连（trust_env=False），避免宿主机的抓包代理干扰。
"""

import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://gw2c-hw-open.longfor.com"

# 小程序前端写死的签名密钥（来自 sign-in-prize 的 JS）
SIGN_SECRET_MICRO_APP = "Q74eKtH5LePYfSjIiflUbCL2gxjTa7rF"

API_KEY_MAIN = "c06753f1-3e68-437d-b592-b94656ea5517"
API_KEY_MEMBER = "98717e7a-a039-46af-8143-be7558a089c0"
API_KEY_WALLET = "f4a2290e-c382-45b7-949c-3253d07e9b5b"

ACTIVITY_NO = "11111111111686241863606037740000"

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/144.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Mac MacWechat/WMPF MacWechat/3.8.7(0x13080712) "
    "UnifiedPCMacWechat(0xf2641d55) XWEB/25561"
)

THIRTY_DAYS = 30


class LonghuError(Exception):
    """业务层可读错误"""


# --------------------------------------------------------------------------
# 底层请求
# --------------------------------------------------------------------------

def _decode(resp: requests.Response) -> dict:
    """接口有时返回裸 JSON，有时返回 base64 包裹的 JSON，两种都兼容。"""
    text = resp.text or ""
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return json.loads(base64.b64decode(text).decode("utf-8"))
    except Exception:
        pass
    return {"code": "parse_error", "message": text[:200]}


def _request(method: str, url: str, headers: dict, body: dict | None = None,
             timeout: int = 20) -> dict:
    session = requests.Session()
    session.trust_env = False  # 忽略系统代理，直连
    clean = {k: v for k, v in headers.items() if v not in (None, "")}
    try:
        if method.upper() == "GET":
            resp = session.get(url, headers=clean, timeout=timeout)
        else:
            resp = session.post(url, headers=clean, json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise LonghuError(f"网络请求失败: {exc}") from exc
    finally:
        session.close()
    return _decode(resp)


def sign_headers(body: dict | None = None) -> dict:
    """复刻小程序签名：md5(排序参数 + '&' + 毫秒时间戳 + '&' + secret)"""
    parts = []
    for key in sorted((body or {}).keys()):
        value = body[key]
        if isinstance(value, (dict, list)):
            continue
        text = str(value)
        if text.strip() and text != "null":
            parts.append(f"{key}={text}")
    joined = "|".join(parts)
    ts = str(int(time.time() * 1000))
    raw = (joined + "&" if joined else "") + ts + "&" + SIGN_SECRET_MICRO_APP
    return {
        "X-LONGZHU-TimeStamp": ts,
        "X-Client-Type": "microApp",
        "X-LONGZHU-Sign": hashlib.md5(raw.encode("utf-8")).hexdigest(),
    }


# --------------------------------------------------------------------------
# 业务接口
# --------------------------------------------------------------------------

def refresh_token(account: dict) -> tuple[bool, str]:
    """滚动续期。成功返回 (True, 新token)，失败返回 (False, 原因)。

    token 本身通常不变，但服务端会把有效期重新算成 30 天。
    """
    token = account.get("token") or ""
    if not token:
        return False, "缺少 token"

    headers = {
        "lmToken": token,
        "X-LF-Bucode": account.get("bu_code", "C20400"),
        "X-LF-Api-Version": "v2_6_0",
        "X-LF-Channel": account.get("channel", "C2"),
        "X-LF-App-Version": "2.6.0",
        "Content-Type": "application/json",
        "User-Agent": account.get("user_agent") or DEFAULT_UA,
        "Origin": "https://longzhu.longfor.com",
        "Referer": "https://longzhu.longfor.com/",
    }
    headers.update(sign_headers({}))

    res = _request("POST", f"{BASE}/supera/mine/v2_6_0/token/token2key", headers, {})
    key = (res.get("data") or {}).get("key")
    if not key:
        return False, res.get("message") or "token2key 未返回 key（token 可能已失效）"

    wallet_headers = {
        "X-Gaia-Api-Key": API_KEY_WALLET,
        "systype": "WALLET",
        "Content-Type": "application/json",
        "User-Agent": account.get("user_agent") or DEFAULT_UA,
        "Origin": "https://longball.longhu.net",
        "Referer": "https://longball.longhu.net/",
    }
    res2 = _request(
        "POST",
        f"{BASE}/lmember-lpns-api-prod/api/lpns/auth/v1/key2token",
        wallet_headers,
        {"key": key, "buCode": account.get("bu_code", "C20400"), "channel": account.get("channel", "C2")},
    )
    data = res2.get("data") or {}
    if res2.get("code") == "0000" and data.get("token"):
        return True, data["token"]
    return False, res2.get("message") or "key2token 未返回 token"


def _task_headers(account: dict) -> dict:
    """活动接口（page-info / clock）共用的请求头。"""
    return {
        "User-Agent": account.get("user_agent") or DEFAULT_UA,
        "Origin": "https://longzhu.longfor.com",
        "Referer": "https://longzhu.longfor.com/",
        "X-LF-DXRisk-Source": "5",
        "X-LF-Bu-Code": account.get("bu_code", "C20400"),
        # 签到必须用签到专用 key（前端写死的常量）。若沿用抓包里的 key，
        # 用户粘贴的是会员接口 cURL 时会拿到 member key，导致 401。
        "X-GAIA-API-KEY": API_KEY_MAIN,
        "X-LF-UserToken": account.get("token") or "",
        "X-LF-Channel": account.get("channel", "C2"),
        "X-LF-DXRisk-Captcha-Token": "undefined",
        "token": account.get("token") or "",
        "Content-Type": "application/json;charset=UTF-8",
    }


def sign_state(account: dict) -> bool | None:
    """查询「今天是否已签到」。True=已签，False=未签，None=查不到。

    以服务端返回的日历为准，比依赖 clock 接口的 is_popup 可靠：
    clock 在「已签到」和「签到被拒」两种情况下都可能不返回奖励明细。
    """
    try:
        res = _request(
            "POST",
            f"{BASE}/lmarketing-task-api-mvc-prod/openapi/task/v1/signature/page-info",
            _task_headers(account),
            {"activity_no": ACTIVITY_NO},
        )
    except LonghuError:
        return None
    if res.get("code") != "0000":
        return None

    data = res.get("data") or {}
    # 用服务端时间判断“今天”，避免容器时区与龙湖不一致
    today = datetime.now()
    raw = data.get("current_date_time")
    if raw:
        try:
            today = datetime.fromtimestamp(int(raw) / 1000)
        except (TypeError, ValueError):
            pass

    for item in data.get("seven_days_signs") or []:
        try:
            day = (int(item.get("sign_year")), int(item.get("sign_month")), int(item.get("sign_day")))
        except (TypeError, ValueError):
            continue
        if day == (today.year, today.month, today.day):
            # 20 = 已签到，10 = 未签到
            return int(item.get("sign_status") or 0) == 20
    return None


def checkin(account: dict) -> tuple[str, str]:
    """每日签到。返回 (status, message)

    status: ok（签到成功）/ already（今日已签）/ fail（失败）
    """
    # 先查状态：已签就不要再打签到接口，免得无谓地触发风控
    before = sign_state(account)
    if before is True:
        return "already", "今日已签到"

    headers = _task_headers(account)
    try:
        res = _request(
            "POST",
            f"{BASE}/lmarketing-task-api-mvc-prod/openapi/task/v1/signature/clock",
            headers,
            {"activity_no": ACTIVITY_NO},
        )
    except LonghuError as exc:
        return "fail", f"签到请求失败：{exc}"

    code = str(res.get("code") or "")
    data = res.get("data") or {}
    message = res.get("message") or ""

    if code == "0000":
        if data.get("is_popup") == 1:
            reward = 0
            for item in (data.get("reward_info") or []):
                reward += int(item.get("reward_num") or 0)
            return "ok", f"签到成功，获得 {reward} 积分"
        # is_popup=0 有两种可能：本来已签，或接口假成功。用日历复核。
        after = sign_state(account)
        if after is False:
            return "fail", "签到接口返回成功但日历仍显示未签，请稍后重试"
        return "already", "今日已签到"

    if "未登录" in message or "登录已过期" in message or code == "801001":
        return "fail", "鉴权失败：token 已失效，需要重新导入"

    # 兜底：接口报错但可能实际上已经签上（历史上出现过报错但状态已更新）
    if sign_state(account) is True:
        return "ok", f"签到成功（接口返回 {code}，已按日历确认）"

    if before is False:
        detail = f"{message or '未知错误'}"
        return "fail", f"签到失败：{detail}（code={code or '无'}）"
    return "fail", f"签到失败：{message or '未知错误'}（code={code or '无'}）"


def fetch_profile(account: dict) -> dict:
    """拉取成长值 / 等级 / 珑珠。token 无效时抛 LonghuError。"""
    headers = {
        "User-Agent": account.get("user_agent") or DEFAULT_UA,
        "lmToken": account.get("token") or "",
        "X-Gaia-Api-Key": API_KEY_MEMBER,
        "X-LF-Bucode": account.get("bu_code", "C20400"),
        "X-LF-Channel": account.get("channel", "C2"),
        "X-LF-Api-Version": "v2_6_0",
        "Content-Type": "application/json",
    }
    res = _request(
        "GET",
        f"{BASE}/supera/member/api/bff/pages/v2_6_0/v1/user-info",
        headers,
    )
    if res.get("code") != "0000":
        raise LonghuError(res.get("message") or "用户信息查询失败")

    data = res.get("data") or {}
    profile = {
        "growth_value": int(data.get("growthValue") or 0),
        "level_name": data.get("levelName") or "",
        "nickname": data.get("nickName") or data.get("nickname") or "",
    }

    balance_headers = dict(headers)
    try:
        res2 = _request(
            "GET",
            f"{BASE}/supera/member/api/bff/pages/v2_6_0/v2/user-lz-balance",
            balance_headers,
        )
        if res2.get("code") == "0000":
            bdata = res2.get("data") or {}
            profile["balance"] = float(bdata.get("balance") or 0)
            profile["expiring_lz"] = float(bdata.get("expiringLz") or 0)
    except LonghuError:
        pass
    return profile


def probe_token(token: str, user_agent: str | None = None,
                bu_code: str = "C20400", channel: str = "C2") -> dict:
    """校验一个 token 是否可用，返回 profile。失败抛 LonghuError。"""
    return fetch_profile({
        "token": token,
        "user_agent": user_agent or DEFAULT_UA,
        "api_key": API_KEY_MAIN,
        "bu_code": bu_code,
        "channel": channel,
    })


def expire_at(seconds: int = THIRTY_DAYS * 86400) -> str:
    """按当前时间推算过期时刻（ISO 字符串，本地时区）。"""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
