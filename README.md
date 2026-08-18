# mc2api

本地运行的 **API 网关 + 号池管理台**。把多个上游账号（`oma_` / `omas_`）收进号池，对外只暴露一组本地客户端 Key（`sk-mc-...`），供 Cursor、Claude Code、或其他 OpenAI / Anthropic 兼容客户端使用。

---

## 一、介绍

### 这是什么

mc2api 在本机提供：

| 能力 | 说明 |
|------|------|
| **本地网关** | `http://127.0.0.1:18095/v1`，兼容 OpenAI Chat Completions 与 Anthropic Messages |
| **号池** | 管理多个上游账号；支持 JSON 导入、Session 签发、Chrome 手动授权 |
| **客户端 API Key** | 与上游 `oma_` 分离；客户端只拿 `sk-mc-...` |
| **调度** | 并发租约 + 低负载优先 + 会话粘性（可按 Key 或 `X-Session-Id`） |
| **代理出口** | 全局 HTTP(S) / SOCKS 代理，用于上游转发与 Session 签发 |
| **管理台** | 浏览器 UI：概览 / 网关 & Key / 号池 / 代理出口 / 请求日志 |

流量路径：

```text
客户端 (Cursor / Claude Code / …)
    │  sk-mc-... + Base URL
    ▼
mc2api :18095/v1
    │  选号、签名、粘性、出口代理
    ▼
上游 (proxy.monkeycode-ai.net 等)
```

### 适合谁

- 自己有多个上游账号，想在本地统一调度
- 希望客户端配置简单（一个 Base URL + 一个 Key）
- 需要会话粘性、并发限制、失败冷却、出口代理

### 不适合什么

- 不是上游平台官方产品，也不替代官方客户端
- 默认只监听本机 `127.0.0.1`，不是多用户公网 SaaS
- 不提供破解、盗号、绕过官方计费或服务条款的能力

---

## 二、使用教程

### 1. 环境要求

- macOS / Linux（Windows 可用 WSL 或自行用 `python3 server.py`）
- Python **3.9+**（系统自带即可）
- 可选：`curl`（启动脚本健康检查）、浏览器（管理台）
- 若使用 **SOCKS** 代理：`pip install PySocks`

### 2. 一键启动

#### Windows

