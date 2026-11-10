"""R-04 历史占用：把真实数据里的取消行分摊到具体销售行（FIFO）。

为什么需要这一步
--------------
order_line.csv 只给了 returnable_qty（该行原始可退数量），没有"这一行已经被退过多少"。
而老师在课堂上一定会问："这条明细为什么只能退 3 件？"
答案就是：原始 8 件 − 历史已退 5 件（来自真实 C 行）= 3。

口径（写进规则与决定.md 的 R-04）
------------------------------
- 分组键：(cust_id, stock_code)。缺 cust_id 的行不参与匹配（无法归属到人，见 R-08）。
- 池内顺序：按 invoice_date、invoice、seq 升序，取消行只扣"时间在它之前"的销售行。
- 池内数量：只有 returnable_qty > 0 的行入池（取消/坏账/非商品/负数量行本身不可退）。
- 取消行数量取 -quantity（quantity < 0）。is_cancel=1 但 quantity >= 0 的行
  （实测 1 行，是退运费/调整）不当作退货数量，单独计数 cancel_pos_skipped。
- 取消量超过池内余量：多出来的部分计入 over_returned（这就是"超量退货"的真实样本来源）。
- 池为空：计入 unmatched_cancel（即"找不到原单"）。

纯函数：本模块不 import sqlite3，也不读文件。排序由 db.iter_for_allocation 负责。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

from csv_loader import LineRaw


@dataclass
class AllocStats:
    groups: int = 0
    assigned: int = 0              # 成功分摊到销售行的取消件数
    unmatched_cancel: int = 0      # 取消行找不到可扣的销售行
    over_returned: int = 0         # 取消量超过池内余量（超量退货）
    cancel_pos_skipped: int = 0    # is_cancel=1 但 quantity >= 0
    guest_skipped: int = 0         # cust_id 为空的取消行，无法归属
    over_samples: tuple[str, ...] = field(default_factory=tuple)


def needs_guest_skip(row: LineRaw) -> bool:
    """缺客户号的行不参与按客户的匹配（R-08）。"""
    return row.cust_id is None


def allocate(window: Iterable[LineRaw]) -> tuple[list[tuple[str, int]], AllocStats]:
    """按 (cust_id, stock_code) 分组做 FIFO 分摊。

    window 必须已按 (cust_id, stock_code, invoice_date, invoice, seq) 排序。
    返回 updates = [(line_key, history_returned), ...]，只含 history_returned > 0 的行。
    """
    updates: list[tuple[str, int]] = []
    stats = AllocStats()
    samples: list[str] = []

    pool: deque[list[object]] = deque()   # [remaining:int, line_key:str]
    group: tuple[str | None, str] | None = None
    group_updates: dict[str, int] = {}

    def close_group() -> None:
        nonlocal group_updates
        for line_key, qty in group_updates.items():
            if qty > 0:
                updates.append((line_key, qty))
        group_updates = {}

    for row in window:
        key = (row.cust_id, row.stock_code)
        if key != group:
            close_group()
            pool.clear()
            group = key
            stats.groups += 1

        if row.is_cancel:
            if row.quantity >= 0:
                stats.cancel_pos_skipped += 1
                continue
            if row.cust_id is None:
                stats.guest_skipped += 1
                continue

            need = -row.quantity
            if not pool:
                stats.unmatched_cancel += 1
                continue

            original_need = need
            while need > 0 and pool:
                remaining, line_key = pool[0]
                take = min(remaining, need)
                remaining -= take
                need -= take
                pool[0][0] = remaining
                group_updates[line_key] = group_updates.get(line_key, 0) + take
                if remaining == 0:
                    pool.popleft()
            stats.assigned += original_need - need
            if need > 0:
                stats.over_returned += 1
                if len(samples) < 5:
                    samples.append(f"{row.line_key}(缺 {need} 件)")
            continue

        if row.returnable_qty > 0:
            pool.append([row.returnable_qty, row.line_key])

    close_group()
    return updates, AllocStats(
        groups=stats.groups,
        assigned=stats.assigned,
        unmatched_cancel=stats.unmatched_cancel,
        over_returned=stats.over_returned,
        cancel_pos_skipped=stats.cancel_pos_skipped,
        guest_skipped=stats.guest_skipped,
        over_samples=tuple(samples),
    )
