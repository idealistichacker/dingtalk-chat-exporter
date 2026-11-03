# DingTalk Chat Exporter

<!-- topics: dingtalk, chat-exporter, message-export, sqlite, data-backup, python, dingtalk-v3, offline-tool -->
钉钉桌面端聊天记录导出工具，支持 V2/V3 加密数据库、多账号切换、消息搜索、附件导出和离线运行。Export DingTalk desktop chat history from local V2/V3 encrypted databases. Supports multi-account switching, message search, attachments, JSON/ZIP export, and offline use.

[中文](#钉钉聊天记录导出工具) | [English](#dingtalk-chat-exporter-1)

## 钉钉聊天记录导出工具

从钉钉桌面客户端的本地加密数据库中读取、搜索和导出聊天记录。项目在本机运行，不调用钉钉开放平台 API，也不上传聊天内容。

> 本项目仅用于个人数据备份和研究。使用前请确认符合所在组织的数据安全政策、隐私法规以及钉钉服务条款。

## 功能

- 支持钉钉桌面端 V2 和 V3 加密 SQLite 数据库
- 自动发现本机登录过的多个钉钉账号，并在网页顶部切换账号
- 浏览、搜索、分页查看会话和消息
- 支持文本、图片、文件、语音、富文本、引用、审批、互动卡片等消息类型
- 预览和导出本地缓存的图片、文档及其他附件
- 按会话勾选导出，可按全部、近 3 个月、近 6 个月、近 1 年或近 2 年筛选
- 导出为自包含 JSON 目录，并支持 ZIP 下载
- JSON 提供统一的 `content` 字段，便于后续检索或交给 AI 工具处理
- 每 4 小时自动同步，也可以在网页中手动同步
- 运行阶段完全离线，数据只写入本地 `data/` 和 `logs/`

## 版本支持

| 数据库目录 | 解密方式 | 额外要求 |
| --- | --- | --- |
| `*_v2` | 调用 [dingwave](https://github.com/p1g3/dingwave) | 将对应平台的二进制文件放入 `tools/` |
| `*_v3` | 内置 PBKDF2 + AES-ECB 解密，并处理 SQLite WAL | `cryptography` 依赖；通常需要本地钉钉日志中的真实 UID |

V3 目录名通常是账号哈希，不一定是真实 UID。程序会扫描钉钉日志中的 `real_uid`、`myOpenId` 等字段，并缓存成功匹配结果。如果历史日志已被清理，可以通过 `DINGTALK_UID` 手动提供真实 UID。

## 快速开始

### 环境要求

- Windows、macOS 或 Linux
- Python 3.10 或更高版本
- 钉钉桌面客户端已安装并登录过，且本机存在聊天数据库
- 导出 V2 数据库时需要 dingwave；V3 不需要下载 dingwave

### 安装

```bash
git clone https://github.com/abbr530/dingtalk-exporter.git
cd dingtalk-exporter
python -m pip install -r requirements.txt
```

Windows 可以直接运行 `setup.bat`，Linux/macOS 可以运行：

```bash
chmod +x setup.sh
./setup.sh
```

如果本机有 V2 账号，从 dingwave Releases 下载对应平台的文件，并放入 `tools/`：

```text
tools/dingwave.exe    # Windows
tools/dingwave        # Linux/macOS
```

### 启动

```bash
python main.py
```

浏览器打开 <http://localhost:8090>。程序会自动扫描 `%APPDATA%\DingTalk\`、`%LOCALAPPDATA%\DingTalk\` 以及 macOS/Linux 的常见数据目录。

## 使用

1. 在顶部账号下拉框选择要查看的钉钉账号。
2. 在左侧浏览、搜索或按群聊/单聊筛选会话。
3. 点击会话查看消息，图片和已缓存附件可以直接预览。
4. 点击“导出”，勾选会话并选择时间范围，然后开始导出。
5. 在“已导出文件”中下载 JSON 目录或 ZIP 文件。
6. 需要立即更新本地数据时，点击“手动同步”。

账号切换会先解密并校验目标数据库，成功后才原子替换当前解密数据库；失败不会覆盖当前账号的数据。

## 手动配置

自动检测失败时，可以使用环境变量指定账号。Windows PowerShell：

```powershell
$env:DINGTALK_UID = "123456789"
$env:DINGTALK_DATA_DIR = "C:\Users\用户名\AppData\Roaming\DingTalk\123456789_v2"
python main.py
```

Linux/macOS：

```bash
export DINGTALK_UID=123456789
export DINGTALK_DATA_DIR="$HOME/.config/DingTalk/123456789_v2"
python main.py
```

V3 的 `DINGTALK_UID` 必须是真实数字 UID，而不是 `*_v3` 目录名；如果日志仍然存在，通常不需要手动设置。常用配置还包括：

| 变量/配置 | 默认值 | 说明 |
| --- | --- | --- |
| `DINGTALK_UID` | 自动识别 | 当前账号真实 UID |
| `DINGTALK_DATA_DIR` | 自动识别 | `*_v2` 或 `*_v3` 目录完整路径 |
| `WEB_HOST` | `0.0.0.0` | Web 服务监听地址 |
| `WEB_PORT` | `8090` | Web 服务端口 |
| `SYNC_INTERVAL_HOURS` | `4` | 自动同步间隔 |

## 导出格式

每次导出生成一个目录：

```text
data/exports/export_123456789_20260805_120000/
├── export.json
├── images/
└── other/
```

`export.json` 顶层包含 `account_uid`、`export_type`、会话列表等字段。每条消息包含发送者、时间、消息类型、原始文本和统一的 `content` 字段，例如：

```json
{
  "sender_name": "用户A",
  "content": "[图片: images/12345_67890.jpg]\n一些描述文字",
  "image_export_paths": ["images/12345_67890.jpg"]
}
```

未缓存到本地的附件不会被凭空下载，会以占位文本保留在导出结果中。

首次下载某个导出目录时会在 `data/exports/` 中生成 ZIP 缓存；后续重复下载会直接复用缓存，不会再次压缩全部附件。

## 本地 API

启动服务后可使用以下接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/api/accounts` | 列出本机账号及当前账号 |
| `POST` | `/api/accounts/select` | 切换账号，JSON body 为 `{ "uid": "..." }` |
| `GET` | `/api/conversations` | 分页获取会话 |
| `GET` | `/api/conversations/{cid}/messages` | 获取会话消息 |
| `GET` | `/api/search?q=关键词` | 搜索消息 |
| `GET` | `/api/stats` | 获取统计信息 |
| `POST` | `/api/sync/trigger?full=false` | 手动增量同步；`full=true` 执行全量导出 |
| `POST` | `/api/export/selected` | 按会话 ID 导出，body 为 `{ "cids": ["..."] }` |

## 项目结构

```text
config.py              # 自动发现账号、路径和运行配置
decrypt.py             # V2/V3 解密、WAL 处理及数据库校验
account_manager.py     # 多账号切换和账号所有权校验
parser.py              # SQLite 会话与消息解析
exporter.py            # JSON、附件和 ZIP 导出
scheduler.py           # 自动/手动同步
main.py                # 服务入口
web/api.py             # FastAPI API
web/static/            # Web 前端
tests/                 # 解密、账号生命周期和 WAL 回归测试
```

运行时生成的 `data/`、`logs/`、虚拟环境和 dingwave 二进制已加入 `.gitignore`，不应提交到公开仓库。

## 开发与测试

安装依赖后运行：

```bash
python -m unittest discover -s tests -v
python -m py_compile config.py decrypt.py account_manager.py parser.py exporter.py scheduler.py main.py web/api.py
node --check web/static/app.js
```

测试覆盖 V2 解密生命周期、V3 原生解密、V3 UID 识别、WAL 帧处理、账号切换和数据库原子替换。

## 限制与隐私

- 只能读取钉钉桌面客户端已经缓存到本机的消息和附件。
- 未缓存的图片、文件不会从钉钉服务器下载。
- V3 历史账号如果没有可识别的真实 UID 日志，可能需要手动配置或无法切换。
- `data/decrypted/` 包含明文聊天数据库，`data/exports/` 可能包含敏感附件；请勿提交 GitHub 或发送给无权访问的人。
- 建议仅在可信的本机地址监听 Web 服务；如需局域网访问，请自行配置防火墙和访问控制。

## 许可证

[MIT License](LICENSE)

---

## DingTalk Chat Exporter

Export, browse, search, and back up chat history from the DingTalk desktop client's local encrypted SQLite databases. The application runs locally and does not use DingTalk Open Platform APIs or upload chat content.

> This project is intended for personal backup and research. Make sure your use complies with your organization's security policy, applicable privacy laws, and DingTalk's terms of service.

### Features

- Supports DingTalk desktop V2 and V3 encrypted SQLite databases
- Discovers multiple local DingTalk accounts and provides account switching in the web UI
- Browse, search, paginate, and filter conversations and messages
- Supports text, images, files, audio, rich text, quotes, approvals, interactive cards, and more
- Preview and export locally cached images, documents, and other attachments
- Export selected conversations with configurable time ranges
- Self-contained JSON exports with optional ZIP downloads
- A normalized `content` field for downstream search and AI tooling
- Automatic synchronization every four hours, plus manual sync
- Offline at runtime; data stays in local `data/` and `logs/` directories

### Database Versions

| Database directory | Decryption | Additional requirement |
| --- | --- | --- |
| `*_v2` | [dingwave](https://github.com/p1g3/dingwave) | Put the platform binary in `tools/` |
| `*_v3` | Built-in PBKDF2 + AES-ECB decryption with SQLite WAL handling | `cryptography`; usually a real UID from local DingTalk logs |

V3 directory names are usually account hashes rather than real UIDs. The application scans local logs for fields such as `real_uid` and `myOpenId`, then caches successful matches. Set `DINGTALK_UID` manually when historical logs are unavailable.

### Requirements

- Windows, macOS, or Linux
- Python 3.10+
- DingTalk desktop client installed and previously logged in on the machine
- dingwave only when exporting a V2 database; V3 does not require it

### Installation

```bash
git clone https://github.com/abbr530/dingtalk-exporter.git
cd dingtalk-exporter
python -m pip install -r requirements.txt
```

Windows users can run `setup.bat`. On Linux/macOS:

```bash
chmod +x setup.sh
./setup.sh
```

For V2 accounts, download the matching dingwave release and place it at `tools/dingwave.exe` (Windows) or `tools/dingwave` (Linux/macOS). V3 users can skip this step.

### Run

```bash
python main.py
```

Open <http://localhost:8090>. The application automatically scans common DingTalk data locations and lists detected `*_v2` and `*_v3` accounts.

### Manual Configuration

PowerShell:

```powershell
$env:DINGTALK_UID = "123456789"
$env:DINGTALK_DATA_DIR = "C:\Users\YourName\AppData\Roaming\DingTalk\123456789_v2"
python main.py
```

For V3, `DINGTALK_UID` must be the real numeric UID, not the hashed directory name.

### Usage

1. Choose an account from the selector at the top of the page.
2. Browse, search, or filter conversations in the left panel.
3. Open a conversation to inspect messages and cached attachments.
4. Select conversations in the export dialog and choose a time range.
5. Download the generated JSON directory or ZIP archive from the exports tab.
6. Use **Manual Sync** whenever the local DingTalk database needs to be refreshed.

Account switching decrypts and validates the target database before atomically replacing the active decrypted database. A failed switch leaves the current account unchanged.

### Export Format

```text
data/exports/export_123456789_20260805_120000/
├── export.json
├── images/
└── other/
```

`export.json` includes `account_uid`, `export_type`, conversations, messages, and a normalized `content` field. Attachments that are not cached locally are represented by placeholder text rather than downloaded from DingTalk.

The first download of an export creates a ZIP cache under `data/exports/`. Repeated downloads reuse that archive instead of compressing every attachment again.

### Local API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/accounts` | List detected accounts and the active account |
| `POST` | `/api/accounts/select` | Switch accounts with `{ "uid": "..." }` |
| `GET` | `/api/conversations` | Paginated conversations |
| `GET` | `/api/conversations/{cid}/messages` | Messages for a conversation |
| `GET` | `/api/search?q=keyword` | Search messages |
| `GET` | `/api/stats` | Conversation and message statistics |
| `POST` | `/api/sync/trigger?full=false` | Trigger incremental sync; `full=true` performs a full export |
| `POST` | `/api/export/selected` | Export selected conversation IDs with `{ "cids": ["..."] }` |

### Development

```bash
python -m unittest discover -s tests -v
python -m py_compile config.py decrypt.py account_manager.py parser.py exporter.py scheduler.py main.py web/api.py
node --check web/static/app.js
```

The test suite covers V2 decryption lifecycle, native V3 decryption, V3 UID discovery, encrypted WAL frames, account switching, and atomic database replacement.

### Limitations and Privacy

- Only messages and attachments cached by the DingTalk desktop client can be exported.
- Uncached images and files are not downloaded from DingTalk.
- Historical V3 accounts may require manual UID configuration when their logs have been removed.
- `data/decrypted/` contains a plaintext chat database, and `data/exports/` may contain sensitive attachments. Never commit these directories to a public repository.
- Bind the web service to a trusted local address and configure firewall access before exposing it to a network.

### License

[MIT License](LICENSE)
