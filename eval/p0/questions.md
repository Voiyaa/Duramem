# P0 多跳评测集（20 题）

> ⚠️ **本文件是答案卷。** 评测执行者只许看「题目」一节；「答案卷」仅供评分用。
> 每题要求证据跨 **≥2 个会话**拼接（会话代号见下），纯单切片可答的题不合格。
> 库：`Duramem开发`（31 切片，4 个有内容会话，2026-09-28 快照）。
>
> 会话代号：
> - **S1** `sess_b4df1b6b`（2026-09-24 早：构想→调研→评审→敲定→后端→前端→bat/导入→模型配置）
> - **S2** `sess_94b95220`（2026-09-24 晚：架构→挂载排障→验证修 bug→下线 hook→zcode 库被删→active db 落地）
> - **S3** `sess_91255ac5`（2026-09-26：读取验证→硬失败根因→三级记忆取舍与落地）
> - **S4** `sess_b3820dd3`（2026-09-27：热重载→dsh→key 核查 push→supermemory→语义化命名）

---

## 题目（执行者看这里）

- **Q01** Duramem 的 hook 自动采集，当初是用什么事件接上的？后来为什么要下线、具体怎么下线的？现在挂载下读取记忆靠什么触发？
- **Q02** 库里曾经出现整批"假向量"，根因是什么？后来加了什么机制防止静默混入？这个机制防住之后，还出过一次"进程级别"的同类问题，是什么？
- **Q03** 在 Duramem 里改配置，为什么有的立即生效、有的要重启？跨进程热重载是怎么实现的、生效范围到哪？
- **Q04** 删除能力有几个层级？模型、前端/CLI 各自能删到什么程度？库级真删除怎么防误删？
- **Q05** 那次 zcode 库被真删除的事故里，丢了什么、哪些能恢复哪些不能？这件事直接催生了什么功能？
- **Q06** 项目测试数量从后端首次完成到现在，经过了哪些关键数字节点？最终是多少？
- **Q07** 三级记忆改造前，助手最初建议不照抄 OpenViking，给出的理由是什么？用户为什么最终仍决定上三级？落地后为什么又做了命名重构？
- **Q08** window=2 回查被预算拒绝的那次硬失败，具体数字和根因是什么？此前还有过一次同族失败，是什么？这类问题最终怎么解决？
- **Q09** 词法路（FTS5）的 search_text 由什么组成？这个组成导致过什么检索失败案例？当初为什么坚持词法路每轮都跑？
- **Q10** L0 的 l0_truncated 标记是怎么判定的？实测发现过什么与标记有关的 bug？全库触顶后的处理决策是什么、为什么？
- **Q11** 对话导入功能最初支持哪几个来源、有什么护栏？后来新增的第五个来源是什么、它有什么特殊的技术点？
- **Q12** 曾经有人提议让模型直接回传对话原文入库（dm_ingest），为什么被否决？这和系统的哪条核心原则一致？
- **Q13** 项目开始时的竞品调研结论是什么？后来又调研了哪个竞品、得出哪三条收获？
- **Q14** "新窗口冷启动、什么都召回不到"这个缺口，最初打算用什么功能补、后来实际用了什么？两者状态如何？
- **Q15** start.bat / stop.bat 一键脚本踩过哪些坑？stop.bat 为什么只能按端口杀进程、按进程名杀会出什么事？
- **Q16** 会话概览（L1）什么时候刷新？刷新阈值是多少？首次实现时在这个逻辑上踩过什么 bug？
- **Q17** window_id 是什么、曾经因为两条采集路径不一致出过什么问题？session_layers 表用什么做唯一键？
- **Q18** 首次 git push 前做了什么安全核查、结论是什么？API key 实际存在哪、为什么当时判断可接受？
- **Q19** 这个系统"到底解决什么问题"是怎么定调的？最初"BM25 只在向量失效时兜底"的方案为什么被推翻？
- **Q20** service.py 的现状规模和重构建议是什么？当初为什么把 MCP 和 REST 全部汇入唯一业务层？

---

## 答案卷（仅供评分，执行者不得读取）

每题：**证据 uid**（评分按"全部证据 uid 是否被召回"计）、**原子事实**（答对判定标准）、**预期跳法**。

