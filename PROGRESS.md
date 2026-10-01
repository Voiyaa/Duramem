# 开发进度

> 细节看 `Duramem-设计文档-v2.md`（实施依据 + 附录 A 决策记录）；环境与坑看 `AGENTS.md`。

## 当前状态

v3 三级分层（L0/L1/L2）完成，**425 测试全绿**（离线），`check.bat` 三道门禁
（ruff + mypy + pytest）全绿。核心链路已闭环：
采集（hook / 历史导入 / 网关）→ 总结（切片 + 会话概览）→ 检索（RRF + 可选重排）
→ 回查（L2 三模式 + L1 两档）→ 管理（前端 6 页 + CLI + MCP）→ **归档导出/导入往返**。

## 最近变更

### 2026-09-29 冷启动 12.6 秒：SSL 上下文重复构造（设计文档 A.17）

- 起于"要不要把热点换成 Rust/C++"，**先量后动**，两半结论都反直觉：
  - **热路径不是瓶颈**：预热检索中位 ~800ms，其中 BGE-M3 嵌入 ~470ms +
    bge-reranker-v2-m3 ~390ms，**全部 Python+SQLite 计算只有 ~32ms（最小 10.6ms）**。
    硬证据：把嵌入/重排换成离线实现 + 把 httpx 打桩成"任何网络调用抛异常"后，
    检索照常成功、零降级告警、零网络尝试。热路径 96% 是模型 API 往返
  - **冷启动 13.4 秒是 bug**：cProfile 指出 12,608ms 全在
    `ssl.load_verify_locations`（12 次 × 1.05s）。httpx 0.28.1 的
    `create_ssl_context()` 用 `create_default_context(cafile=certifi.where())`，
    **每次重解析 235KB CA bundle**（本机 895-918ms/次；不带 cafile 走系统库只要 30ms；
    文件读只 0.1ms，慢在解析）。`_components()` 里 4 个提供方各建客户端 × 2-3 个上下文
- **新增 `duramem/net.py`**（顶层叶子模块，照 `errors.py` 先例）：`ssl_context()`
  进程级缓存 + `shared_client()` **首次发请求时才建**的单例。5 个同步调用点从
  "构造期存 client"改为"用时取 client，超时按请求传"；网关 3 处 `AsyncClient`
  取共享上下文（不共享客户端——绑定 event loop）
- **顺带修掉隐性浪费**：hook 路径在摘要自动触发关闭时根本不调模型，从前却照样
  建 4 个客户端，为从不使用的传输付 12.6s。惰性化后不调模型就一分钱不花
- **实测（前 → 后）**：`_components()` 12,669ms → **10ms**；hook 每事件
  13,374ms → **324ms**；MCP 首次工具调用 13,361ms → **12ms**；前端首屏
  `GET /api/databases` 12,917ms → **23ms**；网关每转发请求 3,400ms → 亚毫秒；
  热 `dm_search` 782 → 801ms（网络主导，未变）
- **否掉的**：Rust/C++ 重写热点（可换的只有 32ms，占热检索 4%）——12.6s 的病因是
  库默认值 + 本机解析 CA 慢，不是语言速度；系统证书库（会改 TLS 信任来源，留独立选项）；
  共享异步客户端（event loop 绑定）；顺手优化 `estimate_tokens`（只值 9ms）
- **踩坑记录**：`shared_client()` 第一版在 `with _lock:` 里调 `ssl_context()`
  （同一把非重入 Lock）→ **自锁死**，无异常无输出进程不退，只能 timeout 杀。
  修法是把上下文在拿锁前取好；`test_close_then_get_again_rebuilds_without_hanging`
  是回归用例
- 验证：新增 `tests/test_net.py` 9 条；ruff 全绿 / mypy 45 文件零问题 /
  **pytest 425 passed**

### 2026-09-29 L1 职责收紧：地图，不是缩印本（设计文档 A.16）

