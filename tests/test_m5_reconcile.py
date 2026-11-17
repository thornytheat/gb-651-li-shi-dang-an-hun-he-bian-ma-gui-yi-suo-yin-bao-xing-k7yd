import json
import threading

import pytest

from app.errors import DomainError
from app.modules.m5_reconcile import core
from app.modules.m5_reconcile.ledger import Ledger

SAMPLE = core.load_sample_dataset()
DATASET = {"left": SAMPLE["left"], "right": SAMPLE["right"], "quarantine": SAMPLE["quarantine"], "empty": SAMPLE["empty"]}

CROSS_SOURCE = {
    "left": [
        {"source": "馆藏目录", "archive_id": "000123", "source_line": 2},
        {"source": "专题索引", "archive_id": "000123", "source_line": 2},
    ],
    "right": [
        {"source": "馆藏目录", "archive_id": "000123", "source_line": 2},
        {"source": "专题索引", "archive_id": "000123", "source_line": 2},
    ],
    "quarantine": 0,
    "empty": 0,
}


@pytest.fixture
def ledger(tmp_path):
    return Ledger(str(tmp_path / "ledger.db"))


# ---- 原字比较 ------------------------------------------------------
def test_reconcile_still_passes_legacy_contract():
    assert core.reconcile(["001"], ["001"])["missing"] == []
    assert core.reconcile(["A"], ["B"])["orphan"] == ["B"]
    assert core.reconcile(["A", "A"], ["A"])["index_count"] == 2
    assert core.reconcile(["9007199254740993"], ["9007199254740993"])["missing"] == []


def test_leading_zero_is_not_normalized():
    out = core.reconcile(["001"], ["1"])
    assert out["missing"] == ["001"] and out["orphan"] == ["1"]


def test_fullwidth_is_preserved_verbatim():
    out = core.reconcile(["ＡＢＣ－００１"], ["ABC-001"])
    assert out["missing"] == ["ＡＢＣ－００１"] and out["orphan"] == ["ABC-001"]


def test_cross_source_same_id_does_not_auto_merge():
    out = core.reconcile(CROSS_SOURCE["left"], CROSS_SOURCE["right"])
    assert out["matched_count"] == 2
    assert out["missing"] == [] and out["orphan"] == []


def test_same_id_only_on_one_side_is_not_a_match():
    left = [{"source": "馆藏目录", "archive_id": "000123"}]
    right = [{"source": "专题索引", "archive_id": "000123"}]
    out = core.reconcile(left, right)
    assert out["matched_count"] == 0
    assert out["missing"] == ["000123"] and out["orphan"] == ["000123"]


# ---- 认领与依据 ----------------------------------------------------
def test_claim_records_basis_and_applies(ledger):
    result = ledger.claim(
        {"source": "馆藏目录", "old_id": "000124", "new_id": "A-0124", "basis": "馆方确认函第 3 条"},
        {"left": [{"source": "馆藏目录", "archive_id": "000124"}], "right": [{"source": "馆藏目录", "archive_id": "A-0124"}]},
    )
    assert result["alias"]["status"] == "active"
    assert result["alias"]["basis"] == "馆方确认函第 3 条"
    assert result["delta"] == {"matched": 1, "missing": -1, "orphan": -1}
    assert result["diff_before"]["missing_count"] == 1
    assert result["diff_after"]["missing_count"] == 0


def test_claim_requires_basis(ledger):
    with pytest.raises(DomainError) as info:
        ledger.claim({"source": "A", "old_id": "1", "new_id": "2"}, DATASET)
    assert info.value.code == "BASIS_REQUIRED"


def test_alias_does_not_cross_sources():
    left = [{"source": "馆藏目录", "archive_id": "000124"}]
    right = [{"source": "专题索引", "archive_id": "A-0124"}]
    out = core.reconcile(
        left,
        right,
        aliases=[{"source": "馆藏目录", "old_id": "000124", "new_id": "A-0124", "basis": "x", "status": "active", "version": 1}],
    )
    assert out["matched_count"] == 0
    assert out["missing"] == ["000124"] and out["orphan"] == ["A-0124"]


def test_fullwidth_alias_matches_without_rewriting_raw_values(ledger):
    dataset = {"left": [{"source": "A", "archive_id": "全001"}], "right": [{"source": "A", "archive_id": "全００１"}]}
    result = ledger.claim({"alias_id": "alias-full", "source": "A", "old_id": "全001", "new_id": "全００１", "basis": "全角原值"}, dataset)
    assert result["delta"]["matched"] == 1
    assert result["alias"]["old_id"] == "全001" and result["alias"]["new_id"] == "全００１"


