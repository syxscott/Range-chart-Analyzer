# Range-chart Analyzer — 代码审阅报告(2026-07-31)

- **审阅日期**:2026-07-31
- **审阅者**:主审(古生物地层学 + AI 博士视角)+ 5 维度并行审阅 agent + 每发现独立对抗验证 agent(共 35 个 agent,约 150 万 token)
- **审阅对象**:当前工作区(git HEAD `4989097` + 未提交的 P0/P1 修复,~3300 行变更)
- **方法**:5 路 agent 并行审阅(古生物领域正确性 / Python 核心 bug / 前端 JS parity / 服务端·GUI·安全 / 测试可信度)→ 30 条发现逐条由独立 agent 对抗验证打分(0-100)→ 主审亲自复核领域核心文件(ics_2024.json、ics.py、darwin_core.py、pbdb.py、quality.py、ssrf.py、json_utils.py、gui.py)并人工复核 2 条验证器失败/存疑的发现
- **对照基线**:`docs/CODE-REVIEW-2026-07-27.md`(旧 P0/P1 清单)

---

## TL;DR

**工程层显著进步,领域数据层仍未达"可入库/可发表"标准。综合约 7.0/10,距自述 8.5 有真实差距。**

已确认修复(上轮 P0/P1 基本落实):
- ✅ 全量 pytest **707 项全过**(EXIT=0);`tests_core.py` 独立运行 **375 passed / 0 failed**——上轮"假性绿色"(独立运行 10 失败被软断言掩盖)已修复
- ✅ `safe_json_loads` 长度仅作 tiebreaker,不再压过真实 payload(H3)
- ✅ SSRF 统一到 `rca_core/ssrf.py`,NAT64 / IPv4-mapped / loopback / 重定向 / DNS pinning 全套到位(H1/H2)
- ✅ DwC-A 真 TAB 分隔、`basisOfRecord=MachineObservation` 合法(H1/H2)
- ✅ ICS 已接入 DwC/PBDB 导出路径,阶名型 FAD/LAD 能区分(C1/C4)
- ✅ M6 注释与实现一致(简单平均)、i18n 三语键补齐

**但本审阅发现 3 条 P0 级新问题**,直接危害数据正确性与可用性:

1. **权威 ICS 表本身带白垩纪阶界错误**(坎潘阶底 79.9 ≠ 83.6、圣通阶底 84.9 ≠ 86.3、土伦阶底 94.0 ≠ 93.9),`ics_stage_from_age(82)` 返回 Santonian(应属 Campanian)——**凡 80–87 Ma 的数据在 DwC/PBDB 导出中系统性错移一个阶**,且前端 `js/ics_table.js` 镜像同一错误。
2. **Tkinter GUI 的历史审计完全失效**:`_save_to_history` 因缩进错误被定义在 `if __name__ == "__main__":` 块内(不是类方法),每次成功提取都在 `self._save_to_history(result)` 抛 `AttributeError`,被 `_poll_queue` 的 except 静默吞掉——"Phase J fix"是死代码,`_active_provider` 也从未赋值。
3. **前端评分链未镜像 Python 修复**:`js/json-utils.js` 仍把文本长度计入主分且无嵌套过滤,长 schema 示例会胜过真实 payload → **纯前端模式静默提取空数据**(parity 破坏);`js/quality.js` 的 FAD<LAD 缺 Ma 年龄分支,合法年龄区间被误判违例。

另有服务端多跑次审计记录永不写入、时区符号取反导致用量归错日、`--host 0.0.0.0` 局域网模式所有 POST 403、金标数据集无任何可执行精度/召回门槛等 High 级问题(详见下文)。

---

## 一、领域数据正确性(古生物·地层学)—— 主审亲自复核

### P0-1(High,置信 100):ICS 2024 表白垩纪阶界错误

`rca_core/resources/ics_2024.json:205/216/236`:

| 阶 | 表中 base_ma | 官方 ICS/GTS2020 | 偏差 |
|---|---|---|---|
| Campanian 坎潘阶 | **79.9** | **83.6** | -3.7 Ma |
| Santonian 圣通阶 | **84.9** | **86.3** | -1.4 Ma |
| Turonian 土伦阶 | 94.0 | 93.9 | +0.1 Ma |