- 实测驱动：8 切片会话的 L1 概览 6480 token，其中 ~5k 是「详细说明」节——而 L1 本身由切片 L0 聚合生成，详述 ⊆ L0，是派生冗余（dm_search 的 results[].summary 已经给过一遍）
- 生成 prompt 删「详细说明」节；导航行升级为「想知道什么 → uid（一句话结论）」（~20 token/切片替代 400-600 的详述小节）；`overview_target_tokens` 默认 4000 → 1600；离线 HeuristicOverviewProvider 同步新结构
- 不变量：L0 仍从 L1 首段机械抽取（一次调用两层）；重排打分体 1200 截断不受损（判别力最强的简述/覆盖度/导航在最前，详述删除后截断不再可能丢独有内容）；导航 uid 跟随走现成的 `dm_read_neighbors(uid, before=0, after=0)`，工具面零增长
- 存量 L1 不自动变瘦（生成产物），等 freshness 刷新或手动重生成；被否方案（加 detail 中间档 / 保留详述但写短 / 新增 dm_read_chunk）见 A.16
- 测试：fixture 与结构断言改为「四节齐备 + 详述节不存在」；会话层/stdio 测试全绿

### 2026-09-29 stats_brief：前端状态卡与模型 dm_stats 同一份投影

- 用户原则：**前端统计的意义就是让人看到模型所处的状态，两边必须一致**。此前 dm_stats 原样返回管理页全量体检（输入 {"db": null}，输出几百 token 大半是模型用不上也无法行动的字段），且与前端各说各话
- `Service.stats_brief(stats, active_info)`：唯一投影，收在服务层，两个消费者共用。常驻字段 = db / active_db / chunks_alive / vectors / messages / sessions_summarized；**异常信号全是条件字段**——vectors_stale、chunks_missing_vectors、summary_truncated_ratio（超 20% 告警线才出现）、warnings，不出现不占一个字节
- dm_stats 返回投影；REST `/stats` 全量响应带 `brief` 子对象；前端状态卡重写为 brief 视图（核心四格 + 条件告警 NoticeBox），管理专用内容退场：size/WAL（列表行已有 size）、索引后端、软删/被取代、运行配置、逐会话游标明细
- 测试：REST brief 契约 + 条件字段单测 + stdio 断言管理字段不出现；前端构建通过

### 2026-09-28（深夜 VII）工程加固：静态检查门禁 + service.py 门面拆分（设计文档 A.15）

- **拆分（不改行为，全部是"搬家 + 门面转发"）**：`service.py` **1,895 → 1,449 行**
  - `duramem/importing.py`（新，459 行）：历史导入 + 库快照 + 归档往返 →
    `ImportExportDomain`；api.py / mcp_server.py / CLI 调用面零变化
  - `duramem/providers/connectivity.py`（新，131 行）：三个连通性探测
  - `duramem/errors.py`（新，16 行）：`DuramemError` 下沉打破导入环；
    `from duramem.service import DuramemError` 照旧
  - 顺手：4 处 `_cache.pop + db.close()` 收敛为 `Service._close_cached(name)`
- **静态检查门禁**：`pyproject.toml` 配 ruff（E/F/W/I/UP/B）+ mypy（只查产品代码）；
  `check.bat`（GBK 无 BOM，三道顺序跑，`check.bat lint` 跳过测试）；
  `.github/workflows/ci.yml`（Python 3.10 + 3.12，与本地同一套命令）
- **门禁抓到的真问题**（不止是风格）：dsh `_parse_file` 的返回注解在说谎
  （`list[tuple[str, str]]` vs 实际四元组）、embedding 里响应体复用请求体变量名
  `payload`、provider_config 里 `field` 一名两义、gateway 的 anthropic 路由
  `info` 死赋值、`outcome` 漏 `| None`、两个类里 `list` 方法遮蔽内建注解
- **否掉的**：big-bang 重构、重命名 `list()` 方法（改走模块级别名，保 API 稳定）、
  `disallow_untyped_defs` 立刻开满、tests/scripts 强制 100 列、前端 tsc 进 CI
- **更正一条旧待办**：记忆里的"归档与导入路径去重"经核实不成立——现版本没有
  重复的 trim → 门槛 → report 逻辑，不做无中生有的改动
- 验证：ruff 全绿 / mypy 44 文件零问题 / **pytest 414 passed**（拆分后首跑 3 挂，
  是 monkeypatch 目标随实现搬家，已改到 `duramem.providers.connectivity.*`）

### 2026-09-28（深夜 VI）留言键移到返回体最前

- 用户要求冷启动备注出现在检索内容之前：`_ColdStartNote.apply` 从「追加到末尾」改为「插到第一个键」，模型按顺序读 JSON 时先框架后内容；原有键相对顺序不动，测试锁序

### 2026-09-28（深夜 V）调试页新增「最近一次 MCP 调用」卡片

- 动机：检索调试面板只能重放自己发的查询，看不见宿主模型的真实调用
  （参数、它实际看到的输出、冷启动留言有没有带上）
