# 底部双转换选币器

基于交易所公开行情的本地网页扫描器。按 **观察 → 临界 → 双转换确认 → 启动** 四个阶段筛选连续回落后的候选币。只读行情，不连接交易账户，也不执行交易。

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
- 服务端同一时刻只允许一个扫描；重复启动返回 409。正常结束、停止或失败后冷却 60 秒，再次启动返回 429 与 `Retry-After`。停止会取消等待中的行情请求。当前状态和缓存限于单个进程，Render 重启会清空冷却状态；这不是跨实例或独立出站 IP 的限流保证。
- `completed_with_warnings` 表示有市场不可用、限流或币种读取失败。每个市场记录总范围、成功处理、匹配、失败、跳过、请求数、418/429 次数和耗时。币种计数按交易所/市场独立统计；成功处理包括已完成成交额检查但未达门槛的币种，匹配按币种去重，结果表仍按币种与周期列出信号。如果市场列表都无法读取，范围标记为未知，不虚构币种数量。

## 受控验收

“受控扫描范围”可限制每市场检查前 1–100 个活跃交易对；留空（API `max_symbols: null`）保持完整市场扫描。此选项只缩小验收范围，不改变成交额门槛或指标定义。

先以单市场、4H、5 个币种验收，再扩大到三个交易所的现货与永续，最后留空运行全市场单周期扫描。例如：

```powershell
python scripts/verify_scan.py --url https://crypto-scanner-web-bzds.onrender.com --exchanges binance --markets spot --limit 5 --output data/acceptance-spot.json
python scripts/verify_scan.py --url https://crypto-scanner-web-bzds.onrender.com --exchanges binance okx gate --markets spot perpetual --full --output data/acceptance-full.json
```

脚本仅通过现有 API 启动和轮询任务，报告保存在被 Git 忽略的 `data/` 中，不参与 CI 的真实网络测试。遇到 418 时服务器停止该域名请求，脚本如实记录部分完成，不改用代理、随机 IP 或其它绕过方法。若脚本达到等待期限，会请求停止扫描并保留最终状态。

## 第一版量化规则

这是根据用户提供的 LTC、DASH、UNI 图例构造的**启发式规则**，不是经过历史回测的收益模型。不同交易所的 SAR、KDJ 初值或 K 线边界也可能与图表软件有差异。

| 条件 | 规则 |
| --- | --- |
| 前置回落 | 转换前 5～12 根平均 K 线，最多 1 根阳线；区间开盘到收盘跌幅至少 3% |
| 布林位置 | BOLL(20,2)；候选低点附近 5 根内曾距下轨 ≤2.5%，当前收盘距近 20 根低点 ≤12% |
| 阴线缩短 | 最近 3 根实体均值不超过前 3 根的 75% |
| MACD | MACD(5,21,4) 的负柱在最近 8 根出现低点后缩短至少 25%，最新柱继续改善 |
| KDJ | KDJ(13,3,3) 最近 5 根 K 值曾 ≤35，且 J 或 K 最新向上 |
| 平均 K 线 | Heikin-Ashi 从阴转阳 |
| SAR | Parabolic SAR(0.02,0.2) 从 K 线上方翻到下方；临界阶段允许 SAR 尚在上方但距价格 ≤3% 且接近中 |
| 双转换 | SAR 与平均 K 线翻转相距不超过 3 根，且仍满足前置回落、布林底部、MACD 和 KDJ 条件 |
| 启动 | 双转换最近 6 根内发生；已收盘价高于布林中轨；最新成交量 ≥此前 5 根均量的 1.3 倍 |

阶段说明：**观察**符合回落、底部及动能衰减初筛；**临界**同时满足 MACD/KDJ 改善，并出现平均 K 线翻阳或 SAR 接近价格；**双转换确认**满足双翻转；**启动**在确认后叠加中轨和成交量条件。评分是上述证据的固定加权排序值，不是上涨概率。

## 限制

- 扫描是手动触发；没有后台定时监控、通知、历史记录、回测或自动交易。
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
