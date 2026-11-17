# LoopLake 回收物料数据台

这是面向社区回收运营分析员的本地数据湖与数仓服务。它将智能回收箱投递、地磅入出站、转运和兑付事件保存为不可改写的原始记录，并生成带有效期维度、物料对账、付款校验和冻结发布的街道月度视图。所有示例均为合成数据。

## 使用

运行环境为 Python 3.11+；依赖版本见 `requirements.txt`。测试：`python -m unittest discover -s tests -v`；语法检查：`python -m compileall -q looplake`；完整本地演示：`python -m looplake.demo`。HTTP 服务可运行 `uvicorn looplake.api:app`，并在 `/docs` 查阅接口；CLI 为 `python -m looplake.cli --help`。设置 `LOOPLAKE_DB` 可改变 API SQLite 路径。

先登记来源和带有效期的街道、社区、设备、物料、站点与人员维度，再按来源导入事件；同一来源和外部 ID 的不同正文会拒绝，完全重复事件幂等返回。维度未就绪的输入进入隔离状态，登记维度后可按 ingestion ID 重试。批量导入支持清单指纹和检查点，防止使用不同清单误续跑。

同一物料批次的投递与地磅入站重量需在容差内相符；不同物料、出站超入站、付款与费率不符均产生可解释的差异。街道月度报表仅纳入已解决批次，不会将投递、地磅与转运三条记录相加。发布前读取预览指纹；发布时再次校验数据与不可变名称。冻结版保留聚合、对账和事实血缘，日后的新批次不会修改旧版。

## 地磅校准证书

称重事实同时保存原始读数 `raw_weight_kg` 与用于核算的入账净重 `weight_kg`（`booked_weight_kg` 保留入账值）。设备校准证书用 `POST /calibrations` 登记，覆盖闭区间 `[valid_from, valid_to]`，系数 `factor` 将原始读数换算为净重；设备纳入校准管理后，落在证书区间外的读数仍可入账，但批次会带 `calibration_unverified` 差异。

- 首日/末日读数不算越界，仅在 `calibration_boundary_fact_ids` 中标记，不阻断批次。
- 证书区间重叠时不拒绝登记：核算按 `certificate_id` 排序取第一张作为临时依据，批次带 `calibration_overlap`，接口返回全部候选与 `ambiguous` 标记。
- `POST /devices/{id}/calibrations/{cert}/revoke` 只改证书状态；批次随即带 `calibration_revoked`，发布被阻断。
- 补发新证书后，旧净重的批次带 `calibration_basis_changed`，必须重算后才能发布；`calibration_notice_after_month_close` 列出通知晚于月结的证书。
- `GET /devices/{id}/calibration-impact?start=&end=` 给出只读重估：逐事实的原始读数、入账净重、拟议净重与差额，并重新判断批次守恒、站点库存与月结资格；不写回事实，不修改历史发布版。
- `POST /facts/{id}/corrections` 追加更正（可按 `calibration_id` 由原始读数×系数推导，或手工给 `new_weight_kg`）。更正只追加、不覆盖：`raw_weight_kg` 与 `booked_weight_kg` 保持原值，当前视图取最新更正；确认后发布新名称的新版本，旧发布版指纹不变。

`python -m looplake.calibration_demo` 可复现失效证书、撤销、补发、临界日、重叠和晚通知的完整验收过程。

## 输入约定

时间带时区，重量用 kg，金额用元；业务日期由事件时间换算为 UTC 后记录。维度的 valid_to 为开区间，校准证书的 valid_to 为闭区间。来源 channel 与事件 channel 必须一致。演示代码 `looplake/demo.py` 展示完整输入和可观察输出。HTTP 错误返回 `code` 和 `detail`；422 表示输入形状不合法，409 表示版本或业务冲突。
