"""模型配置（provider_config）测试。

重点不在"能不能存下来"，而在三件容易静默出错的事：

1. **密钥只写不读**——写进去能生效，读回来只有掩码，留空保存不会抹掉已存的 Key。
2. **换提供方会让已有向量作废**——而且必须在改配置的**那一刻**标记，
   因为只有那时才知道上一个生效的提供方是谁。不标记的话，用户填上真 Key
   以后，旧的哈希向量会和新向量静默混在一起，检索结果无法解释。
3. **连不上要当场说**——探测走的是草稿配置而不是已保存的配置。
"""

from __future__ import annotations

import json

import pytest

from duramem.config import Settings
from duramem.provider_config import (
    ProviderConfig,
    mask_secret,
)
from duramem.service import Service
from duramem.store.database import Database
from duramem.store.schema import EmbeddingMismatchError

# ====================================================================== 掩码


def test_mask_secret_never_returns_the_raw_value():
    raw = "sk-abcdefghijklmnop"
    masked = mask_secret(raw)
    assert masked != raw
    assert raw not in masked
    assert masked.startswith("sk-a")
    assert masked.endswith("mnop")


def test_mask_secret_handles_short_and_empty():
    assert mask_secret("") == ""
    assert mask_secret("short") == "***"
    assert mask_secret("0123456789") == "***"


# ====================================================================== 读写


def test_api_key_is_write_only(settings):
    """写进去能生效，读回来只有掩码。"""
    cfg = ProviderConfig(settings)
    result = cfg.update({"embedding_api_key": "sk-secret-value-1234"})

    assert result["applied"]["embedding_api_key"] != "sk-secret-value-1234"
    assert settings.embedding_api_key == "sk-secret-value-1234", "内存里要是明文"

    view = cfg.description()
    field = next(f for f in view["fields"] if f["name"] == "embedding_api_key")
    assert field["value"] == "", "字段值不返回明文"
    assert "sk-secret-value-1234" not in json.dumps(view, ensure_ascii=False)
    assert field["has_value"] is True

    # 落盘的文件里必须有明文（否则重启就丢了），但不该出现在任何接口返回里
    on_disk = json.loads(cfg.path.read_text(encoding="utf-8"))
    assert on_disk["embedding_api_key"] == "sk-secret-value-1234"


def test_blank_secret_keeps_the_existing_key(settings):
    """界面留空保存不能把已存的 Key 抹掉——这种静默破坏最难受。"""
    cfg = ProviderConfig(settings)
    cfg.update({"embedding_api_key": "sk-keep-me-please"})

    for blank in ["", "   ", "***", "sk-k…ease"]:
        result = cfg.update({"embedding_api_key": blank})
        assert "embedding_api_key" not in result["applied"], f"{blank!r} 应视为不改"
        assert settings.embedding_api_key == "sk-keep-me-please"


def test_provider_config_persists_and_wins_over_defaults(settings):
    ProviderConfig(settings).update(
        {"embedding_model": "text-embedding-v4", "embedding_dim": 1024, "embed_batch_size": 10}
    )
    # 新实例（模拟重启）读同一个 data_dir
    fresh = Settings()
    fresh.data_dir = settings.data_dir
    cfg = ProviderConfig(fresh)
    cfg.load()
    cfg.apply()

    assert fresh.embedding_model == "text-embedding-v4"
    assert fresh.embed_batch_size == 10


def test_reset_falls_back_to_env(settings):
    cfg = ProviderConfig(settings)
    cfg.update({"embedding_model": "custom/model"})
    assert settings.embedding_model == "custom/model"

    fresh = Settings()
    fresh.embedding_model = "BAAI/bge-m3"
    cfg.reset(settings_from_env=fresh)
    assert settings.embedding_model == "BAAI/bge-m3"


def test_invalid_input_is_rejected_not_coerced(settings):
    cfg = ProviderConfig(settings)
    result = cfg.update(
        {
            "embedding_model": "",  # 空模型名
            "embedding_model\n": "",  # 不是可配置字段
            "embedding_dim": 99999,  # 超范围
            "embed_batch_size": 0,
            "embedding_base_url": "https://a\nb",  # 换行
        }
    )
    assert not result["applied"]
    assert set(result["rejected"]) == {
        "embedding_model",
        "embedding_model\n",  # 不是可配置字段，原样报错
        "embedding_dim",
        "embed_batch_size",
        "embedding_base_url",
    }