实测:`ics_stage_from_age(82)` → `"Santonian"`(应为 Campanian);`ics_stage_from_age(85)` → `"Coniacian"`(应为 Santonian)。`ics.py` 的 `ics_stage_from_age` / `ics_age_compare` / `ics_resolve_age_bound` 全部依赖此表,而它们被 darwin_core / pbdb / quality 真实引用,错误直接进入导出文件与质量评分。前端 `js/ics_table.js:26/81` 镜像同一错误值(94 阶双端复刻,维护风险+1)。`tests/test_ics_cenozoic.py` 只钉住新生界,白垩纪无任何回归测试。

**修复**:改回官方 83.6 / 86.3(Coniacian 相应改为 86.3–89.8),并补白垩纪阶界回归测试。

### P0-2(High,置信 75+主审复核):区间字面量解析取年轻端,C1 塌缩以新形式复现

`rca_core/standards/ics.py:117-121` 的 `_EXPLICIT_MA_PATTERN` 要求"数字紧跟 Ma 单位",故 `"259.51-254.14 Ma"` 只匹配到 **`254.14 Ma`**(区间左端后是 `-` 不匹配)。实测 `ics_resolve_age_bound("259.51-254.14 Ma")` → `("Changhsingian", 254.14)`,而 range_base 本应解析为较老端 259.51(Wuchiapingian)。

端到端复现:`range_base="259.51-254.14 Ma"` + `range_top="254.14 Ma"` →
- DwC:`earliestAgeOrLowestStage == latestAgeOrHighestStage == "Changhsingian"`(旧 C1 塌缩复现)
- PBDB:`max_ma == min_ma == "254.14"`(零长度区间,`_validated_pbdb_ages` 接受相等对)

同一字符串在 section 级被 `_AGE_RANGE_WITH_UNIT` 正确解析为 (254.14, 259.51),在 row 级却错误——语义不一致坐实缺陷。**修复**:区间串应取第一个(较老)数字,或复用 pbdb 的区间正则。

### P0-3(High,置信 100):多跑次 /api/extract 审计记录永不写入

`server.py:1093`:`usage=merged_usage` 在 try 块内读取,而 `merged_usage` 唯一赋值在 except 之后(1127)——Python 同函数作用域规则使其必抛 `UnboundLocalError`,被 1123-1124 的 `except Exception: pass` 静默吞掉。**多跑次提取的用量审计从未入库**,且错误被吞,无任何可见信号。

### High:C3 修复不完整(PBDB min_ma/max_ma 对最常见输入仍恒空)

`pbdb.py:_parse_age_range_ma` 只接受显式 Ma 字面量。实测:
- section `age_range="Late Permian (Wuchiapingian - Changhsingian)"` → `(None, None)`
- 行级 `range_base="Bed 3"` / `range_top="Bed 9"` → `_resolve_pbdb_bounds` 无法解析

**床号标注是 range chart 最常见的形式**,此时 `_validated_pbdb_ages` 双 fallback 全空 → PBDB `max_ma=''` / `min_ma=''`,时间查询仍不可用(注释自称已修复,与实现不符)。床位拒绝本身是有意设计(有测试锁定),真正的缺口是 **section 级阶名/期名 age_range 从未接入 ICS 解析**(`ics_resolve_age_bound("Wuchiapingian")` 本可返回 256.825 Ma)。

### High/Medium:其余领域问题(均已对抗验证)

