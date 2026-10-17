# 代码审查报告 — 2026-10-17

范围：`rca_core/`（34k 行）、`server.py`（189 KB）、`app.py` / `main.py`、`js/`（22.5k 行）。
方法：静态检查（ruff 关键规则 + `node --check`）→ 全量测试基线 → 针对性构造输入实测 → 人工阅读高风险路径。
**每个结论都标注了验证方式**：`实测` = 我亲自跑过；`代码` = 读过代码但未运行。

结论：**未发现 P0**（无越权、无任意文件读写、无数据销毁）。发现 6 项 P1、5 项 P2、4 项 P3。
其中 7 个测试当前是红的，根因只有一个（第 6 条）。

---

## 0. 测试基线

| 套件 | 结果 |
|---|---|
| pytest（Anaconda 3.13.5 / pytest 8.3.4） | 收集 3959，**7 失败** |
| `tests_frontend.js` | 825 通过 / 0 失败 |
| 其余 12 个 JS 套件 | 全部通过（聚合 60、对比度 326、差分 1332、编辑历史 334、导出 91、历史 23、名称 137、锐化 175、UI 64+75、可视化 161、契约 58） |

> 环境提醒：本机 `HTTP_PROXY=http://127.0.0.1:14504` 会把 `127.0.0.1` 的请求也代理掉。
> 不设 `no_proxy` 时，`tests/test_sprint_b_server.py::test_non_ascii_csrf_token_rejected_cleanly`
> 会假失败（代理拒绝非 Latin-1 头字节）。**清空代理后该用例通过，不是代码问题**。

---

## 1. P1

### P1-1 客户端可传入的畸形 endpoint 会让服务端丢连接（无任何响应）

- 位置：`server.py:2637`（`_validate_endpoint(endpoint)`）、`rca_core/ssrf.py` 的 IDNA 编码
- 触发：POST `/api/extract`，`endpoint` 的主机名某个 label > 63 字符，或含空 label（`a..b`）
- 实测：

```
CLIENT: RemoteDisconnected Remote end closed connection without response
server exceptions: 1
UnicodeEncodeError: 'idna' codec can't encode characters in position 0-69: label too long
```

- 原因：`_handle_extract_body` 的 `try` 只覆盖「读 body + json 解析」，`_validate_endpoint` 在其外；
  `is_private_host()` 对 `.encode("idna")` 没有兜底，异常直接穿出 `do_POST`。
- 影响：用户把 URL 粘错一个字符（很常见），前端看到的是「服务器无响应」，服务器日志多一条 traceback；
  若该 endpoint 被存进 providers.json，之后每次提取都这样失败。
- 修复：`_validate_endpoint` 调用点包 `try/except (UnicodeError, ValueError)` → 返回 `err.badEndpoint`；
  更根本的是在 `ssrf.is_private_host` 里把 IDNA 编码失败转成「拒绝」而不是抛出。

### P1-2 「截断/不可用载荷」守卫被元数据键架空

- 位置：`rca_core/extractor.py:1228`（`_extracted_any`）、`:1339`（`_payload_mismatch`）
- 原因：`_extracted_any` 遍历**所有** value，只有非空就返回 True —— 包括 `warnings`、`_warnings`、`_extras`。
  而 `_payload_mismatch` 的第一句就是 `if _extracted_any(data): return ""`。
  结果：**只要规范化过程产生过一条警告，"截断但没救出任何行"就永远不会被报成错误**。
- 实测：

```python
out = normalize_occurrence_matrix_result(
    {"columns":[{"label":"A1"}], "legend":[{"code":"A","class":"x"}],
     "occurrences":[{"taxon":"G","site":"A1","code":"","class":""}]})
# occurrences == []   warnings == ['dropped 1 row(s) for blank cells; ...']
_payload_mismatch(out, truncated=True)  ->  ''        # 期望：'truncated output rescued no usable records'
```

  对照：`normalize_result({})`（无警告）→ 正确返回 `'truncated output rescued no usable records'`。
- 附带问题：`_payload_mismatch` 读 `data.get("_warnings")`，但规范化函数写的是 `warnings`；
  两个键并存，截断标签 `truncated_or_unrecognized_payload` 查不到。
- 影响：模型被 max_tokens 截断、只救回内层一行时，结果 `ok=True`、无警告，看起来像一次正常提取。
- 修复：`_extracted_any` 跳过 `_` 前缀键与 `warnings`/`note`/`truncated` 这类元数据；
  同时统一 `warnings` / `_warnings` 的键名。

