"""展示层：把 db 取出来的原始行整理成"界面能直接画"的结构。

边界（很重要）
-------------
1. **这里不做裁决。** 能不能退、能退几件，永远由 `rules.decide` 决定，
   界面只是把参数拼成 CLI 再调 `src/main.py`。本模块算出的"可退"是**展示值**，
   公式与 `rules.compute_retirable` 逐字一致，只为在表格里提前给个数，
   用户真点"提交申请"时还会被规则重新算一遍。
   `can_apply` 只回答"按钮该不该亮"，不保证"提交一定成功"：缺客户号的行按钮
   亮着（`needs_manual=True`），点下去由 R-08 转人工，这是演示里必须能走通的一条。
2. 纯函数：不读文件、不连库、不打印、不发请求 —— 便于单独跑、便于复用。
3. 状态文案与 CLI `query` 的分支顺序一致：非商品 → 坏账 → 取消 → 无可退数量
   → 已有未结案申请 → 历史已退满 → 缺客户号 → 可申请。顺序不同就会
   "界面说可申请、命令行说不可退"，那是最难查的一类不一致。
"""

from __future__ import annotations

from typing import Any

import rules

# 展示色调 → 前端 CSS 类名（.badge.ok / .badge.warn / ...）
_TONE_OK = "ok"
_TONE_INFO = "info"
_TONE_WARN = "warn"
_TONE_DANGER = "danger"
_TONE_MUTED = "muted"


def _field(row: Any, key: str) -> Any:
    """同一套取值能用在 dict / sqlite3.Row / NamedTuple 上。"""
    return row[key] if isinstance(row, dict) else getattr(row, key)


def status_meta(status: str) -> dict[str, str]:
    """状态 → 中文标签 + 色调。标签取自 rules.STATUS_LABELS（唯一来源）。"""
    tones = {
        "APPLIED": _TONE_INFO,
        "ACCEPTED": _TONE_INFO,
        "RECEIVED": _TONE_WARN,
        "CLOSED": _TONE_OK,
        "REJECTED": _TONE_DANGER,
        "PENDING_MANUAL": _TONE_WARN,
    }
    return {
        "status": status,
        "label": rules.STATUS_LABELS.get(status, status),
        "tone": tones.get(status, _TONE_MUTED),
    }