| 发现 | 位置 | 验证 |
|---|---|---|
| DwC 期/世级标签("Late Permian")仍塌缩 earliest=latest(ICS 只认阶名整词) | darwin_core.py:123 | 75 CONFIRMED,实测复现 |
| `"Pleistocene"` 伪条目仅覆盖 0.0117–0.129 Ma(实际应为 2.58–0.0117),与 Chibanian 重复、Holocene 底 0.0117 重叠;文本"Pleistocene"解析成 0.070 Ma 中点(错 ~1.3 Ma) | ics_2024.json:922 | 75 CONFIRMED,主审复核 |
| 中文阶名("吴家坪阶")经 `ics_resolve_age_bound` 恒返回 None,中文图表默认降级 | ics.py:85 | 85 CONFIRMED |
| `_BIOZONE_STAGE_MAP` 仅 8 条;`"clarkina"` 无条件映射 Changhsingian(吴家坪期 Clarkina 带被误判),其余真实菊石/放射虫带名仍静默跳过(Steno 检查近 no-op) | quality.py:797 | 75 CONFIRMED |
| **半球误判**:`_parse_coordinates` 在**全文本**中搜 `[Ss]`/`[Ww]` 而非匹配组内——`"31N, 117E (south of village)"` 北纬被翻成南纬;`"…west bank"` 东经被翻成西经 | darwin_core.py:59-62、pbdb.py:53-54 | 主审发现并复核 |
| **"ma" 子串假阳性**:FAD<LAD age 分支用 `re.search(r"ma|myr", text, re.I)` 判"像年龄",任何含 "ma" 的床/段标签("Madison 3"、"marine unit 4")被当成 Ma,取文本首数字比较 → 合法床号对 `base="Madison 3"/top="Madison 6"` 被误判 FAD<LAD 违例扣分 | quality.py:341-344 + 175-181 | 主审发现并复核 |
| **DwC 丢弃数值 Ma**:`_resolve_age_bounds` 计算出的 `_e_ma/_l_ma` 在 darwin_core.py:122 被弃用,导出中无任何数值年代字段("ICS 集成"在 DwC 侧只产出阶名文本) | darwin_core.py:122 | 主审复核 |
| `ics_age_compare` 用阶中点比较——对相邻阶(Danian/Thanetian)仍单调正确,非 bug,不报 | — | — |

---

## 二、工程 bug 与安全

| 发现 | 位置 | 验证 | 说明 |
|---|---|---|---|
| **Tkinter 历史审计死代码**:`_save_to_history` 缩进在 `if __name__ == "__main__":` 块内(非类方法,运行时 `hasattr(RangeChartApp, ...)` = False),每次成功提取抛 AttributeError 被 `_poll_queue`(gui.py:1673)静默吞掉;`_active_provider` 从未赋值 | gui.py:1725/1913 | 95 CONFIRMED + 主审运行时复核 | 上轮"Phase J fix"实际从未生效 |
| **时区符号取反**:`local_offset = -localtime().tm_gmtoff`(tm_gmtoff 东正西负),东八区所有行的用量归错前一天(UTC 07-31 18:00 的北京 08-01 02:00 被归入 07-30) | usage.py:421 | 主审复核(验证器因输出超限失败,人工确认属实) | 应为 `utc_day + tm_gmtoff` |
| **SSRF 三策略并存**:`_is_safe_endpoint`(llm.py:1226)对 loopback 直接放行且不查 scheme → Ollama `http://127.0.0.1:11434` 连接测试通过、真实提取必被 `validate_endpoint_or_raise` 拒绝;注释声称同策略,与实现矛盾 | llm.py:1226/1821 | 100 CONFIRMED | 测试固化错误行为 |
| **CSRF 回归**:`--host 0.0.0.0` 时 `EXPECTED_HOSTS` 只有 `0.0.0.0/127.0.0.1/localhost` + port,无本机实际 IP → 局域网浏览器同源 POST 恒 403 | server.py:1175/435 | 80 CONFIRMED | README 声称支持局域网访问 |
| **app.py(PyWebView)CSRF 弱化**:`_start_server` 不填充 EXPECTED_HOSTS(空集)→ 校验回退到客户端可控的 Host 头,DNS-rebinding 绕过仍存在(旧 CSRF 修复不完整) | server.py:437 | 75 CONFIRMED | |
| **secrets_store 默认仍混淆级**:keyring 未声明为依赖,本机默认走 `_derive_fernet_key()`(hostname+home+MAC 派生),警告仅走 Python warnings(GUI 不可见);requirements.txt 无 cryptography/keyring | secrets_store.py:208 | 85 CONFIRMED | 比旧版进步:警告显式化了 |
| **history 事务原子性**:`update_result` 在 `transaction()` 内调 `db.execute` 提前 commit,UPDATE+INSERT 两步各自独立事务,中途失败无法回滚 | history.py:416 | 75 CONFIRMED(实测复现) | |
| **M2 过滤回归**:纯包装对象 `{"data": {...}}`(散文包裹路径)时,嵌套过滤剔除内层真实 payload → 整个 payload 被丢弃 | json_utils.py:339 | 75 CONFIRMED(实测复现) | 仅 Level 4 路径受影响;纯 JSON 走 Level 3 不受影响 |