- `_LastCallRecorder`：`_cold` 包装器（本来就裹住全部 7 个工具）在调用收尾
  把 工具名 / 参数 / 输出 / 生效库 / 耗时 / cold_start_injected / pid / ts
  原子写到 `data/mcp_last_call.json`（MCP 写、REST 读——与 active_db.json
  同一跨进程模式反向用）。多窗口最后写入者胜出；输出超 512KB 截断留预览；
  记录失败静默不影响工具调用
- REST `GET /api/mcp/last-call`；调试页顶部新卡片：徽章行（工具 / 库 /
  注入标志 / 耗时 / 时间 / pid）+ 可展开的输入/输出双栏 JSON
- 测试：recorder 单测 5 条（含截断与原子性）+ REST 契约 1 条 + stdio e2e
  断言快照与注入标志。教训：快照只保留"最近一次"，e2e 里标志断言必须在
  下一次调用前做；SDK 会把带默认值的参数全量传入 kwargs，参数断言按关键键

### 2026-09-28（深夜 IV）冷启动注入绑定到特定数据库（A.14 终版）

- note 与开关从全局设置迁进**各库自己的 db_meta**（`cold_start_note` /
  `cold_start_enabled`）：随库文件走——复制、快照、归档导出/导入往返自动携带；
  全局设置键删除（未发布过，无迁移负担），设置页「MCP 冷启动」组撤下
- 编辑入口移到库管理页：每行「冷启动」按钮展开行内编辑器（textarea + 开关），
  行上有留言徽章；REST `GET/PATCH /api/databases/{name}/cold-start`（null = 不改），
  `GET /api/databases` 每行带回 `cold_start`
- 注入器按**本次调用实际生效的库**取配置（显式 db > 轮状态 > 当前指针，解析在
  处理函数之后、刻意不 refresh 轮指针），边沿状态按库隔离；**改 note 内容即重臂**
- 跨进程可见性：每次调用现读 db_meta（WAL 读连接见最新已提交状态），前端改完
  长驻 MCP 子进程下次调用即生效——stdio e2e 用双进程验证（模拟前端的 Service
  写库，子进程不重启读到开关变化）
- 前端 schema 的 `string` 类型支持保留（设置页编辑器通用化，留给后续 string 设置项）

### 2026-09-28（深夜 III）冷启动注入改为手动开关（A.14 修订）

- 废弃"空闲超 N 秒自动重注入"启发式（该键未发布即删，无迁移负担），换成
  `mcp_cold_start_enabled` 布尔开关：**边沿触发**——开关从关拨到开（或进程
  启动即开）的下一次工具调用注入一次，之后静默；长驻子进程想再注入就在
  设置页重拨一次。是否注入、何时注入完全由用户手动掌握
- stdio 端到端补热重载重臂数：关→不注入、重开→长驻子进程下一次调用再注入；
  单元测试重写为边沿语义（含"留言后填、开关没动过也算边沿"的宽容路径）
- 注入器框架语从"首次调用时附带"改为"调用记忆工具时附带"

### 2026-09-28（深夜 II）MCP 冷启动注入：库主留言（设计文档 A.14）

- 设置页新增「MCP 冷启动」组：`mcp_cold_start_note`（textarea，留空 = 完全
  关闭）+ `mcp_cold_start_idle_seconds`（长驻进程的空闲重置线，默认 30 分钟）。
  schema 类型系统新增 `string`，前端设置页加 textarea 编辑分支
- `mcp_server._ColdStartNote`：宿主模型**首次调用 dm_ 工具**时随结果附带
  `cold_start_note`（带"库主备注"框架语）；之后同窗口静默，空闲超线视为
  新一轮再注入。经 functools.wraps 包装器接进全部 7 个工具，注册与 schema
  内省不受影响
- 测试：`test_cold_start.py`（透明性 / 首次注入 / 空闲重置 / 不污染原 payload）
  + runtime settings 热改一条 + stdio 端到端（真子进程验证装饰器注册链路与
  首次注入语义）；前端 tsc 构建通过
- 定位：冷启动失忆的最小件（用户手写版）。组装式库级概览 + miss-fallback
  通道是后续增强位，见 A.14

### 2026-09-28（深夜）性能四修：双嵌 / 网关阻塞 / 链接 O(N²) / 读路径写锁

性能讨论（先扫描后动刀）认定的四个真实问题全部落地，行为语义不变：