### P1-3 occurrence_matrix：两阶段读到的「空码」单元格被规范化阶段丢掉

- 位置：`rca_core/extractor.py:5856`（`if not code and not klass: dropped_blank += 1; continue`）
- 矛盾点：`rca_core/matrix.py:771-775` 明确写着
  「A tile the model did not answer is recorded with an empty code rather than dropped:
  the CELL EXISTS, and dropping it would turn a read failure into a silently wrong absence.」
  —— 两阶段路径按这个设计发出了 `code: ""` 的行，规范化阶段立刻把它删了。
- 实测：3 个墨水单元（其中 1 个模型未读出）→ `occurrences` 只剩 2 条；
  唯一的痕迹是 `warnings` 里一句 `dropped 1 row(s) for blank cells`。
- 影响：**「单元格存在但没读出来」被静默转成「该分类单元在此剖面不存在」**，
  这正是该模式最不能犯的错误（缺失即数据）。
- 修复：`cell_status == "grid_cells"` 时保留空码行（`code=""` + 一个 `unread` 标记），
  只有单遍路径才执行稀疏化丢弃。

### P1-4 generic_table：空表头导致列错位 + 末列数据丢失

- 位置：`rca_core/extractor.py:6088-6111`
- 原因：列循环里 `if not label: continue` 跳过了空表头列，列数组被「压实」；
  行的 `cells` 仍按原始位置切片到 `width = len(压缩后的列)`
- 实测：`columns=[A, "", C]`，`cells=["a1","b1","c1"]` →

```
columns: [{'index':0,'label':'A'}, {'index':2,'label':'C'}]
rows:    [{'cells': ['a1','b1']}]     # C 列下显示 b1，真实的 c1 丢失
```

- 影响：导出的表格**表头与内容不对应**，且没有任何警告 —— 比缺列更危险。
- 修复：保留列位置（空表头用 `"col i"` 占位并告警），或整份拒收；不要压缩。

### P1-5 前端：换图后旧结果仍可见、可导出，且导出文件名叫新图

- 位置：`js/app.js:725`（`handleFile`）、`:1949`（导出全部）
- 事实：`state.result` 只在 `runExtraction` 成功（:1479）与 `resetUpload`（:2018）被写；
  `handleFile` 只 bump token + abort，**从不清理 `state.result` / 结果面板**。
- 结果：选新图后，页面左侧是新图预览，右侧仍是上一张图的表格；
  此时点「导出全部」得到 `{source_file: "B.png", result: <A 的数据>}`。
  这与他们 2026-10-16 刚修好的 `loading` 窗口问题同源，只是窗口从「解码期间」变成了「整个待提取期间」。
- 验证方式：代码确认（未跑浏览器）。
- 修复：`handleFile` token 校验通过后清空 `state.result/rawText` 并调 `updateActionButtons()`；
  或导出时校验 `state._resultForFile === state.file`。

### P1-6 `_safe_local_path` 的 NUL 防护在当前 Python 上已失效（7 个测试红）

- 位置：`server.py:1932`（`real_target = os.path.realpath(target)`）
- 代码注释断言：`realpath` 遇到嵌入 NUL 会抛 `ValueError`，因此不需要显式检查
  （注释还说明「显式检查被实测为死代码，已删除」）。
- 实测（Python 3.13.5 / Windows）：**`os.path.realpath("...\\a\x00b.js")` 不抛异常，原样返回**。
  `open()` 才抛 `ValueError`；而 `_serve_static` 先走 `os.path.isfile`（它对 NUL 返回 False），
  于是 `/js/a%00b.js` 得到 **404** 而不是契约要求的 **403**。
- 影响：`_safe_local_path` 返回了含 NUL 的路径（违反自己的 docstring），
  7 个测试失败（`tests/test_server_path_resolution.py`）。**没有越权**：文件仍打不开。
  但「realpath 会兜住」这条安全论证在当前解释器上不成立，属于随时会变成真漏洞的定时炸弹。
- 修复：显式 `if "\x00" in rel: return None`（这次它不是死代码），并在 `except` 里同时兜 `ValueError`。

---

## 2. P2