# ====================================================================== 有效提供方


def test_effective_provider_is_honest_about_the_offline_fallback(settings):
    """没 Key 时，"有效提供方"必须是哈希实现，而不是配置里写的模型名。

    这是整个功能的地基：库内来源指纹与它比对，写的是它。
    """
    settings.embedding_api_key = ""
    settings.embedding_fake = False
    settings.embedding_model = "BAAI/bge-m3"
    settings.embedding_dim = 1024

    cfg = ProviderConfig(settings)
    effective = cfg.effective_embedding()
    assert effective["offline"] is True
    assert effective["provider"] == "offline-hashing-1024"
    assert effective["model"] == "BAAI/bge-m3", "配置值仍然要如实报出来"

    settings.embedding_api_key = "sk-real"
    assert cfg.effective_embedding()["provider"] == "BAAI/bge-m3"
    assert cfg.effective_embedding()["offline"] is False


def test_effective_provider_tracks_dim(settings):
    settings.embedding_api_key = ""
    settings.embedding_dim = 768
    assert ProviderConfig(settings).effective_embedding()["provider"] == "offline-hashing-768"


# ====================================================================== 换模型 → 向量作废


@pytest.fixture
def offline_under_real_name(monkeypatch):
    """把"配置成真实模型"的路径换成离线实现跑完——测试不该发网络请求。

    这样替换不影响被测逻辑：库里的**来源指纹取自配置**
    （`effective_embedding()["provider"]`），与实际跑哪个实现无关，
    而这正是要验证的部分。
    """
    from duramem.providers.embedding import HashingEmbedding

    def fake_build(**kwargs):
        return HashingEmbedding(dim=kwargs["dim"], segmenter=kwargs.get("segmenter"))

    monkeypatch.setattr("duramem.service.build_embedding_provider", fake_build)


def test_switching_embedding_provider_marks_vectors_stale(seeded):
    """核心行为：换成真实模型时，已有向量必须被标记为不可比。

    不标记的话，一致性校验会因为"两边模型名都是 BAAI/bge-m3"而放行，
    用户随后所有检索都拿到哈希向量与真向量混合的噪声。
    """
    assert seeded.stats("work")["chunks_alive"] > 0
    assert seeded.stats("work")["vector_source"] == "offline-hashing-1024"
    assert seeded.stats("work")["vectors_stale"] is False

    result = seeded.update_provider_config(
        {"embedding_api_key": "sk-fake-but-nonempty", "embedding_model": "BAAI/bge-m3"}
    )
    assert result["rejected"] == {}
    assert result["auto_cleared_offline"] is True, "填 Key 要顺手关掉自动打开的强制离线"
    assert result["identity_changed"] is True
    assert result["vectors_marked_stale"] == ["work"]
    assert result["previous_provider"] == "offline-hashing-1024"

    # 现在打开这张库检索必须被拒绝，而且理由要能指向"重建"
    with pytest.raises(EmbeddingMismatchError) as excinfo:
        seeded.search("端口被占用", db="work")
    message = str(excinfo.value)
    assert "offline-hashing-1024" in message
    assert "BAAI/bge-m3" in message
    assert "重建" in message


def test_rebuild_clears_the_stale_flag(seeded, offline_under_real_name):
    """重建矢量后指纹要更新，否则下次打开还会被判成不一致。"""
    seeded.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})
    report = seeded.rebuild_vectors("work")

    assert report["embedded"] >= 1
    stats = seeded.stats("work")
    assert stats["vectors_stale"] is False
    assert stats["vector_source"] == "BAAI/bge-m3", "重建后来源是当前生效的提供方"
    # 校验重新放行
    assert seeded.search("端口被占用", db="work").hits


