# CORS 代理 / CORS Proxy / CORS プロキシ

**只有当你在应用里看到「网络请求失败 / CORS」错误时才需要这一步。**
*Only needed if you see a "network / CORS" error in the app.*
*アプリで「ネットワーク / CORS」エラーが出た場合のみ必要です。*

---

## 为什么需要？ / Why?

浏览器出于安全策略，只有当目标服务器返回 `Access-Control-Allow-Origin` 响应头时，
才允许网页 JavaScript 读取跨域响应。如果 MiniMax 端点不返回该头，浏览器会拦截响应，
应用就拿不到数据。这个代理是一个**无状态透明转发**：把你的请求原样转发给 MiniMax，
再补上 CORS 头返回。**它不读取、不记录、不存储你的 API Key**——密钥只是随请求头透传。

Browsers only let page JavaScript read a cross-origin response when the server
returns an `Access-Control-Allow-Origin` header. If MiniMax does not, the browser
blocks the response. This proxy is a stateless pass-through that forwards your
request to MiniMax and adds the CORS header. It never reads or stores your API key.

---

## 方案 A：Cloudflare Workers（推荐，免费）

1. 打开 https://dash.cloudflare.com → **Workers & Pages** → **Create Worker**。
2. 把 `cloudflare-worker.js` 的内容整段粘贴进去，点击 **Deploy**。
3. （**强烈建议**）设置共享密钥：
   ```bash
   wrangler secret put PROXY_SHARED_SECRET
   # 输入一个足够长的随机字符串，例如：openssl rand -hex 32
   ```
4. （**强烈建议**）在 Worker 脚本里编辑 `ALLOWED_ORIGINS`，填入你实际部署的网站来源：
   ```js
   const ALLOWED_ORIGINS = [
     'https://yourname.github.io',
     // 'http://localhost:8000',  // 本地调试用
   ];
   ```
5. 复制 Worker 地址，例如 `https://range-proxy.yourname.workers.dev`。
6. 回到应用「API 设置」→「代理地址」，粘贴该地址并保存。

## 方案 B：Deno Deploy（免费）

1. 打开 https://dash.deno.com → 新建 Project。
2. 粘贴 `deno-proxy.js` 内容，部署。
3. （**强烈建议**）在 Project Settings → Environment Variables 设置 `PROXY_SHARED_SECRET`。
4. 编辑 `ALLOWED_ORIGINS`，填入实际网站来源（参见方案 A 第 4 步）。
5. 复制项目地址，填入应用「代理地址」。

## 方案 C：Vercel Edge Function

将 `cloudflare-worker.js` 的 `fetch` 逻辑适配为 Vercel Edge Function（`export const config = { runtime: 'edge' }` + `export default async function handler(req)`），部署后把地址填入应用。

---

## 工作原理 / How it routes

应用会请求 `<代理地址>/v1/messages`。代理保留该路径，转发到
`https://api.minimaxi.com/anthropic/v1/messages`。
路径白名单只允许 `/v1/messages`——不能被滥用为任意 URL 转发器。

The app calls `<proxyUrl>/v1/messages`; the proxy preserves the path and
forwards to `https://api.minimaxi.com/anthropic/v1/messages`. The path
allowlist restricts forwarding to `/v1/messages` only — it is NOT a generic
URL forwarder.

---

## 安全模型 / Security Model

代理默认是**拒绝所有**请求。必须显式启用下面**至少一种**认证方式：

### 浏览器 Origin 白名单（`ALLOWED_ORIGINS`）

将允许访问代理的网站来源写入 `ALLOWED_ORIGINS` 数组，例如：

```js
const ALLOWED_ORIGINS = ['https://yourname.github.io'];
```

支持精确匹配和通配符 `*`（**仅用于本地测试**，生产环境禁止）。

**重要**：浏览器发来的 `Origin` 头可以被任何非浏览器客户端（curl 等）任意伪造，
因此 Origin 白名单**单独使用不足以保护生产部署**——任何人只要发现 Worker 地址，
就可以用 `curl -H "Origin: https://yourname.github.io"` 借用你的代理。

### 共享密钥（`PROXY_SHARED_SECRET`）—— 强烈建议

设置环境变量 `PROXY_SHARED_SECRET`（Cloudflare: `wrangler secret put`，Deno:
Project Settings → Environment Variables）。请求必须携带 `X-Proxy-Key: <密钥>` 头才能通过。

密钥比较是**常数时间**的，防止通过时序分析推断密钥前缀/长度。

**为什么必须设置**：浏览器无法读取用户存储里的任意密钥，
但前端可以在设置面板里让你填入 `X-Proxy-Key` 并随每次请求发送。

### 推荐组合：白名单 + 共享密钥

同时配置两者：浏览器正常 Origin + 自动携带的 `X-Proxy-Key` 通过；
仅伪造 Origin 但无密钥的 curl 请求被拒；
意外暴露给搜索引擎爬虫的 Worker 不会变成公共代理。

### 请求体大小限制 / Body Cap

默认 50 MB。代理**不**信任客户端 `Content-Length` 头——它会流式读取请求体并在
超过上限时立即拒绝（413）。这防止攻击者声明一个虚假的 Content-Length 来诱骗代理
转发超大数据。

### 速率限制 / Rate Limit

每个客户端 IP 在 60 秒内最多 30 次请求。客户端 IP 优先取
`CF-Connecting-IP`（Cloudflare 注入，不可伪造），其次取 **最右** 一个
`X-Forwarded-For`（可信代理追加的那个），而不是最左——因为最左是客户端控制的，
攻击者每请求换一个就能绕过限制。

`_rateMap` 内存上限为 10,000 条目（LRU 淘汰），防止攻击者通过海量不同 IP
耗尽 Worker 内存。

### 响应头过滤 / Response Header Filter

只回显白名单内的上游响应头：`content-type`、`content-length`、
`content-encoding`、`transfer-encoding`、`x-ratelimit-*`、`retry-after`、
`anthropic-ratelimit-*`。其它上游诊断头（`set-cookie`、`x-real-ip`、
`cf-ray`、`x-request-id` 等）一律丢弃，避免泄漏内部信息。

### CORS / Vary: Origin

代理在反射特定 Origin 时始终携带 `Vary: Origin`，防止 CDN 缓存把 origin A
的响应错误地返回给 origin B。

---

## 安全建议（TL;DR） / Security Checklist

- [x] **设置 `PROXY_SHARED_SECRET`**（Cloudflare `wrangler secret put`，Deno env）
- [x] 在 Worker 脚本里填写 `ALLOWED_ORIGINS`（不要留空数组）
- [x] 不要在生产环境把 `ALLOWED_ORIGINS` 写成 `'*'`
- [x] 如果部署到公网，配合 Cloudflare Access / Deno Deploy Access Control 进一步限制
- [x] 监控 Worker 日志中的 4xx/5xx 异常尖峰（异常流量 = 可能被滥用）