---

## 三、前端 JS parity(三端契约声称被打破)

| 发现 | 位置 | 验证 | 说明 |
|---|---|---|---|
| **`js/json-utils.js` 未镜像 H8/M2 修复**:长度仍进主分(`_payloadScore*10 + candidate.length`),无嵌套候选过滤 → 长 schema 示例胜过真实 payload,**纯前端模式静默提取空数据** | js/json-utils.js:250 | 100 CONFIRMED(node 实测) | Python 已修,JS 漏修 |
| **`js/quality.js` FAD<LAD 缺 Ma 年龄分支**:只用 `_parseBedN` 取前导整数,`range_top="250 Ma"`/`range_base="300 Ma"` 被误判违例(实测 JS 0.77/B vs Python 0.97/A) | js/quality.js:247 | 100 CONFIRMED | 评分差异直接改变用户看到的等级 |
| **跨纪惩罚整项计 0 分**(Python 为比例扣分 1-0.5×violations) | js/quality.js:359 | 90 CONFIRMED | |
| **phylo 模式双缺口**:JS prompt 缺 `parent` 字段与降级条款;normalizer 对缺失字段抛异常违反 never-throws 契约;js/prompt.js:109 "byte-identical" 注释不实 | js/prompt.js:246 | 85 CONFIRMED(逐行 diff) | |
| **Steno 生物带检查缺 `_BIOZONE_STAGE_MAP`**:`icsKeyOf` 只做阶名整词匹配,对真实带名("N. optima Zone")返回 None → 近 no-op | js/quality.js:599 | 85 CONFIRMED(node 实测) | |
| **`col.formations` i18n 键三语缺失**(已改名 col.formation),quality issue params 未代入 → 界面出现 `[?col.formations]` 占位符串 | js/table.js:134 | 85 CONFIRMED | |
| **`minimax.js` 只取第一个 Anthropic text 块**,Python `_read_response` 拼接全部 → 跨块 JSON 在浏览器端解析失败 | js/minimax.js:1030 | 75 CONFIRMED | |
| **`aggregate.js` 作者名归一化未镜像 H-7**(em-dash/ex/in 剥离),dedup 键跨端分歧("Smith—Jones" JS `smith—jones` vs Python `smith jones`) | js/aggregate.js:31 | 78 CONFIRMED | |

---

## 四、测试可信度与专业科研软件标准

| 发现 | 位置 | 验证 | 说明 |
|---|---|---|---|
| **Gold 数据集无任何可执行的精度/召回回归门槛**:唯一提取测试 `test_gold_smoke.py:47` 永久 skip,启用后也只断言不崩溃;`test_gold_metrics.py` 全部用内联手写 dict,零引用 `tests/fixtures/gold/`;conftest 声称的 `gold_offline`"预录响应"文件不存在,`--rca-offline` 参数未注册 | tests/test_gold_smoke.py:47 | 90 CONFIRMED | 声明"professional research software"却无一条端到端提取准确率指标 |
| **js/quality.js(761 行核心评分)零行为测试**:仅有 `assert 'scoreRangeChart' in q_src` 源码字符串检查;tests_frontend.js 加载清单不含 quality.js | tests/test_quality_js_integration.py:48 | 92 CONFIRMED | 前端评分在 Node 侧从未真正执行过 |
| **test_ssrf.py 依赖活体 DNS**(`api.anthropic.com`),离线/沙箱 CI 必失败 | tests/test_ssrf.py:99 | 75 CONFIRMED | |
| **无任何 CI/coverage 门槛**(旧 P2-13 未落实),707 项测试仅靠本地手工运行;README 已统一"约 815 项"口径但无 badge/门槛 | pytest.ini:1 | 80 CONFIRMED | |