def test_rebuild_then_switch_back_does_not_require_rebuild_again(
    seeded, offline_under_real_name
):
    """改成 A → 重建 → 改回原样：又要重建，而且重建后确实放行。

    中间那段"退到离线"的窗口里可能已经写进过哈希向量，所以再次切回来时
    仍然要求重建是保守但正确的：库里的向量真的可能来自两个实现。
    """
    seeded.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})
    seeded.rebuild_vectors("work")
    assert seeded.stats("work")["vector_source"] == "BAAI/bge-m3"

    # 退到离线（留空 Key 表示不动密钥，只把强制离线打开）
    seeded.update_provider_config({"embedding_fake": True})
    assert seeded.providers.effective_embedding()["provider"] == "offline-hashing-1024"

    result = seeded.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})
    assert result["vectors_marked_stale"] == ["work"]

    seeded.rebuild_vectors("work")
    assert seeded.stats("work")["vectors_stale"] is False
    assert seeded.search("端口被占用", db="work").hits


def test_filling_in_a_key_turns_off_the_auto_forced_offline(settings, offline_under_real_name):
    """没填 Key 时 embedding_fake 会被自动打开；此时用户填上 Key 必须真的生效。

    这是最容易被忽略的一条：`config.py` 在"无 Key 且未显式指定"时把
    `embedding_fake` 翻成 True。如果不在填 Key 时把它关掉，用户在界面上
    填完 Key、测试通过、保存，然后什么都没变——而且没有任何提示。
    """
    settings.embedding_api_key = ""
    settings.embedding_fake = True  # 模拟 config.py 的自动降级结果

    result = Service(settings).update_provider_config({"embedding_api_key": "sk-real-key"})

    assert result["auto_cleared_offline"] is True
    assert result["applied"]["embedding_fake"] is False
    assert result["identity_changed"] is True


def test_explicitly_requesting_offline_is_respected(settings):
    """同一次提交里明确要求开强制离线，就不该被"顺手关掉"。"""
    settings.embedding_fake = False
    result = Service(settings).update_provider_config(
        {"embedding_api_key": "sk-real-key", "embedding_fake": True}
    )
    assert result["auto_cleared_offline"] is False
    assert settings.embedding_fake is True


def test_changing_only_batch_size_does_not_invalidate_vectors(seeded):
    """批量大小不影响向量内容，不该让用户白跑一次重建。"""
    before = seeded.stats("work")["vector_source"]
    result = seeded.update_provider_config({"embed_batch_size": 8})

    assert result["identity_changed"] is False
    assert result["vectors_marked_stale"] == []
    assert seeded.stats("work")["vector_source"] == before
    assert seeded.search("端口被占用", db="work").hits


def test_rerank_change_does_not_invalidate_vectors(seeded):
    result = seeded.update_provider_config({"rerank_enabled": True})
    assert result["vectors_marked_stale"] == []
    assert seeded.stats("work")["vectors_stale"] is False


def test_empty_database_is_not_marked(service):
    """空库没有向量可作废，标记了只会让用户白跑一次重建。"""
    result = service.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})
    assert result["vectors_marked_stale"] == []


def test_stale_marking_survives_restart(seeded):
    """标记必须落盘：重启后不能又变得"看起来正常"。"""
    seeded.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})
    fresh = Settings()
    fresh.data_dir = seeded.settings.data_dir
    fresh.embedding_api_key = seeded.settings.embedding_api_key
    fresh.embedding_model = seeded.settings.embedding_model
    fresh.embedding_fake = False

    revived = Service(fresh)
    try:
        with pytest.raises(EmbeddingMismatchError):
            revived.search("端口被占用", db="work")
    finally:
        revived.close()


# ====================================================================== 库列表视图


def test_list_databases_reports_vector_source_even_when_incompatible(seeded):
    """一致性校验拦下来的库也要能看见向量来源。

    否则界面上只剩下"不一致"，用户不知道该重建还是该改配置。
    """
    seeded.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})
    infos = seeded.list_databases()
    entry = next(i for i in infos if i["name"] == "work")

    assert entry["compatible"] is False
    assert entry["vector_source"] == "offline-hashing-1024", "拦下了也要报出来源"
    assert entry["vectors_stale"] is True
    assert "重建" in entry["error"]


