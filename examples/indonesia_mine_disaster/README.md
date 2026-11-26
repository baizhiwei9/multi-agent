# 印尼矿难：新闻 → 评估 → 平衡表

```
python examples/indonesia_mine_disaster/run_demo.py
```

逐项打印 `PASS/FAIL`，已知缺陷单列 `[NOTE]`；退出码 0 表示链路与判定都符合预期。

## 链路

```
news/collect（关键词命中）
  → supervisor/tune（经理定权重与大事阈值）
  → assess/classify（判 major 与 impact）
  → balance/build（供给冲击进平衡表）
```

## 这次的输入

| 新闻 | 组 | 期望 |
|---|---|---|
| 印尼 Grasberg 铜矿塌方，采区暂停 | `supply_shock` | 采到，判大事、偏多 |
| 印尼镍矿出口配额下调 10% | `supply_cut` | 采到，权重 0.7 |
| 智利 Codelco 罢工结束、恢复生产 | `supply_shock` | 采到；终态词（结束/恢复生产）触发方向翻转，判偏空 |
| 下游需求走弱、铜杆开工下滑 | `demand_shift` | 程序模式不采；模型复核才采 |
| 雅加达多云 | — | 不入库 |

## 平衡表两段

- 不注入 executor：`degraded=True`，原因写明“计算在 BalanceAI 侧”——不自己估数；
- 注入演示 executor：供给 2600 → 2589.6、缺口 -50 → -60.4、库存天数 12 → 11.96，
  输出标 `source=fixture-demo-executor`，假设写明“仅验证链路形状，非实盘结论”。

演示基线在 `fixture/base_balance.json`，是示例数据，不是任何机构预测。

## 换成真实数据

- 新闻：`NewsWatchFramework(fetch=your_fetch)`，或直接 `collect({"items": [...]})`；
- 平衡表：`BalanceSheetProject(executor=lambda cap, action, params, ctx:
  submit("runner", "py", {...}))`，接 BalanceAI 的真实测算；
- 权重：`supervisor/tune` 的 `weights/major_weight/min_level`，换品种只改参数。