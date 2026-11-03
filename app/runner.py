"""任务编排：续期 → 签到 → 状态刷新 → 落库 → 告警。"""

import logging
import math
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from . import db, longhu

logger = logging.getLogger(__name__)

BARK_DEFAULT_HOSTS = ("day.app",)
BARK_TITLE = "龙湖签到"
BOT_HOSTS = ("qyapi.weixin.qq.com", "oapi.dingtalk.com")


def days_left(expire_at: str | None) -> int | None:
    if not expire_at:
        return None
    try:
        dt = datetime.fromisoformat(expire_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt - datetime.now(timezone.utc)
        # 向上取整：刚续期完应显示 30 天，而不是 29 天
        return max(0, math.ceil(delta.total_seconds() / 86400))
    except Exception:
        return None


def _is_bark(url: str) -> bool:
    """判断是不是 Bark 地址。

    支持官方 https://api.day.app/<key> 和自建服务 https://bark.example.com/<key>。
    自建服务靠「路径只有一段、且不是群机器人域名」来判断。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in BARK_DEFAULT_HOSTS):
        return True
    if any(bot in host for bot in BOT_HOSTS):
        return False
    segments = [s for s in parsed.path.split("/") if s]
    return len(segments) == 1 and not parsed.query


def _build_notify(url: str, text: str) -> tuple[str, dict]:
    """按 webhook 类型返回 (请求地址, 请求体)。

    - Bark：POST <origin>/push，body 里带 device_key
    - 企业微信 / 钉钉群机器人：{"msgtype": "text", ...}
    """
    if _is_bark(url):
        parsed = urlparse(url)
        key = [s for s in parsed.path.split("/") if s][0]
        scheme = parsed.scheme or "https"
        return f"{scheme}://{parsed.netloc}/push", {
            "device_key": key,
            "title": BARK_TITLE,
            "body": text,
            "group": BARK_TITLE,
        }
    return url, {"msgtype": "text", "text": {"content": text}}


def notify(text: str):
    """发告警（Bark / 企业微信 / 钉钉）。没配 webhook 就静默跳过。"""
    url = (db.get_settings().get("webhook_url") or "").strip()
    if not url:
        return

    endpoint, payload = _build_notify(url, text)
    session = requests.Session()
    session.trust_env = False  # 忽略系统代理，避免被本机抓包工具拦住
    try:
        resp = session.post(endpoint, json=payload, timeout=10)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        # Bark 用 code（成功为 200），企业微信/钉钉用 errcode（成功为 0）
        code = data.get("code", data.get("errcode", 0))
        if resp.status_code >= 400 or code not in (0, 200):
            logger.warning("webhook 推送返回异常: HTTP %s %s", resp.status_code, data)
    except Exception as exc:  # 告警失败不影响主流程
        logger.warning("webhook 推送失败: %s", exc)
    finally:
        session.close()


def run_account(account: dict, do_checkin: bool = True) -> dict:
    """执行单个账号的完整流程，返回可直接给前端用的结果。"""
    acc_id = account["id"]
    acc_name = account["name"]
    result = {
        "account_id": acc_id,
        "name": acc_name,
        "refreshed": False,
        "checkin_status": "skipped",
        "checkin_message": "",
        "profile": {},
        "error": "",
    }

    # ---- 1. 续期 ----
    try:
        ok, payload = longhu.refresh_token(account)
    except longhu.LonghuError as exc:
        ok, payload = False, str(exc)

    if ok:
        result["refreshed"] = True
        if payload != account.get("token"):
            db.update_account(acc_id, token=payload)
            account["token"] = payload
        db.update_account(acc_id, expire_at=longhu.expire_at())
        db.add_log(acc_id, acc_name, "refresh", "ok", "token 有效期已续至 30 天")
    else:
        db.add_log(acc_id, acc_name, "refresh", "fail", payload)
        result["error"] = payload
        logger.warning("[%s] 续期失败: %s", acc_name, payload)

    # ---- 2. 签到 ----
    if do_checkin:
        try:
            status, message = longhu.checkin(account)
        except longhu.LonghuError as exc:
            status, message = "fail", str(exc)
        result["checkin_status"] = status
        result["checkin_message"] = message
        db.add_log(acc_id, acc_name, "checkin", status, message)
        db.update_account(acc_id, last_status=status, last_message=message)

        if status == "fail":
            # 签到失败一律告警。之前只在「续期也失败」时才推，导致签到被拒
            # （例如上游返回「网络故障，请稍后再试」）时完全静默。
            if not ok:
                tail = "该账号需要重新导入凭据。"
            else:
                tail = "将按排程在当天内自动重试。"
            notify(
                f"【龙湖签到】{acc_name} 失败\n"
                f"续期: {payload}\n签到: {message}\n{tail}"
            )

    # ---- 3. 刷新资料 ----
    try:
        profile = longhu.fetch_profile(account)
        result["profile"] = profile
        db.update_account(
            acc_id,
            growth_value=profile.get("growth_value", 0),
            level_name=profile.get("level_name", ""),
            balance=profile.get("balance", 0),
            nickname=profile.get("nickname", ""),
        )
    except longhu.LonghuError as exc:
        result["error"] = result["error"] or str(exc)
        db.add_log(acc_id, acc_name, "query", "fail", str(exc))

    db.update_account(acc_id, last_run_at=db.now_iso())

    days = days_left(db.get_account(acc_id).get("expire_at"))
    result["days_left"] = days
    return result


def run_all(do_checkin: bool = True) -> list[dict]:
    accounts = db.list_accounts(only_enabled=True)
    results = []
    for account in accounts:
        try:
            results.append(run_account(account, do_checkin=do_checkin))
        except Exception as exc:  # 单个账号异常不影响其他账号
            logger.exception("[%s] 执行异常", account.get("name"))
            db.add_log(account["id"], account["name"], "run", "fail", f"异常: {exc}")
            results.append({
                "account_id": account["id"], "name": account["name"],
                "checkin_status": "fail", "checkin_message": str(exc), "error": str(exc),
            })
    return results


def daily_summary(results: list[dict]):
    """每日汇总推送：有失败才提醒，全成功则发一条简报。"""
    if not results:
        return
    failed = [r for r in results if r.get("checkin_status") == "fail"]
    ok = [r for r in results if r.get("checkin_status") == "ok"]
    already = [r for r in results if r.get("checkin_status") == "already"]

    if failed:
        lines = [f"【龙湖签到】{len(failed)} 个账号异常"]
        for item in failed:
            lines.append(f"· {item['name']}: {item.get('checkin_message') or item.get('error')}")
        notify("\n".join(lines))
    elif ok:
        notify(f"【龙湖签到】成功 {len(ok)} 个，已签 {len(already)} 个")