- **一次检索只嵌一次**：查询向量在 `pipeline.search` 算一次传给
  `SessionRetriever.search(query_vector=...)`（此前切片路 + 会话路各付一趟
  嵌入 API 往返——检索延迟里最大的纯浪费）。嵌入失败时会话路仍自嵌一次
  （保原降级语义）；KNN 失败不影响把向量交给会话路。
- **网关重活进线程池**：`gateway.py` 的 async 路由里 `_collect`（内含最长
  120s 摘要调用）、`build_memory_block`（嵌入/重排往返）、`_persist_reply`、
  `_collect_reply` 全部改经 `run_in_threadpool` 执行。此前它们跑在事件循环
  线程上，一个请求卡住所有并发请求与流式转发——REST/MCP 侧早已进线程池，
  唯独这条漏了（并发正确性问题，不只是性能）。
- **批量写路径链接 O(N²) 修掉**：`links.py` 拆出 `_compute_pairs` 共享逻辑，
  新增 `rebuild_links_for_many`（存活表只读一次）；`repository.insert_chunks`
  单事务批量入库 + 收尾一次重建；`summarizer` 的入库循环改走批量；
  `rebuild_all`（backfill/归档还原）同样改为单快照。"提交即可见时链接已
  就位"不变式不破（同事务，崩溃整批回滚）；单条 `insert_chunk`（dm_store /
  update_chunk）仍逐条即时维护。
- **bump_hits 缓冲批刷**：命中计数（纯展示）先进内存缓冲，攒满 64 条或滞留
  30s 由下次检索顺手落盘；`Repository.flush_hits` 供显式落盘，挂在
  `Service.close`（REST shutdown / CLI 退出）。此前每次检索直写一次——读路径
  每次抢全局写锁 + 强制 WAL 写。取舍：计数可见性滞后一个窗口、硬杀丢窗口内
  计数。`test_hit_count_does_not_affect_order` 补一次显式 flush 适配新契约。
- 全量测试通过（首次跑出 flush 丢次数的 bug——`list(Counter)` 只取键丢了
  计数，改传 Counter 本体后修复）。

### 2026-09-28（晚）迭代检索环路 P1 落地（设计文档 A.13，schema 3→4）

- `links` 表 + 被动链接（同会话邻接 / tags / keywords 交集，规则生成零模型成本），
  在 insert / update / 软删 / 真删 / 归档还原五个写入路径自动维护；
  存量库 backfill：`duramem links-rebuild`（REST `POST /api/links/rebuild`），
  真库 31 切片建出 168 边
- PPR 扩散（damping 0.85、深度 2）内化进 `dm_search`：**增补不替换**（`expanded`
  独立数组，直接命中逐字节不变），三信号触发（low_yield / weak / 重排分平坦，
  RRF 分不参与平坦判定），触发时 `retrieval_mode` 加 `+expand`
- 前端调试页加「⑥ 图扩散」轨迹卡片；顺手修 v3 遗留 bug——Debug 页读 `item.l0`
  （后端早已改名 `summary`/`abstract`），摘要行一直渲染为空
- 摘要 prompt 加原子事实保全指令（P0 库缺口教训：数字/错误码/结局必须留 L0）
- **实测（`eval/p0/p1_verify.py`）**：扩散单步 P95=1.15ms（线 50ms）✓、零回归 ✓、
  397 测试全绿；auto 信号 8 题全暗增益 +0（"假充分"场景服务端结构性失明）、
  always +2；缺失证据 7/9 图上可达 → **P2 优先级：编排层信号 > 主动链接 > 信号调参**
- 注意：**长驻 MCP 子进程需重启**才有新行为（又一次踩 A.11/A.13 记过的坑）

### 2026-09-28 迭代检索环路 P0 评测完成（设计文档 §16.8，产出 `eval/p0/`）

- 三件套：`recall-strategy.md`（召回策略模板/执行协议）、`questions.md`（20 题多跳
  评测集+答案卷，每题证据跨 ≥2 会话）、`results-2026-09-28.md`（评分报告）
- 执行：4 个宿主模型子代理盲跑（不接触答案卷），34 次工具调用（20 首跳 + 14 环路）
- **结果：单程→环路，uid 全覆盖 60%→75%，原子事实答对率 65%→80%**（+15pp 两项）
- 环路救回的 3 题全是"演化链/结局"型（Q06 测试数演进 / Q07 三级记忆转变 /
  Q08 读取预算失败链），验证了 §16.2 升级信号表里"内容提到'后来/最终'但无结局"是真信号
