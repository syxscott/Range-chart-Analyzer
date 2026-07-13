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
3. 复制 Worker 地址，例如 `https://range-proxy.yourname.workers.dev`。
4. 回到应用「API 设置」→「代理地址」，粘贴该地址并保存。

## 方案 B：Deno Deploy（免费）

1. 打开 https://dash.deno.com → 新建 Project。
2. 粘贴 `deno-proxy.js` 内容，部署。
3. 复制项目地址，填入应用「代理地址」。

## 方案 C：Vercel Edge Function

将 `cloudflare-worker.js` 的 `fetch` 逻辑适配为 Vercel Edge Function（`export const config = { runtime: 'edge' }` + `export default async function handler(req)`），部署后把地址填入应用。

---

## 工作原理 / How it routes

应用会请求 `<代理地址>/v1/messages`。代理保留该路径，转发到
`https://api.minimaxi.com/anthropic/v1/messages`。

The app calls `<proxyUrl>/v1/messages`; the proxy preserves the path and
forwards to `https://api.minimaxi.com/anthropic/v1/messages`.

## 安全建议 / Security

- 默认 `ALLOW_ORIGIN = '*'` 任何人知道地址都能借用你的代理转发（但仍需各自的 API Key）。
  可将其改为你的站点来源（如 `https://yourname.github.io`）以锁定。
- 代理是无状态的，不落盘任何数据。
