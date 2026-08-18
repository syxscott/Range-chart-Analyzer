# Range-chart Analyzer — 代码审阅报告

- **审阅日期**：2026-07-27
- **审阅者**：主审（齐活林 / Delivery Director）+ 多 Agent 并行审阅团队
  - `domain-reviewer`（古生物地层学严谨性 / 数据正确性）
  - `eng-reviewer`（工程 Bug / 安全 / 边界 / Python 正确性）
  - `quality-reviewer`（架构 / 测试 / 可复现性 / i18n / 质量评分卡）
- **审阅范围**：`rca_core/`（核心抽取、聚合、标准导出、质量评分）、`server.py`、`gui*.py`、`proxy/`、`js/`、`prompt.py`、配套标准库 `standards/ics.py / darwin_core.py / pbdb.py`
- **方法**：主审独立精读 8 个核心文件 + 三路 specialist Agent 并行深度审阅，结论交叉印证后合成。

---

## TL;DR

**当前版本远未达到"可入库 / 可发表级科研软件（research-grade）"水准，但已是一个工程鲁棒性不错的"可用科研工具"（综合约 7.0/10）。**

最致命的问题集中在**古生物数据导出正确性**与**测试可信度**两条线：
1. **导出路径从未接入 ICS 年代地层表** → Darwin Core / PBDB 记录只携带阶名文本，永远不带数值 `Ma`，入库即废。
2. **测试套件"假性绿色"** → 核心测试用软断言，10 项真实失败在 pytest 下不可见，CI 全绿掩盖缺陷。

工程层另有 2 个 SSRF 绕过、1 个 JSON 容错评分反转、DwC-A 用逗号而非 TAB 等 Hard 级问题。

---

## 一、总体判定

| 维度 | 评价 | 一句话 |
|------|------|--------|
| 古生物数据正确性 | ❌ 未达专业级 | 导出不携带数值年代、FAD/LAD 塌缩、PBDB 字段语义错 |
| 工程鲁棒性 | ✅ 中上 | SQL 参数化、重定向拦截、never-raises 契约、JSON 容错做得好 |
| 安全性 | ⚠️ 有缺口 | SSRF 有两处绕过、本地密钥零保护、GUI 缺 DNS pinning |
| 测试可信度 | ❌ 虚假绿 | 软断言掩盖失败、文档测试项数自相矛盾 |
| 可复现性 | ⚠️ 有条件 | 记录 seed/指纹但未强制默认 seed |
| 专业度自评 | 7.0 / 10 | 距自述 8.5 有真实差距 |

**结论**：可作为"辅助预标注工具"进入科研工作流，但**在修复 P0 项之前，其导出的 Darwin Core / PBDB 不能直接入库，质量评分卡的部分校验对真实数据静默失效，不应作为唯一权威来源**。

---

## 二、跨维度共识根因（最重要）

**ICS 国际年代地层表（`rca_core/standards/ics.py`）只被 `quality.py` import，导出路径（`darwin_core.py` / `pbdb.py` / `exporter.py`）从不引用。**

- `quality.py:35` 是全局唯一 `from .standards.ics import ...`
- grep `ics|to_ma|age_to|Ma` 在三个导出模块中**零命中**
- 后果：阶名（如 "Wuchiapingian"）→ 数值 `Ma` 的换算**从未发生**。这是以下所有领域硬伤的系统根因。

---

## 三、维度一：古生物领域严谨性（domain-reviewer + 主审）

### Critical / High — 入库门槛

