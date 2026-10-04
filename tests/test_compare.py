"""POST /compare：活动 vs 候选路由的只读归属比较。"""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.publish import PublishStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = PublishStore(tmp_path / "active.json")
    monkeypatch.setattr(main, "store", store)
    return TestClient(app)


def publish(client, routes, expected_version=0):
    return client.post(
        "/publish",
        json={"routes": routes, "expected_version": expected_version},
    )


def compare(client, routes, samples):
    return client.post("/compare", json={"routes": routes, "samples": samples})


ACTIVE_ROUTES = [
    {"method": "GET", "path": "/api/**", "priority": 0, "id": "catchall"},
    {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/api/users/me", "priority": 10, "id": "me"},
]

CANDIDATE_ROUTES = [
    # 候选中 user 提到更前（下标 0），但显式 id 与模板未变 -> 不算改投
    {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/api/**", "priority": 0, "id": "catchall"},
    {"method": "GET", "path": "/api/users/me", "priority": 10, "id": "me"},
    # 新增高优先级静态路由，抢走原属 catchall 的一个点（仅部分相交，可发布）
    {"method": "GET", "path": "/api/health", "priority": 5, "id": "health"},
]


@pytest.fixture()
def published(client):
    assert publish(client, ACTIVE_ROUTES, 0).json()["status"] == "published"
    return client


def test_compare_reports_version_and_matches(published):
    body = compare(
        published,
        CANDIDATE_ROUTES,
        [
            {"method": "GET", "path": "/api/users/1"},
            {"method": "GET", "path": "/api/users/me"},
            {"method": "GET", "path": "/api/health"},
            {"method": "GET", "path": "/api/other"},
            {"method": "GET", "path": "/nope"},
        ],
    ).json()

    assert body["status"] == "compared"
    assert body["version"] == 1
    assert body["parse_errors"] == []
    assert body["conflicts"] == []
    results = body["results"]
    assert [r["index"] for r in results] == [0, 1, 2, 3, 4]

    # /api/users/1：两侧都是 user（尽管候选中下标从 1 移到 0）-> 不改投
    r = results[0]
    assert r["normalized_path"] == "/api/users/1"
    assert r["active"] == {
        "index": 1, "id": "user", "normalized_template": "/api/users/{id:int}"
    }
    assert r["candidate"] == {
        "index": 0, "id": "user", "normalized_template": "/api/users/{id:int}"
    }
    assert r["rerouted"] is False

    # /api/users/me：两侧都是 me
    r = results[1]
    assert r["active"]["id"] == "me"
    assert r["candidate"]["id"] == "me"
    assert r["rerouted"] is False

    # /api/health：活动侧 catchall（无静态路由），候选侧新的 health -> 改投
    r = results[2]
    assert r["active"]["id"] == "catchall"
    assert r["active"]["normalized_template"] == "/api/**"
    assert r["candidate"]["id"] == "health"
    assert r["candidate"]["normalized_template"] == "/api/health"
    assert r["rerouted"] is True

    # /api/other：两侧 catchall，下标在两侧分别是 0 / 1，不算改投
    r = results[3]
    assert r["active"]["index"] == 0
    assert r["candidate"]["index"] == 1
    assert r["rerouted"] is False

    # 无命中：两侧明确记空
    r = results[4]
    assert r["normalized_path"] == "/nope"
    assert r["active"] == {"index": None, "id": None, "normalized_template": None}
    assert r["candidate"] == {"index": None, "id": None, "normalized_template": None}
    assert r["rerouted"] is False


def test_compare_is_readonly_does_not_change_active(published):
    before = published.get("/active").json()
    compare(published, CANDIDATE_ROUTES, [{"method": "GET", "path": "/api/users/1"}])
    after = published.get("/active").json()
    assert before == after
    assert after["version"] == 1


def test_compare_against_empty_active_version_zero(client):
    body = compare(
        client,
        [{"method": "GET", "path": "/x", "id": "x"}],
        [{"method": "GET", "path": "/x"}, {"method": "GET", "path": "/y"}],
    ).json()
    assert body["status"] == "compared"
    assert body["version"] == 0
    assert body["results"][0]["active"]["index"] is None
    assert body["results"][0]["candidate"]["id"] == "x"
    assert body["results"][0]["rerouted"] is True
    # 活动侧无命中 -> 候选无命中，不算改投
    assert body["results"][1]["rerouted"] is False
    # 预检后活动仍为空版本 0
    active = client.get("/active").json()
    assert active["version"] == 0
    assert active["routes"] == []


def test_compare_rejected_candidate_skips_comparison(client):
    body = compare(
        client,
        [
            {"method": "GET", "path": "/x/{id:int}"},
            {"method": "GET", "path": "/x/{id:uuid}"},  # 导出路径重复
            {"method": "FROG", "path": "/z"},           # 非法方法
        ],
        [{"method": "GET", "path": "/x/1"}],
    ).json()
    assert body["status"] == "rejected"
    assert body["version"] == 0
    assert body["results"] is None
    assert [e["index"] for e in body["parse_errors"]] == [1, 2]


def test_compare_rejected_on_full_shadow_with_indices(published):
    body = compare(
        published,
        [
            {"method": "GET", "path": "/files/{name}", "priority": 5, "id": "all"},
            {"method": "GET", "path": "/files/report.pdf", "priority": 0, "id": "f"},
        ],
        [{"method": "GET", "path": "/files/report.pdf"}],
    ).json()
    assert body["status"] == "rejected"
    assert body["results"] is None
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["kind"] == "full_shadow"
    assert body["conflicts"][0]["winner_index"] == 0
    assert body["conflicts"][0]["loser_index"] == 1
    # 活动版本仍是发布后的 1
    assert body["version"] == 1


def test_publish_still_rejects_stale_expected_version_after_compare(published):
    compare(published, CANDIDATE_ROUTES, [{"method": "GET", "path": "/api/users/1"}])
    stale = publish(published, CANDIDATE_ROUTES, expected_version=0).json()
    assert stale["status"] == "version_conflict"
    assert stale["version"] == 1


def test_invalid_samples_reported_per_item_others_continue(published):
    body = compare(
        published,
        ACTIVE_ROUTES,
        [
            {"method": "FROG", "path": "/api/users/1"},
            {"method": "GET", "path": "relative/path"},
            {"method": "get", "path": "/api/users/1"},
            {"method": "GET", "path": "/api/%zz"},
            {"method": "GET", "path": "/api/users/1/"},
        ],
    ).json()
    results = body["results"]
    assert body["status"] == "compared"

    err0 = results[0]["errors"][0]
    assert err0["code"] == "invalid_method"
    assert results[0]["normalized_path"] is None
    assert results[0]["rerouted"] is None
    assert results[0]["active"] is None

    err1 = results[1]["errors"][0]
    assert err1["code"] == "invalid_path"

    # 其余样本继续：小写方法规范化为 GET
    assert results[2]["errors"] == []
    assert results[2]["method"] == "GET"
    assert results[2]["active"]["id"] == "user"
    assert results[2]["rerouted"] is False

    # 非法百分号转义逐项报错
    assert results[3]["errors"][0]["code"] == "invalid_path"

    # 尾随斜杠被规范化移除，正常命中
    assert results[4]["normalized_path"] == "/api/users/1"
    assert results[4]["active"]["id"] == "user"


def test_encoded_slash_is_not_a_segment_separator(published):
    body = compare(
        published,
        ACTIVE_ROUTES,
        [
            {"method": "GET", "path": "/api/users%2F1"},
            {"method": "GET", "path": "/api/users/1"},
        ],
    ).json()
    results = body["results"]
    # %2F 保留在单段内：/api/users%2F1 只有两段，只能被 /api/** 吸收
    assert results[0]["normalized_path"] == "/api/users%2F1"
    assert results[0]["active"]["id"] == "catchall"
    # 真正的斜杠切两段，命中 int 参数路由
    assert results[1]["active"]["id"] == "user"


def test_reroute_depends_on_explicit_id_not_just_template(published):
    # 同方法、同规范化模板，但显式 id 不同 -> 视为改投
    candidate = [
        {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "v2-user"},
        {"method": "GET", "path": "/api/**", "priority": 0, "id": "catchall"},
        {"method": "GET", "path": "/api/users/me", "priority": 10, "id": "me"},
    ]
    body = compare(
        published, candidate, [{"method": "GET", "path": "/api/users/1"}]
    ).json()
    r = body["results"][0]
    assert r["active"]["normalized_template"] == r["candidate"]["normalized_template"]
    assert r["active"]["id"] == "user"
    assert r["candidate"]["id"] == "v2-user"
    assert r["rerouted"] is True


def test_auto_generated_ids_identity_is_template_based_when_no_explicit_id(client):
    # 活动与候选均无显式 id：下标移动不影响归属身份（模板相同即不改投）
    routes_a = [
        {"method": "GET", "path": "/a/**", "priority": -1},
        {"method": "GET", "path": "/a/b", "priority": 0},
        {"method": "GET", "path": "/a/c", "priority": 0},
    ]
    # 候选仅调换注册顺序（两条静态路由互换），无 full_shadow（静态互不遮蔽）
    routes_b = [
        {"method": "GET", "path": "/a/c", "priority": 0},
        {"method": "GET", "path": "/a/b", "priority": 0},
        {"method": "GET", "path": "/a/**", "priority": -1},
    ]
    assert publish(client, routes_a, 0).json()["status"] == "published"
    body = compare(client, routes_b, [{"method": "GET", "path": "/a/b"}]).json()
    r = body["results"][0]
    # /a/b 两侧都命中静态路由（同优先级静态胜过通配）
    assert r["active"]["normalized_template"] == "/a/b"
    assert r["candidate"]["normalized_template"] == "/a/b"
    assert r["active"]["id"] == "route-1"
    assert r["candidate"]["id"] == "route-1"
    assert r["rerouted"] is False


def test_wildcard_and_root_matching(client):
    routes = [{"method": "GET", "path": "/a/**", "id": "w"}]
    body = compare(
        client,
        routes,
        [
            {"method": "GET", "path": "/a"},      # ** 匹配零段
            {"method": "GET", "path": "/a/b/c"},
            {"method": "GET", "path": "/"},       # 根不与 /a/** 相交
            {"method": "POST", "path": "/a"},     # 方法不同
        ],
    ).json()
    results = body["results"]
    assert results[0]["candidate"]["id"] == "w"
    assert results[1]["candidate"]["id"] == "w"
    assert results[2]["candidate"]["index"] is None
    assert results[3]["candidate"]["index"] is None


def test_compare_missing_envelope_fields_422(client):
    assert client.post("/compare", json={"routes": []}).status_code == 422
    assert client.post("/compare", json={"samples": []}).status_code == 422
    assert client.post(
        "/compare",
        json={
            "routes": [],
            "samples": [{"path": "/x"}],
        },
    ).status_code == 422


def test_compare_uses_same_snapshot_version_within_one_request(client):
    # 活动侧版本号直接取自固定快照；发布后版本为 1
    assert publish(client, [{"method": "GET", "path": "/a"}], 0).json()["status"] == "published"
    body = compare(client, [{"method": "GET", "path": "/b"}], [{"method": "GET", "path": "/a"}]).json()
    assert body["version"] == 1


def test_priority_descending_and_registration_order_tiebreak(client):
    # 同级注册先后用于唯一命中（两条同级路由前缀不相交，可通过发布校验）；
    # 高优先级静态在另一前缀上压过低优先级通配。
    candidate = [
        {"method": "GET", "path": "/w/**", "priority": 0, "id": "w-wild"},
        {"method": "GET", "path": "/z/a", "priority": 0, "id": "z-static"},
        {"method": "GET", "path": "/x/**", "priority": 0, "id": "x-wild"},
        {"method": "GET", "path": "/x/b", "priority": 9, "id": "high"},
    ]
    body = compare(
        client,
        candidate,
        [
            {"method": "GET", "path": "/w/a"},
            {"method": "GET", "path": "/z/a"},
            {"method": "GET", "path": "/x/b"},
            {"method": "GET", "path": "/x/c"},
        ],
    ).json()
    results = body["results"]
    assert results[0]["candidate"]["id"] == "w-wild"
    assert results[1]["candidate"]["id"] == "z-static"
    # /x/b：高优先级静态胜过先注册的同级通配
    assert results[2]["candidate"]["id"] == "high"
    # /x/c：仅通配命中
    assert results[3]["candidate"]["id"] == "x-wild"
