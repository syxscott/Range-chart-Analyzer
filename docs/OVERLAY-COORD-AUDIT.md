# P1-9 / G2 实测：`pos_0_999` 到底是不是像素？

> 结论先行：**不是像素，是归一化轴位置（0..999）**。`axis_calibration` 只记录轴两端对应的**数据值**，不含任何像素锚点。所以「`pos → 数据值`」是精确线性算术，「`pos → 原图像素`」**当前数据不足**。
> 复现命令：`python scripts/audit/overlay_coord_probe.py`（`--json` 输出完整报告）。

## 为什么这件事必须先测

`docs/OWNER_DECISIONS.md` 的 **G2** 要在「真 overlay（端点映射回原图像素）」与「半真 overlay（在归一化坐标上标注）」之间做选择，成本差 10–24 人日。此前只有架构推断（"`axis_calibration` 只存轴值、不存像素锚点"），没有实测数据。本文件是那次实测的记录。

## 实测方法

`scripts/audit/overlay_coord_probe.py` 扫描 `tests/fixtures/real_payloads/*.json`（仓库内 8 个真实模型回复），对同时含 `axis_calibration` 与 `*_pos_0_999` 字段的载荷，用下式逐字段比对：

```
predicted = at_0 + (pos / 999) * (at_999 - at_0)
residual  = |declared_value - predicted|
```

同时检查 `axis_calibration` 内是否存在任何像素侧键（`pixel` / `px` / `pixel_at_0` / `pixel_at_999` / `anchors` / `data_value` / `tick_pixels` / `image_width` / `image_height`）。

**探针本身做过变异测试**（否则"0 处不匹配"可能只是探针没在工作）：
- 基线：`max_abs_residual ≈ 4.85e-7`，`pixel anchors: NONE`
- 注入 `axis_calibration.vertical.pixel_at_0` → `pos → pixel possible` 翻为 `True`，命中被报告
- 把某个 `declared_value` 加 50 → `max_abs_residual = 50.0`，`pos → data value linear` 翻为 `False`

## 实测结果

| 指标 | 值 |
|---|---|
| 扫描 fixture | 8 |
| 含 `axis_calibration` | 3 |
| 含 `pos_0_999` 字段 | 4 |
| 两者都有 | 3 |
| 参与线性校验的字段（带 declared value） | 60 |
| 最大绝对残差 | **4.95e-07** |
| `axis_calibration` 内的像素锚点 | **0 个**（`NONE`） |

三个带校准的载荷，形状完全一致——只有轴端值：

```json
{"vertical": {"at_0": 260.0, "at_999": 160.0, "unit": "Ma"}}
{"vertical": {"at_0": 83.6,  "at_999": 113.0, "unit": "Ma (approx., ...)"}}
{"x": {"at_0": 20.0, "at_999": 140.0, "unit": "Species number (S), SCS panel"},
 "y": {"at_0": 0.0,  "at_999": 24.0,  "unit": "Latitude (N) °"}}
```

单个字段的样子（注意 `pos` 与 `value` 都是"轴上的量"，没有 `x`/`y` 像素）：

```json
{"pos": 130, "axis": "vertical", "value": 246.986987, "unit": "Ma"}
```

## 这决定了什么

1. **半真 overlay 现在就能做**：在归一化坐标系里按 `pos` 画标记，不需要任何新数据、不需要改 schema。它回答"该分类单元在轴上位于哪一段"，视觉上叠在原图之上是近似对齐，不是精确对齐。
2. **真 overlay 缺一代数据**：`pos → pixel` 需要至少两个像素锚点（把 0 与 999 映射到图像列/行的绝对像素）。`rca_core/geometry.py` 的 `AxisCalibration` / `fit_linear` **已经实现了这个拟合**，缺的是采集入口（人工点两个刻度，或确定性刻度检测——后者现已由 `rca_core/axis_detect.py` 提供）。
3. **不要用 `image_width`/`image_height` 硬推**：`pos` 是相对**轴**的，不是相对整幅图的。图有页边距、面板裁剪、多面板拼版，按图宽等比换算会引入几十像素的系统误差，且误差随版面变化——比不做更糟，因为它看起来像对齐了。

## 建议给 G2 的裁定材料

| 方案 | 需要的新数据 | 现在可做 | 风险 |
|---|---|---|---|
| A. 半真 overlay（归一化坐标） | 无 | ✅ 是 | 视觉近似，用户可能误以为精确；需在 UI 上写明坐标系 |
| B. 真 overlay（像素） | 每图 ≥2 个像素锚点 | 拟合已就绪、采集未接 | 锚点错一次，整图标注偏移且看起来"合理" |
| C. 折中：真 overlay 但只对已人工校准的图 | 复用 B 的锚点，缺失时回退 A | 是 | 同一界面两种精度，需显式标识 |

**本文件只提供实测，不代替 G2 的业务裁定。**

## 维护

- 数据结构变了（例如 prompt 新增像素字段）→ 重跑探针，若 `pixel anchors` 不再为 `NONE`，`tests/test_overlay_coord_probe.py` 会失败并提示重开 G2。
- 该测试同时锁住"残差必须 <0.01"与"像素锚点必须不存在"两个事实，避免结论悄悄过期。