# ---- 歧义保留 ------------------------------------------------------
def test_sample_has_pending_ambiguity():
    out = core.reconcile(SAMPLE["left"], SAMPLE["right"], SAMPLE["quarantine"], SAMPLE["empty"], SAMPLE["suggested_claims"])
    groups = {group["reason"]: group for group in out["ambiguous_groups"]}
    assert set(groups) == {"ONE_TO_MANY", "MANY_TO_ONE"}
    assert groups["ONE_TO_MANY"]["old_ids"] == ["000124"]
    assert set(groups["ONE_TO_MANY"]["new_ids"]) == {"A-0124", "A-0125"}
    assert groups["MANY_TO_ONE"]["old_ids"] == ["000404", "000405"]
    assert groups["MANY_TO_ONE"]["new_ids"] == ["000777"]


def test_seeded_sample_keeps_differences_until_claimed():
    out = core.reconcile(SAMPLE["left"], SAMPLE["right"], SAMPLE["quarantine"], SAMPLE["empty"], SAMPLE["suggested_claims"])
    assert out["matched_count"] == 4
    assert "000124" in out["missing"] and "A-0124" in out["orphan"]
    assert out["balanced"] is True


def test_pending_aliases_do_not_change_difference_zero(ledger):
    assert ledger.seed(SAMPLE["suggested_claims"]) == len(SAMPLE["suggested_claims"])
    assert ledger.seed(SAMPLE["suggested_claims"]) == 0
    out = ledger.diff_summary(DATASET, ledger.aliases())
    assert out["missing_count"] == 4 and out["orphan_count"] == 4


def test_claiming_one_side_of_ambiguity_is_a_conflict(ledger):
    ledger.seed(SAMPLE["suggested_claims"])
    with pytest.raises(DomainError) as info:
        ledger.claim(
            {"alias_id": "alias-0124-a", "source": "馆藏目录", "old_id": "000124", "new_id": "A-0124", "basis": "确认函", "expected_version": 1},
            DATASET,
        )
    assert info.value.code in ("ONE_TO_MANY", "MANY_TO_ONE")
    assert ledger.aliases()[0]["status"] == "pending"
    assert ledger.conflicts()[-1]["reason"] == info.value.code


def test_many_to_one_collision_rejected(ledger):
    dataset = {"left": [{"source": "A", "archive_id": "0001"}, {"source": "A", "archive_id": "0002"}], "right": [{"source": "A", "archive_id": "A1"}]}
    ledger.claim({"source": "A", "old_id": "0001", "new_id": "A1", "basis": "确认函 1"}, dataset)
    with pytest.raises(DomainError) as info:
        ledger.claim({"source": "A", "old_id": "0002", "new_id": "A1", "basis": "确认函 2"}, dataset)
    assert info.value.code == "MANY_TO_ONE"
    assert [alias["status"] for alias in ledger.aliases()] == ["active"]


def test_sample_ambiguity_blocks_claim_until_candidate_excluded(ledger):
    """歧义保留待裁决：先排除竞争候选，才能认领存活候选。"""
    ledger.seed(SAMPLE["suggested_claims"])
    with pytest.raises(DomainError) as blocked:
        ledger.claim(
            {"alias_id": "alias-many-a", "source": "馆藏目录", "old_id": "000404", "new_id": "000777", "basis": "馆方确认", "expected_version": 1, "decision_key": "claim-many-blocked"},
            DATASET,
        )
    assert blocked.value.code == "MANY_TO_ONE"
    # 000405 仍待裁决时，被拒绝的认领不改变差异计数。
    assert ledger.diff_summary(DATASET, ledger.aliases())["missing_count"] == 4

    ledger.undo("alias-many-b", {"basis": "排除竞争候选 000405", "expected_version": 1}, DATASET)
    claimed = ledger.claim(
        {"alias_id": "alias-many-a", "source": "馆藏目录", "old_id": "000404", "new_id": "000777", "basis": "馆方确认", "expected_version": 1, "decision_key": "claim-many-after-exclusion"},
        DATASET,
    )
    assert claimed["delta"]["matched"] == 1
    assert claimed["delta"]["missing"] == -1
    assert claimed["delta"]["orphan"] == -1