def line_state(line: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """一条明细的展示状态：可退几件、现在能不能申请、为什么不能。

    `line` 支持 db.LineRow / dict / sqlite3.Row 之类的属性或键访问。
    """
    returnable_qty = int(_field(line, "returnable_qty"))
    history_returned = int(_field(line, "history_returned"))
    cust_id = _field(line, "cust_id")
    is_cancel = int(_field(line, "is_cancel"))
    is_bad_debt = int(_field(line, "is_bad_debt"))
    is_non_product = int(_field(line, "is_non_product"))

    state = state or {}
    active_qty = int(state.get("active_qty") or 0)
    open_req = state.get("open") or None

    retirable = rules.compute_retirable(returnable_qty, history_returned, active_qty)

    if is_non_product:
        code, text, tone = "NON_PRODUCT", "不可退（非商品行）", _TONE_MUTED
        reason = "运费 / 银行费用 / 手工调整这类行，不是客户买的商品"
    elif is_bad_debt:
        code, text, tone = "BAD_DEBT", "不可退（坏账调整）", _TONE_DANGER
        reason = "A 开头的坏账调整行，不属于客户订单"
    elif is_cancel:
        code, text, tone = "CANCEL", "取消行", _TONE_MUTED
        reason = "这行本身就是取消行，不能再退"
    elif returnable_qty <= 0:
        code, text, tone = "NO_QTY", "不可退（无可退数量）", _TONE_MUTED
        reason = "原始可退数量为 0 或负数量异常行"
    elif open_req is not None:
        meta = status_meta(str(open_req["status"]))
        code = "ALREADY_OPEN"
        text = f"{meta['label']}（{open_req['req_no']} {open_req['qty']} 件）"
        tone = _TONE_INFO
        reason = "同一明细同时只允许一条生效申请（R-06），要先走完或拒绝这条"
    elif retirable == 0 and history_returned > 0:
        code, text, tone = "EXHAUSTED", "历史已退满", _TONE_WARN
        reason = "历史上已经退满过，没有剩余额度"
    elif cust_id is None:
        code, text, tone = "MANUAL", "可申请（缺客户号→人工）", _TONE_WARN
        reason = "缺 Customer ID 无法自动判定归属，提交后会转人工核验（R-08）"
    else:
        code, text, tone = "APPLICABLE", "可申请", _TONE_OK
        reason = ""

    return {
        "retirable": retirable,
        "active_qty": active_qty,
        "returnable_qty": returnable_qty,
        "history_returned": history_returned,
        "open_request": open_req,
        "code": code,
        "text": text,
        "tone": tone,
        "reason": reason,
        # R-08：缺客户号的行**也能提交**，提交后由 rules.check_anomaly 转成
        # PENDING_MANUAL（不占额度）。所以这里不能把按钮禁掉——否则界面一边写着
        # "可申请（缺客户号→人工）"，一边点不动，最该演示的那条规则反而走不通。
        "can_apply": code in ("APPLICABLE", "MANUAL"),
        "needs_manual": code == "MANUAL",
    }


def line_view(line: Any, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """一行明细 → 界面行数据（字段名与 order_line 一致，另加展示状态）。"""
    info = line_state(line, state)
    return {
        "line_key": _field(line, "line_key"),
        "invoice": _field(line, "invoice"),
        "seq": int(_field(line, "seq")),
        "stock_code": _field(line, "stock_code"),
        "description": _field(line, "description") or "",
        "quantity": int(_field(line, "quantity")),
        "invoice_date": _field(line, "invoice_date"),
        "price": float(_field(line, "price")),
        "cust_id": _field(line, "cust_id"),
        "country": _field(line, "country") or "",
        "sheet": _field(line, "sheet"),
        **info,
    }


def invoice_view(lines: list[Any], states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """一张发票 → 表头 + 行 + 合计。lines 为空时返回 found=False。"""
    rows = [
        line_view(line, states.get(str(_field(line, "line_key"))))
        for line in lines
    ]
    if not rows:
        return {"found": False, "header": None, "lines": [], "totals": None}

    first = rows[0]
    return {
        "found": True,
        "header": {
            "invoice": first["invoice"],
            "cust_id": first["cust_id"],
            "country": first["country"],
            "invoice_date": first["invoice_date"],
            "sheet": first["sheet"],
            "lines": len(rows),
        },
        "lines": rows,
        "totals": {
            "retirable": sum(row["retirable"] for row in rows),
            "applicable": sum(1 for row in rows if row["can_apply"]),
            "blocked": sum(1 for row in rows if not row["can_apply"]),
            "lines": len(rows),
        },
    }


def demo_plan(line: Any) -> dict[str, Any]:
    """演示剧本的目标值与预期：申请件数取 min(3, 可退)，和 tools/demo.py 同口径。"""
    returnable = int(_field(line, "returnable_qty"))
    history = int(_field(line, "history_returned"))
    retirable = max(0, returnable - history)
    qty = min(3, max(1, retirable))
    return {
        "invoice": _field(line, "invoice"),
        "seq": int(_field(line, "seq")),
        "line_key": _field(line, "line_key"),
        "stock_code": _field(line, "stock_code"),
        "description": _field(line, "description") or "",
        "returnable_qty": returnable,
        "history_returned": history,
        "retirable": retirable,
        "qty": qty,
        "after_apply": retirable - qty,
    }


def transition_targets(status: str) -> list[dict[str, Any]]:
    """四个动作目标 + 是否合法。界面据此把"走对了"和"故意走错"都摆出来。

    这里只做**预告**：点了之后照样交给 CLI 的 check_transition 裁决，
    预览说"会被拒"而实际放行也不可能发生（同一张 TRANSITIONS 表）。
    """
    labels = {
        "ACCEPTED": ("accept", "受理"),
        "RECEIVED": ("receive", "收货"),
        "CLOSED": ("close", "结案"),
        "REJECTED": ("reject", "拒绝"),
    }
    allowed = rules.next_statuses(status)
    return [
        {
            "to": to_status,
            "command": command,
            "verb": verb,
            "label": rules.STATUS_LABELS.get(to_status, to_status),
            "legal": to_status in allowed,
        }
        for to_status, (command, verb) in labels.items()
    ]
