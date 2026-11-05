#!/usr/bin/env node
/**
 * 顶象滑块（basic 类型）求解器：跑真实 SDK，拖对缺口，换回 captcha token。
 *
 * 背景（2026-11 实测）：
 *   - 龙湖 App 用的 SDK 是 ly-sta.longhu.net/dx-captcha/index.js（v1.3.41-372）
 *   - 主包默认去 cdn.dingxiang-inc.com 拉组件，但那份是 288 版，
 *     模块 ID 对不上，会报 "t is not a constructor"；必须换成 ly-sta 的 372 版
 *   - 失败重试有上限：同一张图第 3 次直接 4011 HIGH_RISK(retry=0)
 *
 * 输出一行 JSON 到 stdout：
 *   {"ok":true,"token":"...","sid":"...","y":48,"mode":"auto"}
 *
 * 用法：
 *   node captcha_browser.js --mode=manual --device=<指纹>
 *   node captcha_browser.js --mode=auto --device=<指纹> --dist=120
 */
const { chromium } = require("playwright-core");
const fs = require("fs");
const os = require("os");
const path = require("path");

const APP_ID = "d1a43734fc59aeae9f1562dbd70fdf54";
const API = "https://ly-ver.longhu.net";
const STA = "https://ly-sta.longhu.net/dx-captcha";
const COMP_URL = `${STA}/basic-captcha-js.js`;
const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36";

const args = Object.fromEntries(
  process.argv.slice(2).map((a) => {
    const m = a.match(/^--([^=]+)=(.*)$/);
    return m ? [m[1], m[2]] : [a.replace(/^--/, ""), true];
  })
);
const MODE = args.mode || "auto";
const DEVICE = args.device || "";
const DIST = Number(args.dist || 0);
const TIMEOUT = Number(args.timeout || 120) * 1000;

const HTML = `<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f5f5f5"><div id="mount"></div>
<script src="${STA}/index.js"></script></body></html>`;

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

(async () => {
  // 组件走 ly-sta 的 372 版；CDN 上的 288 版模块 ID 不匹配
  let comp = null;
  try {
    const r = await fetch(COMP_URL);
    comp = await r.text();
  } catch (e) {
    emit({ ok: false, error: "组件下载失败: " + e.message });
    return;
  }

  let browser;
  try {
    browser = await chromium.launch({ channel: "chrome", headless: MODE !== "manual" });
  } catch (e) {
    emit({ ok: false, error: "无法启动 Chrome（需要 playwright-core + 本机 Chrome）" });
    return;
  }

  const ctx = await browser.newContext({
    viewport: { width: 480, height: 760 },
    deviceScaleFactor: 1,
    userAgent: UA,
  });
  if (DEVICE) {
    await ctx.addInitScript(([k, v]) => {
      try { localStorage.setItem(k, v); } catch (e) {}
    }, ["_dx_app_" + APP_ID, DEVICE]);
  }
  const page = await ctx.newPage();
  await page.route("**/basic-captcha-js.js*", (r) =>
    r.fulfill({ status: 200, contentType: "application/javascript; charset=utf-8", body: comp })
  );

  const st = { sid: null, y: null, token: null, v1: null };
  page.on("response", async (r) => {
    const u = r.url();
    try {
      if (/\/api\/a\?/.test(u)) {
        const j = JSON.parse(await r.text());
        st.sid = j.sid; st.y = j.y;
      } else if (/\/api\/v1/.test(u)) {
        st.v1 = JSON.parse(await r.text());
        if (st.v1 && st.v1.success && st.v1.token) st.token = st.v1.token;
      }
    } catch (e) {}
  });

  let done = false;
  const finish = async (extra) => {
    if (done) return;
    done = true;
    emit({ ok: !!st.token, token: st.token, sid: st.sid, y: st.y, mode: MODE, ...extra });
    await browser.close().catch(() => {});
    process.exit(0);
  };

  const timer = setTimeout(() => finish({ error: "超时" }), TIMEOUT);

  await page.setContent(HTML, { waitUntil: "networkidle", timeout: 40000 }).catch(() => {});
  await page.evaluate(([APP_ID, API]) => {
    new window._dx.Captcha(document.getElementById("mount"), {
      appId: APP_ID, apiServer: API,
      onSuccess: () => {}, onClose: () => {}, onError: () => {},
    }).show();
  }, [APP_ID, API]);

  await page.waitForSelector("[id^=dx_captcha_basic_slider]", { timeout: 15000 }).catch(() => {});

  if (MODE === "auto" && DIST > 0) {
    const box = await page.evaluate(() => {
      const el = document.querySelector("[id^=dx_captcha_basic_slider]");
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    });
    if (box) {
      await page.mouse.move(box.x, box.y);
      await page.mouse.down();
      await page.waitForTimeout(120);
      for (let i = 1; i <= 26; i++) {
        const t = i / 26, e = 1 - Math.pow(1 - t, 3);
        await page.mouse.move(box.x + DIST * e, box.y + (Math.random() - 0.5));
        await page.waitForTimeout(12 + Math.random() * 14);
      }
      await page.waitForTimeout(250);
      await page.mouse.up();
    }
  }

  // 轮询等结果（manual 模式下由人来拖）
  try {
    for (let i = 0; i < Math.floor(TIMEOUT / 500); i++) {
      if (done) return;
      if (st.token) {
        clearTimeout(timer);
        return finish({});
      }
      if (st.v1 && !st.v1.success && st.v1.code === "4011") {
        clearTimeout(timer);
        return finish({ error: "HIGH_RISK：同图重试次数已达上限" });
      }
      await page.waitForTimeout(500);
    }
  } catch (e) {
    if (done) return;
  }
  clearTimeout(timer);
  if (!done) await finish({ error: "未拿到 token" });
})();