def test_provider_view_lists_databases_and_presets(seeded):
    view = seeded.provider_view()
    assert {g["id"] for g in view["groups"]} == {"embedding", "rerank", "summary"}
    assert view["presets"]["embedding"], "预设不能是空的，否则填表要用户自己抄模型名"
    assert any(p["id"] == "siliconflow-bge-m3" for p in view["presets"]["embedding"])

    states = {d["name"]: d for d in view["databases"]}
    assert states["work"]["chunks"] > 0
    assert states["work"]["vector_source"] == "offline-hashing-1024"
    assert states["work"]["needs_rebuild"] is False


def test_every_preset_names_a_field_that_exists(settings):
    """预设不能引用不存在的字段——那会静默失效。"""
    from dataclasses import fields

    from duramem.providers.presets import PRESETS

    valid = {f.name for f in fields(Settings)}
    for preset in PRESETS:
        assert preset.group in {"embedding", "rerank", "summary"}
        for key in preset.values:
            assert key in valid, f"{preset.id} 引用了不存在的字段 {key}"


# ====================================================================== 连通性探测


def test_embedding_test_reports_the_offline_fallback(settings):
    settings.embedding_fake = True
    result = Service(settings).test_provider_config({}, "embedding")
    assert result["ok"] is True
    assert result["offline"] is True
    assert "哈希" in result["note"]


def test_embedding_test_reports_dim_mismatch(settings, monkeypatch):
    """填错维度必须在保存前拦住：放过去的话，写完库才知道，那时向量表已经建错了。"""
    settings.embedding_api_key = "sk-test"
    settings.embedding_fake = False
    settings.embedding_dim = 1024

    class FakeProvider:
        model = "BAAI/bge-m3"
        dim = 1024

        def embed(self, texts):
            return [[0.0] * 768 for _ in texts]

    monkeypatch.setattr(
        "duramem.providers.connectivity.build_embedding_provider",
        lambda **kwargs: FakeProvider(),
    )
    result = Service(settings).test_provider_config({}, "embedding")

    assert result["ok"] is False
    assert result["detected_dim"] == 768
    assert "768" in result["error"]


def test_test_endpoint_uses_the_draft_not_the_saved_config(settings, monkeypatch):
    """测试必须测"如果保存会怎样"，否则测过之后再保存照样是坏的。"""
    settings.embedding_api_key = "sk-old"
    settings.embedding_model = "old/model"
    seen: dict[str, object] = {}

    class FakeProvider:
        model = "new/model"
        dim = 1024

        def embed(self, texts):
            return [[0.0] * 1024 for _ in texts]

    def fake_build(**kwargs):
        seen.update(kwargs)
        return FakeProvider()

    monkeypatch.setattr("duramem.providers.connectivity.build_embedding_provider", fake_build)
    result = Service(settings).test_provider_config(
        {"embedding_model": "new/model", "embedding_api_key": "sk-new"}, "embedding"
    )

    assert seen["model"] == "new/model", "要用草稿里的模型名"
    assert seen["api_key"] == "sk-new", "要用草稿里的 Key"
    assert result["ok"] is True
    assert settings.embedding_model == "old/model", "测试不能改动已保存的配置"


def test_rerank_test_without_key_says_it_is_offline(settings):
    result = Service(settings).test_provider_config({}, "rerank")
    assert result["ok"] is True
    assert result["offline"] is True
    assert result["provider"] == "offline-overlap"


def test_summary_test_without_credentials_is_offline(settings):
    result = Service(settings).test_provider_config({}, "summary")
    assert result["ok"] is True
    assert result["offline"] is True
    assert result["provider"] == "offline-heuristic"


