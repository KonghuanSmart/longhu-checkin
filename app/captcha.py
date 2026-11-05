"""顶象滑块求解：调 tools/captcha_browser.js 换回 captcha token。

为什么需要它（2026-11-05 抓包实测）：
    当天第一次签到，clock 接口必返 8040012，缺的不是设备指纹，而是
    X-LF-DXRisk-Captcha-Token —— 也就是过完滑块后 /api/v1 发的那串 token。
    拼装格式是 "<token>:<指纹>"。

求解器跑的是真实 SDK：主包用 ly-sta 的 v1.3.41-372，组件强制替换成同版本的
basic-captcha-js.js（CDN 那份是 288 版，模块 ID 不匹配会直接报错）。

注意限额：同一张图最多试 2 次，第 3 次服务端返回 4011 HIGH_RISK 且 retry=0。
所以识别不准时不要连续重试，宁可换人工模式。
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
SCRIPT = TOOLS_DIR / "captcha_browser.js"

# 兼容开发机上的临时依赖目录
_NODE_PATHS = [
    TOOLS_DIR / "node_modules",
    Path("/tmp/pw-tools/node_modules"),
]


def _node_env() -> dict:
    env = os.environ.copy()
    found = [str(p) for p in _NODE_PATHS if p.is_dir()]
    if found:
        env["NODE_PATH"] = os.pathsep.join(found)
    return env


def available() -> tuple[bool, str]:
    """检查 node / playwright-core / Chrome 是否就绪。"""
    if shutil.which("node") is None:
        return False, "没有 node"
    if not SCRIPT.is_file():
        return False, "缺少 tools/captcha_browser.js"
    if not any(p.is_dir() for p in _NODE_PATHS):
        return False, "没有 playwright-core（npm i playwright-core）"
    return True, "ok"


def solve(device: str, mode: str = "manual", dist: int | None = None,
          timeout: int = 120) -> tuple[str | None, str]:
    """跑一次滑块求解。返回 (token, 说明)。

    mode="manual" 会弹出浏览器窗口，需要人拖一下（最可靠）；
    mode="auto"   会按 dist 自动拖，dist 不准就白费一次机会。
    """
    ok, why = available()
    if not ok:
        return None, why

    cmd = ["node", str(SCRIPT), f"--mode={mode}", f"--timeout={timeout}"]
    if device:
        cmd.append(f"--device={device}")
    if dist:
        cmd.append(f"--dist={int(dist)}")

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout + 30, env=_node_env()
        )
    except subprocess.TimeoutExpired:
        return None, "求解器超时"

    line = ""
    for text in (proc.stdout or "").splitlines():
        text = text.strip()
        if text.startswith("{"):
            line = text
    if not line:
        return None, (proc.stderr or "无输出")[-200:]

    try:
        data = json.loads(line)
    except ValueError:
        return None, "输出无法解析"

    if data.get("ok") and data.get("token"):
        return data["token"], "ok"
    return None, data.get("error") or "未拿到 token"


def captcha_header(token: str, device: str) -> str:
    """拼成接口要的格式：token 在前，指纹在后，冒号分隔。"""
    return f"{token}:{device}" if device else token


def mode_from_env() -> str:
    """LONGHU_CAPTCHA=manual / auto / off（默认 off）。"""
    raw = (os.environ.get("LONGHU_CAPTCHA") or "off").strip().lower()
    return raw if raw in ("manual", "auto") else "off"