- 三类失败归因，各指向不同修复层：**路由缺口**（Q03/Q12——召回"看起来够"，升级信号
  未亮 → 需要"子问题覆盖度"检查，不是整题感觉）；**库缺口**（Q16 阈值 0.10 /
  Q20 合并动机——答案根本不在任何切片里，检索救不了"没有"，§16.7 写入侧的实证）；
  **定位精度**（read_original minimal 的 500 token 截断两次够不到目标段）
- 服务端零改动；P1（links 表 + PPR 扩散）未动，新增待办：切片化时原子事实保全检查

### 2026-09-27 迭代检索环路设计定稿（设计文档 §0.2 + §16，v3→v4）

- 命题：检索从单向流扩为环路（检索→记忆→推理→再检索/对比）。理念原型是注意力
  机制本身：记忆=静态 V、检索入口=K、上下文=Q，记忆必须经 Q 调制才有意义
- 六条决策（V–AA）：环路定位为双系统慢路径（单程快路径永远先跑、行为不变）；停止
  信号=信息增益三档+步数/token 预算；推理分层=图扩散（PPR）承担大多数跳、LLM 只管
  卡壳、服务端不做自主 LLM 环；`links` 表被动链接先行（tags/keywords 交集 + 同会话
  邻接，零模型成本）、主动抽取等评测说话（schema 3→4）；对比消歧成为显式步骤
  （memory-to-memory，现有管线的盲区）；落地三阶段 P0 编排层（服务端零改动）→
  P1 图快路径（内化进 `dm_search`，工具面零增长）→ P2 完整环路
- 状态：**设计定稿、未实现**；P0 前置产物是多跳评测集 20 题（跨 ≥2 会话拼接）
- 文档：§0.2 变更记录、§16 全章、A.4 未完成表加行、AGENTS.md 必读清单同步

### 2026-09-26 导出/导入归档往返（设计文档 A.12）

- `export` 升到 **format_version 2**：补入 `summary_cursors` 与 `session_layers`
  （L1 是花模型调用生成的，旧归档丢了就要重新生成）；向量仍不在归档里（可重建）
- 新增 `import-archive`（CLI）/ `POST /api/import-archive`（路径）/
  `POST /api/import-archive/upload`（浏览器上传）；service 层拆
  `import_archive` / `import_archive_payload` 两条入口共用语义
- 还原语义：**只进新库不合并**（uid/消息 id/游标是咬合的寻址体系）；
  **消息 id 逐字保留**（否则区间指针全失效，window 模式才炸）；派生字段
  （分词/token/指纹）重算，业务字段（uid/软删/版本链/命中数/pending）保真；
  跳过 reindex 时标 `archive-import` 指纹**大声拒绝**而非静默退化
- 前端「库管理」页：行内「导出归档」下载 + 页头「导入归档」上传
- 实测抓到一个缓存坑：标记作废前构造的组件不会重新校验 → 标记后必须作废缓存
- 测试：`test_archive.py` 8 个 + `test_api.py` 2 个；真实库（692 消息/23 切片）
  全链路冒烟：导出→还原→向量重建→检索命中与源库一致

### 2026-09-26（早）三级记忆改造完成（上一会话，A.11 前后）

- 会话（容器）持 L1 概览 + L0，切片（条目）持 L0 + L2；首个 schema 迁移 1→2
- `dm_read_session`、`session_layers_vec` 独立向量空间、L1 精排体截 1200 token
- 356 测试全绿；抓到迁移挂错路径、decide_refresh 顺序反等 4 个真 bug

## 下一步（候选）

1. **迭代检索环路 P2**（设计文档 §16.8）：编排层"子问题覆盖度"信号（P0/P1 实测
   指认的真瓶颈）→ 主动链接（摘要期抽取语义关联，补词面图的不可达缺口）→
   LLM 下一跳生成与增益停止信号 → LoCoMo/LongMemEval 基线
2. **真实模型验收**：自主回捞率 20 题（R5 唯一能被证伪的地方）、L0 触顶校准
   （当前库 22/31 触顶——但 L0 是每轮注入成本，涨上限要权衡）
3. 开源发布维护：GitHub 已 push（Voiyaa/Duramem）
4. Claude Code 压缩后回注（SessionStart compact `additionalContext`，载荷未实测）
5. 前端「归档导入」的 dry-run 预览交互（当前直接确认后导入）