def test_sample_many_to_one_leaves_unclaimed_record_visible(ledger):
    ledger.seed(SAMPLE["suggested_claims"])
    ledger.undo("alias-many-b", {"basis": "排除", "expected_version": 1}, DATASET)
    ledger.claim(
        {"alias_id": "alias-many-a", "source": "馆藏目录", "old_id": "000404", "new_id": "000777", "basis": "馆方确认", "expected_version": 1},
        DATASET,
    )
    report = core.reconcile(SAMPLE["left"], SAMPLE["right"], SAMPLE["quarantine"], SAMPLE["empty"], ledger.aliases())
    assert "000404" not in report["missing"] and "000777" not in report["orphan"]
    # 多对一碰撞的另一条实际记录 000405 不被误并，仍出现在缺失侧。
    assert "000405" in report["missing"]
    assert report["matched_count"] == 5
    assert report["balanced"] is True


def test_cross_source_claim_does_not_merge_other_source_records(ledger):
    """用实际样例中的跨来源同号记录验证：认领只在所属来源生效。"""
    ledger.seed(SAMPLE["suggested_claims"])
    ledger.undo("alias-full", {"basis": "先排除全角候选", "expected_version": 1}, DATASET)
    ledger.claim(
        {"alias_id": "alias-full", "source": "馆藏目录", "old_id": "全001", "new_id": "全００１", "basis": "全角原值", "expected_version": 2},
        DATASET,
    )
    report = core.reconcile(SAMPLE["left"], SAMPLE["right"], SAMPLE["quarantine"], SAMPLE["empty"], ledger.aliases())
    by_source = {row["source"]: row for row in report["sources"]}
    # 专题索引没有登记别名：000404 的差异必须保留，不能被馆藏目录的认领影响。
    assert by_source["专题索引"]["matched_count"] == 2
    assert by_source["专题索引"]["missing"] == [] and by_source["专题索引"]["orphan"] == []
    assert "000404" in by_source["馆藏目录"]["missing"]
    assert by_source["馆藏目录"]["matched_count"] == 3
    ledger.seed(
        [
            {"alias_id": "m1", "source": "A", "old_id": "1", "new_id": "X", "basis": "b1"},
            {"alias_id": "m2", "source": "A", "old_id": "2", "new_id": "X", "basis": "b2"},
        ]
    )
    empty = {"left": [], "right": []}
    out = ledger.diff_summary(empty, ledger.aliases())
    groups = core.ambiguous_groups(ledger.aliases())
    assert groups[0]["reason"] == "MANY_TO_ONE"
    assert groups[0]["new_ids"] == ["X"]
    assert out["matched_count"] == 0
    assert [candidate["status"] for candidate in groups[0]["candidates"]] == ["pending", "pending"]


# ---- 差异计数与决定同事务 ------------------------------------------
def test_split_applies_with_diff_counts(ledger):
    dataset = {
        "left": [{"source": "A", "archive_id": "1"}, {"source": "A", "archive_id": "2"}],
        "right": [{"source": "A", "archive_id": "X"}, {"source": "A", "archive_id": "Y"}],
    }
    claim = ledger.claim({"alias_id": "alias-1x", "source": "A", "old_id": "1", "new_id": "X", "basis": "b"}, dataset)
    assert claim["delta"]["matched"] == 1
    split = ledger.split(
        "alias-1x",
        [{"old_id": "1", "new_id": "Y", "alias_id": "part-y"}, {"old_id": "2", "new_id": "X", "alias_id": "part-x"}],
        {"basis": "原认领实为两条", "expected_version": 1},
        dataset,
    )
    assert split["alias"]["status"] == "split"
    assert split["diff_after"]["matched_count"] == 2
    assert split["delta"]["matched"] == 1
    assert sorted(alias["status"] for alias in ledger.aliases()) == ["active", "active", "split"]


def test_undo_restores_both_side_differences(ledger):
    dataset = {"left": [{"source": "A", "archive_id": "全001"}], "right": [{"source": "A", "archive_id": "全００１"}]}
    ledger.claim({"alias_id": "alias-full", "source": "A", "old_id": "全001", "new_id": "全００１", "basis": "b"}, dataset)
    undo = ledger.undo("alias-full", {"basis": "撤回", "expected_version": 1}, dataset)
    assert undo["delta"] == {"matched": -1, "missing": 1, "orphan": 1}
    assert undo["alias"]["status"] == "revoked"
    summary = ledger.diff_summary(dataset, ledger.aliases())
    assert summary["missing_count"] == 1 and summary["orphan_count"] == 1


# ---- 幂等、版本与并发 ----------------------------------------------
def test_retry_does_not_duplicate_claim(ledger):
    request = {"source": "A", "old_id": "0001", "new_id": "A1", "basis": "确认函", "decision_key": "dec-1"}
    dataset = {"left": [{"source": "A", "archive_id": "0001"}], "right": [{"source": "A", "archive_id": "A1"}]}
    first = ledger.claim(request, dataset)
    second = ledger.claim(request, dataset)
    assert second["operation_state"] == "REPLAYED"
    assert second["alias"]["version"] == first["alias"]["version"] == 1
    assert len(ledger.aliases()) == 1
    assert len([op for op in ledger.operations() if op["state"] == "applied"]) == 1


