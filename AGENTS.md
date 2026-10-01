# Duramem

独立记忆服务：挂载到 AI 聊天窗口，跨窗口记忆延续。记忆以 SQLite 单文件持久化，
MCP 供模型工具，向量 + FTS5 双路 RRF 融合检索，L0 摘要 / L1 会话概览 / L2 原文三级分层。

**实施依据是 `Duramem-设计文档-v2.md`**（含 v3 三级分层、v4 迭代检索环路设计稿与附录 A 的全部实现决策）。
动手前必读：该文档的 §0.1（三级分层）、§16（迭代检索环路——**已定稿未实现**，别把它当现状）、附录 A（每条偏离都写了为什么）。

## 快速上手

```bash
.venv/Scripts/python.exe -m duramem serve        # REST + 前端 http://127.0.0.1:8001
.venv/Scripts/python.exe -m duramem mcp          # MCP stdio
check.bat                                        # 提交前三道门禁（ruff → mypy → pytest）
check.bat lint                                   # 只跑静态检查（几秒）
.venv/Scripts/python.exe -m pytest -q            # 全部测试，离线，不需要 API Key
start.bat / stop.bat                             # Windows 一键起停（GBK 编码，别加 BOM）
```

测试离线可跑的原因：`HashingEmbedding` / `OverlapReranker` / `HeuristicSummarizer`
是确定性实现，相似度是真实的（共享词项越多余弦越高），断言有意义。

**提交前必须 `check.bat` 全绿**（CI 跑同一套：`.github/workflows/ci.yml`，Python 3.10 + 3.12）。
三道门禁的配置都在 `pyproject.toml` 的 `[tool.ruff]` / `[tool.mypy]`：ruff 管风格、
import 排序与常见 bug 模式（产品代码 100 列硬限，tests/scripts 豁免行宽）；
mypy 只查产品代码。静态检查不过就别推 CI。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python + FastAPI（8001） |
| 前端 | Vite + React + TS + Tailwind（构建产物 `frontend/dist`，由 serve 托管） |
| 存储 | SQLite（WAL）+ FTS5（jieba）+ sqlite-vec（cosine，chunk_size=64） |
| 模型 | OpenAI 兼容接口，可换硅基流动 / 百炼 / Ollama；无 Key 自动退离线哈希 |

## 目录结构

```
duramem/
  service.py        # 服务层门面：MCP 与 REST 共用，能力对等（改行为先改这里）
  errors.py         # DuramemError（叶子模块：领域模块要抛它但不能 import service）
  net.py            # 共享 HTTP 传输：单例 client + 缓存 SSL 上下文（见 A.17）
  importing.py      # 导入/归档域：历史导入、库快照、归档导出/导入往返（Service 转发）
  mcp_server.py     # 7 个 dm_ 工具，content blocks + structuredContent
  api.py            # REST 路由（前端契约测试锁形状）
  store/            # schema（DDL+迁移）/ registry（多库）/ repository / session_layers / links
  retrieval/        # pipeline（RRF+重排+图扩散）/ fusion / lexical / vector_index / sessions / expand
  reader.py         # L2 回查（mode/detail/预算）与 L1 读取
  summarizer.py     # 切片总结（模型定边界、区间服务自算、双重去重）
  session_summarizer.py  # L1 概览（四段结构、L0 机械派生、freshness 策略）
  providers/        # embedding / rerank / presets / connectivity（连通性探测）
  importers/        # ZCode / dsh / Claude Code / 通用 JSONL 历史导入
  hooks.py          # 宿主 hook 采集（transcript_path）
  collectors/       # OpenAI 兼容记忆网关
frontend/src/components/  # 设计系统：ui（卡片/提示/标记）、controls、data、Menu、icons、Sidebar / TopBar
frontend/src/styles/      # 组件层样式（dm-card / dm-field / dm-btn …），页面传的工具类总能覆盖
frontend/src/pages/       # 每页一个文件；页面私有的拆分件放同名小写子目录（pages/chunks/ 等）
tests/              # 离线测试；conftest.py 是全部夹具的出处（扩散默认关，test_links 显式开）
scripts/            # seed_demo / verify_ui_e2e（API 层端到端）
eval/p0/            # 迭代检索环路评测：召回策略 / 20 题答案卷 / 结果 / P1 验证脚本
check.bat           # 提交前三道门禁；.github/workflows/ci.yml 跑同一套
```

## 代码约定与已知坑（踩过的，别再踩）

- **文档即决策记录**：改设计先改设计文档附录 A，写明"做了什么+为什么+否掉了什么"。
- **按域加能力，别继续堆 service.py**：它是门面；导入/归档在 `importing.py`、
  连通性探测在 `providers/connectivity.py`、异常在 `errors.py`。新功能进对应域，
  Service 加门面转发即可（god object 的教训与拆分边界见设计文档 A.15）。