1. 安装 [Python 3.9+](https://www.python.org/downloads/windows/)，安装时勾选 **Add python.exe to PATH**
2. 若从 GitHub 下载的 ZIP，先解除锁定（否则可能弹「Internet 安全设置阻止打开」）：
   - 双击 `unblock.bat`，或
   - 右键 `start.bat` → 属性 → 勾选 **解除锁定** → 确定
3. 启动：
   - **推荐**：双击 `start.bat`（启动并打开管理台）
   - 或在 **cmd** 中：`start.bat`
   - 或在 Git Bash 中：

```bash
bash ./start.sh start --open
```

> 不要用 `sh ./start.sh`。管理台：http://127.0.0.1:18095/admin  
> 若 `start.bat` 一闪乱码/报「不是内部或外部命令」，多半是 ZIP 未解除锁定或未装 Python。

#### macOS / Linux

在项目目录下：

```bash
cd /path/to/mc2api
chmod +x start.sh 一键启动.command   # 首次
./start.sh start --open              # 后台启动并打开管理台
```

**macOS 双击**：在 Finder 中双击 `一键启动.command`（若提示无法打开，右键 → 打开，或先执行上面的 `chmod +x`）。

常用命令：

```bash
./start.sh              # 后台启动（默认）
./start.sh start --open # 启动并打开管理台
./start.sh stop         # 停止
./start.sh restart      # 重启
./start.sh status       # 状态 / 地址
./start.sh logs         # 跟踪日志
./start.sh fg           # 前台运行（调试）
./start.sh open         # 仅打开管理台
```

启动成功后：

| 入口 | 地址 |
|------|------|
| 管理台 | http://127.0.0.1:18095/admin |
| 网关 | http://127.0.0.1:18095/v1 |
| 健康检查 | http://127.0.0.1:18095/healthz |
| 默认客户端 Key | `data/default_client_key.txt`（首次启动自动生成） |
| 日志 | `data/server.log` |
| 数据库 | `data/console.db` |

> 管理台默认绑定本机，**无需 Admin Token**。请勿把端口暴露到公网。

### 3. 准备号池（上游账号）

打开管理台 → **号池**，任选一种方式入库：

#### 方式 A：JSON 导入

粘贴单个对象或数组，例如：

```json
{
  "api_key": "oma_xxxxxxxx",
  "signing_secret": "omas_xxxxxxxx",
  "email": "you@example.com",
  "label": "主号"
}
```

点击 **导入 JSON**。

#### 方式 B：Session 签发

1. 浏览器登录 [monkeycode-ai.com](https://monkeycode-ai.com)（兼容 .net）
2. 取出 Cookie 中的 `monkeycode_ai_session`（或完整 Cookie 字符串）
3. 粘贴到输入框 → **Session 签发入库**

#### 方式 C：手动授权（Chrome）

点击 **手动授权（新 Chrome Profile）**，在弹出的独立 Chrome 中完成登录；成功后自动签发并入库，临时 Profile 会清理。

入库后可在表格中：启用/停用、清冷却、改最大并发、删除。

### 4. 生成客户端 API Key

打开 **网关 & API Key**：

1. 填写名称（如 `cursor`、`claude-code`）
2. 点击 **生成 Key**
3. 复制 `sk-mc-...`（完整 Token 仅在此可见/可复制）

客户端使用该 Key 访问本地网关，**不要**把上游 `oma_` 填进客户端。

### 5. 客户端怎么填

| 配置项 | 值 |
|--------|-----|
| **Base URL** | `http://127.0.0.1:18095/v1` |
| **API Key** | `sk-mc-...`（管理台生成） |
| **模型** | 见下方模型表 |

支持的网关接口：

- `POST /v1/chat/completions` — OpenAI 兼容
- `POST /v1/messages` — Anthropic 兼容
- `GET /v1/models` — 模型列表

#### 示例：curl（Anthropic Messages）

```bash
KEY="$(tr -d '[:space:]' < data/default_client_key.txt)"

curl -s http://127.0.0.1:18095/v1/messages \
  -H "Authorization: Bearer $KEY" \
  -H "x-api-key: $KEY" \
  -H "Content-Type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "deepseek-v4-flash",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

#### 示例：curl（OpenAI Chat）

```bash
curl -s http://127.0.0.1:18095/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-v4-flash",
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

### 6. 模型别名（常用）

客户端可写短名，mc2api 会映射到上游模型：

| 客户端可写 | 实际上游 |
|------------|----------|
| `deepseek-v4-flash` | `monkeycode-basic/deepseek-v4-flash` |
| `deepseek-v4-pro` | `monkeycode-pro/deepseek-v4-pro` |
| `qwen3.5-plus` / `qwen3.5` | `monkeycode-basic/qwen3.5-plus` |
| `glm-5.2` | `monkeycode-pro/glm-5.2` |
| `gpt-5.4` / `gpt-5.5` | `monkeycode-ultra/gpt-5.4` 等 |
| `claude-haiku-4-5` / `claude-sonnet-4-6` | 映射到 basic/flash 线路 |
| `claude-opus-4-6` / `claude-opus-4-7` | 映射到 pro 线路 |

也可直接写上游全名，如 `monkeycode-basic/deepseek-v4-flash`。

> 未在别名表中的模型名会原样转发；若上游不认，可能返回 403 等错误。

### 7. 调度与粘性会话

调度策略（与常见多账号网关类似）：

1. **并发租约**：每账号 `max_concurrent`（默认 3），满了可等待（默认最多约 30s）
2. **低负载优先**：在途请求少、优先级高的账号优先
3. **会话粘性**：
   - 默认按 **客户端 Key** 粘住同一上游账号
   - 若请求带 `X-Session-Id`（或 `X-Client-Session`），则按该会话 ID 粘性
4. **失败冷却**：上游 401/403/429/5xx 等会进入冷却；粘性账号不可用时会 **重绑** 到可用账号
5. **鉴权类失败**（401/402/403/429）会清理该账号上的粘性绑定

多轮对话建议固定同一个客户端 Key，或始终传同一个 `X-Session-Id`。

### 8. 代理出口

打开管理台 → **代理出口**：

1. 勾选 **启用代理出口**
2. 填写代理 URL，例如：
   - `http://127.0.0.1:7890`
   - `http://user:pass@host:port`
   - `socks5://127.0.0.1:1080`（需 `pip install PySocks`）
3. **保存**（热更新，无需重启）
4. 可用 **测试连通** 查看出口 IP 与上游是否可达

生效范围：

- 网关转发上游
- Session 签发 / mint

不走代理：本机管理台、Chrome 手动授权与 CDP。

配置落盘：`data/proxy.json`。

### 9. 环境变量（可选）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MC_CONSOLE_HOST` | `127.0.0.1` | 监听地址 |
| `MC_CONSOLE_PORT` | `18095` | 端口 |
| `MC_CONSOLE_DATA` | `./data` | 数据目录 |
| `MC_CONSOLE_UPSTREAM` | `https://proxy.monkeycode-ai.net/v1` | 默认上游 |
| `MC_CONSOLE_WEB` | `https://monkeycode-ai.com` | 控制台 Web（签发/手动授权，自动回退 .net） |
| `MC_CONSOLE_TIMEOUT` | `300` | 上游超时（秒） |
| `MC_CONSOLE_MAX_CONCURRENT` | `3` | 账号默认最大并发 |
| `MC_CONSOLE_STICKY_TTL` | `1800` | 粘性 TTL（秒） |
| `MC_CONSOLE_CAPACITY_WAIT` | `30` | 并发满时等待秒数 |
| `MC_CONSOLE_COOLDOWN_BASE` | `30` | 冷却基数（秒） |
| `MC_CONSOLE_COOLDOWN_MAX` | `600` | 冷却上限（秒） |

示例：

```bash
MC_CONSOLE_PORT=18096 ./start.sh restart --open
```

### 10. 数据与隐私（本地）

均在 `data/` 下（已被 `.gitignore` 忽略）：

| 路径 | 内容 |
|------|------|
| `console.db` | 账号、客户端 Key、请求日志 |
| `proxy.json` | 代理出口配置 |
| `server.log` / `server.pid` | 日志与进程号 |
| `default_client_key.txt` | 首次自动生成的默认 Key |
| `chrome-auth/` | 手动授权临时目录 |

请自行保管 `oma_` / `omas_` / `sk-mc-` / 代理密码，勿提交到 Git 或发到公开渠道。

---

## 三、免责声明

1. **仅供学习**  
   本项目**仅供个人学习、研究与技术交流**使用，不得用于任何商业用途或生产环境。下载、运行即视为你已理解并同意本声明全部条款。

2. **非官方**  
   本项目为第三方本地工具，与上游平台 / ohmyagent / 相关官方实体无隶属、无授权背书关系。名称与接口仅用于描述对接目标。

3. **合规使用**  
   你应确保对所用账号、Cookie、API Key、代理拥有合法使用权，并遵守上游平台的服务条款、当地法律法规。禁止将本工具用于盗号、撞库、滥用、欺诈、攻击或其他违法违规用途。

4. **风险自担**  
   使用本软件产生的账号封禁、额度损失、数据泄露、服务中断、法律纠纷等后果，由使用者自行承担。作者与贡献者在法律允许的最大范围内不承担责任。

5. **无担保**  
   软件按「现状」提供，不提供任何明示或默示担保，包括但不限于适销性、特定用途适用性、不侵权、可用性或准确性。上游接口、模型映射、签名方式可能随时变更，本工具可能因此失效。

6. **安全提示**  
   - 默认无公网鉴权的管理台，仅建议本机使用  
   - 若修改 `MC_CONSOLE_HOST` 为 `0.0.0.0` 或做端口转发，等于把号池与 Key 暴露给网络，风险自负  
   - 日志可能包含模型名、错误信息等，请注意存放位置  

7. **开源与修改**  
   你可以自行修改、学习本项目代码；对外再分发时，请勿进行误导性的「官方」宣传，并保留本免责声明。

---

## 四、目录结构（简要）

```text
mc2api/
├── start.sh              # 启停脚本
├── 一键启动.command       # macOS 双击启动
├── server.py             # 网关 + 管理 API + 调度
├── chrome_auth.py        # Chrome 手动授权
├── static/admin.html     # 管理台 UI
├── data/                 # 本地数据（勿提交）
└── README.md
```

---

## 五、常见问题

**Q: 启动后客户端 403 / Forbidden？**  
A: 多半是模型名不被上游接受。请改用别名表中的短名（如 `deepseek-v4-flash`），并确认号池账号未冷却、未停用。

**Q: 503 没有可用账号？**  
A: 号池为空，或账号全在冷却。到管理台清冷却 / 启用账号 / 重新入库。

**Q: 粘性怎么验证？**  
A: 同一 `sk-mc` 连续请求，或固定 `X-Session-Id`，在「请求日志」中看是否落到同一上游账号。

**Q: 如何完全退出？**  
A: `./start.sh stop`。仅关闭终端或「一键启动」窗口不会停掉后台进程。

---

如有问题，先看 `data/server.log` 与管理台「请求日志」。
