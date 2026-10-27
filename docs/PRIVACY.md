# 隐私与数据边界

## 三种数据，三种待遇

| 数据 | 位置 | 处理 |
| --- | --- | --- |
| 公开 demo | `demo/trace.demo.js`（随仓库发布） | **完整脱敏**：`.env` 值、key 形状、私钥块、Bearer 头、邮箱、家目录用户名 |
| 你自己生成的 `trace.json` | 本地 | 不自动脱敏；`--redact` 是一个**显式**选择 |
| 别人拖进网页的 `trace.json` | 只在浏览器内存里 | 不上传、不落盘、不发请求；关掉页面即消失 |

## 公开 demo 的脱敏做了什么

`htrace/redact.py` 两遍扫描，作用在整个 trace 文档的所有字符串上：

1. **值级**：读 `$HERMES_HOME/.env`（以及多 profile 各自的 `.env`），把长度 ≥ 8 的
   每个值逐字替换成 `<redacted>`。这是最硬的一层——即使正则没认出 key 的形状，
   真实密钥也不会漏。
2. **形状级**：
   - `sk-…` / `sk-ant-…` / `ghp_`、`github_pat_` / `AKIA…` / Telegram bot token
   - `Authorization: Bearer …`、私钥块（`-----BEGIN … PRIVATE KEY-----`）
   - `api_key=…`、`token=…`、`password=…` 之类的赋值
   - 邮箱
   - 家目录：`C:\Users\<name>`、`C:/Users/<name>`、`/c/Users/<name>`、`/home/<name>`、
     `/Users/<name>`，**含 JSON 里多层转义的形式**（`C:\\Users\\…`），以及裸登录名
     （`ls -la` 的属主列、进程列表里会出现）

每次构建把统计写进 `source.redaction_stats`，演示页的总览里也照实显示，例如：

```json
{"username": 558, "home_path": 798, "secret_assign": 9, "email": 3}
```

构建后自检（发布前必跑）：

```bash
grep -oE "<你的用户名>|sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}" demo/trace.demo.js | sort | uniq -c
# 期望：无输出
```

## 明确不做的事

- 不做 MITM，不装根证书，不代理任何流量。
- 不把 trace 上传到任何服务；`htrace serve` 只监听 `127.0.0.1`。
- 不在 hook 里记录 stdout（stdout 在阻塞事件上是 Hermes 的指令通道，写东西会改变行为）。
- 不替用户自动脱敏他们自己的 trace，因为那会制造「以为安全」的错觉；只给显式的
  `--redact` 和一份说明。

## 未来的上传后端（预留，未实现）

`htrace serve` 已经预留 `GET /api/trace`；前端在拿不到内置 demo 时会尝试这个端点。
若以后要接「用户上传 state.db 到服务器解析」，必须先补：鉴权、按用户隔离的存储、
TTL 清理、上传前的显式同意、以及一份能说清「谁在什么时间读到了什么」的审计记录。
在此之前，静态站 + 本地 CLI 是唯一支持的组合。