| 编号 | 问题 | 位置 | 说明 |
|------|------|------|------|
| C1 | DwC FAD/LAD 塌缩 | `darwin_core.py:97-104` 调 `_first_stage_before`，但该函数（`:135-147`）**忽略第二个参数直接 `return age_range`** | `earliestAgeOrLowestStage` 与 `latestAgeOrHighestStage` 永远相等，首现/末现不分 |
| C2 | PBDB early/late_interval 均为 biozone | `pbdb.py:90-91` | 两地层字段都被赋成生物带，且语义类型错误（应为年代区间） |
| C3 | PBDB min_ma/max_ma 恒空 | `pbdb.py` `max_ma/min_ma = sec_data.get("max_ma","")` | 文本 `age_range` 无数字时取空；数值年代缺失 |
| C4 | ICS 表未接入导出（根因） | `standards/ics.py` 仅 `quality.py` 引用 | 阶名→Ma 换算从未发生 |
| H1 | `basisOfRecord="MachineGenerated"` 非法 | `darwin_core.py:129` | DwC 词表无此值，应为 `MachineObservation` |
| H2 | DwC-A 声明 TSV 实给 CSV | `darwin_core.py:214` `csv.writer(output, lineterminator="\n")` 缺 `delimiter="\t"`，而 `meta.xml:199` 声明 `fieldsTerminatedBy="\t"` | GBIF/iDigBio 解析错位 |
| H3 | 岩石地层被丢弃 | `darwin_core.py` `lithostratigraphicTerms` 恒为 `""` | 组/段（Formation/Member）信息未导出 |
| H4 | Lazarus / range-extension 信号丢失 | `endpoint_kind` / `occurrence_mode` 抽取后被导出路径丢弃 | 再沉积、延伸分布等关键科研信号未传递 |

### Medium — 方法学可信度

- **M1 众数合并固化系统偏差**：`aggregate.py` 合并多个 run 时直接取众数，遇系统偏差无校正。
- **M4 `eval_metrics._normalize_taxon` 虚高匹配准确率**：剥离 `cf.`/`aff.`/`sp.`/`?` 后使近似名被算作命中，精度评估失真。
- **M5 abundance `SUM-TO-100` 无条件施加**：`prompt.py:179-180` 对丰度图强制归一，但部分图本身非百分比口径。
- **M6 confidence 未传导出导出**：逐行 `confidence`/`note` 未进入 DwC/PBDB 输出，下游无法按置信度筛选。

### 主审独立发现（已核实）

- **P1-1  Phylogenetic-tree 模式错配**：`extractor.py:1771`（`extract_phylogenetic_tree` 内）调用 `prompt_version_for_mode("columnar_section")`，应为 `"phylogenetic_tree"`。导致系统发育树抽取的 `request_meta.prompt_version` 记录错误，破坏可复现性溯源。（已在源码注释中见到 `P1-1 (REVIEW-2026-07-27)` 标记，建议同步修正。）

### 主审被纠正的点（已排除）

- 主审曾怀疑 `pbdb._parse_coords` 无法解析 `"31.5 S 117.5 W"`。经 domain-reviewer 核实：pbdb 该正则分支**可以解析**，仅半球判定应严格匹配组内字母即可。**非 bug**，已从报告剔除。

---

## 四、维度二：工程实现与安全性（eng-reviewer）

### Critical / High

| 编号 | 问题 | 位置 | 修复方向 |
|------|------|------|----------|
| H1 | Gemini 路径 SSRF 校验过弱 | `llm.py:1213-1229` `_is_safe_endpoint` 仅查 scheme/hostname，不校验私有/loopback/link-local | 改用 `rca_core.ssrf.validate_endpoint_or_raise`，与 anthropic/openai 一致 |
| H2 | NAT64 前缀绕过 is_global | `ssrf.py:47-67` / `server.py:340-384` `ip.is_global` 对 `64:ff9b::169.254.169.254` 返回 True | 显式拒绝 `64:ff9b::/96`、`64:ff9b:1::/48`，并检查 `ipv4_mapped` |
| H3 | `safe_json_loads` 评分反转 | `json_utils.py:342-358` 主分数并入 `len(src)`，长 schema 示例压过真实 payload | 长度仅作同分 tiebreaker，不进主分 |
| H4 | DwC-A 逗号而非 TAB | `darwin_core.py:214` | `csv.writer(output, delimiter="\t", lineterminator="\n")` |

### Medium