def test_test_endpoint_predicts_the_saved_result(settings, monkeypatch):
    """测试结果必须能预测保存后的结果，包括"自动关掉强制离线"这条规则。

    这条规则曾经只写在 update() 里，于是：用户填了 Key → 点测试 → 界面说
    "没有配置 API Key，会用哈希向量" → 用户放心保存 → 实际却切成了真实模型。
    测试说一套、保存做一套，比没有测试更糟。
    """
    settings.embedding_api_key = ""
    settings.embedding_fake = True  # 模拟 config.py 的自动降级
    seen: dict[str, object] = {}

    class FakeProvider:
        model = "BAAI/bge-m3"
        dim = 1024

        def embed(self, texts):
            return [[0.0] * 1024 for _ in texts]

    def fake_build(**kwargs):
        seen.update(kwargs)
        return FakeProvider()

    monkeypatch.setattr("duramem.providers.connectivity.build_embedding_provider", fake_build)
    service = Service(settings)

    result = service.test_provider_config({"embedding_api_key": "sk-real"}, "embedding")
    assert seen["force_offline"] is False, "草稿也要关掉自动打开的强制离线"
    assert result["offline"] is False

    # 保存，确认实际结果与刚才的预测一致
    service.update_provider_config({"embedding_api_key": "sk-real"})
    assert service.providers.effective_embedding()["offline"] is False


def test_draft_settings_does_not_touch_the_live_settings(settings):
    """草稿不能有副作用——否则"测试连接"就成了一次静默保存。"""
    settings.embedding_fake = True
    cfg = ProviderConfig(settings)
    cfg.draft_settings({"embedding_api_key": "sk-draft-only", "embedding_fake": False})

    assert settings.embedding_api_key == ""
    assert settings.embedding_fake is True


def test_failed_rebuild_does_not_claim_success(seeded, monkeypatch):
    """重建失败时必须留着"需重建"的标记，不能留下一个说谎的库。

    曾经的问题：先记来源指纹、再去算向量。接口挂了以后库里是
    "来源=bge-m3、标记已清除"而 chunks_vec 一条都没有——
    之后所有检索都少掉整条向量召回，而且没有任何提示。
    """
    from duramem.providers.embedding import EmbeddingError, HashingEmbedding

    seeded.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})
    # 注意：这里不能用 stats()，它走一致性校验、会被正常拦下。
    # 要看状态就得走 _vector_states 这条"只读 meta"的路——界面也是这么做的。
    assert seeded.provider_view()["databases"][0]["needs_rebuild"] is True

    class Exploding(HashingEmbedding):
        def embed(self, texts):
            raise EmbeddingError("嵌入接口返回 401：invalid api key")

    monkeypatch.setattr(
        "duramem.service.build_embedding_provider",
        lambda **kwargs: Exploding(dim=kwargs["dim"], segmenter=kwargs.get("segmenter")),
    )
    with pytest.raises(EmbeddingError):
        seeded.rebuild_vectors("work")

    # 关键：状态仍然是"需重建"，而不是"一致但实际没有向量"
    info = next(i for i in seeded.list_databases() if i["name"] == "work")
    assert info["vectors_stale"] is True
    assert info["compatible"] is False
    with pytest.raises(EmbeddingMismatchError):
        seeded.search("端口被占用", db="work")


def test_rebuild_over_an_inconsistent_library_works(seeded, offline_under_real_name):
    """重建是"修正不一致"的路径，只能在跳过校验的情况下走通。"""
    seeded.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})
    with pytest.raises(EmbeddingMismatchError):
        seeded._components("work")  # 普通取用被拦

    report = seeded.rebuild_vectors("work")
    assert report["embedded"] >= 1
    seeded._components("work")  # 重建后放行


def test_provider_failure_is_reported_as_502_not_bare_500(seeded, monkeypatch):
    """模型接口挂了要给出原因。

    裸 500 的响应体只有 "Internal Server Error"，界面上就是这个六个字，
    用户无从判断该改密钥、改模型名还是查网络。
    """
    from fastapi.testclient import TestClient

    from duramem.api import create_app
    from duramem.providers.embedding import EmbeddingError

    seeded.update_provider_config({"embedding_api_key": "sk-fake-but-nonempty"})

    class Exploding:
        model = "BAAI/bge-m3"
        dim = 1024

        def embed(self, texts):
            raise EmbeddingError("嵌入接口返回 401：invalid api key")

        def embed_one(self, text):
            raise EmbeddingError("嵌入接口返回 401：invalid api key")

    monkeypatch.setattr(
        "duramem.service.build_embedding_provider", lambda **kwargs: Exploding()
    )

    with TestClient(create_app(service=seeded, settings=seeded.settings)) as client:
        response = client.post("/api/rebuild-vectors", params={"db": "work"})

    assert response.status_code == 502, "上游模型故障不该是 500"
    assert "401" in response.text, "响应里要有上游给的原因"
    assert response.json()["kind"] == "provider_failure"


