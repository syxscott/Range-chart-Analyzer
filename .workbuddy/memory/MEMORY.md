# 项目长期约定

- 实现类任务默认由主流程独立完成、无需多 Agent 交叉复核；仅当用户明确要求（如 2026-09-01 的全面代码审查）时才组织多 Agent 团队。
- 宣称修复完成前必须以实际测试为依据：至少覆盖相关定向测试、全量 Pytest、独立核心测试、JavaScript 测试、编译/语法检查，并明确报告可选依赖或真实科研验证限制。

## 关键工程事实（供后续提问定位）

- 三端共用 `rca_core`（Fluent GUI / Tkinter GUI / server.py），浏览器 `js/` 是 Python 核心的对等实现（prompt、json_utils、normalize、aggregate、quality 均有 parity 测试锁定，改动必须双端同步）。
- 四提取模式：range_chart / columnar_section / abundance_diagram / phylogenetic_tree；prompt 版本号参与缓存键。
- 测试基线（2026-09-01 Sprint A 后实测）：pytest = 846 收集 / 0 失败 / exit 0（Anaconda python + pytest 8.3.4；shell 默认 python 是 WorkBuddy 托管 3.13.12，无 pytest）；node tests_frontend.js = 287 过；数据不变量锁在 tests/test_ics_invariants.py。
- ✅ P0 已修复（2026-09-01）：ics_2024.json 已按 **ICS v2024/12** 全面重建（98 条目，新增 Jiangshanian/Stage 2-4；约 70 个界线数值修正；Gelasian 已归 Quaternary；P–T 锚点 251.902），三方权威交叉验证（stratigraphy.org + Macrostrat #1 + PBDB），js/ics_table.js 与 ics.py 已双端同步，并补齐数据 invariant 测试。完整报告 docs/CODE_REVIEW_2026-09-01.md，修复记录 docs/FIX-2026-09-01-ICS-data.md。
- ⚠️ 教训（务必记住）：**测试可能锁定过期数据**。原 test_ics_cretaceous.py / test_ics_cenozoic.py 自称引用 ICS v2024/12，实际锁的是 GTS2016 值。任何"权威数据"改动都必须回到 stratigraphy.org 官方图表逐条核对，不能信测试里的自述版本。
- 待办：ICS v2026-06 偏移（Olenekian 底 250.8、Anisian 底 247.0、Wuchiapingian 底 259.857）待独立版本升级采纳；Roadian 底 274.4 在二叠纪分会内部有争议（Permophiles #78），涉瓜德鲁普统底界的研究需标注采用版本。Sprint B（P1 14 项，含 aggregate.py L586-604 species 限定词大小写 bug、提取流水线截断/缓存键、server 长任务阻塞、JS quality 评分漂移）。
- 数据目录：~/.range_chart_analyzer/（providers.json / rca.db / extract_cache.sqlite）。
- 领域数据权威源：rca_core/resources/ics_2024.json（与 js/ics_table.js 必须一致）。

## 2026-10-17 UI 审查修复轮后的基线（最新）