### Q01 hook 生死簿
- 证据：S1 `Fevs51SW5TGp`（UserPromptSubmit + Stop 两个 hook）＋ S2 `FGIrzzWSuskR`（下线：删 ~/.zcode/cli/config.json 的 hooks 段，备份 config.json.bak-no-hooks-20260924-141615）＋ S4 `6sz0SL88jByJ`（现在唯一触发=模型自主调用 dm_* 工具）
- 原子事实：①两个事件名；②下线方式与备份文件名；③现触发方式。
- 预期跳法：首跳"hook 采集"命中 S1 或 S2 其一 → 需第二跳换词（"下线"/"触发"）拿其余两个会话。

### Q02 假向量事故链
- 证据：S1 `zICnPezrcAG5`（根因：无 .env 与 EMBEDDING_API_KEY 时 config.py 自动 embedding_fake=True，148 条向量实为 offline-hashing-1024；修复：库内记 vector_source 指纹，换身份→标"需重建"拒绝检索）＋ S2 `y0C08CiWqU4c`（进程级：重建后旧 MCP 子进程仍用 offline-hashing-1024，60 条向量候选全被下限剔除、retrieval_mode 退化 lexical_only；assert_embedding_compatible 只比对模型名与维度就放行）
- 原子事实：①offline-hashing-1024 与 148 条；②vector_source 指纹机制；③lexical_only 退化与 60 条候选。
- 预期跳法：首跳"假向量"命中 S1 → 从 S1 摘要提取 vector_source/嵌入模型不一致 → 二跳搜 S2。

### Q03 配置生效与热重载
- 证据：S1 `zICnPezrcAG5`（embedding 配置曾被 NEEDS_RESTART 锁只读→拆"模型"页立即生效）＋ S2 `g9X485G2WSGz`（改动生效需重启 ZCode 会话与界面服务）＋ S4 `DqEZL26BO7N5`（REST 进程实时、MCP 进程不实时；_maybe_hot_reload 挂 _components 入口，按 mtime/size 指纹检测两份运行时配置文件并重放复合顺序、原子写入；364 测试；需重启 MCP）
- 原子事实：①NEEDS_RESTART 与模型页拆分；②需要重启的对象；③_maybe_hot_reload 的指纹检测机制。
- 预期跳法：首跳"配置 生效/重启"很难同时命中三处 → 需二跳"热重载"。

### Q04 删除能力三层级
- 证据：S1 `7IPnkGifDngm` 或 `nV8isHwqmiLD`（G 决策引入 deleted_at/superseded_by 软删与版本链）＋ S2 `FGIrzzWSuskR`（库真删除：连 .db/-wal/-shm、需输入库名确认、_purge_files 不再吞 PermissionError）＋ S2 `QUPjXo1VOdHp`（切片真删除 hard_delete/purge_chunk：DELETE /api/chunks/{uid}?purge=true，仅前端与 CLI，模型只有软删除；版本链解引用、原文不动）
- 原子事实：①模型仅软删；②库名确认；③purge 接口路径与权限边界。
- 预期跳法：首跳命中 S2 一条 → 需二跳拿另一条 S2 切片 + S1 的软删设计。

### Q05 zcode 库删除事故
- 证据：S1 `kRsmNuOxLXzH`（恢复手段：导入器，列出→挑选→导入）＋ S2 `sHWq4JVn3zJB`（丢 5346 消息/148 切片可从 ZCode 会话库 --list 63 个对话重导；手工标题/L0/标签、软删状态不可恢复）＋ S2 `QUPjXo1VOdHp`（催生 active_db 指针热切换）
- 原子事实：①5346/148/63 三个数字；②不可恢复项；③催生 active_db。
- 预期跳法：首跳"库被删除"命中 S2 → 恢复手段需要跳到 S1 的导入器切片。

### Q06 测试数演进链
- 证据：S1 `jMTpLlaYMiez`（133）→ S1 `zICnPezrcAG5`（+41→268）→ S2 `QUPjXo1VOdHp`（287）→ S3 `wbXfFM5f5S4v`（356=284+72）→ S4 `VYbb6sYaDAfa`（382）
- 原子事实：最终 382；节点 133→268→287→356→382（至少答出 133、356、382 三个节点且最终=382 计满分）。
- 预期跳法：纯拼接题，任何单程命中都只含 1-2 个节点；需要沿"测试"线索多次跳。

### Q07 三级记忆思想转变
- 证据：S3 `FYQuxZQmQy2S`（不照抄理由：存储式三级三代价=同步快照/生成成本/有损且不可判；建议读取层连续预算渲染）＋ S3 `Du2ejrsnWOE8`（用户理由：平常命中 L1、现代 LLM 百万上下文；L1 升 L2）＋ S4 `VYbb6sYaDAfa`（命名重构：L 编号无法表达归属→summary_text/original_text/abstract_text/overview_text，be22845）
- 原子事实：①三代价；②用户两条理由；③四个新名。
- 预期跳法：首跳"OpenViking 三级"命中 S3 → 命名重构在 S4 需二跳。

