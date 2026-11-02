"""凭据解析：支持粘贴 cURL、上传 HAR、直接填 token 三种方式。

统一输出：{token, user_agent, api_key, bu_code, channel, note}
"""

import json
import re
from urllib.parse import urlparse

DEFAULT_BU_CODE = "C20400"
DEFAULT_CHANNEL = "C2"
DEFAULT_API_KEY = "c06753f1-3e68-437d-b592-b94656ea5517"


class ParseError(Exception):
    """解析失败，消息可直接展示给用户"""


def _pick_token(headers: dict) -> str | None:
    """按可信度取 token：签到用的 X-LF-UserToken 优先，其次 token/lmToken。"""
    for name in ("x-lf-usertoken", "token", "lmtoken", "authtoken"):
        value = headers.get(name)
        if value and len(value) >= 16 and value.lower() != "undefined":
            return value.strip()
    return None


def _score_url(url: str) -> int:
    """给不同接口打分，越像签到请求分越高。"""
    path = urlparse(url).path
    if "signature/clock" in path:
        return 100
    if "signature/page-info" in path:
        return 90
    if "user-info" in path or "my-info" in path:
        return 80
    if "user-lz-balance" in path:
        return 70
    if "longfor.com" in url:
        return 40
    return 0


def _finish(headers: dict, source: str, score: int) -> dict:
    token = _pick_token(headers)
    if not token:
        raise ParseError("没找到 token，请确认抓的是签到或会员相关请求")

    api_key = headers.get("x-gaia-api-key") or headers.get("x-gaia-apikey")
    return {
        "token": token,
        "user_agent": headers.get("user-agent"),
        "api_key": api_key if api_key and api_key.lower() != "undefined" else DEFAULT_API_KEY,
        "bu_code": headers.get("x-lf-bucode") or headers.get("x-lf-bu-code") or DEFAULT_BU_CODE,
        "channel": headers.get("x-lf-channel") or DEFAULT_CHANNEL,
        "source": source,
        "note": f"从 {source} 解析，接口匹配度 {score}",
    }


# --------------------------------------------------------------------------
# cURL
# --------------------------------------------------------------------------

def parse_curl(text: str) -> dict:
    if "curl" not in text.lower():
        raise ParseError("这段内容里没有 curl 命令")

    # 合并续行：反斜杠续行、Windows 的 ^ 续行
    flat = text.replace("\\\n", " ").replace("\\\r\n", " ").replace("^\n", " ")

    headers = {}
    # -H 'k: v' / --header "k: v" / --header='k: v'
    pattern = re.compile(r"(?:-H|--header)[\s=]+(['\"])(.*?)\1", re.S)
    for match in pattern.finditer(flat):
        raw = match.group(2)
        if ":" in raw:
            key, value = raw.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    body = ""
    body_match = re.search(r"(?:--data-raw|--data|-d)[\s=]+(['\"])(.*?)\1", flat, re.S)
    if body_match:
        body = body_match.group(2)

    url_match = re.search(r"https?://[^\s'\"]+", flat)
    url = url_match.group(0) if url_match else ""
    score = _score_url(url)

    result = _finish(headers, "cURL", score)
    if body:
        result["note"] += f"，请求体 {len(body)} 字节"
    return result


# --------------------------------------------------------------------------
# HAR
# --------------------------------------------------------------------------

def parse_har(raw: str | bytes) -> dict:
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        data = json.loads(raw)
    except Exception as exc:
        raise ParseError(f"HAR 文件不是合法 JSON：{exc}") from exc

    entries = (data.get("log") or {}).get("entries") or []
    if not entries:
        raise ParseError("HAR 里没有任何请求记录")

    best_headers = None
    best_score = -1

    for entry in entries:
        request = entry.get("request") or {}
        url = request.get("url") or ""
        if "longfor.com" not in url:
            continue
        headers = {}
        for item in (request.get("headers") or []):
            name = (item.get("name") or "").lower()
            if name:
                headers[name] = item.get("value") or ""
        if not _pick_token(headers):
            continue
        score = _score_url(url)
        if score > best_score:
            best_score = score
            best_headers = headers

    if not best_headers:
        raise ParseError("HAR 里没有带 token 的 longfor 请求，请确认抓到了签到或会员页面")

    return _finish(best_headers, "HAR", best_score)


# --------------------------------------------------------------------------
# 直接填 token
# --------------------------------------------------------------------------

def parse_token(token: str) -> dict:
    token = (token or "").strip()
    if not token:
        raise ParseError("token 不能为空")
    if len(token) < 16:
        raise ParseError("token 长度不对，正常是 32 位十六进制")
    if not re.fullmatch(r"[0-9a-zA-Z]+", token):
        raise ParseError("token 只能是字母和数字")
    return {
        "token": token,
        "user_agent": None,
        "api_key": DEFAULT_API_KEY,
        "bu_code": DEFAULT_BU_CODE,
        "channel": DEFAULT_CHANNEL,
        "source": "手动填写",
        "note": "手动填写 token，已使用默认 UA",
    }


def parse(mode: str, payload: str | bytes) -> dict:
    if mode == "curl":
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        return parse_curl(text)
    if mode == "har":
        return parse_har(payload)
    if mode == "token":
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        return parse_token(text)
    raise ParseError(f"不支持的导入方式：{mode}")