- 测试基线（本轮实测）：pytest ≈3966 收集，唯一失败 `test_non_ascii_csrf_token_rejected_cleanly` = **本机 socket 环境性偶发**（HEAD stash 2 次验证同样失败、文件级 8/8 全过），勿再排查；JS 13 套件全绿。i18n：py 445×3、js 323×3。
- **re-export 契约不可按"未使用"清理**：gui.py 的 `_ALLOW_PRIVATE`/`validate_endpoint`/`_is_private_host` 被 test_gui_fluent_ssrf.py:64 断言 `gui._validate_endpoint is ssrf.validate_endpoint`。
- **修掉 pinned difference 必须同步改钉它的测试**：X-09（rcaToCsv 尾行 CRLF）就曾让 test_export_render_parity.py 的 ENV BROKEN canonical 断言与 pinned 测试全红。改导出字节级行为时先 grep tests/ 里的 "pinned"。
- `--success-color` 现为 #059669（emerald-600，WCAG 1.4.11 达标）；css 全部字号已 rem 化（html{font-size:100%} 锚点），新增 font-size 禁止用 px。
- WEB-04（大表虚拟滚动）**已于 2026-10-18 做掉**：`content-visibility: auto` 加在 `<tr>`（**绝不可加 `<td>`**——被包含的 cell 不再参与列首选宽度，这才是"列宽错位"的真因；此前审查因此挂起的理由是错的）。实测列宽漂移 0px、打印 5000/5000 完整。阈值 `RCA_VIRTUAL_ROW_THRESHOLD=600`（小表不加，避免凭空开销）、固定行高只作用于大表（否则裁切 `other_fossils` 的 `cell-wrapping` 长文本）。**性能收益未能在本机复现**（成员报 3.3×，我测 1.0×），属"安全但收益待验"。
- PySide6 本机未装：Fluent 端改动只能静态验证 + 内核通路实测（tests/test_gui_fluent_ssrf.py 会 skip）。
- CI：ci.yml 需显式装 numpy（detect_grid 静默依赖，except 会吞 ImportError）。
- **新加 UI 控件三件套**：`__init__` 构造、`retranslate()` 文案、`_update_result_actions()` 门控，缺一就是切语言旧文案/零结果死按钮。自查轮（10-18）曾连漏两处。
- **写 CSV 文件必须剥内核 to_csv 的内联 BOM 再用 utf-8-sig**（双 BOM 会让 Excel 首格乱码）；Tk `_export_csv` 有现成 strip 代码可参照。
- **两端方法不同名**：Tk `gui._resolved_mode()`（普通方法）vs Fluent `_current_mode()`；跨端照抄前先核对目标文件的真名。
- **前后端错误键契约**：js/minimax.js 的 `RCA_KNOWN_ERROR_KEYS` 白名单 + js/i18n.js 三语字典是 server `err.*` 键的消费闸口；server 新增错误键必须三处同步（2026-10-18 曾漏 `err.methodNotAllowed`，已补）。已知留档：`RCA_PROVENANCE_TOKEN` 部署场景下 Fluent 溯源导出缺 `X-Provenance-Token` 头会 403（本地 loopback 不受影响，未修）。
- **浏览器验证能力（2026-10-18 新发现，优先用它）**：仓库有 `playwright` 依赖但浏览器二进制**未下载**；用 `chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'})` + `NODE_PATH=<repo>/node_modules` 即可跑真实 Blink 验证（脚本放系统临时目录）。这比 node vm 桩强得多，UI 改动优先用它。
- **🔴 Qt 线程模型铁律（2026-10-18 实测踩坑）**：**QTimer 由所属线程的事件循环驱动**。在 `QThread.run()` 里创建的 QTimer **永不触发**（该线程从不 exec），无论 start/stop/finally 做得多正确——单测用 stub timer 全绿，心跳实为 no-op。实测证据固化在 `tests/qt_thread_probe.py`（worker 0 tick vs GUI 5 tick）。**凡要在长任务里做周期性 UI 刷新，timer 必须挂在 GUI 线程。**
- **stub 测试的结构性盲区（三次同因）**：stub 只能验证契约，不能验证运行环境。①测不出虚拟化/真实布局 ②测不出 Qt 线程模型与事件循环 ③测不出性能。**凡涉及线程/事件循环/真实布局/耗时，必须用真实运行时**——本机可用：Chrome（`chromium.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'})`）+ PyQt6 6.7.1（`QT_QPA_PLATFORM=offscreen`）。
- **给成员派活时别指定错误的验证手段**：我曾让成员"AST 抽真实源码跑"来验证心跳——那只测代码结构，测不到运行时行为，**验证手段选错时成员越认真越容易给出错误的安全感**。
- **PyQt 信号两个陷阱**：① 接收者若是临时对象（`w.connect(Page().on_x)`）PyQt 只弱引用，**被 GC 后槽静默不执行**——表象与"业务失败"无法区分，测试必须显式断言"槽被调用了"。② 同一 worker 发出的多个信号按 **FIFO** 入队（实测 `['done','finished']`），清理逻辑可依赖此顺序。
- **测性能必须测完整管线，不是子步骤**：2026-10-18 三次翻车——① 成员报的 149ms 只是 QTableWidget 填充，完整管线（+首次布局）实测只有 50ms，据此裁决"model 化不做"；② `resizeColumnsToContents()` 实测**恒定 ~10ms 与行数无关**（Qt 有缓存），"大表下它才是瓶颈"的前提不成立；③ 我第一版浏览器脚本用 `display:none` 往返测重排，没触发真实跳过路径，测出 1.0× 的假阴性。
- **打包（PyInstaller 6.22 实测，2026-10-18 发布 0.1.2）**：spec 里 UPX 需**三处同时设**才生效——`EXE(upx=True)`、`CONF['upx_available']=True`、`CONF['upx_dir']`（api.py:482/:1106/:663，onefile 走内嵌 PKG）。①对成品 archive 内层 UPX 是死路（PyInstaller 6 已 zlib 压缩 4334/4335 条目，双重压缩近零收益）；②`Analysis(upx_dir=, upx_exclude=)` **静默接受但完全无效**（日志 0 行、体积不变，别被骗）。排除：Qt6*/QtWebEngine/python3*.dll/MS 运行时/OpenSSL。成果 258.8→234.7MB（-9.3%）。**发布前必跑冒烟**：console 版 `server --port N --no-browser` + curl `/health`，windowed 版起 20s 查进程存活。
- **"需在有 X 的机器上验证"之前，先查 CI 有没有那个环境**：2026-10-18 两个成员都标注"心跳/XLSX 需真 PySide6 机器确认"，而 CI 的 `qt` job 恰好就是（offscreen + 真 PySide6，`-k "gui or fluent"` 跑到 100%）。**先看 CI 再下结论**。
- **CI 可移植性**：测试里禁止硬编码本机路径（曾硬编码某开发者 node 路径导致 CI FileNotFoundError）。用 `shutil.which` → 标准位置 → `pytest.skip(原因)`。
