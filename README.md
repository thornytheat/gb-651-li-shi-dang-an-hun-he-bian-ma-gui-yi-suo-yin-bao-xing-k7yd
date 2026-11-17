# 历史档案混合编码归一与索引保形

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 文件采信：`/api/intake/run`
- M2 编码判定：`/api/encoding/detect`
- M3 字段保形：`/api/fields/normalize`
- M4 内容归一：`/api/content/normalize`
- M5 索引对账：`/api/reconcile`
- M6 批次导出：`/api/export/evidence.csv`

        ## 核心规则

        候选解码必须完整且无替换字符；档号、盒号与超大流水号始终按字符串逐字保留。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18210`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## M5 索引对账要点

        - 档号是实体身份：比较按原字逐字进行，不去前导零、不做全半角折叠、不 trim；`000123` 与 `123`、`ＡＢＣ－００１` 与 `ABC-001` 都不会自动相等。
- 同一字符串出现在不同来源时不会自动配对；只有同来源、同原字才算天然匹配。
- 馆方确认的新旧档号以「来源 + 旧档号 → 新档号 + 依据」登记为别名；别名只在所属来源内生效。
- 候选别名一律先进入待裁决，只有认领（`/api/reconcile/claim`）后才参与对账；多对一、一对多碰撞保留待裁决，不会自动合并。
- 认领、拆分、撤销都返回前后差异计数（`diff_before` / `diff_after` / `delta`），并与决定在同一事务中落库。
- 决定带 `decision_key` 幂等：重试返回首次结果；旧版本决定回放返回 `STALE_VERSION` 且不覆盖新裁决；竞争失败写入 `alias_conflicts`，可在页面查看。
- 中断重启时未完成决定标记为 `interrupted`，重试同键续跑而不会重复认领；撤销别名后两侧差异恢复。
- 样例 `sample_data/reconcile_records.json` 含跨来源同号记录与一对多、多对一候选。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
