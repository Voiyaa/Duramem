"""运行时设置测试。

文档的验收标准里有"固定楼层总结可开关、可调间隔"和"L1 原文访问全局开关生效"，
这些开关必须真的能在不重启服务的前提下改到，所以有这一批测试。
后半部分测跨进程热重载：界面进程改了配置文件，MCP 进程不重启也要跟上。
"""

from __future__ import annotations

import json
import os

from duramem.runtime_settings import NEEDS_RESTART, RUNTIME_TUNABLE

# 热重载按 (mtime_ns, size) 判文件变化。两次同尺寸的连续写入可能落进同一个
# 文件系统时间戳，所以模拟界面写入时用显式递增的 mtime 盖章，不赌时钟。
_UI_WRITE_SEQ = [0]


def _ui_write(path, payload) -> None:
    """模拟界面进程：绕开本进程的 update_settings，直接落盘配置文件。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    _UI_WRITE_SEQ[0] += 1
    stamp = 1_700_000_000_000_000_000 + _UI_WRITE_SEQ[0] * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def test_tunable_updates_take_effect_immediately(seeded):
    assert seeded.settings.rerank_top_k == 5
    result = seeded.update_settings({"rerank_top_k": 2})
    assert result["applied"] == {"rerank_top_k": 2}
    assert seeded.settings.rerank_top_k == 2
    assert len(seeded.search("端口", db="work").hits) <= 2


def test_l2_switch_takes_effect_without_restart(seeded):
    """验收标准：原文回查全局开关生效，且不用重启。"""
    uid = seeded.search("端口", db="work").hits[0].uid
    assert seeded.read_original(uid, db="work", mode="full").ok

    assert seeded.update_settings({"original_access_enabled": False})["applied"]
    blocked = seeded.read_original(uid, db="work", mode="full")
    assert blocked.ok is False
    assert any("已关闭 L2" in w for w in blocked.warnings)

    # 关闭后 has_original 必须为 false，否则模型会去调一个必然失败的工具
    assert all(hit.has_original is False for hit in seeded.search("端口", db="work").hits)

    seeded.update_settings({"original_access_enabled": True})
    assert seeded.read_original(uid, db="work", mode="full").ok


def test_auto_summary_switch_and_interval(seeded):
    """验收标准：固定楼层总结可开关、可调间隔。"""
    assert seeded.settings.auto_summary_enabled is False
    result = seeded.update_settings({"auto_summary_enabled": True, "summary_interval": 3})
    assert set(result["applied"]) == {"auto_summary_enabled", "summary_interval"}
    assert seeded.settings.auto_summary_enabled is True
    assert seeded.settings.summary_interval == 3


def test_rerank_switch_swaps_the_reranker_in_place(seeded):
    """重排开关影响的是组件构造期捕获的对象，必须就地替换。"""
    components = seeded._components("work")
    assert components.pipeline.reranker.enabled is False

    seeded.update_settings({"rerank_enabled": True})
    assert components.pipeline.reranker.enabled is True, "已在用的组件也要换掉"

    seeded.update_settings({"rerank_enabled": False})
    assert components.pipeline.reranker.enabled is False


def test_read_soft_limit_override_updates_the_tracker(seeded):
    assert seeded.usage.soft_limit_tokens == seeded.settings.read_soft_limit_tokens
    seeded.update_settings({"read_soft_limit_tokens": 123})
    assert seeded.usage.soft_limit_tokens == 123, "计数是有状态对象，不会自己重读配置"


def test_secrets_cannot_be_changed_from_the_ui(seeded):
    """运行时设置这条路依然不许碰密钥。

    注意范围：模型相关的字段**移到 `provider_config` 了**（那边可以改，但
    读回只给掩码），所以这里的拒绝理由从"该改 .env"变成了"不支持的配置项"。
    这条测试要守住的是"这个接口不能用来写密钥"，不是"密钥永远改不了"。
    """
    result = seeded.update_settings(
        {"embedding_api_key": "sk-hacked", "embedding_model": "evil/model", "backend_port": 1}
    )
    assert not result["applied"]
    assert set(result["rejected"]) == {"embedding_api_key", "embedding_model", "backend_port"}
    assert "重启" in result["rejected"]["backend_port"]
    assert seeded.settings.embedding_api_key != "sk-hacked"


def test_unknown_field_is_rejected(seeded):
    result = seeded.update_settings({"nonexistent_field": 1})
    assert "nonexistent_field" in result["rejected"]


def test_type_coercion_and_validation(seeded):
    # 字符串布尔
    assert seeded.update_settings({"overview_access_enabled": "false"})["applied"]
    assert seeded.settings.overview_access_enabled is False
    # 负数拒绝
    result = seeded.update_settings({"vector_top_k": -5})
    assert "vector_top_k" in result["rejected"]
    # 无法解析
    result = seeded.update_settings({"overview_access_enabled": "maybe"})
    assert "overview_access_enabled" in result["rejected"]


def test_overrides_persist_across_service_instances(settings):
    from duramem.service import Service

    first = Service(settings)
    first.create_database("work")
    first.update_settings({"rrf_k": 33, "auto_summary_enabled": True})
    first.close()

    second = Service(settings)
    assert second.settings.rrf_k == 33, "运行时设置应持久化"
    assert second.settings.auto_summary_enabled is True
    second.close()


def test_reset_falls_back_to_env_defaults(settings):
    from duramem.service import Service

    service = Service(settings)
    service.create_database("work")
    service.update_settings({"rrf_k": 33})
    assert service.settings.rrf_k == 33

    service.reset_settings(["rrf_k"])
    assert service.settings.rrf_k == 60, "重置后应回到 .env / 默认值"

    overrides = json.loads(
        (settings.data_dir / "runtime_settings.json").read_text(encoding="utf-8")
    )
    assert "rrf_k" not in overrides
    service.close()


def test_settings_view_reports_source(seeded):
    seeded.update_settings({"rrf_k": 42})
    view = seeded.settings_view()

    by_name = {item["name"]: item for item in view["tunable"]}
    assert by_name["rrf_k"]["value"] == 42
    assert by_name["rrf_k"]["overridden"] is True
    assert by_name["vector_top_k"]["overridden"] is False

    # 只读项要带上，且密钥必须打码
    assert "embedding_model" in view["readonly"]
    assert view["readonly"]["embedding_api_key"] in {"", "***"}
    # 模型字段不再"需要重启"——它们改由模型配置页管，改了立刻生效
    assert "embedding_model" not in view["needs_restart"]
    assert "backend_port" in view["needs_restart"]


def test_every_schema_field_is_tunable_or_explained(seeded):
    """清单里的字段必须真的可改，否则前端会显示一个改了没反应的开关。"""
    view = seeded.settings_view()
    names = {item["name"] for item in view["tunable"]}
    assert names <= RUNTIME_TUNABLE, f"清单里有不可改字段：{names - RUNTIME_TUNABLE}"
    assert not (names & NEEDS_RESTART)


def test_schema_describes_all_tunable_fields(seeded):
    """反向检查：白名单里的字段都应在清单里出现，不能有"能改但界面看不到"的。"""
    documented = {item["name"] for item in seeded.settings_view()["tunable"]}
    undocumented = RUNTIME_TUNABLE - documented
    assert not undocumented, f"这些字段可改但未在设置清单里：{undocumented}"


def test_corrupt_overrides_file_is_ignored(settings):
    """覆盖文件损坏时不能崩，退回到 .env 默认值。"""
    from duramem.service import Service

    (settings.data_dir).mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "runtime_settings.json").write_text("{ 坏掉的 json", encoding="utf-8")

    service = Service(settings)
    assert service.settings.rrf_k == 60
    service.close()


# ======================================================================
# 跨进程热重载：界面进程改文件，本进程（模拟 MCP）不重启也要跟上。
# 触发一律走经过 _components 的公开路径（MCP 全部工具都走这条路）。


def test_ui_runtime_change_is_picked_up_on_next_tool_call(seeded):
    assert seeded.settings.read_soft_limit_tokens == 0
    _ui_write(seeded.runtime.path, {"read_soft_limit_tokens": 123})

    seeded.stats(db="work")

    assert seeded.settings.read_soft_limit_tokens == 123
    assert seeded.usage.soft_limit_tokens == 123, "预算是有状态对象，必须跟着重载同步"


def test_removed_override_reverts_to_env_default(seeded):
    """界面删掉覆盖项（重置）后，本进程不能残留旧值。"""
    default = seeded.settings.vector_top_k
    _ui_write(seeded.runtime.path, {"vector_top_k": 99})
    seeded.stats(db="work")
    assert seeded.settings.vector_top_k == 99

    _ui_write(seeded.runtime.path, {})
    seeded.stats(db="work")
    assert seeded.settings.vector_top_k == default


def test_ui_rerank_flip_rebuilds_cached_reranker(seeded):
    """跨进程路径与进程内 update_settings 不同：整批作废缓存而不是就地换。

    原因是 provider 层的字段（embedder / 摘要模型）同样构造期捕获，无法逐个
    就地换；统一作废重建保证任意字段组合都正确。代价是每次变更后首次调用
    多一次组件构造，可接受。
    """
    assert seeded._components("work").pipeline.reranker.enabled is False

    _ui_write(seeded.runtime.path, {"rerank_enabled": True})
    assert seeded._components("work").pipeline.reranker.enabled is True

    _ui_write(seeded.runtime.path, {"rerank_enabled": False})
    assert seeded._components("work").pipeline.reranker.enabled is False


def test_ui_corrupt_file_keeps_previous_values(seeded):
    """读坏文件时保留旧覆盖值等下次重试，而不是悄悄清空。"""
    _ui_write(seeded.runtime.path, {"vector_top_k": 99})
    seeded.stats(db="work")
    assert seeded.settings.vector_top_k == 99

    _ui_write(seeded.runtime.path, "{ 坏掉的 json")
    seeded.stats(db="work")
    assert seeded.settings.vector_top_k == 99


def test_ui_provider_change_rebuilds_components(seeded):
    """模型配置页改了摘要模型：settings 更新 + 组件整批重建。

    组件在构造期捕获 embedder / reranker / 摘要模型，只改 settings 不作废
    缓存的话，MCP 这边会一直用旧模型。
    """
    components = seeded._components("work")
    old_pipeline = components.pipeline

    _ui_write(
        seeded.settings.data_dir / "provider_config.json",
        {"summary_model": "other-model"},
    )
    new_components = seeded._components("work")

    assert seeded.settings.summary_model == "other-model"
    assert new_components.pipeline is not old_pipeline, "组件捕获了旧模型，必须整批重建"


def test_unchanged_file_does_not_rebuild_components(seeded):
    """文件没变时热重载必须是空操作，不能每次调用都拆一遍缓存。"""
    _ui_write(seeded.runtime.path, {"vector_top_k": 99})
    seeded.stats(db="work")
    pipeline = seeded._components("work").pipeline

    seeded.stats(db="work")
    assert seeded._components("work").pipeline is pipeline


def test_save_is_atomic_and_leaves_no_temp_file(seeded):
    """界面写入走原子替换：不留 .tmp 残骸，MCP 才不会读到半截 JSON。"""
    seeded.update_settings({"rrf_k": 11})
    assert list(seeded.settings.data_dir.glob("runtime_settings.json.tmp")) == []
    data = json.loads(seeded.runtime.path.read_text(encoding="utf-8"))
    assert data["rrf_k"] == 11