**已改进**:软断言(375 项独立运行全过)、DwC-A TAB 有测试、SSRF 有 18 项测试、biozone order 有数值比较测试。**仍缺**:白垩纪 ICS 回归、区间字面量解析、前端评分行为、金标端到端。

---

## 五、专业级评分(0-10)

| 维度 | 分 | 关键理由 |
|---|---|---|
| 工程鲁棒性 | 8 | SSRF 全套硬化、never-raises、JSON 容错、参数化 SQL;但 gui 死代码、事务原子性、时区取反 |
| 古生物数据正确性 | **5** | ICS 已接入但**权威表自身错**(白垩纪错移一阶)、区间字面量取错端、床号/中文/期世标签回退路径仍塌缩或恒空 |
| 前端 parity | **5** | json-utils 评分未镜像(静默空数据)、quality 评分分歧、prompt 双缺口 |
| 测试可信度 | 6.5 | 707 全绿 + 软断言已修;但金标无门槛、前端核心零行为测试、无 CI |
| 可复现性 | 6.5 | request_meta 记录良好;未强制 seed |
| 安全态势 | 7.5 | SSRF/CSRF 设计专业,存 3 处不一致与 1 处回归 |
| 文档/维护 | 6.5 | README 已统一;注释与实现多处矛盾("byte-identical"、"已修复"注释) |

**综合:约 7.0/10**。距自述 8.5 的真实差距:(a) 权威表数据错误 + 回退路径塌缩仍使部分导出数据在科学上不可用;(b) 前端契约三处实质性分叉;(c) 金标回归门槛缺失,"专业级"无度量支撑。

---

## 六、修复优先级

**P0(阻断可信度,1-2 天)**
1. 修 `ics_2024.json` 白垩纪阶界(83.6/86.3/93.9)+ `js/ics_table.js` 同步 + 补回归测试
2. 修 `gui.py` 缩进,把 `_save_to_history` 移回类内并赋值 `_active_provider`;加"历史已保存"状态反馈
3. `js/json-utils.js` 镜像 H8/M2:长度移出主分 + 嵌套候选过滤(两端行为差异写 parity 测试)
4. 修 `server.py` multi-run `merged_usage` 初始化;`usage.py` 时区符号

**P1(科研正确性,3-5 天)**
5. `ics_resolve_age_bound` 支持区间字面量取较老端;`_parse_coordinates` 半球判定改匹配组内;quality `ma|myr` 改词边界
6. PBDB section 级阶名 age_range 接入 ICS;DwC 增加数值 Ma 字段(measurementOrFact 或 chronostratigraphicAge 带 Ma);期/世级标签映射
7. `js/quality.js` 补 Ma 分支与比例扣分、`_BIOZONE_STAGE_MAP`;`js/prompt.js` phylo 补 parent 与降级条款;`minimax.js` 拼接全部 text 块
8. 统一 SSRF 策略(连接测试与提取一致);`--host 0.0.0.0` 时把本机 IP 加入 EXPECTED_HOSTS;app.py 填充 EXPECTED_HOSTS
9. history `transaction()` 改单事务;M2 过滤对纯包装对象兜底

**P2(专业度,1-2 周)**
10. 金标数据集落地:预录响应 + 精度/召回回归门槛(当前 fixture 目录与测试完全脱节)
11. js/quality.js 行为测试(vm 装载)+ CI(GitHub Actions)+ coverage 门槛
12. secrets_store 声明 cryptography/keyring 依赖并在 GUI 设置页显示加密状态;test_ssrf.py 用 mock 替代活体 DNS;aggregate.js 镜像 H-7

---

*本报告仅审阅,未修改任何项目文件。所有结论可由 `文件:行号` 复核;领域数值可与官方 ICS 表(https://stratigraphy.org/chart)核对。*
