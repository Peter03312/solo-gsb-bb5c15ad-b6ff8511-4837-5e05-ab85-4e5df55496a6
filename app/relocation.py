"""路径搬迁预演：按有序源前缀/目标前缀规则改写条目路径，并在结果路径上复检冲突（纯内存，不访问文件系统）。

语义概述：

* 条目与规则前缀均复用 :mod:`app.normalize` 的 Windows 路径规范化；不安全
  路径/前缀由请求模型校验阶段拒绝（API 层映射为 422），故本模块的输入恒为
  安全条目与安全规则。
* 规则按**规范化路径段的比较键**匹配：规则源前缀的各段（``casefold`` 折叠）
  与条目规范路径的前若干段**逐段相等**即命中（段边界对齐，``a/b`` 不匹配
  ``a/bb``）。**最长源前缀优先，同长度取输入顺序中最后一条**；没有任何规则
  命中时保留原规范路径（``matched_rule_index`` 为 ``null``）。
* 命中后新路径 = 目标前缀规范段 + 条目未匹配的后缀段；未命中时新路径即
  原规范路径。
* 在全部新路径上复检两类冲突：

  - **同键重复**（``DUPLICATE_KEY``）：两条目新路径的 Windows 比较键相同
    （file/directory 同键也算），双向各记一条；
  - **文件占据祖先路径**：某**文件**的新路径是另一条目新路径的严格祖先时，
    后代记 ``BLOCKED_BY_FILE_ANCESTOR``，文件记 ``BLOCKS_DESCENDANT``。

  存在任一冲突的条目 ``usable`` 为 ``False``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .normalize import NormalizedEntry

FILE_TYPE = "file"


class RelocationConflictType(str, Enum):
    DUPLICATE_KEY = "DUPLICATE_KEY"  # 结果路径同键重复
    BLOCKED_BY_FILE_ANCESTOR = "BLOCKED_BY_FILE_ANCESTOR"  # 后代：祖先路径被文件占据
    BLOCKS_DESCENDANT = "BLOCKS_DESCENDANT"  # 文件：自身占据其他条目的祖先路径


@dataclass(frozen=True)
class RelocationRule:
    """一条搬迁规则（源前缀、目标前缀均已规范化为安全的路径段）。"""

    index: int  # 请求 rules 中的下标（从 0 开始）
    source_parts: tuple[str, ...]
    target_parts: tuple[str, ...]

    @property
    def source_key_parts(self) -> tuple[str, ...]:
        return tuple(part.casefold() for part in self.source_parts)


@dataclass
class PlannedRelocationEntry:
    """单条条目的搬迁预演结果。"""

    index: int  # 条目下标（从 1 开始，与输入一致）
    entry_type: str  # "file" | "directory"
    original_path: str
    new_parts: list[str]
    matched_rule_index: int | None
    conflicts: list[tuple[RelocationConflictType, int]] = field(default_factory=list)

    @property
    def new_path(self) -> str:
        return "/".join(self.new_parts)

    @property
    def new_key_parts(self) -> list[str]:
        return [part.casefold() for part in self.new_parts]

    @property
    def usable(self) -> bool:
        return not self.conflicts


def _match_rule(
    key_parts: list[str], rules: list[RelocationRule]
) -> RelocationRule | None:
    """按比较键段匹配规则：最长源前缀优先，同长度取最后一条；未命中返回 None。"""
    best: RelocationRule | None = None
    best_len = -1
    entry_key = tuple(key_parts)
    for rule in rules:
        n = len(rule.source_parts)
        if n > len(entry_key):
            continue
        if entry_key[:n] == rule.source_key_parts and n >= best_len:
            # >= 使同长度（即同源前缀）的后一条规则覆盖先一条。
            best = rule
            best_len = n
    return best


def plan_relocation(
    entries: list[NormalizedEntry], rules: list[RelocationRule]
) -> list[PlannedRelocationEntry]:
    """对全部条目做搬迁改写并在结果路径上复检冲突（纯内存）。

    调用方保证 ``entries`` 与规则前缀均已通过 Windows 规范化且安全。
    返回列表严格保持 ``entries`` 的输入顺序。
    """
    planned: list[PlannedRelocationEntry] = []
    for entry in entries:
        rule = _match_rule(entry.key_parts, rules)
        if rule is None:
            # 未命中任何规则：保留原规范路径。
            new_parts = list(entry.canonical_parts)
            matched_rule_index: int | None = None
        else:
            # 目标前缀 + 未匹配后缀（后缀沿用条目自身的规范段，保留原大小写）。
            prefix_len = len(rule.source_parts)
            new_parts = [*rule.target_parts, *entry.canonical_parts[prefix_len:]]
            matched_rule_index = rule.index
        planned.append(
            PlannedRelocationEntry(
                index=entry.index,
                entry_type=entry.entry_type,
                original_path=entry.original_path,
                new_parts=new_parts,
                matched_rule_index=matched_rule_index,
            )
        )

    # 1) 结果路径同键重复：每个重复组内两两成对、双向记录。
    buckets: dict[str, list[PlannedRelocationEntry]] = {}
    for item in planned:
        buckets.setdefault("/".join(item.new_key_parts), []).append(item)
    for members in buckets.values():
        if len(members) < 2:
            continue
        for member in members:
            for other in members:
                if other.index != member.index:
                    member.conflicts.append(
                        (RelocationConflictType.DUPLICATE_KEY, other.index)
                    )

    # 2) 文件占据其他条目的祖先路径（严格祖先，不含自身所在深度）。
    files_by_key: dict[str, list[PlannedRelocationEntry]] = {}
    for item in planned:
        if item.entry_type == FILE_TYPE:
            files_by_key.setdefault("/".join(item.new_key_parts), []).append(item)
    for descendant in planned:
        parts = descendant.new_key_parts
        for depth in range(1, len(parts)):
            for ancestor in files_by_key.get("/".join(parts[:depth]), ()):
                descendant.conflicts.append(
                    (
                        RelocationConflictType.BLOCKED_BY_FILE_ANCESTOR,
                        ancestor.index,
                    )
                )
                ancestor.conflicts.append(
                    (RelocationConflictType.BLOCKS_DESCENDANT, descendant.index)
                )

    # 去重并稳定排序：先按相关原下标，再按冲突类型。
    for item in planned:
        item.conflicts = sorted(
            set(item.conflicts), key=lambda conflict: (conflict[1], conflict[0].value)
        )
    return planned