| # | 位置 | 问题 | 验证 |
|---|---|---|---|
| P2-1 | `rca_core/modes.py:74` | `__all__` 列了不存在的 `CLASSIFY_PROMPT_VERSION`（真名是 `CLASSIFICATION_PROMPT_VERSION`）。`from rca_core.modes import *` 直接抛 `AttributeError` | 实测 |
| P2-2 | `rca_core/bed_parser.py:253` / `:276` | `label_ordinal` 与 `ordinal_kind` 不一致：`"Bed 23"` → kind `"letter"`（因为 `re.search(r"\b[A-Za-z]{3,}\b")` 命中了 `Bed`），而 `"23c"` → `"number"`；`"zone Z1"` 返回 1（docstring 明写应为 None）；`"23-25"` 返回 25。这些值供 extractor 的「端点标签/索引互相矛盾就交换」启发式和导出校验使用，误判会交换本不该交换的区间端点 | 实测 |
| P2-3 | `rca_core/extractor.py:5425` | 两阶段矩阵路径硬编码 `media_type="image/png"`，但 `render_label_image` 在图幅 ≥2400px 时会**原样返回输入的 JPEG base64**。对严格校验 media type 的厂商（Anthropic）是 400 | 代码 + 实测函数行为 |
| P2-4 | `rca_core/matrix.py:430` | `legend_codes()` 对 legend 里的非 dict 项直接 `.get()`：`legend_is_plausible` 会过滤非 dict 后判定「合理」，于是混合列表（`["A", {"code":"B"}]`）能通过检查，随后 `legend_codes` 抛 `AttributeError`，整个两阶段路径被放弃（有 catch，降级为单遍） | 实测 |
| P2-5 | `server.py:794` | `is_loopback_bind("")` 返回 True（视为 localhost），但 `ThreadingHTTPServer(("", port))` 实际绑定 **0.0.0.0**。`--host ""` 时不会打印「已暴露到局域网」的警告 | 实测（`getsockname() == ('0.0.0.0', …)`） |

---

## 3. P3

| # | 位置 | 问题 | 验证 |
|---|---|---|---|
| P3-1 | `rca_core/aggregate.py:1044` | `merge_results` 遇到非字符串 `species`（如 `123`）抛 `TypeError: expected string or bytes-like object`。**服务端路径不可达**（规范化已 stringify），仅对手工构造/历史导入的 payload 成立，属防御性缺口 | 实测 |
| P3-2 | `rca_core/aggregate.py:1606` | `sections` 的 agreement 计数没有「单轮去重」，一轮里出现两个同名 section 时得到 `agreement="3/2"`、`agreement_count=3 > runs=2`（`species_ranges` 路径有去重，两处不一致） | 实测 |
| P3-3 | `rca_core/aggregate.py` 数值合并 | 合并后数值字段变字符串：`columns[].index` 由 `0/1` 变成 `"0"/"1"`，`range_top_idx` 同理。当前消费者都能容忍，但属于类型漂移 | 实测 |
| P3-4 | `rca_core/extractor.py:1350` | `warnings` / `_warnings` 两套键名并存（见 P1-2），截断标签可能写进 UI 不读的那个键 | 实测 |

---

## 4. 复核后**撤回**的疑点（避免误报记录在案）

以下由并行子审查提出，我逐条验证后判定不成立，不计入缺陷：

| 疑点 | 结论 |
|---|---|
| `to_newick` 遇重复 node id 静默丢节点 | 不成立：会发 `RuntimeWarning` 明确报告重复 id 和不可达节点 |
| `col.occurrenceMode` 等新增导出列缺 i18n key | 不成立：中/英/日三语在 `rca_core/i18n.py` 与 `js/i18n.js` 均存在 |
| 合并 columnar 结果会覆盖 `sections` 的结构化字段 | 不成立：实测 `lithology_blocks` 完整保留 |
| 非 ASCII CSRF 头导致断连 | 不成立：是本机 HTTP 代理导致，清代理后正常返回 403 |
| `group_identity` 对 0/False 抛错 | 函数不存在，无法复现 |

---

## 5. 建议修复顺序

1. **P1-1**（一行 try/except，影响面最大）→ **P1-6**（一行显式 NUL 检查，把测试基线修绿）
2. **P1-2**（`_extracted_any` 跳过元数据键；统一 warnings 键）——这条直接决定「截断结果会不会被当成正常结果」
3. **P1-3 / P1-4**（数据正确性：缺失被当成不存在；列错位）
4. **P1-5**（换图清结果）
5. P2 五项，其中 P2-2 需要连带看 extractor 的端点交换启发式，改动要有测试锁定

---

## 6. 限制

- 未跑真实 VLM 调用（无凭证、不花配额），所有提取路径用构造 payload + 真实规范化函数验证。
- 浏览器行为（P1-5）为代码推导，未做 DOM 级复现。
- `gui.py` / `gui_fluent*.py`（约 45 万字符）只做了静态检查（无未定义名、无语法错误），未逐函数审。