- **M1** FAD/LAD 未区分（与 C1 同根）——`darwin_core.py` + `pbdb.py`。
- **M2** JSON 嵌套对象"掏空"外层字段——`json_utils.py:134-176`，内层胜出时外层同级字段丢失。
- **M3** GUI 路径缺 DNS pinning（TOCTOU 重绑）——`llm.py` 直连时仅依赖 `validate_endpoint_or_raise`，无 pinning。
- **M4** `secrets_store` 密钥同机零保护——`secrets_store.py:111-126` Fernet key 由本机可读信息派生，任意本地用户可重算解密。建议用 OS keyring / 口令派生 + `0o600`。
- **M5** cache 跨进程无锁 + LRU 不稳定——`cache.py:63-67` 进程内 RLock；`_evict` 时间戳同值时 `LIMIT` 选择不稳，可能误删新项。
- **M6** aggregate 置信度"按一致率加权"实为简单平均——`aggregate.py:714-747`，`runs` 多为 1，注释与实现不符。
- **M7** `ics.py` 死代码 + 逐 stage 现场编译 + 导入期硬失败——`:72-74` 死正则；`ics_parse_age_range` O(stages²)；`:16` 导入即 `json.loads` 无兜底。

### Low

- L1 三套 SSRF 校验器并存（`ssrf.py` / `server.py` / `llm.py`），易再次分叉 → 统一到 `rca_core.ssrf`。
- L2 `server.py:712` `..` 检查应先 `rel.replace("\\","/")`。
- L3 `aggregate.py:187-191` `repr(sorted(set))` 对 `set` 非确定性 repr 致签名不稳。
- L4 `db.py` 冗余 DDL + `executescript` 在事务内自提交削弱原子性。
- L5 PBDB csv 默认逗号，需确认是否应 TSV（`pbdb.py:145`）。
- L6 `darwin_core.py:150` `_ics_stage_to_pbdb_name` 死代码。
- L7 `extractor.py` 未检查 `decode_error`/media type，坏图直接发 LLM。
- L8 `server.py` pinning opener 含死代码 monkey-patch，脆弱但可用。

---

## 五、维度三：科研软件专业度（quality-reviewer）

### 评分卡（0–10）

| 维度 | 分 | 关键理由 |
|------|---|---------|
| 架构 / 模块化 | 8 | `rca_core/` 职责分明、Python/JS 共享契约；但 ICS 未接入导出 |
| 测试覆盖与严格度 | 4 | 软断言掩盖 10 项失败；两文件收集 0 |
| 可复现性 | 6 | 记录 seed/指纹但未强制默认 seed |
| 数值稳定性 | 7 | `json_utils` 严格拒 NaN/Inf；置信度加权确定 |
| 文档质量 | 4 | README 测试项数 169/26 双矛盾、依赖描述错 |
| i18n 正确性 | 5 | zh 比 en/ja 多 5 键，缺失键软断言未捕获 |
| 安全态势 | 8 | SSRF/密钥/CSRF 有专业设计，但存 H1/H2/M3/M4 缺口 |
| 可维护性 | 7 | 注释详尽、commit 有追溯；quality.py 技术债集中 |
| 性能 | 7 | `json_utils` 括号扫描最坏 O(n²)（仅脏文本）；ICS 静态加载 |
| 打包 / 分发 | 6 | 8 可选包 + 一键 bat；README 依赖描述误导 |

### 重点发现

- **测试"假性绿色"（严重）**：`tests_core.py` 用自定义 `check()`（`:32-39`）只打印不 `assert`；独立运行 **10 项 FAIL**（`i18n-parity-en/ja`、`gemini-key-in-header`、`gemini-key-not-in-url`、`m5-clamp-max` 各两次），但 **pytest 下 45 项全 PASS**（`sys.exit(1)` 仅 `__main__` 触发）。CI 全绿掩盖失败。
- **README 矛盾**：行 168 写"169 项"、行 205 写"26 项"；实测 `tests_core.py` 45 函数 / ~375 软断言 + `tests/` 637 项 ≈ 815 项。"169/26" 均不成立。
- **i18n 键不齐**：`i18n.py` 中 zh 含 5 个 en/ja 缺失键（`quality.agreement_overflow`/`chimera_dropped`/`empty_result`/`fad_lt_lad`/`missing_biozone`），en/ja 界面渲染原始 key 串。
- **`quality.py` 校验静默失效**：
  - FAD<LAD 仅查 bed 序（`_parse_bed_n` `:133-151`），遇年龄/阶名解析为 `None` 直接跳过 → 对多数 range-chart 结果无效。
  - biozone-order 检查（`:610-703`）对真实菊石/放射虫带名无法解析 → `continue` 跳过，近 no-op。
  - `:357` "permutian" 拼写错误（死代码）。
