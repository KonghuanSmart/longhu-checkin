"""龙湖签到服务：Web 前端 + REST API + 内置定时任务。"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, longhu, parser, runner, scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    scheduler.start()
    logger.info("服务已启动，签到窗口 %s", scheduler.window_text())
    yield


app = FastAPI(title="龙湖签到", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------

def _decorate(account: dict) -> dict:
    item = dict(account)
    for key in ("token", "user_agent", "api_key"):  # 敏感字段不外发
        item.pop(key, None)
    token = account.get("token") or ""
    item["token_mask"] = f"{token[:6]}...{token[-4:]}" if len(token) > 12 else "****"
    item["days_left"] = runner.days_left(account.get("expire_at"))
    return item


@app.get("/api/accounts")
def api_accounts():
    return [_decorate(acc) for acc in db.list_accounts()]


@app.post("/api/accounts/bind")
async def api_bind(
    mode: str = Form(...),
    name: str = Form(""),
    payload: str = Form(""),
    file: UploadFile | None = File(None),
):
    if mode == "har":
        if file is None:
            raise HTTPException(400, "HAR 模式需要上传文件")
        raw = await file.read()
    else:
        raw = payload

    try:
        parsed = parser.parse(mode, raw)
    except parser.ParseError as exc:
        raise HTTPException(400, str(exc)) from exc

    account = {
        "token": parsed["token"],
        "user_agent": parsed.get("user_agent") or longhu.DEFAULT_UA,
        "api_key": parsed.get("api_key"),
        "bu_code": parsed.get("bu_code", "C20400"),
        "channel": parsed.get("channel", "C2"),
    }

    # 校验 token：先直接查资料，失败则尝试续期后重试
    profile = None
    verify_error = ""
    try:
        profile = longhu.probe_token(
            account["token"], account["user_agent"], account["bu_code"], account["channel"]
        )
    except longhu.LonghuError as exc:
        verify_error = str(exc)
        ok, new_token = longhu.refresh_token(account)
        if ok:
            account["token"] = new_token
            try:
                profile = longhu.probe_token(
                    account["token"], account["user_agent"],
                    account["bu_code"], account["channel"],
                )
                verify_error = ""
            except longhu.LonghuError as exc2:
                verify_error = str(exc2)

    if profile is None:
        raise HTTPException(400, f"凭据校验失败：{verify_error or '未知原因'}，请重新抓取")

    final_name = (name or "").strip() or profile.get("nickname") or f"龙湖账号 {len(db.list_accounts()) + 1}"

    account_id = db.create_account({
        "name": final_name,
        "token": account["token"],
        "user_agent": account["user_agent"],
        "api_key": account["api_key"],
        "bu_code": account["bu_code"],
        "channel": account["channel"],
        "growth_value": profile.get("growth_value", 0),
        "level_name": profile.get("level_name", ""),
        "balance": profile.get("balance", 0),
        "nickname": profile.get("nickname", ""),
        "expire_at": longhu.expire_at(),
    })
    db.add_log(account_id, final_name, "bind", "ok", f"{parsed['note']}，凭据校验通过")

    return {"ok": True, "id": account_id, "name": final_name, "profile": profile}


@app.delete("/api/accounts/{account_id}")
def api_delete(account_id: int):
    if not db.get_account(account_id):
        raise HTTPException(404, "账号不存在")
    db.delete_account(account_id)
    return {"ok": True}


@app.post("/api/accounts/{account_id}/run")
def api_run(account_id: int, app: str | None = None):
    """立即跑一轮。app=auto 强制尝试 APP 入口，app=off 跳过，默认按设置。"""
    account = db.get_account(account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    mode = (app or "").strip().lower() or None
    if mode and mode not in runner.APP_MODES:
        raise HTTPException(400, "app 只能是 auto / manual / off")
    result = runner.run_account(account, do_checkin=True, app_mode=mode)
    return result


@app.get("/api/accounts/{account_id}/logs")
def api_account_logs(account_id: int, limit: int = 20):
    return db.list_logs(account_id, limit)


# --------------------------------------------------------------------------
# 概览 / 设置 / 日志
# --------------------------------------------------------------------------

@app.get("/api/overview")
def api_overview():
    accounts = db.list_accounts()
    today = date.today().isoformat()
    stats = {"ok": 0, "already": 0, "fail": 0, "pending": 0}
    min_days = None

    for acc in accounts:
        days = runner.days_left(acc.get("expire_at"))
        if days is not None:
            min_days = days if min_days is None else min(min_days, days)
        ran_today = (acc.get("last_run_at") or "")[:10] == today
        if not acc.get("enabled"):
            stats["pending"] += 1
        elif not ran_today:
            stats["pending"] += 1
        else:
            status = acc.get("last_status") or ""
            stats[status if status in stats else "fail"] += 1

    return {
        "total": len(accounts),
        "stats": stats,
        "min_days": min_days,
        "next_run": scheduler.next_run_text(),
        "checkin_time": db.get_settings().get("checkin_time", "08:05"),
    }


@app.get("/api/logs")
def api_logs(limit: int = 50):
    return db.list_logs(None, limit)


@app.get("/api/settings")
def api_get_settings():
    data = db.get_settings()
    return {
        "checkin_time": data.get("checkin_time", "08:00"),
        "checkin_jitter": data.get("checkin_jitter", "60"),
        "window": scheduler.window_text(),
        "webhook_url": data.get("webhook_url", ""),
        "app_checkin_mode": data.get("app_checkin_mode", "manual"),
        "next_run": scheduler.next_run_text(),
    }


@app.post("/api/settings")
def api_save_settings(
    checkin_time: str = Form(...),
    webhook_url: str = Form(""),
    checkin_jitter: str | None = Form(None),
    app_checkin_mode: str | None = Form(None),
):
    try:
        hour, minute = checkin_time.split(":")
        assert 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59
    except Exception:
        raise HTTPException(400, "时间格式应为 HH:MM") from None

    values = {"checkin_time": checkin_time, "webhook_url": webhook_url.strip()}
    if checkin_jitter is not None and checkin_jitter.strip() != "":
        try:
            jitter = int(float(checkin_jitter))
            assert 0 <= jitter <= 1439
        except Exception:
            raise HTTPException(400, "随机浮动应为 0–1439 之间的分钟数") from None
        values["checkin_jitter"] = str(jitter)

    if app_checkin_mode is not None and app_checkin_mode.strip() != "":
        mode = app_checkin_mode.strip().lower()
        if mode not in runner.APP_MODES:
            raise HTTPException(400, "APP 入口方式只能是 auto / manual / off")
        values["app_checkin_mode"] = mode

    db.save_settings(values)
    scheduler.reschedule()
    return {
        "ok": True,
        "window": scheduler.window_text(),
        "next_run": scheduler.next_run_text(),
    }


@app.get("/api/health")
def api_health():
    return {"ok": True, "data_dir": os.environ.get("DATA_DIR", "./data")}
