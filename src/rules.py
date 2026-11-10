"""业务规则 R-03~R-08 的纯函数实现。

设计纪律
--------
1. 不 import sqlite3、不读文件、不打印 —— 全部可单测、秒级。
2. 每个校验失败必须返回明确错误码 + 中文提示，禁止静默 return False。
3. 不重算上游已经算好的权威值（is_cancel / is_non_product / line_key），
   只做交叉复核（recheck_flags），不一致由 verify 统计为 flag_mismatch 并报警。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 与 src/prepare.py 的非商品判定规则保持一致（实测与 is_non_product 列零冲突）
PRODUCT_CODE_RE = re.compile(r"^[0-9]{5}[A-Za-z]{0,2}$")


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    code: str
    message: str
    data: dict[str, object] = field(default_factory=dict)


def accept(code: str = "OK", message: str = "通过", **data: object) -> CheckResult:
    return CheckResult(ok=True, code=code, message=message, data=dict(data))


def reject(code: str, message: str, **data: object) -> CheckResult:
    return CheckResult(ok=False, code=code, message=message, data=dict(data))


# ---------------------------------------------------------------- 归一化


def normalize_invoice(value: object) -> str:
    """发票号全程当字符串：一旦 astype(int)，'C536379' 这种取消单就丢了。"""
    return str(value if value is not None else "").strip().upper()


def normalize_stock_code(value: object) -> str:
    return str(value if value is not None else "").strip().upper()


def normalize_customer_id(value: object) -> str | None:
    """13085.0 / '13085' / ' 13085 ' 统一成 '13085'；空值返回 None（绝不进唯一键）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def normalize_ts(value: str) -> str:
    """统一成 'YYYY-MM-DDTHH:MM:SS'，让字符串比较等价于时间比较。

    CSV 里是 '2009-12-01 07:45:00'（空格分隔），--now 是 ISO 格式（T 分隔）。
    不做这个归一，同一天内的比较会因空格(32) < 'T'(84) 而判错。
    """
    return (value or "").strip().replace(" ", "T")


# ---------------------------------------------------------------- 标记复核 R-03 / R-05


def recheck_flags(*, invoice: str, stock_code: str) -> tuple[int, int, int]:
    """按规则复算 (is_cancel, is_bad_debt, is_non_product)，用于与上游标记位比对。"""
    inv = normalize_invoice(invoice)
    code = normalize_stock_code(stock_code)
    is_cancel = 1 if inv.startswith("C") else 0
    is_bad_debt = 1 if inv.startswith("A") else 0
    is_non_product = 0 if PRODUCT_CODE_RE.match(code) else 1
    return is_cancel, is_bad_debt, is_non_product


# ---------------------------------------------------------------- 可退计算 R-04


def compute_retirable(returnable_qty: int, history_returned: int, active_qty: int) -> int:
    """可退 = 原始可退 − 历史已退 − 系统内生效申请占用；不足按 0 对外。"""
    raw = returnable_qty - history_returned - active_qty
    return raw if raw > 0 else 0


# ---------------------------------------------------------------- 单项校验


def check_found(found: bool, line_key: str) -> CheckResult:
    if found:
        return accept()
    return reject(
        "E_NOT_FOUND",
        f"未找到明细 {line_key}，请核对发票号与发票内序号",
        line_key=line_key,
    )


def check_retirable_line(
    *, returnable_qty: int, is_cancel: int, is_bad_debt: int, is_non_product: int
) -> CheckResult:
    if is_bad_debt:
        return reject("E_NON_RETURNABLE", "该行是坏账调整（A 开头），不属于客户订单，不可退",
                      reason="bad_debt")
    if is_non_product:
        return reject("E_NON_RETURNABLE", "该行是非商品行（运费/银行费用/手工调整），不可退",
                      reason="non_product")
    if is_cancel:
        return reject("E_NON_RETURNABLE", "该行本身是取消行，不可再退", reason="cancel")
    if returnable_qty <= 0:
        return reject(
            "E_NON_RETURNABLE",
            f"该行可退数量为 {returnable_qty} 件（负数量异常行或无可退数量），不可退",
            reason="no_qty",
            returnable_qty=returnable_qty,
        )
    return accept()


def check_duplicate(active_request_no: str | None) -> CheckResult:
    if active_request_no is None:
        return accept()
    return reject(
        "E_DUPLICATE",
        f"该明细已有生效申请 {active_request_no}，本次未新增记录",
        active_request_no=active_request_no,
    )


def check_qty(qty: int, retirable: int) -> CheckResult:
    if qty <= 0:
        return reject("E_INVALID_QTY", f"申请数量必须为正整数，收到 {qty}", qty=qty)
    if qty > retirable:
        return reject(
            "E_EXCEED",
            f"退货数量超过可退：可退 {retirable}、申请 {qty}",
            qty=qty,
            retirable=retirable,
        )
    return accept()


def check_not_in_future(invoice_date_iso: str, now_iso: str) -> CheckResult:
    if normalize_ts(invoice_date_iso) > normalize_ts(now_iso):
        return reject(
            "E_ANOMALY",
            f"申请时间 {normalize_ts(now_iso)} 早于开票时间 {normalize_ts(invoice_date_iso)}，已转人工核验",
            invoice_date=invoice_date_iso,
            now=now_iso,
        )
    return accept()