- **可复现性有条件成立**：聚合层确定性好，但上游 LLM 采样非确定，除非用户显式设 seed+temperature 且 provider 遵守；代码未强制默认 seed。

---

## 六、综合修复优先级

### P0（1–2 天，阻断可信度 / 入库门槛）
1. DwC-A 改 TSV：`darwin_core.py:214` 加 `delimiter="\t"`。
2. 修复 `tests_core.py` 软断言：改为 `assert` 或 pytest 适配，让 10 项失败可见；定位 `gemini-key-*`/`m5-clamp-max` 为真实 bug 还是过期测试。
3. 补全 en/ja 的 5 个 i18n 键，让 parity 测试硬失败。
4. 修 `tests_exporter_xlsx.py` import 期崩溃（不变量断言移出模块顶层）。
5. SSRF H1/H2：统一校验器并拒绝 NAT64 前缀与私有/loopback/link-local。

### P1（3–5 天，科研正确性）
6. 在 `darwin_core.py` / `pbdb.py` / `exporter.py` 接入 `ics.py`，输出数值 `Ma`（base/young）；真正实现 FAD/LAD 推导（修 C1/M1）。
7. 修 PBDB `early/late_interval` 语义与 `min_ma/max_ma` 赋值（C2/C3）。
8. `basisOfRecord` 改 `MachineObservation`（H1）；保留岩石地层与 `endpoint_kind`/`occurrence_mode`（H3/H4）。
9. `safe_json_loads` 评分反转修复（H3）；JSON 嵌套掏空修复（M2）。
10. FAD<LAD 与 biozone-order 用 ICS 做数值比较真正生效；扩展 `_resolve_biozone_stage`。
11. 强制默认 `seed`（可配置），文档说明可复现前提。

### P2（1–2 周，打磨）
12. `json_utils` 括号扫描加最坏情况保护。
13. 统一 README 测试项数 / 依赖 / 评分口径；加 coverage 门槛与 CI badge。
14. `secrets_store` 改 OS keyring / 口令派生 + `0o600`（M4）；GUI 路径加 DNS pinning（M3）。
15. cache `_evict` 稳定次序 + `OperationalError` 重试（M5）。
16. aggregate 置信度加权语义对齐注释或改注释（M6）；`ics.py` 预编译 + 导入兜底（M7）。
17. 清理 `quality.py:357` typo；补"年龄型 FAD/LAD"与"未知带名跳过"单测。
18. 修 `extractor.py:1771` 模式错配（P1-1，主审发现）。

---

## 七、专业度评分汇总

**综合：约 7.0 / 10**（quality-reviewer 量表；主审与 eng-reviewer 一致认为工程层可达 7–8，领域正确性层因 C1–C4/H1–H4 拉低至 4–5）。

距项目自述的 8.5 分有真实差距，主要差距来自：**(a) 领域导出正确性硬伤、(b) 测试假性绿色、(c) 质量评分对真实数据静默失效**。

---

## 八、附录 — 测试实况（供验证）

- `tests_core.py`：45 pytest 函数 = ~375 软断言；独立运行 **365 passed / 10 failed**（EXIT=1，pytest 下 45 PASS）。
- `tests/`：637 项（60 文件）。
- `tests_exporter_xlsx.py`：0 收集（import 期 `ValueError`）。
- `tests_llm.py`：0 收集。
- 全量合计 ≈ 815 项（"169/26" 均不成立）。

---

*本报告仅做审阅，未修改任何项目文件。所有结论均可由对应 `文件:行号` 复核。*
