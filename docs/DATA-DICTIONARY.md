# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |

## M5 对账与别名台账

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `source` | string | 否 | 记录来源；别名与匹配仅在同一来源内生效 |
| `archive_id` | string | 否 | 档号原值；逐字比较，前导零与全半角不改写 |
| `alias_id` | string | 否 | 认领编号；未提供时由来源与新旧档号派生 |
| `old_id` / `new_id` | string | 否 | 别名两端的档号原值 |
| `basis` | string | 否 | 馆方确认依据；认领时必填 |
| `status` | string | 否 | 别名状态：`pending` 待裁决、`active` 已认领、`revoked` 已撤销、`split` 已拆分 |
| `version` | integer | 否 | 别名乐观版本；决定提交时携带 `expected_version` |
| `decision_key` | string | 是一 | 决定幂等键；同键重试返回首次结果 |
| `diff_before` / `diff_after` | object | 是 | 决定前后的匹配、缺失、孤立计数 |
| `operation_state` | string | 否 | `APPLIED`、`REPLAYED`、`RECOVERED` |

对账守恒：索引侧记录数 = 已匹配 + 缺失；确认侧记录数 = 已匹配 + 孤立；输入合计 = 索引侧 + 隔离 + 空。

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。
