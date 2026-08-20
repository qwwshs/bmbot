# Berry Melody 查分 Bot（NoneBot2 插件）

基于 [NoneBot2](https://nonebot.dev/) + OneBot V11 的《Berry Melody》音游查分 QQ 机器人。

支持绑定游戏存档（`FormalSave.txt`）、生成 Rating 查分图（B30 / N10 / OVERFLOW / GOAL 推分目标）、单曲模糊查询、歌曲别名管理。

## ✨ 功能

| 命令 | 说明 |
|---|---|
| `/bmhelp` | 查看帮助 |
| `/bmbind` | 绑定存档：发送后 5 分钟内发送存档 txt 文件（聊天文件或群文件均可） |
| `/bmrating [QQ号]` | 以图片输出 Rating 查分（B30 + N10 + OVERFLOW + GOAL 推分目标）；带 QQ 号可查指定玩家（仅超管） |
| `/bmratingstyle` | 切换查分样式（新版网格卡 / 旧版榜单，按用户保存） |
| `/bmsong <曲名>` | 单曲查询（支持中/日/英文模糊搜索，多结果回复序号） |
| `/bmaddname <别名>` | 为歌曲添加自定义别名（白名单） |
| `/bmremovename <别名>` | 删除歌曲别名（白名单，流程与 bmaddname 一致） |
| `/bmnamelist` | 查看全部别名对应关系（白名单） |
| `/bmaddtowhitelist <QQ>` | 添加白名单成员（仅超管） |
| `/bmremovefromwhitelist <QQ>` | 移除白名单成员（仅超管） |
| `/bmcharter <谱师>` | 按谱师查询谱面（模糊搜索，多结果回复序号；自动扩展关联名义组） |
| `/bmsetuptheprimitivecharter <谱师>` | 设置基元谱师名义（本名，须能在定数表中查到）（白名单） |
| `/bmremovetheprimitivecharter <谱师>` | 移除基元谱师名义（连同其关联名义）（白名单） |
| `/bmrelatedcharter <基元谱师>` | 添加基元谱师名义的马甲/合作名义（须能在定数表中查到）（白名单） |
| `/bmremoverelatedcharter <基元谱师>` | 解除基元谱师名义与另一名义的关联（白名单） |
| `/bmrelatedcharterlist` | 列出所有基元谱师与其关联名义（白名单） |
| `/bmchartlist <定数1> [定数2] [难度...]` | 按定数区间生成定数表图（13=13.0~13.5，13+=13.6~13.9，13.4=精确；`all`=全部定数；不写难度=全部） |
| `/bmrandom <定数1> [定数2] [难度...]` | 在定数区间内随机挑一首曲目（文字返回） |
| `/bmbotversion` | 查看 bot 版本 |

> 中文匹配：支持简体中文匹配繁体中文与日本汉字（借助 OpenCC + 内置日文汉字映射 + 定数表的「原曲名」列）。
>
> 谱师关联：部分谱师有马甲或合作名义（如 `BZAIG&LeadLink&霜炎`）。先用 `bmsetuptheprimitivecharter` 把本名设为基元，再用 `bmrelatedcharter` 关联其他名义；之后 `bmcharter` 查询本名或任意关联名义都会输出整组谱面。

## 📦 环境要求

- Python 3.10+（3.14 已验证）
- NoneBot2 ≥ 2.5.0（`pip install nonebot2[fastapi,httpx]`）
- OneBot V11 协议端：NapCat / SnowLuma 等（需支持正向 WebSocket）
- 依赖：`nonebot-adapter-onebot`、`cryptography`、`pillow`、`opencc-python-reimplemented`
- 可选依赖：`nonebot-adapter-console`（本地调试）

## 🚀 安装

```bash
# 1. 克隆
git clone https://github.com/qwwshs/bmbot.git
cd bmbot

# 2. 创建虚拟环境并安装
python -m venv .venv
# Windows: .venv\Scripts\activate | Linux: source .venv/bin/activate
pip install -e .[dev]      # 依赖见 pyproject.toml

# 3. 复制配置模板
cp .env.example .env       # Windows: copy .env.example .env
```

## ⚙️ 配置（.env）

| 变量 | 说明 |
|---|---|
| `ENVIRONMENT` | `prod`（正式，日志 INFO）/ `dev`（调试，日志 DEBUG） |
| `ONEBOT_ACCESS_TOKEN` | 与协议端约定的访问令牌（两端必须一致） |
| `ONE_BOT_WS_URL` | 正向 WS 地址（SnowLuma 默认 `ws://127.0.0.1:3001/onebot/v11/ws`，NapCat 一般为 `ws://127.0.0.1:3001`） |
| `BM_ADMIN_QQS` | 可使用 `/bmaddname` 等别名命令的 QQ 白名单，如 `[10001, 10002]` |
| `BM_SUPER_ADMIN_QQ` | 超管 QQ：唯一可管理白名单的人（不填则无人可管理，务必填写） |

## 📁 资源文件

插件运行需要两份资源（部分**不随仓库分发**，需自行放置）：

1. **定数表** `qwwshs/plugins/bm/constexcel.xlsx`
   - 仓库内含一份（游戏版本更新后请从官方群/查分器仓库更新）
   - 首次运行自动解析并缓存到 `data/bm/constants.json`
2. **曲绘** `qwwshs/plugins/bm/images/`
   - 存放 `曲名.png` 或 `曲名_难度.png`（如 `Ether Vortex_RU.png`）
   - 首次渲染自动生成 168px 缩略图缓存到 `data/bm/thumbs/`
   - 未找到曲绘时显示占位符，不影响查分

## 🗄️ 数据与隐私

- 绑定存档按 QQ 拆分存储：`data/bm/bindings/<qq>.json`（内容含解密后的游戏存档，**不要提交到 git**）
- 群聊中接收存档后会**自动撤回文件消息**（需 bot 为群管理员）；群文件上传方式会尝试删除群文件
- 别名：`data/bm/aliases.json`；白名单：`data/bm/whitelist.json`（运行时增删）
- 谱师关联：`data/bm/charters.json`（基元谱师名义与其马甲/合作名义，运行时增删）

## 🧮 算法（与官方查分器一致）

- **ChartPotential**：分段线性函数（1000000 → 定数+3.25；其余区间见 `rating.py`）
- **Rating** = 0.8 × B30 平均 + 0.2 × N10 平均
- **B30**：全部谱面 ChartPotential 降序前 30（不足 30 仍 ÷30），图内附 OVERFLOW 后 3 首
- **N10**：固定 22 首曲池内前 10（前 5 权重 0.6，后 5 权重 0.4，÷5）
- **GOAL 推分目标**：每行显示推 0.005 Rating 所需的最低分数（无法推分显示 `GOAL 无法推分`）

## 🖥️ 部署与运维

开发机调试：

```bash
nb run --reload
```

生产服务器（推荐 screen + 看门狗）：

```bash
screen -dmS nb bash -c "cd ~/bmbot && nb run"
# 掉线自动重启 + QQ 通知：见 scripts/watchdog.sh（填入接收通知的 QQ 后加 cron）
# 资源诊断：scripts/diag.sh
```

脚本说明：

- `scripts/watchdog.sh`：cron 每分钟检查 bot 存活，掉线自动重启并 QQ 通知（通过协议端 HTTP API；连续重启自动冷却防刷屏；`touch manual_stop` 可临时暂停监控）
- `scripts/diag.sh`：一键输出 CPU/内存/磁盘/OOM/重启循环诊断

## ⚠️ 安全提示

- `.env`、`data/`、曲绘包等已被 `.gitignore` 排除，请勿强制提交
- 白名单与超管 QQ 均在 `.env` 配置，代码中不包含任何真实 QQ 号
- 协议端的 HTTP API（用于看门狗通知）建议仅监听 127.0.0.1 并设置访问令牌

## 📄 License

仅供学习交流使用。游戏资源（曲绘、定数表）版权归原游戏厂商所有。
