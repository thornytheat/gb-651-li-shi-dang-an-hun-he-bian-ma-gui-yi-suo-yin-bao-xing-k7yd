REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    "DUPLICATE_CLAIM": "同一来源内重复认领", "MANY_TO_ONE": "多对一碰撞待裁决",
    "ONE_TO_MANY": "一对多碰撞待裁决", "ALIAS_NOT_FOUND": "认领不存在",
    "INVALID_ALIAS": "认领缺少必要字段", "BASIS_REQUIRED": "认领缺少确认依据",
    "INVALID_SPLIT": "拆分参数不合法", "ALREADY_SPLIT": "认领已经拆分",
    "DECISION_KEY_REUSED": "决定键重复用于不同内容",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")