- **模型 API 的 HTTP 一律走 `duramem.net`**：`net.shared_client()` 取进程级单例
  （超时按请求传，各家不同），网关的 `AsyncClient` 至少要用 `net.ssl_context()`。
  **别在提供方构造期建 `httpx.Client`**——实测单次构造 ~3.4s（每个客户端要建 2–3 个
  SSL 上下文，而 certifi 那个 235KB CA bundle 每次重解析要 ~900ms），四个提供方
  会把这笔钱压在"打开一个库"的路径上（12.6s，见 A.17）。那条路径在 hook 上是
  每事件付一次，而 hook 根本不调模型。
- **`net.py` 的两把锁不可嵌套**：`_lock` 是普通 `Lock` 不是 `RLock`，在 `with _lock:`
  里再调 `ssl_context()`（它也要同一把锁）会**自锁死**——无异常、无输出、进程不退。
  要拿上下文就在拿锁之前拿好。
- **改检索性能之前先量**（`eval/` 与 A.17 的做法）：热检索 ~800ms 里 96% 是模型 API
  往返，Python+SQLite 计算只有 ~32ms。别凭直觉换语言或重写热点——先写分段计时脚本，
  再把"网络调用打桩成抛异常"跑一遍，剩下的才是真能优化的部分。
- **注解必须与实现同步**：`check.bat` 是门禁（ruff + mypy + pytest，CI 同套）。
  mypy 上线时抓到过"注解说谎"的真实案例（dsh `_parse_file` 的返回形状、
  embedding 里响应体复用请求体变量名）——改签名/改返回结构时注解一并改。
- **类里不要定义名为 `list` 的方法**：类体内 `list[...]` 注解会被解析成它
  （现有两处用模块级别名 `LayerRows` / `StrList` 绕开；新代码请换名字）。
- 迁移挂在 `registry.open_one`（打开路径），不是 `init_database`（只在建库时跑）。
- 向量写入先删后插（vec0 不支持 INSERT OR REPLACE）；换嵌入模型 = 标 `vector_source`
  作废 + 重建成功才清标记（先清标记再算会留下说谎的库）。
- 配置文件（`runtime_settings.json` / `provider_config.json` / `active_db.json`）
  写入一律临时文件 + `os.replace` 原子替换；读取按指纹缓存、变了即重载。
- 状态变了必须作废组件缓存（`_invalidate_cache`），否则"大声拒绝"形同虚设。
- 会话状态（L0 触顶、pending_changes、游标）是**要保真的数据**，生成路径的
  写语义（清零/重标）不适用于还原/导入类路径。
- MCP stdio 的 stdout 是协议通道：任何日志只进 stderr，jieba 日志已压到 ERROR。
- `.bat` 用 GBK 无 BOM；延时用 `ping -n` 不用 `timeout`；`stop.bat` 只按端口杀。
- 密钥明文存 `data/provider_config.json` 是有意取舍（数据可读可调试），接口只回掩码。
- **前端默认样式写在组件层**（`src/styles/*.css` 的 `@layer components`），别和页面覆盖用的工具类同层：
  以前 Card 默认的 `p-3` 与页面传的 `p-0` 同是工具类，谁赢取决于 CSS 生成顺序——实际 `p-3` 赢，`p-0` 全部失效。
- 前端的导入源一律用规范名（`zcode-db` / `zcode-rollout` / `claude-code`）：后端认别名，
  但 `/api/import/sources` 的 `defaults[].source` 只有规范名，用别名会对不上默认路径。

## 尚未完成（2026-09-29）

- 迭代检索环路：P0/P1 已完成（`eval/p0/`、A.13；links 表 + PPR 扩散已上线，
  注意长驻 MCP 子进程要重启才有新行为）；P2（编排层信号 + 主动链接）未动
- 真实模型验收：自主回捞率 20 题、LoCoMo/LongMemEval 基线
- Claude Code 压缩后回注（SessionStart compact additionalContext）
- 启动速度剩余项（A.17 的量后结论，按收益排序）：
  1. jieba 词典构建 ~573-600ms/进程——**唯一值得上 Rust 的地方**（jieba-rs），
     但要改分词结果 → FTS 全库重建，且动 A.3 的受保护 token 行为，得单独立项；
  2. 系统证书库替代 certifi bundle（省 ~0.9s/进程，换 TLS 信任来源）；
  3. `estimate_tokens` 的逐字符循环（占热检索 ~9ms）；
  4. `start.bat` 里 `ping -n 7` 等 6 秒，而服务实际 ~1.1 秒就绪。
- 详见设计文档 A.4 未完成表
