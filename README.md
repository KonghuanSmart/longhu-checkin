# 龙湖天街自动签到

一个自带 Web 看板的龙湖会员每日签到服务。**导入一次凭据，之后全自动**——token 会在每次执行时自动续期，不用反复抓包，也不依赖青龙面板。

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

![看板预览](docs/screenshot.png)

## 目录

- [为什么需要抓一次包](#为什么需要抓一次包)
- [功能特性](#功能特性)
- [快速开始](#快速开始)
- [抓包获取 token](#抓包获取-token)
- [导入账号](#导入账号)
- [使用说明](#使用说明)
- [配置项](#配置项)
- [HTTP API](#http-api)
- [工作原理](#工作原理)
- [成长值与会员等级](#成长值与会员等级)
- [目录结构](#目录结构)
- [数据与备份](#数据与备份)
- [常见问题](#常见问题)
- [安全提醒](#安全提醒)
- [免责声明](#免责声明)

## 为什么需要抓一次包

龙湖的账号体系里，手机号只是**绑定在微信身份上的附属信息**，不是独立登录凭证。这一点经过实测确认：

| 探测接口 | 结果 |
|---|---|
| `/api/passport/wxLogin/v1/sendSms`（不带 ticket） | `900700 绑定有效期已过，请返回后重新绑定` |
| `/api/passport/wxLogin/v1/check`（空参数） | `20001 三方类型不能为空；临时凭证不能为空；应用id不能为空` |
| 全站 `thirdType` 取值 | 只有一个：`wx-pub` |
| 密码登录接口 | 不存在（`setPassword` / `forgetPassword` 属于钱包支付密码） |

整个 passport 模块只有 4 个接口，`ticket` 必须由微信客户端生成的 `authCode` 换取，脚本无法凭空构造。所以**做不成「手机号 + 验证码」直接登录**，任何声称能这么做的方案都是假的。

但这件事只影响「第一次」。token 可以滚动续期，导入一次之后服务会自己续，**不需要长期抓包**。

## 功能特性

- **多账号管理**：一个面板管多个龙湖账号，卡片式展示
- **三种导入方式**：粘贴 cURL / 上传 HAR / 直接填 token，提交时立即校验，不留脏数据
- **token 自动续期**：每次执行前滚动续期，有效期重置为 30 天 → 导入一次后不会再过期
- **随机签到窗口**：默认在 08:00–09:00 之间随机取一个时刻执行，每天不同，页面上改完立即生效，无需重启
- **状态看板**：成长值、等级、珑珠余额、token 剩余天数、签到历史
- **严格三态判定**：区分「签到成功 / 今日已签 / 鉴权失败」，不会把失败当成功（这是很多同类脚本的隐患）
- **失败告警**：续期或签到失败时推送 **Bark / 企业微信 / 钉钉**，类型自动识别
- **零外部依赖**：SQLite 单文件存储，一个容器跑完，不需要数据库和青龙

## 快速开始

### Docker Compose（推荐）

```bash
git clone https://github.com/ken861222/longhu-checkin.git
cd longhu-checkin

docker compose up -d --build
```

打开 `http://你的IP:8088` 即可。数据落在 `./data/longhu.db`。

> `docker-compose.yml` 里默认用清华 pip 源加速构建。如果在海外服务器构建，把 `PIP_INDEX_URL` 那行删掉会更快。

### Docker CLI

```bash
docker build -t longhu-checkin .

docker run -d --name longhu-checkin \
  --restart unless-stopped \
  -p 8088:8000 \
  -v $(pwd)/data:/data \
  -e TZ=Asia/Shanghai \
  longhu-checkin
```

### 从源码运行

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

DATA_DIR=./data uvicorn app.main:app --host 0.0.0.0 --port 8000
```

需要 Python 3.10+（用到了 `dict | None` 语法）。

## 抓包获取 token

这是整个流程里唯一需要手工操作的环节，做完一次就不用再碰。

### 要抓的是什么

你要抓的是**微信小程序「龙湖」的业务请求**。以下特征可以帮你确认抓对了：

| 项目 | 值 |
|---|---|
| 请求域名 | `gw2c-hw-open.longfor.com` |
| 小程序 appid | `wx50282644351869da` |
| 关键请求头 | `X-LF-UserToken`（32 位十六进制） |
| 签到接口 | `POST /lmarketing-task-api-mvc-prod/openapi/task/v1/signature/clock` |

### 环境准备

- 一台电脑：macOS 或 Windows
- **微信 PC 版**（能打开小程序；Mac 版微信 3.8+ 也可以）
- 抓包工具：macOS 推荐 [Proxyman](https://proxyman.io/)（免费版够用），Windows 推荐 [Reqable](https://reqable.com/)，也可用 Charles / Fiddler
- **关闭所有 TUN 模式的代理**：Shadowrocket、Clash、Surge、V2Ray 等

> 最后一条是最容易被忽略、也是最常见的失败原因。TUN 型代理工作在 IP 层，会把流量在抓包工具看到之前就抢走，表现就是「抓包工具里一条 longfor 都没有，但微信明明能用」。

### macOS + Proxyman 详细步骤

**1. 安装并信任根证书**

启动 Proxyman → 菜单 `Certificate` → `Install Certificate on this Mac` → 输入密码。然后打开「钥匙串访问」：

```
钥匙串访问 → 系统 → 找到 "Proxyman CA" → 双击
→ 展开「信任」→ 使用此证书时：始终信任 → 关闭窗口输入密码
```

证书没被信任时，HTTPS 请求会解不开，只能看到灰色的 `CONNECT` 隧道。

**2. 确认 SSL 抓取已开启**

Proxyman 默认对所有域名开启 SSL Proxying。如果你之前手动关过，在菜单 `Certificate` → 勾选 `Enable SSL Proxying`。

**3. 退出其它代理工具**

在菜单栏里把 Shadowrocket / Clash 的「连接」关掉。确认默认路由回到物理网卡：

```bash
netstat -rn | grep default
# 应该是 en0 / en1，而不是 utun8 之类
```

**4. 完全退出微信**

`Cmd + Q` 真正退出，不是关窗口。小程序容器的网络服务进程只在启动时读取代理配置，不重启可能抓不到。

**5. 打开小程序并登录**

重新打开微信 → 搜索「**龙湖**」→ 进入小程序（也可能叫「珑珠」或「龙湖天街」）→ 用手机号 + 短信验证码登录。

**6. 触发签到请求**

进入会员 / 积分 / 任务中心，点一次「**签到**」。即使提示「今日已签到」也没关系，请求头里的凭据照样有效。

**7. 在 Proxyman 里找到它**

左下角过滤框输入 `longfor`（或者更精确的 `signature`），找到这条：

```
POST  gw2c-hw-open.longfor.com
      /lmarketing-task-api-mvc-prod/openapi/task/v1/signature/clock
```

点开确认 Request Headers 里存在 `X-LF-UserToken`，就说明抓对了。

**8. 复制成 cURL**

右键这条请求 → `Copy` → `Copy as cURL`。

**9. 收尾**

抓完后关掉 Proxyman（`Cmd+Q`），系统代理会自动恢复；需要的话再把 VPN 开回来。

### Windows + Reqable / Charles

流程完全一致，只有两处差异：

- 根证书装到「**受信任的根证书颁发机构**」，不是「个人」
- 微信 PC 版同样需要**退出后重开**才能读到代理设置

### 三种提交方式怎么选

| 方式 | 适用场景 | 说明 |
|---|---|---|
| **粘贴 cURL** | 最推荐 | 一条命令包含 UA、bu_code、channel，信息最全 |
| **上传 HAR** | 顺手抓了一堆请求 | 在 Proxyman 里选中域名 → 右键 Export → HAR；服务会自动挑匹配度最高的那条 |
| **直接填 token** | 你只拿到了那串 32 位字符 | 使用内置的默认 UA，通常也能正常工作 |

### 抓不到？对照排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 列表里完全没有 `longfor` | TUN 型代理抢走了流量 | 关掉 Shadowrocket / Clash 的 VPN，确认默认路由回到 `en0/en1` |
| 只有灰色 `CONNECT`，点开没有内容 | 根证书未信任，或没开 SSL Proxying | 重装证书并设为「始终信任」，确认 SSL Proxying 已勾选 |
| 微信的请求一条都看不到 | 微信没有完全退出 | `Cmd+Q` 彻底退出后重新打开 |
| 有请求但没有 `X-LF-UserToken` | 只停在首页，没进会员页 | 进会员 / 签到页面后再抓 |
| 提示「没找到 token」 | 抓的是静态资源或非业务请求 | 换一条带 token 的接口，如 `user-info` |
| 提示「凭据校验失败」 | token 已过期 | 重新抓一次；有效期 30 天 |

### 关于有效期

token 的有效期是 **30 天，且是滑动过期**——每次服务续期都会把有效期重新算成 30 天。所以只要服务每天正常运行，它就永远不会过期。

只有一种情况需要重新抓包：**服务连续停机超过 30 天**。这时日志里会出现 `token2key 未返回 key（token 可能已失效）`，重新抓一次导入即可。

## 导入账号

点右上角「**添加账号**」，三选一粘贴/上传，然后「**校验并添加**」。

提交时会立刻调用用户信息接口验证 token，通过后才写入数据库；失败会直接告诉你原因，不会产生无效账号。

一个账号可以随时删除，也可以点「**立即签到**」手动执行一轮（包含续期 + 签到 + 刷新资料）。

## 使用说明

### 看板

顶部五个指标：账号总数、今日签到成功数、待执行/失败数、最短 token 剩余天数、下次执行时间。

账号卡片显示：昵称、token 脱敏值、成长值、等级、珑珠余额、token 有效期进度条、最近一次签到结果。

### 设置

- **签到窗口**：起点默认 `08:00`，随机浮动默认 `60` 分钟，即每天在 **08:00–09:00 之间随机取一个时刻**执行，秒级随机，每天都不一样。浮动填 `0` 就退化成固定时刻。保存后立即重新排程，顶部「下次执行」会显示摇到的具体时间
- **告警推送地址**：Bark / 企业微信 / 钉钉三选一，**类型自动识别，不需要选**

### 告警推送

失败时推一条消息，避免「静默漏签」——这个项目的旧脚本就踩过坑：token 失效时把空响应当成「今日已签到」，日志显示成功但实际没签到。现在只要续期失败或签到失败，就会走告警。

**Bark（iOS，推荐个人用）**

填 Device Key 对应的地址即可，官方服务和自建服务都支持：

```
https://api.day.app/你的DeviceKey                    # 官方
https://bark.example.com/你的DeviceKey               # 自建
```

服务会 POST 到 `<你的域名>/push`，报文为：

```json
{
  "device_key": "你的DeviceKey",
  "title": "龙湖签到",
  "body": "【龙湖签到】1 个账号异常\n· 测试账号: token 已失效",
  "group": "龙湖签到"
}
```

推送会归到「龙湖签到」分组，历史消息好找。

**企业微信 / 钉钉群机器人**

填完整的 webhook 地址：

```
https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
https://oapi.dingtalk.com/robot/send?access_token=xxxxxxxx
```

报文是标准的 `{"msgtype": "text", "text": {"content": "..."}}`。

三种类型的识别规则：域名是 `day.app` 或其子域 → Bark；域名是 `qyapi.weixin.qq.com` / `oapi.dingtalk.com` → 群机器人；其余情况若路径只有一段（形如 `/DeviceKey`）→ 视为自建 Bark。

> 推送会忽略系统代理（`trust_env = False`）。如果你刚抓完包、系统代理还指向 Proxyman 而 Proxyman 已关闭，告警不会因此失败。

### 日志

页面底部是全局日志，也可通过 `GET /api/accounts/{id}/logs` 看单个账号。每条记录带动作类型（绑定 / 续期 / 签到 / 查询）和状态（ok / already / fail）。

## 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATA_DIR` | `/data` | SQLite 数据库存放目录 |
| `TZ` | `Asia/Shanghai` | 时区，影响定时任务的触发时刻 |

页面内设置项存在数据库里，不走环境变量：

| 设置项 | 默认值 | 说明 |
|---|---|---|
| `checkin_time` | `08:00` | 签到窗口起点，`HH:MM` 格式 |
| `checkin_jitter` | `60` | 随机浮动分钟数，`0` = 固定时刻；实际执行时间 = 起点 + `0~N` 分钟内的随机值 |
| `webhook_url` | 空 | 告警推送地址，支持 Bark / 企业微信 / 钉钉，留空则不推送 |

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | Web 看板 |
| `GET` | `/api/health` | 健康检查，返回数据目录 |
| `GET` | `/api/overview` | 概览统计、下次执行时间 |
| `GET` | `/api/accounts` | 账号列表（token 已脱敏） |
| `POST` | `/api/accounts/bind` | 绑定账号，`multipart/form-data` |
| `DELETE` | `/api/accounts/{id}` | 删除账号及其日志 |
| `POST` | `/api/accounts/{id}/run` | 立即执行一轮（续期 + 签到 + 刷新） |
| `GET` | `/api/accounts/{id}/logs` | 单个账号日志，`?limit=20` |
| `GET` | `/api/logs` | 全局日志，`?limit=50` |
| `GET` | `/api/settings` | 读取设置与下次执行时间 |
| `POST` | `/api/settings` | 保存设置，`form` 字段 `checkin_time` / `checkin_jitter` / `webhook_url` |

绑定账号示例：

```bash
# 方式一：粘贴 cURL
curl -X POST http://127.0.0.1:8088/api/accounts/bind \
  -F "mode=curl" \
  -F "name=我的账号" \
  -F "payload=$(pbpaste)"

# 方式二：上传 HAR
curl -X POST http://127.0.0.1:8088/api/accounts/bind \
  -F "mode=har" \
  -F "name=我的账号" \
  -F "file=@capture.har"

# 方式三：直接填 token
curl -X POST http://127.0.0.1:8088/api/accounts/bind \
  -F "mode=token" \
  -F "name=我的账号" \
  -F "payload=0123456789abcdef0123456789abcdef"
```

## 工作原理

```
┌──────────────┐   cURL / HAR / token   ┌─────────────────────┐
│  浏览器看板   │ ─────────────────────▶ │  FastAPI            │
└──────────────┘                        │  ├ parser  解析校验  │
       ▲                                │  ├ runner  编排      │
       │  状态 / 日志 / 统计             │  ├ apscheduler 定时  │
       └──────────────────────────────── │  └ SQLite  存储      │
                                        └──────────┬──────────┘
                                                   │ 08:00-09:00 随机
                                                   ▼
                                        ┌─────────────────────┐
                                        │ 龙湖开放平台         │
                                        └─────────────────────┘
```

### token 续期链

这是整套方案能「导入一次就长期可用」的关键：

```
token ──[ token2key ]──▶ key ──[ key2token ]──▶ token'（有效期重置 30 天）
```

- `token2key` 用当前 token 换一个**一次性**的 key（每次调用都不同）
- `key2token` 用这个 key 换回 token，返回 `expire: 2592000`（30 天）
- 返回的 token **通常与原来完全相同**，但服务端把有效期重新起算了

也就是说它是**滑动过期**的：每续一次，就从当天重新算 30 天。只要每天跑一次，就永远不会失效。

### 随机签到窗口

每天在同一分同一秒请求，一个月就是 30 次特征完全一致的调用，太像脚本。所以签到时点改成了在一个窗口内随机：

```
窗口 = [08:00, 09:00)
实际执行 = 08:00 + random(0, 3600) 秒
```

cron 触发器只能表达固定值，做不到这种随机，所以调度结构改成「一次性任务 + 巡场补位」：

- 用 `DateTrigger` 排一个一次性任务，时刻在窗口内随机（秒级），每天都不一样
- 另挂一个每 10 分钟跑一次的巡场任务：一次性任务执行完会被自动移除，巡场发现任务不在了就补上下一次（排到明天的窗口）
- 如果今天窗口起点已过但还没跑（比如 08:30 才启动容器），就从「现在」到窗口结束之间随机，不回溯到过去
- 如果今天已经跑过（数据库里记着 `last_run_date`），任何重排都直接排到明天，不会重复签

改设置后立即重建排程，不用重启容器。浮动填 `0` 就退化成原来的固定时刻。

### 请求签名

部分接口（如 `token2key`）需要 `X-LONGZHU-Sign` 签名头，算法为：

```
md5( 参数按键名排序后拼成 k=v|k=v + "&" + 毫秒时间戳 + "&" + 固定 secret )
```

secret 是小程序前端写死的常量，提取自公开的 H5 前端 JS（`sign-in-prize` 页面脚本）。它写在 `app/longhu.py` 的 `SIGN_SECRET_MICRO_APP` 里。如果哪天龙湖改版换了密钥，续期会失效，更新这个常量即可。

### 请求头里哪些是必需的

实测结论（逐个删头验证）：

| 请求头 | 必需 | 说明 |
|---|---|---|
| `X-LF-UserToken` | ✅ | 用户身份，缺了直接 `801007 用户未登录` |
| `X-LF-Bu-Code` | ✅ | 业务码，当前为 `C20400`，缺了同样报未登录 |
| `X-GAIA-API-KEY` | ✅ | 网关校验，错了返回 401 |
| `X-LF-DXRisk-Token` | ❌ | 风控令牌，实测不带也能签到——省掉了最大的时效隐患 |
| `Cookie` | ❌ | 实测不带也能签到，省掉了 `acw_tc` 30 分钟过期的问题 |
| `User-Agent` | ⚠️ | 建议与抓包时一致，UA 突变可能触发风控 |

## 成长值与会员等级

签到的产出是**成长值**，成长值累积到门槛就升级。小程序里的等级图只标了门槛，没标所需天数，这里把两头对上算清楚。

### 每日发放规则

小程序「每日签到」日历上每天的图标标注了当天能拿多少成长值（`+5` / `+8` / `+10`，带「连签」红标的是加成日）。按日历反推，规则是**按星期固定循环**的，既不是随机、也不是连签天数递增：

| 星期 | 成长值 | 备注 |
|---|---|---|
| 周一 | +5 | |
| 周二 | +5 | |
| 周三 | +8 | 连签日 |
| 周四 | +5 | |
| 周五 | +10 | 连签日 |
| 周六 | +5 | |
| 周日 | +10 | 连签日 |
| **一周合计** | **48** | 日均 6.86 |

日历上「再签到 2 天，可获得连签礼」这句提示可以交叉验证：当天是周一，再签 2 天到周三，正是下一个连签日。

### 等级门槛

| 等级 | 成长值门槛 |
|---|---|
| V1 | 0 |
| V2 | 100 |
| V3 | 2000 |
| V4 | 4000 |
| V5 | 10000 |

### 达成所需天数

按每周 48 成长值、每天不断签计算：

| 等级 | 门槛 | 从 0 累计签到 | 折合 | 从上一等级起 |
|---|---|---|---|---|
| V1 | 0 | 0 天 | 注册即得 | — |
| V2 | 100 | **15 天** | 约半个月 | 15 天 |
| V3 | 2000 | **292 天** | 约 9.6 个月 | 278 天 |
| V4 | 4000 | **584 天** | 约 1 年 7 个月 | 292 天 |
| V5 | 10000 | **1459 天** | 近 4 年（差 1 天） | **875 天** |

有意思的是难度分布很不均匀：V1→V2 只要半个月，V2→V3 和 V3→V4 各约 9～10 个月，**V4→V5 一跳 6000 成长值，比前三级加起来（584 天）还长 1.5 倍**。

假设从 2026-11-02 开始每天签到，时间点是：11/16 到 V2，2027-08-20 到 V3，2028-06-07 到 V4，2030-10-30 到 V5。

### 自己算

把下面这段存成 `.py` 直接跑，改 `start` 可以模拟不同起手日：

```python
DAILY  = [5, 5, 8, 5, 10, 5, 10]              # 周一 … 周日
LEVELS = [("V1", 0), ("V2", 100), ("V3", 2000), ("V4", 4000), ("V5", 10000)]

def days_to(target: int, start: int = 0) -> int:
    """从 start 表示的星期(0=周一)开始连续签到，累计成长值达到 target 所需天数。"""
    if target <= 0:
        return 0
    acc = days = 0
    while acc < target:
        acc += DAILY[(start + days) % 7]
        days += 1
    return days

for name, need in LEVELS:
    print(f"{name}  {need:>6}  {days_to(need):>5} 天")
```

起手是星期几对结果几乎没有影响——7 种起手日算下来 V5 都落在 1459 天，因为误差被长的周期吃掉了。如果账号已有成长值，把 `target` 换成 `门槛 - 当前成长值` 即可。

### 两点存疑

- 2026-11-01 是周日，按规则该发 +10，但日历上那一格是空白。如果月初真的不发放，每月会少约 10 点，V5 实际要多花十几天。
- 断签是否影响连签日的加成，没有经过实测验证。上表按**每天连续签到**计算，漏签会让实际时间变长。

## 目录结构

```
longhu-app/
├── app/
│   ├── main.py          FastAPI 路由与页面入口
│   ├── longhu.py        龙湖 API 客户端：签名 / 续期 / 签到 / 查询
│   ├── parser.py        cURL、HAR、token 三路解析
│   ├── runner.py        单账号流程编排 + 告警推送
│   ├── scheduler.py     APScheduler 定时任务
│   ├── db.py            SQLite 存储层
│   └── static/
│       └── index.html   单文件看板（原生 JS，无构建步骤）
├── docs/
│   └── screenshot.png
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 数据与备份

所有数据都在 `DATA_DIR/longhu.db` 这一个文件里（含账号、日志、设置）。

```bash
# 备份
cp data/longhu.db data/longhu.db.bak-$(date +%F)

# 迁移到新机器：把 data 目录整个拷过去即可
```

容器是用卷挂载的，删容器、重建镜像都不会丢数据。

## 常见问题

**Q：需要一直开着电脑吗？**
不需要。这东西本来就是给服务器/NAS 用的，24 小时挂在后台，每天跑一次就结束，内存占用约 40–50 MB。

**Q：token 会不会过期？**
不会，除非服务连续停机超过 30 天。每次执行前都会续期，把有效期重置为 30 天。

**Q：能管多个账号吗？**
可以，卡片式管理，每个账号独立续期、独立签到、独立记日志。

**Q：会封号吗？**
每天只调 3～4 次官方接口，频率远低于正常使用。但自动化操作本身存在风险，请自行评估，不要拿重要账号试。

**Q：为什么不像有些脚本那样只读 cookie？**
这个站点的签到接口用 `X-LF-UserToken` 而不是 cookie 鉴权，而且实测 cookie 根本不是必需的。用 token 更稳定，也不会因为 `acw_tc` 30 分钟过期而失效。

**Q：签到失败会怎样？**
连续失败会通过 webhook 告警（如果配了）。日志里能看出是续期失败还是签到失败，前者通常意味着需要重新导入凭据。

**Q：能推到手机吗？支持 Bark 吗？**
支持。在设置里填 `https://api.day.app/你的DeviceKey` 就行，推送会归到「龙湖签到」分组。自建 Bark 同样可用，填 `https://你的域名/DeviceKey`。企业微信、钉钉机器人也仍然支持，类型自动识别。

**Q：能配合青龙用吗？**
不需要。这个服务内置了定时器，本身就是青龙脚本的替代品。如果你之前用青龙跑同类脚本，直接把这个容器起来、把原任务停掉即可。

**Q：改代码后怎么更新？**

```bash
git pull
docker compose up -d --build
```

数据在卷里，不会丢。

## 安全提醒

**这个服务本身没有登录鉴权**，任何能访问端口的人都能看到你的账号状态、也能删账号。所以：

- ❌ 不要把 `8088` 直接映射到公网
- ✅ 只在内网 / 局域网使用
- ✅ 必须公网访问时，前面挂一层带认证的反向代理

Nginx + Basic Auth 示例：

```nginx
server {
    listen 443 ssl;
    server_name longhu.example.com;

    ssl_certificate     /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;

    location / {
        auth_basic           "restricted";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass           http://127.0.0.1:8088;
        proxy_set_header     Host $host;
    }
}
```

```bash
# 生成密码文件
htpasswd -c /etc/nginx/.htpasswd youruser
```

另外，接口响应里**不会**返回 token 原文、User-Agent 和 api-key，只给形如 `a1b2c3...c5d6` 的掩码。

## 免责声明

- 本项目仅供个人学习与技术研究使用，用于自动化自己账号的日常签到。
- 请勿用于商业用途，请勿批量注册、倒卖账号或任何违反服务条款的行为。
- 项目通过公开的前端接口与公开的前端常量实现，不涉及破解、绕过或攻击任何系统。
- 使用本项目产生的任何后果（包括但不限于账号异常、数据丢失）由使用者自行承担。

## License

[MIT](LICENSE)
