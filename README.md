# Range Chart Analyzer

从**地层沿线图（stratigraphic range chart / 种属延限图）**中提取结构化数据的工具，
提供**三种使用方式**，共用同一套提取核心：

1. **桌面 GUI**（Python / Tkinter）——本地程序，无 CORS 问题。
2. **Web + 本地后端**（Python）——浏览器界面，后端代发请求，**彻底解决 CORS**。
3. **纯前端 Web**（可直连 / 走代理）——零后端的静态站（受浏览器 CORS 限制）。

用户提供自己的 MiniMax API Key，上传一张图表，MiniMax M3 视觉模型识读后以表格展示，
支持复制与下载。**界面支持中/英/日三语，图表可识别中/英/日/俄四种语言。**

> 提取逻辑移植自 [RLPE](https://github.com/syxscott/RLPE-Radiolarian-Plate-Extractor)
> 项目的 range-chart 视觉抽取模块。

---

## 核心特性

- **多种 LLM 提供商**：内置 100+ 预设（MiniMax、OpenAI、Claude、Gemini、DeepSeek、Qwen、Kimi、智谱、各类中转聚合），统一配置 + 切换。
- **可编辑结果**：表格可在线编辑（双击单元格），可新增 / 删除行，所见即所得。
- **多格式导出**：JSON、CSV、TSV、**Excel (XLSX)**（每个表一个 Sheet，物名斜体）。
- **历史记录**：所有提取自动保存到 SQLite，支持加载、编辑备注、重新导出、删除。
- **Token 用量统计**：每次调用的输入/输出 Tokens、缓存命中率、延迟、成功率，按天/提供商/模型聚合可视化。
- **柱状对比图模式**：除种属延限图外，还支持柱状对比图（columnar section）的结构化提取。
- **三语 UI**（中/英/日），三种启动方式（GUI / Web+后端 / 纯前端 Web）。

---

## 为什么有后端？（CORS）

浏览器直连 MiniMax 时，若对方未返回 `Access-Control-Allow-Origin` 响应头，
浏览器会拦截响应（你会看到「网络请求失败 / CORS」）。**桌面 GUI 和本地后端方案
从服务端用 `urllib` 发起请求，不受浏览器 CORS 限制，因此推荐使用。**

---

## 一键启动（最快）

```bash
python main.py           # 现代 Fluent GUI（默认，未装 PySide6 时自动回退 Tkinter）
python main.py gui       # 强制经典 Tkinter GUI
python main.py server    # 启动 Web + 本地后端，并自动打开浏览器
```

Windows 直接双击 `run.bat` 即可启动（默认现代 Fluent GUI）。

现代 GUI 需要 `pip install PySide6 PySide6-Fluent-Widgets`（可选，~150MB）；
未安装时 `python main.py` 会自动回退到零依赖的 Tkinter 界面。

`python main.py server` 可选参数：`--port 8080`、`--host 0.0.0.0`、`--no-browser`。

---

## 方式一：桌面 GUI（最简单，推荐）

```bash
# 可选：安装 Pillow 以显示图片缩略图（不装也能用）
pip install -r requirements.txt

python gui.py
# Windows 也可直接双击 run_gui.bat
```

- 左侧填 API Key、选图片；点「开始提取」。
- 右侧四个标签页展示结果，每个表可「复制 (TSV)」或「导出 CSV」，也可「导出全部 JSON」。
- 右上角切换中/英/日界面。
- 设置自动保存到 `~/.range_chart_analyzer.json`（勾选「记住 API Key」才存密钥）。

**依赖**：Python 3.9+（自带 tkinter）。Pillow 可选。**无需任何其他第三方库。**

---

## 方式二：Web + 本地后端（浏览器界面 + 无 CORS）

```bash
python server.py --port 8000
# Windows 也可直接双击 run_server.bat（会自动打开浏览器）
```

浏览器访问 `http://127.0.0.1:8000/`。在「连接模式」保持「自动」即可——
页面由本地服务器打开时会自动走后端 `/api/extract`，浏览器不再直连 MiniMax，
**CORS 问题消失**。其余用法与纯前端一致。

---

## 方式二点五：Modern UI（PyWebView 原生窗口，可选）

如果你想要比浏览器标签页更"原生"的体验，但不想用 Tkinter，可以装上 PyWebView
把现有 Web 前端嵌进一个原生窗口：

```bash
pip install pywebview
python main.py --ui modern    # 等价于 python main.py modern
```

- 共用方式二同一套后端、API Key、配置与多语言；`gui.py` 不受影响。
- 如果没装 pywebview，或当前系统没有可用的 WebView 引擎（Linux 缺
  `webkit2gtk-4.0`、Windows 缺 Edge WebView2），程序会打印提示并自动
  在默认浏览器里打开 `http://127.0.0.1:<port>/`，退出码仍为 0。
- 启动时会显示 `app/loading.html` 旋转加载页，后端就绪后自动跳到主页。

---

## 方式三：纯前端静态站（无后端）

直接双击 `index.html`，或用任意静态服务器托管 `index.html` + `css/` + `js/`。
此模式下浏览器直连 MiniMax，**可能被 CORS 拦截**；若被拦截：

- 在「连接模式」选「浏览器直连 / 代理」，并在「代理地址」填入你自建的代理
  （见 [`proxy/README.md`](proxy/README.md)，含 Cloudflare / Deno / Vercel 方案）。
- 或改用方式一 / 方式二（更省事）。

---

## 提高准确度的建议 / Tips for better accuracy

视觉模型识读密排斜体拉丁学名时可能出现 OCR 误读。以下措施可显著提升准确度：

1. **上传高清原图**。截图越清晰、分辨率越高，小字识读越准。避免上传被压糊的小截图。
2. **调高「图像分辨率上限」**（默认 4000px）。工具默认用无损 PNG 压缩以保留小字细节；分辨率越高识读越准，但请求体越大。设 0 则完全不压缩。
3. **开启「运行次数」多次运行**（如 3 次）。同一张图跑多次并按"众数"合并，结果表会显示每行的**一致性**（如 3/3、1/3）；低一致性行以警告色标注，提示你人工核对——这能有效压制单次随机误读。
4. **在「图注/备注」框粘贴物种名列表**（若有原文），帮助模型校正学名拼写。
5. **人工核对**低置信度与低一致性的行，尤其是种加词与延限层位。
6. 注意区分**年代阶(Stage，如"吴家坪阶")**与**岩石组(Formation，如"大隆组")**，以及**菊石/生物带**列——本工具的 Prompt 已强化区分，但仍建议复核。

Upload high-resolution originals, raise the image-resolution cap, and enable
multiple runs (the agreement column flags rows that need review) to minimize
OCR misreads on dense italic species names.

---

## 应用图标 / Logo

GUI 使用 `rca_core/logo.py` 用 Pillow 生成窗口图标（无外部资源依赖）：
左侧深海军蓝地层柱 + 右侧翠绿延限线，象征 range chart 的核心概念。
多分辨率 ICO（16/24/32/48/64/128/256）覆盖 Windows 任务栏与窗口装饰位。
重新生成：`python -m rca_core.logo`。

The GUI uses a `rca_core/logo.py` Pillow-generated window icon (no external
assets): navy stratigraphic column + emerald range lines on the right.
Regenerate with `python -m rca_core.logo`.

---

## 提取字段 / Extracted fields

| 表 Table | 字段 Fields |
|---|---|
| Sections 地层剖面 | name, age_range, formations, formation_thickness_m, coordinates |
| Species Ranges 种属延限 | species, section, range_base (老), range_top (新), biozone |
| Biozones 生物带 | name, age, thickness_m |
| Other Fossils 其他化石 | free-text 记录 |

外加整体 `confidence`（0~1 置信度）。所有语言的图表统一输出**标准拉丁学名**与英文地质字段。

---

## 项目结构 / Layout

```
Range-chart Analyzer/
├── gui.py                  # 方式一：Tkinter 桌面 GUI
├── server.py               # 方式二：静态托管 + /api/extract 后端
├── run_gui.bat             # Windows GUI 启动器
├── run_server.bat          # Windows 服务器启动器
├── requirements.txt        # 仅 Pillow（可选）
├── tests_core.py           # 核心单元测试（169 项）
├── tests_logo.py           # logo 生成测试（9 项）
├── tests/                  # 新增单元测试（test_csrf / test_ssrf / test_parity / test_enhance / test_cache / test_quality）
├── rca_core/logo.py        # 窗口 logo 生成器（Pillow）
├── assets/
│   ├── logo.png            # 256x256 主图
│   └── logo.ico            # Windows 多分辨率图标（16/24/32/48/64/128/256）
├── rca_core/               # 三端共用的 Python 核心
│   ├── prompt.py           #   系统提示（四语图表 + 拉丁学名）
│   ├── json_utils.py       #   稳健 JSON 解析（括号平衡扫描）
│   ├── extractor.py        #   MiniMax 调用（urllib）+ 图片压缩 + 归一化
│   ├── exporter.py         #   表配置 + CSV/TSV/JSON 导出
│   └── i18n.py             #   GUI 中/英/日 翻译
├── index.html              # 方式三：纯前端页面
├── css/style.css
├── js/                     # 纯前端逻辑（config/i18n/prompt/json-utils/
│                           #   minimax/table/export/app）
└── proxy/                  # 可选 CORS 代理（Cloudflare / Deno / 说明）
```

三端共享：`rca_core`（Python，GUI + 后端）与 `js/`（浏览器）实现同一套提取契约
（相同的 system prompt、相同的 JSON 容错解析、相同的结果字段）。

---

## 安全提示 / Security

- API Key 存放在你的设备：GUI 存于 `~/.range_chart_analyzer.json`（可关闭），
  Web 存于浏览器 `localStorage`（可关闭）。**请勿在公用电脑上勾选「记住」。**
- 图片与请求由你的设备直接发往 MiniMax（GUI/后端）或你自建的代理，本项目不上传你的数据到第三方。
- `server.py` 默认仅监听 `127.0.0.1`（本机），不对外暴露。

---

## 测试 / Tests

```bash
python tests_core.py     # 26 项：JSON 解析 / 归一化 / 导出 / i18n 键对齐
```

---

## 许可 / License

沿用来源项目 RLPE 的许可条款。仅供科研与教育用途。