### Q08 读取预算两次同族失败
- 证据：S1 `kRsmNuOxLXzH`（同族失败一：L1 渲染区间内所有消息膨胀 14690 字符被预算拒）＋ S3 `K6LmrVMqBFhV`（失败二：window=2 膨胀 13522 字符/7956 token 被拒；根因 expand_range 只按条数不看体积，msg 12 有 7478 字符超长回复；预算 4000 来自 config.py:114）＋ S3 `wbXfFM5f5S4v`（最终：ReadUsageTracker 取代限额器、READ_SOFT_LIMIT_TOKENS 默认 0，M 决策取消硬上限）
- 原子事实：①14690；②7956/13522/7478 与根因；③取消硬上限的终局方案。
- 预期跳法：首跳"回查 被拒/预算"命中 S3 → 14690 在 S1 导入切片里，需二跳。

### Q09 词法臂边界
- 证据：S2 `g9X485G2WSGz`（search_text = L0 + 标题 + 关键词）＋ S2 `y0C08CiWqU4c`（cloudflared 全库检索 0 命中）＋ S1 `7IPnkGifDngm`/S3 `ap9Lbdc0yAFR`（B 决策：向量不会失败、它会自信地召回错的东西；专有名词/错误码类必须词法）
- 原子事实：①search_text 组成；②cloudflared 案例；③B 决策理由。
- 预期跳法：首跳"词法/FTS5"命中 S2 → B 决策理由在 S1/S3 需二跳。

### Q10 L0 触顶
- 证据：S2 `E0bt1yTX4VHb`（判定：estimate_tokens(l0_text) > L0_SOFT_LIMIT_TOKENS 默认 256 置 1；bug：148 条里 145 条卡 251-256 但标记全 0，HeuristicSummarizer 先 truncated 再判断）＋ S3 `udPQgWAI7ieL`（l0_truncated: 16/16=100% 触顶）＋ S4 `DqEZL26BO7N5`（决策：暂缓上调软上限，因为会触发全库向量重建）
- 原子事实：①256 与判定式；②145/148 标记全 0 的 bug；③暂缓理由。
- 预期跳法：首跳"L0 触顶"可能命中 S2 或 S3 之一 → 另外两处需跳。

### Q11 导入器演进
- 证据：S1 `kRsmNuOxLXzH`（四来源 zcode-db/zcode-rollout/claude-code/generic；mode=ro 只读；幂等 (window_id, session_id, seq)；83% 非对话内容标 role=system）＋ S4 `EVgi9WDZ0DQS`（第五来源 dsh：SESSION_FORMAT_VERSION v3、zstd 多帧拼接流需跨帧解压、目录名有损编码用日志首行 cwd）
- 原子事实：①四源与幂等键；②dsh 名称与至少两个技术点。
- 预期跳法：首跳"导入"命中 S1 → dsh 在 S4 需二跳。

### Q12 dm_ingest 否决
- 证据：S4 `6sz0SL88jByJ`（三理由：输出 token 成本高、必为转述、破坏原文锚定）＋ S3 `ap9Lbdc0yAFR`（原则：可核验、原文锚定、命题是"让记忆可核验可纠正"）
- 原子事实：①三条否决理由；②对应原则。
- 预期跳法：首跳"dm_ingest"直接命中 S4 → 原则呼应需二跳。

### Q13 竞品两次调研
- 证据：S1 `xq1hg91jjvTY`（最初：memsearch 最接近、5.5/7、生态空白=无产品把 memory↔raw_segment 做成第一类数据结构）＋ S4 `bFlWbSftBD5r`（supermemory 30.9k stars/MIT；三收获：用 MemoryBench 跑基线、借鉴 Profile 做库级画像、不抄事实图）
- 原子事实：①memsearch 5.5/7 与空白点；②supermemory 三收获。
- 预期跳法：首跳"竞品/调研"命中其一 → 另一个需跳。

### Q14 冷启动两种解法
- 证据：S1 `1BLJQMBeqYBF`（原方案：主 LLM 会话级总结补冷启动；未实现，is_session_summary 只在 models.py:104 声明）＋ S4 `9XlPwpQisqFF`（实际：会话 L0 简介，从 L1 首段机械抽取、零 LLM、进 session_layers_vec/fts，用于治冷启动）
- 原子事实：①会话级总结未实现（字段只声明）；②简介治冷启动、零 LLM。
- 预期跳法：首跳"冷启动"命中其一 → 另一个需跳。