def test_old_decision_replay_does_not_override_new_ruling(ledger):
    dataset = {"left": [{"source": "A", "archive_id": "0001"}], "right": [{"source": "A", "archive_id": "A1"}]}
    ledger.claim({"alias_id": "alias-a", "source": "A", "old_id": "0001", "new_id": "A1", "basis": "初裁", "decision_key": "d1", "expected_version": 0}, dataset)
    ledger.undo("alias-a", {"basis": "复核撤回", "decision_key": "d2", "expected_version": 1}, dataset)
    with pytest.raises(DomainError) as info:
        ledger.claim({"alias_id": "alias-a", "source": "A", "old_id": "0001", "new_id": "A1", "basis": "旧决定回放", "decision_key": "d3", "expected_version": 1}, dataset)
    assert info.value.code == "STALE_VERSION"
    alias = ledger.aliases()[0]
    assert alias["status"] == "revoked" and alias["version"] == 2
    assert [item["reason"] for item in ledger.conflicts()] == ["STALE_VERSION"]


def test_concurrent_claims_are_visible_and_single_winner(tmp_path):
    path = str(tmp_path / "race.db")
    dataset = {"left": [{"source": "A", "archive_id": "0001"}], "right": [{"source": "A", "archive_id": "A1"}]}
    outcomes = []

    def attempt(decision_key):
        try:
            ledger = Ledger(path)
            result = ledger.claim({"source": "A", "old_id": "0001", "new_id": "A1", "basis": "并发", "decision_key": decision_key}, dataset)
            outcomes.append(("ok", decision_key, result["alias"]["version"]))
        except DomainError as exc:
            outcomes.append(("err", decision_key, exc.code))

    threads = [threading.Thread(target=attempt, args=(key,)) for key in ("race-1", "race-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(item[0] for item in outcomes) == ["err", "ok"]
    ledger = Ledger(path)
    assert len(ledger.aliases()) == 1
    assert ledger.conflicts()[0]["reason"] == outcomes[0][2] if outcomes[0][0] == "err" else ledger.conflicts()[0]["reason"] == outcomes[1][2]


# ---- 中断重启 ------------------------------------------------------
def test_interrupted_operation_resumes_without_double_claim(tmp_path):
    path = str(tmp_path / "resume.db")
    dataset = {"left": [{"source": "A", "archive_id": "0001"}], "right": [{"source": "A", "archive_id": "A1"}]}
    ledger = Ledger(path)
    ledger.claim({"alias_id": "alias-a", "source": "A", "old_id": "0001", "new_id": "A1", "basis": "b", "decision_key": "resume-1"}, dataset)

    # 模拟中断：决定已落库但日志停在 intent。
    import sqlite3

    with sqlite3.connect(path) as db:
        db.execute("UPDATE alias_operations SET state='intent', result=NULL WHERE decision_key='resume-1'")

    resumed = Ledger(path)
    assert resumed.operations()[0]["state"] == "applied"
    replay = resumed.claim({"alias_id": "alias-a", "source": "A", "old_id": "0001", "new_id": "A1", "basis": "b", "decision_key": "resume-1"}, dataset)
    assert replay["operation_state"] in ("REPLAYED", "RECOVERED")
    assert len(resumed.aliases()) == 1
    assert len(resumed.revisions("alias-a")) == 1


def test_orphan_intent_is_marked_interrupted_and_retry_applies(tmp_path):
    path = str(tmp_path / "intent.db")
    dataset = {"left": [{"source": "A", "archive_id": "0001"}], "right": [{"source": "A", "archive_id": "A1"}]}
    ledger = Ledger(path)
    request = {"source": "A", "old_id": "0001", "new_id": "A1", "basis": "b", "decision_key": "crash-1"}
    # 只写 intent，模拟进程在第一阶段后崩溃。
    import sqlite3

    canonical = json.dumps(request | {"kind": "claim", "alias_id": ""}, ensure_ascii=False, sort_keys=True)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO alias_operations (decision_key, kind, alias_id, expected_version, request, state, result, error, created_at, updated_at) VALUES (?, 'claim', '', NULL, ?, 'intent', NULL, NULL, 'x', 'x')",
            ("crash-1", canonical),
        )
    assert ledger.operations()[0]["state"] == "intent"
    recovered = Ledger(path)
    assert recovered.operations()[0]["state"] == "interrupted"
    # 同样的决定键与内容重试：内容不一致会被拒绝，换新键则正常认领。
    with pytest.raises(DomainError) as info:
        recovered.claim(request | {"alias_id": ""}, dataset)
    assert info.value.code == "DECISION_KEY_REUSED"
    result = recovered.claim(request | {"decision_key": "crash-2"}, dataset)
    assert result["status"] == "APPLIED" and len(recovered.aliases()) == 1


# ---- API 端到端 ----------------------------------------------------
def test_api_claim_and_reconcile_reflects_ledger(client):
    payload = {
        "left": [{"source": "馆藏目录", "archive_id": "000124"}],
        "right": [{"source": "馆藏目录", "archive_id": "A-0124"}],
        "quarantine": 0,
        "empty": 0,
    }
    before = client.post("/api/reconcile", json=payload).json()
    assert before["missing"] == ["000124"] and before["orphan"] == ["A-0124"]

    claim = client.post(
        "/api/reconcile/claim",
        json={"source": "馆藏目录", "old_id": "000124", "new_id": "A-0124", "basis": "馆方确认函第 3 条", "dataset": payload},
    )
    assert claim.status_code == 200
    alias_id = claim.json()["alias"]["alias_id"]

    after = client.post("/api/reconcile", json=payload).json()
    assert after["matched_count"] == 1 and after["missing"] == [] and after["orphan"] == []
    assert after["aliases_applied"][0]["basis"] == "馆方确认函第 3 条"
    assert after["ledger"]["revisions"] and after["ledger"]["revisions"][0]["diff_after"]["missing_count"] == 0

    evidence = client.get(f"/api/reconcile/claims/{alias_id}/evidence").json()
    assert evidence["evidence"][0]["kind"] == "seed" or evidence["evidence"][0]["basis"] == "馆方确认函第 3 条"

    undone = client.post(f"/api/reconcile/claims/{alias_id}/undo", json={"basis": "复核撤回", "expected_version": 1, "dataset": payload})
    assert undone.status_code == 200
    restored = client.post("/api/reconcile", json=payload).json()
    assert restored["missing"] == ["000124"] and restored["orphan"] == ["A-0124"]


def test_api_stale_decision_is_visible(client):
    payload = {"left": [{"source": "A", "archive_id": "1"}], "right": [{"source": "A", "archive_id": "2"}]}
    first = client.post("/api/reconcile/claim", json={"alias_id": "alias-race", "source": "A", "old_id": "1", "new_id": "2", "basis": "b", "expected_version": 0, "dataset": payload})
    assert first.status_code == 200
    client = client
    second = client.post(
        "/api/reconcile/claims/alias-race/undo",
        json={"basis": "撤回", "expected_version": 1, "decision_key": "u1", "dataset": payload},
    )
    assert second.status_code == 200
    stale = client.post(
        "/api/reconcile/claim",
        json={"alias_id": "alias-race", "source": "A", "old_id": "1", "new_id": "2", "basis": "旧决定", "expected_version": 1, "decision_key": "old", "dataset": payload},
    )
    assert stale.status_code == 422
    assert stale.json()["error"] == "STALE_VERSION"
    ledger = client.get("/api/reconcile/ledger").json()
    assert ledger["conflicts"][-1]["reason"] == "STALE_VERSION"
    assert ledger["aliases"][0]["status"] == "revoked"


def test_api_split_endpoint(client):
    payload = {"left": [{"source": "A", "archive_id": "1"}, {"source": "A", "archive_id": "2"}], "right": [{"source": "A", "archive_id": "X"}, {"source": "A", "archive_id": "Y"}]}
    assert client.post("/api/reconcile/claim", json={"alias_id": "alias-1x", "source": "A", "old_id": "1", "new_id": "X", "basis": "b", "expected_version": 0, "dataset": payload}).status_code == 200
    split = client.post(
        "/api/reconcile/claims/alias-1x/split",
        json={"expected_version": 1, "basis": "实为两条", "dataset": payload, "parts": [{"old_id": "1", "new_id": "Y", "alias_id": "s1"}, {"old_id": "2", "new_id": "X", "alias_id": "s2"}]},
    )
    assert split.status_code == 200
    assert split.json()["diff_after"]["matched_count"] == 2
    report = client.post("/api/reconcile", json=payload).json()
    assert report["matched_count"] == 2


def test_page_renders_ledger_sections(client):
    response = client.get("/reconcile")
    assert response.status_code == 200
    assert "别名认领" in response.text
    assert "待裁决" in response.text
    assert "alias-0124-a" in response.text