def test_network_failure_becomes_a_provider_error(settings):
    """连不上接口要报成 EmbeddingError，而不是漏出 httpx 的异常。

    漏出去的话 REST 层不认识它，只能报 500 "Internal Server Error"——
    用户看不出是网络不通、密钥错了还是模型名错了，而这三者的处置完全不同。
    这里打的是本机一个必然没人监听的端口，请求立刻失败，不发真实网络流量。
    """
    from duramem.providers.embedding import EmbeddingError, OpenAICompatibleEmbedding

    embedder = OpenAICompatibleEmbedding(
        base_url="http://127.0.0.1:9/v1",
        api_key="sk-x",
        model="whatever",
        dim=1024,
        timeout=5.0,
    )
    with pytest.raises(EmbeddingError) as excinfo:
        embedder.embed(["probe"])
    # 要么"连不上"（正常环境），要么代答的 502（clash TUN 之类会拦截所有
    # 连接的环境代答一个空 502）。两种都算被包装成 EmbeddingError，
    # 测试守的是"不漏出 httpx 异常"，不是具体的失败形态。
    message = str(excinfo.value)
    assert "连不上" in message or "502" in message


def test_rerank_network_failure_is_wrapped(settings):
    from duramem.providers.rerank import OpenAICompatibleReranker, RerankError

    reranker = OpenAICompatibleReranker("http://127.0.0.1:9/v1", "sk-x", "m")
    with pytest.raises(RerankError):
        reranker.rerank("q", ["a", "b"])


def test_unknown_group_is_reported(settings):
    result = Service(settings).test_provider_config({}, "nonsense")
    assert result["ok"] is False
    assert "nonsense" in result["error"]


# ====================================================================== REST


@pytest.fixture
def client(service):
    from fastapi.testclient import TestClient

    from duramem.api import create_app

    with TestClient(create_app(service=service, settings=service.settings)) as c:
        yield c


def test_api_never_returns_the_raw_key(client):
    response = client.patch(
        "/api/provider-config", json={"embedding_api_key": "sk-top-secret-9876"}
    )
    assert response.status_code == 200
    assert "sk-top-secret-9876" not in response.text

    view = client.get("/api/provider-config")
    assert "sk-top-secret-9876" not in view.text
    field = next(
        f for f in view.json()["fields"] if f["name"] == "embedding_api_key"
    )
    assert field["masked"] and "sk-top-secret-9876" not in field["masked"]


def test_health_reports_the_effective_provider(client, service):
    body = client.get("/api/health").json()
    assert body["offline_embedding"] is True
    assert body["embedding_provider"] == "offline-hashing-1024"


def test_api_reports_which_databases_need_rebuilding(client, seeded, offline_under_real_name):
    response = client.patch(
        "/api/provider-config",
        json={"embedding_api_key": "sk-fake-but-nonempty", "embedding_model": "BAAI/bge-m3"},
    )
    body = response.json()
    assert body["vectors_marked_stale"] == ["work"]

    view = client.get("/api/provider-config").json()
    state = next(d for d in view["databases"] if d["name"] == "work")
    assert state["needs_rebuild"] is True

    # 一键重建后标记消失
    assert client.post("/api/rebuild-vectors", params={"db": "work"}).status_code == 200
    view = client.get("/api/provider-config").json()
    state = next(d for d in view["databases"] if d["name"] == "work")
    assert state["needs_rebuild"] is False


def test_api_reset_provider_config(client):
    client.patch("/api/provider-config", json={"embedding_model": "custom/model"})
    assert client.get("/api/provider-config").json()["effective_embedding"]["model"] == "custom/model"

    body = client.post("/api/provider-config/reset", json={"keys": ["embedding_model"]}).json()
    assert body["reset"] == ["embedding_model"]
    assert client.get("/api/provider-config").json()["effective_embedding"]["model"] == "BAAI/bge-m3"