### Q15 stop.bat 按端口杀
- 证据：S1 `kRsmNuOxLXzH`（三坑：GBK 无 BOM、延时用 ping -n 不用 timeout、只按端口杀）＋ S2 `omtNRKy9XxKM`（后果依据：每个聊天窗口一个 duramem.exe MCP 子进程，按进程名杀会连带杀掉）
- 原子事实：①GBK 与 ping -n；②按进程名杀的后果。
- 预期跳法：首跳"bat 脚本"命中 S1 → "为什么"的依据在 S2 挂载切片，需二跳。

### Q16 会话概览刷新
- 证据：S3 `wbXfFM5f5S4v`（bug：decide_refresh 顺序反了导致首次生成走不到）＋ S4 `9XlPwpQisqFF`（概览不自动生成=决策 T；freshness 刷新策略）＋ R 决策阈值 0.10（宽容器按变化占比，在 S3/S4 摘要或原文）
- 原子事实：①不自动生成；②decide_refresh 顺序反 bug；③阈值 0.10。
- 预期跳法：首跳"概览 刷新"命中其一 → 其余需跳；0.10 可能需要 read_original。

### Q17 window_id 口径
- 证据：S2 `omtNRKy9XxKM`（实测 hook 与 import 对同一会话写出不同 window_id）＋ S2 `g9X485G2WSGz`（修复：统一共用 window_from_directory）＋ S4 `9XlPwpQisqFF`（window_id=来源标签"宿主:项目目录名"；session_layers 唯一键 UNIQUE(window_id, session_id)）
- 原子事实：①不一致问题与统一函数名；②唯一键与 window_id 语义。
- 预期跳法：首跳"window_id"命中 S2 → 语义与唯一键在 S4，需二跳。

### Q18 push 安全审查
- 证据：S1 `zICnPezrcAG5`（密钥只写不读、接口回掩码；明文存 data/provider_config.json）＋ S4 `BKl0lNYpHA5p`（核查四层：未读密钥文件/交付物零命中/零网络调用/.gitignore 覆盖 data/ 与 .env、git ls-files data/ 为空；结论无泄露；A.9 取舍=本地单机只绑 127.0.0.1；push 走 clash 代理 127.0.0.1:33210）
- 原子事实：①核查层面与结论；②存储位置与可接受理由；③代理端口 33210。
- 预期跳法：首跳"push/密钥"命中 S4 → 掩码与 A.9 取舍在 S1，需二跳。

### Q19 命题定调与 B 决策
- 证据：S3 `ap9Lbdc0yAFR`（命题："记忆机制会不可逆地销毁信息，而你对这个过程毫无控制权"；四痛点；B 推翻理由"向量不会失败，它会自信地召回错的东西"）＋ S1 `7IPnkGifDngm`（B 条原始评审：BM25 兜底会漏专有名词/错误码/路径）
- 原子事实：①命题原话（或四痛点中两条）；②B 决策理由。
- 预期跳法：首跳"解决什么问题"命中 S3 → S1 的评审切片需二跳（或反向）。

### Q20 service.py god object
- 证据：S2 `fVMag52npzkq`（设计：MCP 与 REST 共用唯一业务层 service.py，多库注册表+组件缓存）＋ S4 `0EmSETDN0kZT`（现状：1812 行/59 方法/11 领域；三步：拆导入归档域约 400 行（触发条件破 2000 行）、合并 import_archive_payload 与 import_conversations、按域拆 api.py 706 行）
- 原子事实：①1812/59；②三步与 2000 行触发条件；③共用业务层的设计理由。
- 预期跳法：首跳"service.py 重构"命中 S4 → 设计初衷在 S2 架构切片，需二跳。

---

## 评分口径

- **单程命中**：hop 0（题目原文一次 `dm_search`，top_k=5）返回 uid 是否覆盖该题全部证据 uid（满分=全覆盖；另记部分命中率）。
- **环路命中**：按策略执行 ≤3 额外跳后，全部召回 uid 的并集覆盖情况。
- **答对率**：召回文本（含 read_original 取回内容）是否足以推出全部原子事实。由主持者对照本卷判定。
- 环路的增益包括：会话跳（read_session 导航）带来的 uid 也计入。
