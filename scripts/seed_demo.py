"""造演示数据，让前端每个页面都有内容可看。

用法：python scripts/seed_demo.py [数据目录]
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from duramem.config import Settings  # noqa: E402
from duramem.models import Message  # noqa: E402
from duramem.service import Service  # noqa: E402

WORK_SESSION_A = [
    ("user", "我这边后端报 ERR_CONN_REFUSED_0x7f，不知道咋回事"),
    ("assistant", "连接被拒绝。先看端口 BACKEND_PORT 是不是被占了，默认 8001"),
    ("user", "怎么看端口占用"),
    ("assistant", "Windows 上 netstat -ano | findstr 8001，找到 PID 再 taskkill /PID <pid> /F"),
    ("user", "换 9000 了，好了"),
    ("assistant", "记得同步改 .env 里的 BACKEND_PORT=9000，否则重启又回到 8001"),
    ("user", "另外许可证那边有什么坑"),
    ("assistant", "要避开 AGPL 和 BSL 的依赖。jina-reranker-v2 是 CC-BY-NC 非商用，建议换 bge-reranker-v2-m3"),
    ("user", "向量库用什么"),
    ("assistant", "用 sqlite-vec 走进程内，配合 FTS5 做词法，RRF 融合两路。单库超 20 万条再考虑换 zvec"),
    ("user", "为什么不用 zvec"),
    ("assistant", "zvec 有 HNSW 真索引，但它是二进制格式，丢掉 .dump 可读性。我们的核心卖点是数据可读可改可调试，所以先不上"),
]

WORK_SESSION_B = [
    ("user", "检索调试面板要展示什么"),
    ("assistant", "三路排名并列：向量原始排名、词法原始排名、RRF 融合后排名。还要颜色区分仅向量命中、仅词法命中、两路都命中"),
    ("user", "为什么要显示原始排名"),
    ("assistant", "因为调参时要回答的是「这一路由什么贡献」。只给最终结果就只能靠猜"),
    ("user", "摘要上限为什么按 token 不按字符"),
    ("assistant", "256 个字符在纯中文下约 150-200 token，但中英混排带路径和代码标识符时可能只剩 60-80，同一个数字差三倍"),
    ("user", "那摘要会不会太短"),
    ("assistant", "短是刻意的：摘要每一轮都注入，放长就是重复成本。细节本来就该留在概览里按需回查"),
]

LIFE_SESSION = [
    ("user", "周末想去爬山，帮我看看要带什么"),
    ("assistant", "看天气。带够水、干粮、轻便雨衣，穿抓地好的鞋"),
    ("user", "上次那件冲锋衣放哪了"),
    ("assistant", "你说收进储藏间左侧的收纳箱了"),
    ("user", "哦对"),
    ("assistant", "另外记得充电宝，山上没信号但备用电源还是有用"),
]

MANUAL = [
    ("用户偏好用 sqlite-vec 而不是 zvec", "因为要看得到数据。二进制格式的向量库和「数据可读可改可调试」冲突。", "向量库偏好", ["sqlite-vec", "zvec"]),
    ("评估过 mem0，最终没采用", "mem0 存的是抽取后的事实，原文被丢掉了，没有指向原文的指针。", "mem0 评估", ["mem0", "记忆框架"]),
    ("决定切片区间指针由服务自己算", "模型能判断哪几条消息属于这个记忆，但让它数字符必然错。所以原文与 char 偏移都由服务从 msg 区间重新渲染。", "区间指针", ["区间指针", "摘要"]),
    ("表结构里 embedding 不放在主表", "二进制列躺在主表里，.dump 和导出 JSON 全是乱码。拆到独立的 chunks_vec，可以单独重建。", "向量表拆分", ["chunks_vec", "表结构"]),
]


def seed(data_dir: Path, reset: bool = True) -> None:
    if reset and data_dir.exists():
        shutil.rmtree(data_dir)

    settings = Settings()
    settings.data_dir = data_dir
    settings.embedding_fake = True
    settings.rerank_enabled = False
    settings.summary_min_messages = 4
    settings.auto_summary_enabled = False

    service = Service(settings)
    try:
        for name in ("work", "life"):
            try:
                service.create_database(name)
            except ValueError:
                pass

        for session_id, script in (("chat-A", WORK_SESSION_A), ("chat-B", WORK_SESSION_B)):
            service.ingest(
                [
                    Message(f"st-win-{session_id}", session_id, index, role, content,
                            speaker="我" if role == "user" else "助手")
                    for index, (role, content) in enumerate(script)
                ],
                db="work",
                auto_summarize=False,
            )
            outcome = service.summarize(f"st-win-{session_id}", session_id, db="work")
            print(f"work/{session_id}: 切片 {outcome.added} 条")

        service.ingest(
            [
                Message("st-win-life", "chat-L", index, role, content,
                        speaker="我" if role == "user" else "助手")
                for index, (role, content) in enumerate(LIFE_SESSION)
            ],
            db="life",
            auto_summarize=False,
        )
        print("life/chat-L: 切片", service.summarize("st-win-life", "chat-L", db="life").added, "条")

        for l0, l1, title, keywords in MANUAL:
            service.store(summary_text=l0, original_text=l1, db="work", title=title,
                          keywords=keywords, tags=["manual", "决定"])
        print("手工切片:", len(MANUAL), "条")

        # 制造一些命中次数、一条软删除、一条版本链，让前端那些标记都有样本
        for _ in range(9):
            service.search("ERR_CONN_REFUSED_0x7f 端口", db="work")
        for _ in range(4):
            service.search("向量库 sqlite-vec", db="work")

        victim = service.search("许可证 AGPL", db="work").hits[0]
        service.forget(victim.uid, db="work")

        old = service.store(summary_text="这条记忆会被新版本取代", original_text="旧版内容", db="work",
                            title="旧版本示例", tags=["决定"])
        from duramem.models import ChunkDraft

        components = service._components("work")
        components.repo.insert_chunk(
            ChunkDraft(l0_text="取代旧版本的新记忆", l2_text="新版内容",
                       title="新版本示例", tags=["决定"]),
            source_db=components.db.db_uuid,
            supersede_uid=old["uid"],
        )

        stats = service.stats("work")
        print(f"work: 切片 {stats['chunks_alive']} 有效 / {stats['chunks_total']} 总，"
              f"消息 {stats['messages']}，向量 {stats['vectors']}")

        # 直接调 repo.insert_chunk 的路径不会自动嵌入（向量由调用方批量补齐），
        # 所以这里补一次 pending，确保演示数据里没有"缺向量"的切片
        backfilled = service.embed_pending("work")
        if backfilled.get("embedded"):
            print(f"补算向量: {backfilled['embedded']} 条")

        # 先 checkpoint，否则统计到的主文件大小还在 WAL 里，会打印出误导性的小数字
        for name in ("work", "life"):
            service._components(name).db.checkpoint()

        for name in ("work", "life"):
            final = service.stats(name)
            print(f"{name}: 切片 {final['chunks_alive']} 有效，"
                  f"主文件 {final['size_bytes']/1024:.0f} KB"
                  f"（另有 WAL {final['wal_bytes']/1024:.0f} KB），"
                  f"缺向量 {final['chunks_missing_vectors']} 条")
    finally:
        service.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./.demo-data")
    seed(target)
    print("数据目录:", target.resolve())
