# 底部双转换选币器

基于交易所公开行情的网页扫描器。按 **观察 → 临界 → 双转换确认 → 启动** 四个阶段筛选下跌反转、低位横盘蓄势和双转换后的二次启动准备。只读行情，不连接交易账户，也不执行交易。

## 运行

需要 Python 3.11+。Windows 本地使用可双击 `启动选币器.bat`，首次启动会安装依赖，然后在 `http://127.0.0.1:8911` 扫描。如果当前电脑已配置名为 `BottomReversalScanner` 的按需后台任务，启动脚本会运行该任务，关闭命令窗口不会停止服务；否则请保持命令窗口打开。电脑重启后需再次启动服务。公网部署不使用这个 bat 文件。

也可手动运行：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8911
```

打开 `http://127.0.0.1:8911`。网页关闭后，服务仍需手动停止。筛选设置保存在当前浏览器的 localStorage。

扫描结果现会保存在本地 `data/last_task.json`，服务重启后可继续查看上一轮结果。`data/` 不包含在源码压缩包中。

## 公网部署与自动更新

已部署网站：[https://crypto-scanner-web-bzds.onrender.com](https://crypto-scanner-web-bzds.onrender.com)。Render 免费实例在空闲后会休眠，首次访问可能需要等待启动。

项目是 FastAPI 服务，使用仓库根目录的 `render.yaml` 在 Render 创建 Python Web Service，`.python-version` 指定 Python 3.11。生产启动命令是 `python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT`；Render 向 `GET /health` 发健康检查，不会因此请求交易所。页面中的 API 请求均使用同源相对路径，部署时不需要修改前端网址，也不需要交易所 API Key。

首次部署需要在 Render 控制台连接此 GitHub 仓库，从 `main` 的 `render.yaml` 创建 Blueprint。之后向 `main` 更新代码会触发 `.github/workflows/test.yml` 中的 Python 3.11 测试；Render 配置为 **GitHub 检查通过后自动部署**。只把测试通过的提交作为有效更新。

当前配置选用 Render 免费单实例，适合公开原型。免费实例可能休眠，首次访问会等待启动；全市场扫描可能较慢。实例文件系统并非持久存储，重新部署或重启后 `data/last_task.json` 可能丢失；目前也没有跨实例任务状态共享。若需要持续运行、多人同时操作或永久保存历史结果，应升级运行方案并引入持久化任务与数据存储。

部署后可用 `GET /health` 验证服务，并在首页发起扫描。`GET /api/health` 继续保留给本地 Windows 启动脚本使用。

## 图表与指标

开始扫描后，「行情图表」区会列出所选交易所、市场中**达到前一完整 UTC 自然日成交额门槛**的交易对；可选择其中任一币种和周期查看图表，点击结果表中的币种也可打开对应图表。图表依次显示**普通 K 线、Heikin-Ashi 平均 K 线、MACD、KDJ、成交量**，两种 K 线可直接对照。顶部可开关 BOLL、SAR、MACD、KDJ、成交量；展开「图表指标设置」可配置 BOLL 周期与标准差、SAR Step/Max、MACD Fast/Slow/Signal、KDJ 周期和平滑参数。悬停任一种 K 线时显示对应的 OHLC。图表参数保存在浏览器中，**仅影响显示，不改变已有扫描结果**；BOLL、SAR、MACD、KDJ 仍基于原始 K 线计算。

当前运行中命中的结果优先使用扫描时的 K 线；服务重启后打开旧结果会重新向交易所读取最新完整 K 线，页面会标出这一点，图形可能因此与当时的扫描结果不同。图表请求失败会显示真实错误，不生成模拟 K 线。

## 扫描范围

- Binance、OKX、Gate 的 USDT 现货和 USDT 永续。
- 周期可多选：15m、30m、1H、2H、4H、6H、8H、12H、1D、2D、3D、5D、1W；默认 4H。
- 2H、6H、8H、12H 和多日/周线等非原生周期由较短周期按 UTC 边界合成，丢弃不完整分组。
- 逐一读取所选交易所/市场中全部活跃 USDT 交易对的日线，按**昨天 00:00–24:00 UTC** 已收盘日线的 USDT 成交额筛选；默认门槛为 1000 万 USDT。符合门槛的币种全部进入所选周期的扫描，不设置前 N 名上限。Binance 使用日线 quote asset volume，OKX 使用 volCcyQuote，Gate 现货使用 quote volume、永续使用 sum。
- 所有指标和阶段只使用已收盘 K 线。某一交易所或币种请求失败时，错误在页面底部显示，其他市场继续扫描。

## 请求与扫描控制

- 扫描和图表共用同一个进程级公开行情客户端、连接池、域名冷却状态及短期缓存。每个域名有独立 semaphore：Binance Spot 3、Binance Futures 2、OKX 3、Gate 3；请求启动间隔分别不低于 0.35、0.5、0.35、0.3 秒，避免快速响应时仍形成突发流量。Gate 的两个市场共享同一域名限制。
- HTTP 429：读取 `Retry-After`（秒数或 HTTP 日期），整个域名先停止发送请求，等待后最多总计 3 次尝试。超过 30 秒的等待或重试用尽时，本轮跳过该市场，保留完整冷却时间，不把上游等待截短。OKX JSON 错误码 `50011` 同样进入限流处理。
- HTTP 418：不重试，立即封锁该域名后续网络请求。本轮跳过受影响市场并显示明确提示。优先采用 `Retry-After`，缺失时读取 Binance 消息中的 ban expiry，再缺失则保守冷却 15 分钟。已在网络中的少量请求可能仍返回，但排队请求不会继续发送。封禁状态独立于其它域名，也共享给图表接口；可用 `/api/exchanges/status` 查看状态和经过脱敏的上游诊断。
- `X-MBX-USED-WEIGHT-1M` 存在时记录在状态与限流日志中；不依赖该头一定存在。网络异常、超时和 5xx 最多尝试 3 次，单次 HTTP timeout 为 15 秒，重试退避 1、2 秒。错误提示不展示 Python traceback，诊断不保留完整响应 HTML，IP 地址会脱敏。
- 原始响应缓存保持接口 URL、symbol、period、limit 等完整参数作为 key，K 线缓存 40 秒、交易对列表 600 秒。并发的相同请求复用已完成缓存。缓存最多 128 项、原始响应计量不超过 8 MiB；图表 K 线缓存最多 64 项、5 分钟，扫描成功的 K 线可直接用于图表。图表采用与扫描相同的取数长度；扫描算法和已收盘 K 线过滤保持不变。
- 服务端同一时刻只允许一个扫描；重复启动返回 409。正常结束、停止或失败后冷却 60 秒，再次启动返回 429 与 `Retry-After`。停止会取消等待中的行情请求。已观察到的域名封禁写入 repository，SQLite 数据保留时，重启会恢复剩余封禁时间；Render Free 丢失数据库时无法恢复。这不是跨实例或独立出站 IP 的限流保证。
- `completed_with_warnings` 表示有市场不可用、限流或币种读取失败。每个市场记录总范围、成功处理、匹配、失败、跳过、请求数、418/429 次数和耗时。币种计数按交易所/市场独立统计；成功处理包括已完成成交额检查但未达门槛的币种，匹配按币种去重，结果表仍按币种与周期列出信号。如果市场列表都无法读取，范围标记为未知，不虚构币种数量。

## 受控验收

“受控扫描范围”可限制每市场检查前 1–100 个活跃交易对；留空（API `max_symbols: null`）保持完整市场扫描。此选项只缩小验收范围，不改变成交额门槛或指标定义。

先以单市场、4H、5 个币种验收，再扩大到三个交易所的现货与永续，最后留空运行全市场单周期扫描。例如：

```powershell
python scripts/verify_scan.py --url https://crypto-scanner-web-bzds.onrender.com --exchanges binance --markets spot --limit 5 --output data/acceptance-spot.json
python scripts/verify_scan.py --url https://crypto-scanner-web-bzds.onrender.com --exchanges binance okx gate --markets spot perpetual --full --output data/acceptance-full.json
```

脚本仅通过现有 API 启动和轮询任务，报告保存在被 Git 忽略的 `data/` 中，不参与 CI 的真实网络测试。遇到 418 时服务器停止该域名请求，脚本如实记录部分完成，不改用代理、随机 IP 或其它绕过方法。若脚本达到等待期限，会请求停止扫描并保留最终状态。

## 当前量化规则：低位蓄势与 7 根双转换

根据用户最新的形态描述，旧版“5～12 根最多一根阳线、跌幅 ≥3%、MACD/KDJ 必须同时满足、翻转相距 ≤3 根”已替换。指标计算参数不变：MACD(5,21,4)、SAR(0.02,0.2)、BOLL(20,2)、KDJ(13,3,3)。这是启发式研究规则，尚未回测；不能声称已通过某个真实币种图例的收益验证。

### 前置结构与核心候选

前置结构在最早一次有效转换之前评估；尚无转换时使用最新完整 K 线之前的窗口。以下数值是当前默认量化近似，后续可用历史样本校准。

| 类型 | 当前定义 |
| --- | --- |
| A：底部下跌反转 | 转换前 4～16 根中至少 60% 的 HA 偏空、后半段收盘均值低于前半段、整体回落至少 1%。允许夹杂阳线，不再要求连续大跌或 ≥3% |
| B：低位横盘蓄势 | 转换前 12 根高低价总振幅 ≤8%；后 6 根振幅 ≤前 6 根的 80%，或前后两段各自振幅均 ≤2%；价格位于此前最多 60 根高低范围的下方 60%。压缩用普通 OHLC 判定，不强制布林收口 |
| 二次启动准备 | 已出现过相距 ≤7 根的双转换，随后至少 12 根形成压缩平台；从旧转换收盘曾上涨 ≥1%，平台低点较高点回撤 ≥1% 且未明显跌破旧结构低点（0.5% 容差）。旧转换搜索最多 40 根；这种背景不能代替新的双转换确认 |
| 核心双转换 | SAR、HA 各自最近一次阴转阳均在 **0～7 根已收盘 K 线前**，相距 ≤7 根，并且目前仍同时多头；同一根最强，1～2 根强，3～5 根正常，6～7 根候选 |
| 扩展观察 | 两次转换均在 0～10 根内，但至少一次已有 8～10 根；只标“观察”，不给双转换确认分。超过 10 根的旧转换不作为新启动；新二次蓄势可独立显示准备状态 |

MACD、KDJ、布林带、成交量和 HA 下影线**不是核心双转换准入门槛**。仍需 A、B 或二次蓄势背景；持续上涨中任意两次翻阳不会直接入选。SAR 等于 K 线最低价时保留多头判断，避免标准 SAR 的价格钳制形成虚假反复转换；零振幅且 SAR 等于价格时沿用上一状态。已重新转空的旧翻转不能用于确认。

### 四阶段与评分

- **观察**：有前置结构，并出现 HA 实体缩短、不再明显创新低、平台压缩或 MACD 衰减中的任一证据；或进入 8～10 根扩展观察。
- **临界**：前置结构已有稳低、缩实体或压缩迹象，同时最近 7 根 HA 翻阳仍有效，或空头 SAR 距价格 ≤3% 且接近中。MACD/KDJ 不必同时达标。
- **双转换确认**：前置结构加核心双转换成立，即使 MACD 尚未金叉、KDJ 已高位、成交量未放大，也保留候选。
- **启动**：确认后收盘站上布林中轨，且至少两项转强：上轨向上扩张、成交量 ≥前 5 根均量 1.3 倍、MACD 金叉或正柱扩大、KDJ 向上、连续至少两根 HA 阳线且下影线 ≤实体 20%。不再强制放量；进一步突破转换前平台高点且至少三项转强时，强度另标“强势启动”，仍属于启动阶段。

结果包含形态类型、转换间距、新鲜度、强度、评分明细与规则版本。新鲜度按两次转换中较早者计：0～2 根非常新鲜、3～5 根正常、6～7 根仍有效、8～10 根扩展观察。旧历史不会被改写，页面标记旧版结果；需重新扫描产生新规则结果。

| 评分项 | 分值 |
| --- | --- |
| 基础结构 | 20 |
| 核心双转换 | 20 |
| 共振间距 | 同根 10；1～2 根 8；3～5 根 5；6～7 根 2 |
| 新鲜度 | 0～2 根 5；3～5 根 3；6～7 根 1 |
| MACD | 最多 10：负柱相对前置窗口低点收缩至少 25% 且改善 4、DIF 向上 2、金叉 2、正柱扩大 2 |
| KDJ | 最多 8：低位金叉 8、中位金叉 5、低位抬头 4、普通抬头 2；K>80 不加分，不删除候选 |
| 布林带 | 最多 12：低点接近下轨 2、平均带宽收窄 2、站上中轨 4、上轨向上且带宽扩张 4 |
| 成交量 | 最多 8：平台 12 根均量 ≤此前 12 根的 80% 加 3；当前量 ≥前 5 根均量 1.3 倍加 5 |
| HA | 最多 7：无下影线 4、短下影线 3（互斥），连续阳线再加最多 3；长影线只降低评分并提示 |
| 离低点过远 | 回升 >12% 扣 6，>20% 扣 12，不再一律删除；>8% 提示 |

总分限制 0～100；悬停评分可看明细。这是证据排序值，不是上涨概率。昨日成交额门槛默认仍为 1000 万 USDT，前端设置、已收盘处理、请求限流及自动扫描去重均沿用原流程。

## 限制

- 支持手动扫描和服务端自动扫描、SQLite 历史、日志提醒及可选 webhook；没有回测、Telegram/微信集成或自动交易。当前 Render Free 的长期自动运行与持久化尚需部署配置，不能视为已经获得每小时可靠运行保证。
- 全市场日成交额筛选需要对每个活跃交易对请求日线，可能需要数分钟或更久。交易所可能限频、地区限制或暂时不可访问；失败项会显示在错误列表中，不能视为通过或不通过门槛。
- 前一日按 UTC 日历日计算，与本地自然日或交易所界面的其他时区设置可能不同。新上市币种如没有完整的昨日 UTC 日线，不会进入扫描。
- “观察”与“临界”是早期预警，不能当成已确认的反转。即使达到“启动”，也可能是假突破。
- FastAPI 需要运行中的 Python 服务，不能直接放到 GitHub Pages 静态托管；公网版本通过 Render 托管。
- 公开网页允许访客触发扫描，当前有单进程互斥、结束冷却和域名请求限速；正式多人使用前仍需考虑任务队列和访问控制。共享出站 IP 上的其它服务可能消耗上游配额，程序不能保证 Binance Futures 在所有托管环境中可用。

## 验证

```powershell
python -m pip install pytest
python -m pytest -q
```

页面不收集 API Key、Secret 或私钥。行情接口参考：[Binance 官方文档](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints)、[OKX 官方文档](https://www.okx.com/docs-v5/en/)、[Gate 官方文档](https://www.gate.com/docs/developers/apiv4/en/)。

## 自动扫描

自动扫描复用现有 `_scan`、`PublicHttp`、连接池、限流状态和图表，与手动扫描共用上述 **7 根共振新规则**。指标公式及参数、已收盘要求、增量检查和信号 ID 去重机制保持不变；更新策略不会自动开启免费实例的长期自动扫描。

- 默认配置：Binance / OKX / Gate，Spot + Perpetual，**4H 已收盘 K 线**，每小时一次，昨天 UTC 日成交额 1000 万 USDT。默认关闭，首次上线建议每市场 5 个币种，先验证三个现货市场，再加入 OKX/Gate 永续。
- 自动设置在服务端数据库保存，手动扫描及图表参数仍按原方式保存。关闭自动扫描停止未来调度，正在执行的一轮可用“停止扫描”结束；公网自动任务的停止也需要管理凭证。
- `FastAPI lifespan` 管理一个时钟任务。启动创建、关闭取消；启用后下一整点检查。`AUTO_SCAN_SCHEDULER_MODE=external` 不启动内部时钟，等待受保护的外部触发。两种模式都经过同一个扫描互斥槽位和至少一小时的自动启动间隔；不支持更高频率。每个部署只运行 **1 个 uvicorn worker / 1 个实例**。
- 手动正在执行时，自动轮次记录 `skipped_due_to_manual_scan`；自动正在执行时，手动启动返回 409。冷却和自动启动频率限制也会记录 skipped 轮次。
- 每个 `exchange:market:pair:period` 保存 `last_closed_candle_time` 和最多 106 根完整 K 线。UTC 边界尚未推进时直接跳过，无 K 线请求、无指标计算；边界推进后请求最新几根（按缺口扩大、最多 106 根），合并并验证连续性。只对交易所实际返回的已收盘 K 线分析，时钟不会生成正式信号。如果交易所仍返回上一根，记录 `no_new_candle`。历史不足 70 根或缺失不产生信号。
- 交易对列表复用原 600 秒缓存；昨日成交额按交易所/市场/币种/UTC 日期持久缓存（包含成功读出的 0 成交额），同一天不重复下载。网络失败和缺失昨日数据不当成 0 缓存。当前有效增量 K 线也可供图表复用。
- Futures 的 418 冷却仍有效时，整市场 `skipped_rate_limited`，不会新发请求；其他市场继续，最终可为 `completed_with_warnings`。

### 历史、去重与提醒

`SignalRepository` 是存储边界；`SQLiteSignalRepository` 集中管理 SQL，默认文件 `data/scanner.db`，可通过 `SCANNER_DB_PATH` 改为持久磁盘路径。数据库包含 signals、scan_runs、checkpoints、daily_turnover、settings。scan_runs 记录手动、自动及跳过任务；进程重启把未结束任务标为失败。该接口以后可用 PostgreSQL 实现替换，当前尚未实现 PostgreSQL。

信号 ID：`exchange:market:pair:period:double_flip:candle_time`，时间标准化为 UTC ISO 8601，以已收盘 K 线**开盘时间**为准。`double_flip` 表示现有底部双转换策略族，阶段字段区分“观察 / 临界 / 双转换确认 / 启动”，预警不会伪装成已确认信号。数据库主键防止重复保存，提醒原子领取标记防止同一 ID 重复发送。

每个交易对第一次成功分析都是**静默基线**，保存信号并设 `notified=true`、`baseline=true`，不发送提醒；后续扩展到新交易对也不会批量补发。新的完整 K 线产生新 ID 才提醒。后续完整 K 线分析不再符合原策略时，原有效记录标为 `status=invalid`，页面显示“失效（原阶段）”；历史保留。初版不区分结构低点破位和其它失效原因。

提醒与指标分离：`NotificationService` → 默认 `LogNotificationService`，仅记录新信号。配置 `SIGNAL_WEBHOOK_URL` 时使用 webhook，POST 包含 event、id、exchange、market、pair、period、stage、score、candle_time、detected_at；5 秒 timeout，最多 2 次尝试，不记录 URL/token/响应。失败不阻断扫描，记录提醒失败警告。先领取再发送，进程中断或失败后**不自动重发**，可能漏通知；`notified` 表示已领取/基线抑制，不保证外部平台已送达。HTTP 重试使用相同 `Idempotency-Key`，接收方需要按 ID 去重，网络本身不能保证远端 exactly-once。

网页新增自动状态、上次运行/耗时/新信号、市场状态、最新信号及 `/history`。历史每页最多 100 条，支持交易所、市场、周期、币种、阶段、发现 UTC 日期筛选，点击记录复用现有图表。历史保存信号字段，打开图表默认显示最新完整行情，不是永久历史快照。

### API 与管理权限

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/auto-scan/status` | 设置、运行状态、存储限制、上次结果、下次内部检查时间 |
| POST | `/api/auto-scan/enable` | 保存设置并启用，JSON 沿用扫描参数，固定 `periods:["4h"]` / `interval_seconds:3600` |
| POST | `/api/auto-scan/disable` | 停止未来调度 |
| POST | `/api/internal/auto-scan` | 受保护的外部触发，快速返回任务 ID；使用 `/api/task` 轮询结果 |
| GET | `/api/signals` | `limit`（1–100）、`offset`、exchange/market/period/pair/stage/date_from/date_to |
| GET | `/api/scan-runs` | 最近任务记录，limit 最多 100 |

内部触发始终要求环境变量 `AUTO_SCAN_TOKEN` 和 `Authorization: Bearer …`。未配置返回 503，错误/缺失 token 返回 401。Render 公网管理写接口和自动任务停止同样受保护；本地未设置 token 时仅 enable/disable 可直接使用。网页“管理凭证”只用于管理请求，不保存到 localStorage，不写入静态 JS。token 由管理员安全配置，**不要把值写入仓库、README、命令行参数或截图**。公开状态和历史可供访客读取。

### Render Free 与长期运行

当前 `render.yaml` 仍使用 Free，外部调度模式。**Free 15 分钟无请求会休眠，休眠/重启/部署会丢失 SQLite；不能保证持久历史、跨重启增量缓存或内部每小时调度。** [Render 官方限制](https://render.com/docs/free)。UI/API 会明确显示此限制，外部模式未配置 cron 时 `next_run=null`，不会虚构下一次运行时间。

已准备 `.github/workflows/auto-scan.yml`：每小时 UTC 第 5 分钟，串行调用 `scripts/auto_scan.py`，健康检查唤醒服务、调用受保护入口、轮询完成；不复制策略。默认由仓库 variable `AUTO_SCAN_CRON_ENABLED` 控制，值为 `true` 才执行；需要 Actions secrets `AUTO_SCAN_BASE_URL`（本站 URL）和 `AUTO_SCAN_TOKEN`（与 Render 同值）。**仅上传 workflow 不代表已经开启线上定时任务。** GitHub schedule 可能延迟，公开仓库长期无活动时可能停用；不是精确时钟保证。[GitHub 官方说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

可靠的当前 SQLite 方案：先将 Web Service 升级为付费常驻实例、挂载持久磁盘，例如 `/var/data`，然后配置 `SCANNER_DB_PATH=/var/data/scanner.db`、`SCANNER_STORAGE_DURABLE=true`、`AUTO_SCAN_SCHEDULER_MODE=internal` 和管理 token，再启用自动扫描。`SCANNER_STORAGE_DURABLE` 仅为部署声明，**不能替代实际磁盘挂载**。持久磁盘下可由内部时钟每小时运行，无需 GitHub cron。付费操作需要账号支付方式及用户授权，项目不会自动升级计费。

另一条路径是常驻/外部 cron + 外部持久数据库（需后续实现 PostgreSQL repository）。Render Cron 最低月费、且不能挂载持久磁盘，单独创建 cron 并不能解决 SQLite 保存问题，因此本次未擅自创建付费 Cron。[Render Cron 文档](https://render.com/docs/cronjobs)

### 本阶段验证

新增测试全部 mock 行情/HTTP：调度启停、同 K 线零请求、新 K 线增量、未收盘排除、重复保存/通知、静默基线、扩展交易对基线、SQLite 重启、失效、历史分页、成交额缓存、手动互斥、418 隔离、webhook 失败、固定 4H/小时参数、启停 API、token 验证及 Free 限制提示。沿用 `python -m pytest -q` 与现有 GitHub Tests workflow，CI 不请求真实交易所。