def check_anomaly(
    *,
    cust_id: str | None,
    found: bool,
    invoice_date_iso: str,
    now_iso: str,
) -> CheckResult:
    """R-08：三类无法判定的情况转异常队列，不报错退出。"""
    if not found:
        return reject("E_NOT_FOUND", "未找到明细，无法进入标准流程", reason="not_found")
    if cust_id is None:
        return reject(
            "E_MANUAL",
            "该明细缺少 Customer ID，无法自动判定归属，已转异常队列（PENDING_MANUAL）",
            reason="missing_cust",
        )
    future = check_not_in_future(invoice_date_iso, now_iso)
    if not future.ok:
        return reject(
            "E_MANUAL",
            future.message + "，已转异常队列（PENDING_MANUAL）",
            reason="future_date",
        )
    return accept()


# ---------------------------------------------------------------- 状态机 R-09


# 合法流转表：只允许这 5 条边，其余一律拒绝（含"原地不动"和"倒着走"）。
# 为什么写死成表而不是 if-else：老师问"APPLIED 能不能直接 CLOSED"时，
# 答案就在这张表里，不靠读代码脑补。
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "APPLIED": ("ACCEPTED", "REJECTED"),
    "PENDING_MANUAL": ("ACCEPTED", "REJECTED"),
    "ACCEPTED": ("RECEIVED",),
    "RECEIVED": ("CLOSED",),
    "CLOSED": (),
    "REJECTED": (),
}

STATUS_LABELS: dict[str, str] = {
    "APPLIED": "已提交",
    "ACCEPTED": "已受理",
    "RECEIVED": "已收货",
    "CLOSED": "已结案",
    "REJECTED": "已拒绝",
    "PENDING_MANUAL": "待人工核验",
}


def next_statuses(status: str) -> tuple[str, ...]:
    return TRANSITIONS.get(status, ())


def check_transition(from_status: str, to_status: str) -> CheckResult:
    """校验一条流转是否合法；非法时给出"现在能去哪"的提示，而不是只说不行。"""
    if from_status not in TRANSITIONS:
        return reject(
            "E_BAD_STATUS",
            f"未知状态「{from_status}」，无法流转",
            from_status=from_status,
            to_status=to_status,
        )
    allowed = TRANSITIONS[from_status]
    if to_status in allowed:
        return accept()
    if not allowed:
        return reject(
            "E_BAD_TRANSITION",
            f"申请已处于终态「{from_status}（{STATUS_LABELS.get(from_status, '')}）」，"
            f"不能再改成 {to_status}",
            from_status=from_status,
            to_status=to_status,
            allowed=allowed,
        )
    return reject(
        "E_BAD_TRANSITION",
        f"不允许从 {from_status}（{STATUS_LABELS.get(from_status, '')}）"
        f"直接跳到 {to_status}（{STATUS_LABELS.get(to_status, '')}）；"
        f"下一步只能是 {'/'.join(allowed)}",
        from_status=from_status,
        to_status=to_status,
        allowed=allowed,
    )


def decide(
    *,
    line_key: str,
    found: bool,
    returnable_qty: int,
    is_cancel: int,
    is_bad_debt: int,
    is_non_product: int,
    cust_id: str | None,
    invoice_date_iso: str,
    history_returned: int,
    active_qty: int,
    active_request_no: str | None,
    now_iso: str,
    qty_requested: int,
) -> CheckResult:
    """统一裁决。顺序固定，越底层的错误越先报，避免误导性提示。

    NOT_FOUND → NON_RETURNABLE → DUPLICATE → INVALID_QTY → EXCEED → ANOMALY → OK
    """
    steps = (
        lambda: check_found(found, line_key),
        lambda: check_retirable_line(
            returnable_qty=returnable_qty,
            is_cancel=is_cancel,
            is_bad_debt=is_bad_debt,
            is_non_product=is_non_product,
        ),
        lambda: check_duplicate(active_request_no),
    )
    for step in steps:
        result = step()
        if not result.ok:
            return result

    retirable = compute_retirable(returnable_qty, history_returned, active_qty)
    qty_result = check_qty(qty_requested, retirable)
    if not qty_result.ok:
        detail = dict(qty_result.data)
        detail.update(
            retirable=retirable,
            returnable_qty=returnable_qty,
            history_returned=history_returned,
            active_qty=active_qty,
        )
        return CheckResult(
            ok=False,
            code=qty_result.code,
            message=qty_result.message,
            data=detail,
        )

    anomaly = check_anomaly(
        cust_id=cust_id,
        found=found,
        invoice_date_iso=invoice_date_iso,
        now_iso=now_iso,
    )
    if not anomaly.ok:
        return anomaly

    return accept(
        message=(
            f"已受理，该明细可退 {retirable} → {retirable - qty_requested}"
            f"（原始 {returnable_qty}、历史已退 {history_returned}、已占用 {active_qty}）"
        ),
        retirable=retirable,
        remaining=retirable - qty_requested,
        returnable_qty=returnable_qty,
        history_returned=history_returned,
        active_qty=active_qty,
    )