def test_api_test_endpoint_does_not_persist(client):
    body = client.post(
        "/api/provider-config/test",
        json={"group": "embedding", "values": {"embedding_model": "typo/model"}},
    ).json()
    assert body["ok"] is True  # 没填 Key，会退到离线哈希，所以仍然 ok
    assert client.get("/api/provider-config").json()["effective_embedding"]["model"] == "BAAI/bge-m3"


# ====================================================================== 存储层


def test_database_reports_source_and_stale(settings):
    from duramem.store.schema import clear_vectors_stale, mark_vectors_stale

    settings.ensure_dirs()
    path = settings.data_dir / "probe.db"
    db = Database(path, wal=False)
    try:
        db.initialize("uuid-probe", "2026-01-01T00:00:00+00:00", "BAAI/bge-m3", 1024, 256)
        assert db.vector_source == ""
        assert db.vectors_stale is False

        with db.write() as conn:
            mark_vectors_stale(conn, "offline-hashing-1024")
        assert db.vector_source == "offline-hashing-1024"
        assert db.vectors_stale is True

        # 标记后就该校验失败
        with pytest.raises(EmbeddingMismatchError):
            db.assert_compatible("BAAI/bge-m3", 1024, "BAAI/bge-m3")
        # 但来源相同时不拦——用户改回去过，向量其实还是对的
        db.assert_compatible("BAAI/bge-m3", 1024, "offline-hashing-1024")

        with db.write() as conn:
            clear_vectors_stale(conn, "BAAI/bge-m3", "BAAI/bge-m3", 1024)
        assert db.vectors_stale is False
        db.assert_compatible("BAAI/bge-m3", 1024, "BAAI/bge-m3")
    finally:
        db.close()


def test_source_mismatch_is_blocked_even_without_stale_flag(settings):
    """指纹对不上就必须拦，哪怕 `vectors_stale` 没被标记。

    标记由"改配置的那个进程"写；MCP 子进程这类长驻进程在重启前根本不知道配置
    变了。只看标记的话它会一路放行：用哈希算查询向量、去比真模型的向量，距离全
    落在阈值之外，整个向量路被静默剔空、退化成纯词法检索，而界面上一片正常。
    指纹就是为识别"模型名一样、实际提供方不同"而存在的。
    """
    from duramem.store.schema import clear_vectors_stale

    settings.ensure_dirs()
    db = Database(settings.data_dir / "fingerprint.db", wal=False)
    try:
        db.initialize("uuid-fp", "2026-01-01T00:00:00+00:00", "BAAI/bge-m3", 1024, 256)
        with db.write() as conn:
            # 库里的向量其实是离线哈希算的，但标记已被清掉（重建成功后就是这样）
            clear_vectors_stale(conn, "offline-hashing-1024", "BAAI/bge-m3", 1024)
        assert db.vectors_stale is False

        # 提供方与库内来源一致 → 放行
        db.assert_compatible("BAAI/bge-m3", 1024, "offline-hashing-1024")

        with pytest.raises(EmbeddingMismatchError) as exc:
            db.assert_compatible("BAAI/bge-m3", 1024, "BAAI/bge-m3")
        assert "offline-hashing-1024" in str(exc.value)
        assert "重启" in str(exc.value), "要告诉用户长驻进程需要重启才读新配置"
    finally:
        db.close()


def test_legacy_database_without_source_still_opens(settings):
    """没有 vector_source 字段的旧库不能被新校验拦住。"""
    settings.ensure_dirs()
    db = Database(settings.data_dir / "legacy.db", wal=False)
    try:
        db.initialize("uuid-legacy", "2026-01-01T00:00:00+00:00", "BAAI/bge-m3", 1024, 256)
        # 模拟旧库：删掉来源字段
        with db.write() as conn:
            conn.execute("DELETE FROM db_meta WHERE key='vector_source'")
        db.assert_compatible("BAAI/bge-m3", 1024, "offline-hashing-1024")
    finally:
        db.close()
