"""relocation-preview：路径搬迁预演测试（前缀匹配 + 结果冲突复检 + 422 校验）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _post(payload):
    return client.post("/api/v1/relocation-preview", json=payload)


def _preview(entries, rules):
    r = _post({"entries": entries, "rules": rules})
    assert r.status_code == 200, r.text
    return r.json()


def _by_index(body):
    return {e["index"]: e for e in body["entries"]}


# ---------------------------------------------------------------------------
# 规则匹配：最长源前缀优先、同长度取最后一条、未命中保留原路径
# ---------------------------------------------------------------------------


def test_longest_source_prefix_wins():
    body = _preview(
        entries=[
            {"path": "docs/sub/a.txt", "type": "file"},
            {"path": "docs/b.txt", "type": "file"},
            {"path": "other/c.txt", "type": "file"},
        ],
        rules=[
            {"source_prefix": "docs", "target_prefix": "site"},
            {"source_prefix": "docs/sub", "target_prefix": "deep"},
        ],
    )
    by = _by_index(body)
    assert by[1]["new_path"] == "deep/a.txt"
    assert by[1]["matched_rule_index"] == 1
    assert by[2]["new_path"] == "site/b.txt"
    assert by[2]["matched_rule_index"] == 0
    # 未命中任何规则：保留原规范路径。
    assert by[3]["new_path"] == "other/c.txt"
    assert by[3]["matched_rule_index"] is None
    assert body["summary"] == {"total": 3, "usable": 3, "conflicts": 0}


def test_same_length_later_rule_wins():
    body = _preview(
        entries=[{"path": "docs/a.txt", "type": "file"}],
        rules=[
            {"source_prefix": "docs", "target_prefix": "first"},
            {"source_prefix": "docs", "target_prefix": "second"},
        ],
    )
    entry = body["entries"][0]
    assert entry["new_path"] == "second/a.txt"
    assert entry["matched_rule_index"] == 1


def test_prefix_must_align_on_segment_boundary():
    # 源前缀 a/b 不得匹配 a/bb：段边界必须对齐。
    body = _preview(
        entries=[{"path": "a/bb/c.txt", "type": "file"}],
        rules=[{"source_prefix": "a/b", "target_prefix": "x"}],
    )
    entry = body["entries"][0]
    assert entry["new_path"] == "a/bb/c.txt"
    assert entry["matched_rule_index"] is None


def test_exact_prefix_match_relocates_to_target_as_is():
    # 条目路径与源前缀等长：无后缀，新路径即目标前缀。
    body = _preview(
        entries=[{"path": "docs", "type": "directory"}],
        rules=[{"source_prefix": "docs", "target_prefix": "site/docs"}],
    )
    entry = body["entries"][0]
    assert entry["new_path"] == "site/docs"
    assert entry["matched_rule_index"] == 0


def test_windows_normalization_reused_for_entries_and_rules():
    # 大小写折叠、反斜杠分隔符与段尾裁剪在匹配与拼接中一致生效。
    body = _preview(
        entries=[{"path": "Docs\\ReadMe.TXT", "type": "file"}],
        rules=[{"source_prefix": "docs/", "target_prefix": "WEB\\assets\\"}],
    )
    entry = body["entries"][0]
    assert entry["new_path"] == "WEB/assets/ReadMe.TXT"
    assert entry["matched_rule_index"] == 0


def test_empty_rules_keeps_all_paths():
    r = _post(
        {
            "entries": [
                {"path": "a.txt", "type": "file"},
                {"path": "dir", "type": "directory"},
            ]
        }
    )
    assert r.status_code == 200
    body = r.json()
    by = _by_index(body)
    assert by[1]["new_path"] == "a.txt"
    assert by[1]["matched_rule_index"] is None
    assert by[2]["new_path"] == "dir"
    assert body["summary"]["usable"] == 2


# ---------------------------------------------------------------------------
# 结果路径同键重复
# ---------------------------------------------------------------------------


def test_duplicate_key_after_relocation_conflict():
    body = _preview(
        entries=[
            {"path": "old/a.txt", "type": "file"},
            {"path": "keep/b.txt", "type": "file"},  # 未命中，保持 keep/b.txt
            {"path": "legacy/a.txt", "type": "file"},  # 也搬到 new/a.txt
            {"path": "safe.txt", "type": "file"},
        ],
        rules=[
            {"source_prefix": "old", "target_prefix": "new"},
            {"source_prefix": "legacy", "target_prefix": "new"},
        ],
    )
    by = _by_index(body)
    assert by[1]["new_path"] == "new/a.txt"
    assert by[3]["new_path"] == "new/a.txt"
    for i in (1, 3):
        assert by[i]["usable"] is False
        assert by[i]["conflicts"] == [
            {"type": "DUPLICATE_KEY", "related_index": (3 if i == 1 else 1)}
        ]
    # 未参与冲突的条目保持可用。
    assert by[2]["usable"] is True
    assert by[2]["conflicts"] == []
    assert by[4]["usable"] is True
    assert body["summary"] == {"total": 4, "usable": 2, "conflicts": 2}


def test_relocated_result_collides_with_unmatched_original():
    # 搬迁结果恰好撞上未命中规则、保留原路径的条目：仍属同键重复。
    body = _preview(
        entries=[
            {"path": "old/a.txt", "type": "file"},  # -> new/a.txt
            {"path": "new/a.txt", "type": "file"},  # 未命中，恰为 new/a.txt
        ],
        rules=[{"source_prefix": "old", "target_prefix": "new"}],
    )
    by = _by_index(body)
    assert by[1]["usable"] is False
    assert by[2]["usable"] is False
    assert by[2]["matched_rule_index"] is None


def test_duplicate_key_casefold_equivalence():
    # 目标前缀的大小写差异经比较键抹平，仍判同键重复。
    body = _preview(
        entries=[
            {"path": "a/x.txt", "type": "file"},
            {"path": "b/x.txt", "type": "file"},
        ],
        rules=[
            {"source_prefix": "a", "target_prefix": "Out"},
            {"source_prefix": "b", "target_prefix": "OUT"},
        ],
    )
    by = _by_index(body)
    assert by[1]["usable"] is False
    assert by[2]["usable"] is False
    rels = {c["related_index"] for c in by[1]["conflicts"]}
    assert rels == {2}


def test_file_and_directory_same_new_key_conflict():
    body = _preview(
        entries=[
            {"path": "old/dir", "type": "directory"},
            {"path": "other/dir", "type": "file"},
        ],
        rules=[
            {"source_prefix": "old", "target_prefix": "r"},
            {"source_prefix": "other", "target_prefix": "r"},
        ],
    )
    by = _by_index(body)
    assert by[1]["new_path"] == "r/dir"
    assert by[2]["new_path"] == "r/dir"
    assert by[1]["usable"] is False
    assert by[2]["usable"] is False
    assert all(
        c["type"] == "DUPLICATE_KEY"
        for e in (by[1], by[2])
        for c in e["conflicts"]
    )


def test_preexisting_duplicate_without_rules_conflict():
    # 不搬也可能冲突：原路径同键重复直接在结果路径上暴露。
    body = _preview(
        entries=[
            {"path": "docs/readme.txt", "type": "file"},
            {"path": "Docs\\ReadMe.TXT", "type": "file"},
        ],
        rules=[],
    )
    by = _by_index(body)
    assert by[1]["new_path"] == "docs/readme.txt"
    assert by[2]["new_path"] == "Docs/ReadMe.TXT"
    assert by[1]["usable"] is False
    assert by[2]["usable"] is False


# ---------------------------------------------------------------------------
# 文件占据其他条目的祖先路径
# ---------------------------------------------------------------------------


def test_file_ancestor_conflict_after_relocation():
    # 搬迁前无层级冲突（a 是目录）；规则把文件 #3 搬到 out，恰为 #1/#2 祖先。
    body = _preview(
        entries=[
            {"path": "a/readme.txt", "type": "file"},
            {"path": "a/sub", "type": "directory"},
            {"path": "src/blocker", "type": "file"},
        ],
        rules=[
            {"source_prefix": "a", "target_prefix": "out"},
            {"source_prefix": "src/blocker", "target_prefix": "out"},
        ],
    )
    by = _by_index(body)
    assert by[3]["new_path"] == "out"
    # 文件祖先 #3 双向记录。
    assert by[3]["usable"] is False
    assert by[3]["conflicts"] == [
        {"type": "BLOCKS_DESCENDANT", "related_index": 1},
        {"type": "BLOCKS_DESCENDANT", "related_index": 2},
    ]
    assert by[1]["usable"] is False
    assert by[1]["conflicts"] == [
        {"type": "BLOCKED_BY_FILE_ANCESTOR", "related_index": 3}
    ]
    assert by[2]["conflicts"] == [
        {"type": "BLOCKED_BY_FILE_ANCESTOR", "related_index": 3}
    ]
    assert body["summary"]["conflicts"] == 3
    assert body["summary"]["usable"] == 0


def test_directory_ancestor_is_not_conflict():
    # 目录占据祖先路径是正常布局。
    body = _preview(
        entries=[
            {"path": "x", "type": "directory"},
            {"path": "x/y.txt", "type": "file"},
        ],
        rules=[],
    )
    by = _by_index(body)
    assert by[1]["usable"] is True
    assert by[2]["usable"] is True


def test_multiple_file_ancestors_listed_nearest_first():
    body = _preview(
        entries=[
            {"path": "deep/leaf.txt", "type": "file"},
            {"path": "f1", "type": "file"},
            {"path": "f1/deep", "type": "file"},
        ],
        rules=[
            {"source_prefix": "deep/leaf.txt", "target_prefix": "f1/deep/leaf.txt"},
            {"source_prefix": "f1/deep", "target_prefix": "f1/deep"},
        ],
    )
    by = _by_index(body)
    # 后代 #1 被 #3（近）与 #2（远）两个文件祖先阻挡，按相关原下标排序。
    assert by[1]["conflicts"] == [
        {"type": "BLOCKED_BY_FILE_ANCESTOR", "related_index": 2},
        {"type": "BLOCKED_BY_FILE_ANCESTOR", "related_index": 3},
    ]


def test_entry_can_have_both_conflict_types():
    # #3 既是同键重复成员（与 #1），又是 #4 的文件祖先。
    body = _preview(
        entries=[
            {"path": "dup/a.txt", "type": "file"},  # -> x/a.txt
            {"path": "keep/z.txt", "type": "file"},  # 未命中，无关
            {"path": "old/a.txt", "type": "file"},  # -> x/a.txt（与 #1 重复）
            {"path": "child/down", "type": "directory"},  # -> x/a.txt/down
        ],
        rules=[
            {"source_prefix": "dup", "target_prefix": "x"},
            {"source_prefix": "old", "target_prefix": "x"},
            {"source_prefix": "child", "target_prefix": "x/a.txt"},
        ],
    )
    by = _by_index(body)
    types_for_3 = {(c["type"], c["related_index"]) for c in by[3]["conflicts"]}
    assert types_for_3 == {
        ("DUPLICATE_KEY", 1),
        ("BLOCKS_DESCENDANT", 4),
    }
    assert by[3]["usable"] is False
    types_for_1 = {(c["type"], c["related_index"]) for c in by[1]["conflicts"]}
    assert types_for_1 == {
        ("DUPLICATE_KEY", 3),
        ("BLOCKS_DESCENDANT", 4),
    }
    # 后代 #4 的祖先路径上有两个同键文件（#1、#3），都要列出。
    assert {c["related_index"] for c in by[4]["conflicts"]} == {1, 3}
    assert by[4]["usable"] is False
    assert by[2]["usable"] is True


# ---------------------------------------------------------------------------
# 输出顺序与结构
# ---------------------------------------------------------------------------


def test_response_order_and_fields():
    body = _preview(
        entries=[
            {"path": "a", "type": "directory"},
            {"path": "b.txt", "type": "file"},
        ],
        rules=[{"source_prefix": "a", "target_prefix": "moved/a"}],
    )
    assert body["target"] == "windows"
    assert [e["index"] for e in body["entries"]] == [1, 2]
    first = body["entries"][0]
    assert set(first.keys()) == {
        "index",
        "type",
        "original_path",
        "new_path",
        "matched_rule_index",
        "usable",
        "conflicts",
    }
    assert first["original_path"] == "a"
    assert first["type"] == "directory"


# ---------------------------------------------------------------------------
# 请求校验（422）
# ---------------------------------------------------------------------------


def test_unsafe_entry_path_rejected_422():
    for bad in ("../escape", "/abs/path", "CON", "C:/win/x", ""):
        r = _post(
            {
                "entries": [{"path": bad, "type": "file"}],
                "rules": [{"source_prefix": "a", "target_prefix": "b"}],
            }
        )
        assert r.status_code == 422, bad


def test_unsafe_rule_prefix_rejected_422():
    for field_name in ("source_prefix", "target_prefix"):
        for bad in ("../escape", "//server/share", "/abs", "NUL", ""):
            rule = {"source_prefix": "a", "target_prefix": "b"}
            rule[field_name] = bad
            r = _post(
                {
                    "entries": [{"path": "a/x.txt", "type": "file"}],
                    "rules": [rule],
                }
            )
            assert r.status_code == 422, (field_name, bad)


def test_entries_required_nonempty_and_type_validation():
    assert _post({"entries": [], "rules": []}).status_code == 422
    assert _post({"rules": []}).status_code == 422
    # 仅接受 file / directory，symlink 不属于本接口。
    assert _post(
        {
            "entries": [{"path": "lnk", "type": "symlink"}],
            "rules": [],
        }
    ).status_code == 422


def test_rules_field_optional_but_entries_required():
    r = _post({"entries": [{"path": "a.txt", "type": "file"}]})
    assert r.status_code == 200
    assert r.json()["entries"][0]["new_path"] == "a.txt"
