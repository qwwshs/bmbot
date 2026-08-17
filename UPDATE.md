# Berry Melody Bot 更新指南

> 本文档面向**人类维护者与 AI 代理**：游戏更新（新谱面 / 新皮肤 / 新角色头像 /
> 新曲目数据）后，如何用 AssetStudio.CLI 解包新 APK、把素材同步到服务器，
> 并自动把新谱面的数据（定数 / 谱师 / 曲师）写入定数表。

## 目录

1. [工具与目录约定](#1-工具与目录约定)
2. [解包新 APK（AssetStudio.CLI）](#2-解包新-apkassetstudiocli)
3. [谱面更新流程](#3-谱面更新流程)
4. [皮肤素材更新流程](#4-皮肤素材更新流程)
5. [曲绘与玩家头像更新](#5-曲绘与玩家头像更新)
6. [定数表自动同步](#6-定数表自动同步)
7. [上传服务器与部署](#7-上传服务器与部署)
8. [更新后验证](#8-更新后验证)
9. [常见问题](#9-常见问题)

---

## 1. 工具与目录约定

| 路径 | 说明 |
| --- | --- |
| `E:\nb\test\Berry Melody.apk` | **最新版游戏 APK**（每次更新放这里） |
| `E:\nb\test\Berry Melody\` | **解包基础目录**（人工维护，含 `ass\` 解包产物与源文件） |
| `E:\nb\test\AssetStudio-net10.0-win\` | AssetStudio 工具（CLI + GUI，从 `F:\AssetStudio-net10.0-win.zip` 解压） |
| `F:\AssetStudio-net10.0-win.zip` | AssetStudio 工具压缩包备份 |
| `E:\nb\test\qwwshs\` | bot 仓库（推送到 GitHub，服务器拉取部署） |

服务器目录：`/home/admin/nbbot/qwwshs/`（git 仓库 + `data/` 运行时数据）。

**解包产物结构**（`Berry Melody\ass\`）：

```
ass\
├── TextAsset\    # 谱面文本（含 Info 对照文件：Song::/Chart:: 块）
├── Sprite\       # 精灵图（note 皮肤素材等）
├── Texture2D\    # 纹理（歌曲封面 / 角色头像 / 头像默认图等）
├── Material\     # 材质（_MainTex 引用 → 皮肤↔素材对应关系）
└── ...           # 其他类型（Font/AudioClip 等，一般无需更新）
```

## 2. 解包新 APK（AssetStudio.CLI）

新 APK 放到 `E:\nb\test\Berry Melody.apk` 后，用 CLI 直接解包（无需先解压 APK）：

```bat
cd /d E:\nb\test\AssetStudio-net10.0-win

:: 谱面文本（TextAsset，含 Info）
AssetStudio.CLI.exe "E:\nb\test\Berry Melody.apk" "E:\nb\test\Berry Melody\ass" --game Normal --types TextAsset

:: 精灵图（note 皮肤素材）
AssetStudio.CLI.exe "E:\nb\test\Berry Melody.apk" "E:\nb\test\Berry Melody\ass" --game Normal --types "Sprite:Both"

:: 纹理（歌曲封面 / 角色头像）
AssetStudio.CLI.exe "E:\nb\test\Berry Melody.apk" "E:\nb\test\Berry Melody\ass" --game Normal --types Texture2D
```

- 输出目录直接指向 `Berry Melody\ass\`，CLI 会按类型建子文件夹
- **重复运行自动跳过已存在文件**（"files already exist"），可增量更新
- 解包耗时约 1-5 分钟（APK 约 1.7GB，可挂后台跑）
- 如需要其他类型（Font/AudioClip 等）：`--types "Font:Both|AudioClip"`，`--game Normal` 不变

## 3. 谱面更新流程

1. **本地**：把新谱面复制到 `qwwshs\plugins\bm\chart\`（文件名格式 `曲名 难度`，
   `.txt` 后缀可有可无），**`Info` 文件必须一并复制**（定数/谱师/曲师来源）。
   - 谱面目录与 `Info` 均已 gitignore，不会进仓库。
   - 对照新旧清单确认新增（示例：新版本新增了 `Infinity`、`Spiritworks`、
     `The Echo of Peach Color`、`small DENG kitchen` 等谱面）。
2. **上传服务器**：

   ```bash
   rsync -av "E:/nb/test/qwwshs/qwwshs/plugins/bm/chart/" admin@101.132.120.132:/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/chart/
   ```

3. **重启 bot**（触发定数表自动同步，见第 6 节）：

   ```bash
   ssh admin@101.132.120.132 "bash /home/admin/nbbot/qwwshs/scripts/restart-bot.sh"
   ```

## 4. 皮肤素材更新流程

1. **本地**：从 `Berry Melody\ass\Sprite\` 复制新皮肤素材到 `qwwshs\plugins\bm\note\`。
   - 同名纹理多皮肤共用时需按 `ass\Material\*.json` 的 `_MainTex` PathID 从
     `data.unity3d` 提取对应版本（参考 `Phi_Tap.png` / `flick2.png` 的提取过程）。
2. **上传服务器**：

   ```bash
   scp "E:/nb/test/qwwshs/qwwshs/plugins/bm/note/新素材.png" admin@101.132.120.132:/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/note/
   ```

3. **注册到皮肤表**：若新增了皮肤或素材文件名变化，修改
   `qwwshs/plugins/bm/chartpreview.py` 的 `SKIN_SETS`（皮肤名 → 各音符类型素材文件名），
   提交代码后部署。
4. **重启 bot** 刷新素材缓存（`_note_image_cache` 首次使用后缓存，缺素材会缓存 None）。

## 5. 曲绘与玩家头像更新

两个都来自 `Berry Melody\ass\Texture2D\`，输出到 `qwwshs\plugins\bm\images\`（gitignore）。

### 5.1 曲绘（歌曲封面）

- 来源：`ass\Texture2D\<曲名>.png`（如 `3rd Avenue.png`），曲绘按曲名命名
- **注意：曲目曲绘的真身是 Addressables 里的 `SongPackage/<章节>/<曲名>/Header.png`**
  （2048×1536），全量导出时所有歌的 Header/Capsule **同名互相覆盖**，最终只剩
  UI 那几张——这是部分曲目缺曲绘的根因。缺失时用 `--containers` 逐曲提取：

  ```bash
  cd /d E:\nb\test\AssetStudio-net10.0-win
  # 容器路径查 assets/aa/catalog.json（搜曲名）
  AssetStudio.CLI.exe "E:\nb\test\Berry Melody\assets\aa\Android\defaultlocalgroup_assets_all_0d65a21c70a7587b3e13734f4f1e101a.bundle" "E:\nb\test\Berry Melody\ass_song\<曲名>" --game Normal --types Texture2D --containers "SongPackage/Single/<曲名>"
  # 取 Header.png（分难度版 Header_TT/DM/RU 也一并取出），转 RGB 存 images\<内部名>.png
  ```

  MIRЯOЯ（Ryceam 章节）在当前 APK bundle 无实体、旧解包中也没有——
  `--names` 提取会拿到**别的歌**的同名 Header（已踩坑）。bundle 里查不到
  容器的曲目视为无本地曲绘源。该曲已下架，曲绘不必补充。
- **重要事实：本游戏没有服务器**——所有资源（含 SideStory/Ryceam 等章节）
  都打进 APK 本地；「服务器下发」是早期误判。当前 APK 的 Addressables bundle
  只含基础章节，SideStory 1 / Ryceam 等章节素材来自**更早版本的解包**
  （如 BLACK DIAMOND 的曲绘）。新 APK 更新后这些章节可能重新入包，
  用下面方法从 bundle 精确提取即可。
- Info 中**没有曲绘字段**（字段只有 Path/Title/Artist/Painter/Tag 等，
  `Painter` 是画师人名）；曲绘按内部名对应 SongPackage 容器的 Header.png。
- 处理：按需裁切/缩放后存为 `images\<曲名>.png`（存内部名或显示名均可，
  运行时按 曲名/原曲名/别名+难度变体 匹配，大小写不敏感）
- **文件名大小写**（love_in_adversity 踩坑）：Windows 不区分大小写，
  服务器 Linux 区分——文件名与曲名大小写不一致时服务器上会缺图。
  运行时已加小写索引回退（song.py/render.py），但新增曲绘仍应尽量
  与曲名大小写一致。
- **同名曲目曲绘区分**（Ether Vortex 踩坑）：显示名 `Ether Vortex` 的内部名
  是 `Ether Vortex Final`，Remix 版显示名 `Ether Vortex (Chikanya Remix)` 的
  内部名才是 `Ether Vortex`。曲绘文件用**显示名**命名（`Ether Vortex Final.png`
  对应原版、`Ether Vortex (Chikanya Remix).png` 对应 Remix），避免原版按曲名
  优先命中 Remix 的图。
- **完整性检查**（每次更新必做）：对照全量定数表检查每首都有曲绘：

  ```bash
  # 输出缺失曲绘的曲目（对照定数表，与运行时 find_cover 同逻辑）
  python scripts/check-covers.py
  ```

  注意 Windows 开发机上大小写不敏感会掩盖问题，脚本已模拟 Linux
  严格匹配（song.py 的小写索引回退会被计入命中）。
- **不用管的曲目（check-covers 报缺失属预期）**：
  - **游戏已下架（曲绘已删，勿再补充）**：`Varcolac`、`始め恋`（lian）、
    `MIRЯOЯ`（MIRROR）
  - **当前 APK 无曲绘源（章节曲包表/未发布曲目，需人工从游戏截图补充）**：
    `終わりの少女 feat. こにゃばた (full)`、`小登厨`（small DENG kitchen）、
    `蜜糖色的回响`（The Echo of Peach Color）
- 上传：

  ```bash
  rsync -av "E:/nb/test/qwwshs/qwwshs/plugins/bm/images/" admin@101.132.120.132:/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/images/
  ```

### 5.2 玩家头像（角色头像）

- 来源：`ass\Texture2D\<角色EName>.png`（**98×98 小头像**，如 `Elodea.png`、`Ayira.png`；
  另有 `Default Head.png` 为默认头像）
- 处理：把 98×98 源图放大/锐化为 **150×150**，存为 `images\<角色EName>.png`
- **角色名映射**：存档里的角色 Path 与头像文件名不一致时，在
  `qwwshs/plugins/bm/render.py` 的 `CHAR_AVATAR_FILES` 里补映射
  （如存档 `Elidia` → 头像文件 `Elodea.png`；新角色直接同名则无需映射）
- 新角色检查点：
  1. 新 APK 解包后 `Texture2D\` 里出现的新 `<EName>.png`
  2. 存档新增的角色 Path 是否在 `CHAR_AVATAR_FILES` 或 `images\` 中有对应文件
  3. 头像上传后重启 bot（`_avatar_cache` 有缓存，重启后生效）
- 上传：

  ```bash
  scp "E:/nb/test/qwwshs/qwwshs/plugins/bm/images/新角色.png" admin@101.132.120.132:/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/images/
  ```

## 6. 定数表更新（每次更新必做）

**机制**：`scripts/sync-constants.py` 扫描 `chart/Info`，把定数表中缺失的曲目
（含定数 / 谱师 / 曲师）写入 `data/bm/constants_extra.json`；bot 启动加载定数表时
（`constants.py` 的 `get_song_constants`）自动合并补充条目。**每次部署重启都会自动执行**。

### 6.1 重要规则：曲名用内部名

**存档成绩键（`BestScore_<曲名>_<难度>`）与定数表主表曲名都用「内部名」**，
不是 Info 的显示名。例如：

| 内部名（主表/存档键） | 显示名（Info 的 Title，存「原曲名」列） |
| --- | --- |
| `Infinity` | `IF = Infinity` |
| `Magic Sink` | `マジックの沈淪` |
| `Twin Nebula` | `双生のネビュラ` |

sync-constants.py 已按此规则生成条目（内部名作曲名、显示名进原曲名），
**人工维护定数表时也请遵守**，否则新曲成绩无法匹配。

### 6.2 正式入库（每次更新后执行一次）

补充表（`data/bm/constants_extra.json`）只是运行时合并，**每次游戏更新后
要正式合并进 `constexcel.xlsx` 并提交**：

```bash
# 在仓库根目录执行：扫描 Info + 把新曲正式写进 constexcel.xlsx
python scripts/sync-constants.py --apply

# 检查输出报告（新增了哪些曲目），然后提交部署
git add qwwshs/plugins/bm/constexcel.xlsx
git commit -m "定数表：新增 X 首曲目"
git push origin main
```

- `--apply` 只追加主表中**没有**的曲目（按内部名归一化匹配），不覆盖已有行
- 已有曲目缺失难度/谱师时，由运行时补充表自动补（无需改 xlsx）
- 补充表是幂等的：每次 sync 重新生成，不会累积旧条目
- 注意：`constexcel.xlsx` 可能被 Excel 打开导致写入被锁；脚本会直接覆盖写，
  若仍失败请关闭 Excel 后重试

**全量定数表缓存图（`/bmchartlist all`）**：全量表渲染约一分钟，故落盘为
`data/bm/all_charts.jpg`（压缩 JPEG），命令直接发缓存图。`restart-bot.sh`
部署时会自动运行 `scripts/build-all-charts.py` 重建——按定数内容哈希判断，
**内容变了才重建**，平时部署只多算一次哈希。也可手动运行：

```bash
python scripts/build-all-charts.py
```

缓存缺失时（如新机器部署首次启动前），`/bmchartlist all` 会现场渲染并落盘，
仅第一次慢。`data/` 已 gitignore，缓存不入库。

### 6.3 人工补录：Info 未收录的曲目

Info 中**没有**的曲目（测试谱、未收录谱、新曲抢先版）不会自动入库。
这类谱面文件里通常是**占位数据**，需要人工补录。示例（已按此流程处理）：

| 曲目（内部名） | 显示名 | 占位数据 | 正确曲师 |
| --- | --- | --- | --- |
| `small DENG kitchen` | 小登厨 | `_temp_1783411654` | AoiGroove真琴 |
| `The Echo of Peach Color` | 蜜糖色的回响 | `粉丝感谢` | AoiGroove真琴 |

**识别占位数据**：谱面 `#info` 段的 `Artist`/`Title` 是 `_temp_`、`粉丝感谢`、
`~欢迎光临...` 等非正式内容时，说明是未正式发布的曲目。

**人工补录步骤**：

1. **修正谱面文件**（本地 `qwwshs/plugins/bm/chart/`，每个难度都要改）：
   把 `#info` 段的 `Artist:` 改为正确曲师，`Title:` 改为显示名（如有）：

   ```bash
   # 示例：修正 Artist 字段（本地 + 服务器）
   python - <<'EOF'
   import re
   from pathlib import Path
   for name in ["small DENG kitchen IL", "small DENG kitchen RL", "small DENG kitchen TT"]:
       f = Path(f"qwwshs/plugins/bm/chart/{name}")
       text = f.read_text(encoding="utf-8-sig", errors="replace")
       f.write_text(re.sub(r"(?m)^\s*Artist:.*$", "Artist: AoiGroove真琴;", text, count=1),
                    encoding="utf-8")
   EOF
   ```

2. **写入定数表**（仓库根目录，复用 sync-constants 的 xlsx 写入逻辑）：

   ```bash
   python - <<'EOF'
   import importlib.util
   spec = importlib.util.spec_from_file_location("sync", "scripts/sync-constants.py")
   sync = importlib.util.module_from_spec(spec)
   spec.loader.exec_module(sync)
   entries = {
       "small DENG kitchen": {  # 内部名作 key（与存档 BestScore_ 键一致）
           "RL": None, "IL": None, "TT": None, "RU": None, "DM": None, "FL": None,
           "aliases": [], "artist": "AoiGroove真琴", "originalName": "小登厨", "charter": {},
       },
   }
   sync.apply_to_xlsx(entries)
   EOF
   ```

   要点：
   - **key 用内部名**（存档成绩键 `BestScore_<内部名>_<难度>` 靠它匹配）
   - 显示名（如"小登厨"）放 `originalName` 列
   - 定数未知时留 `None`，等官方数据出来再补（`RL/IL/TT` 列填小数即可）

3. **上传谱面 + 提交部署**：

   ```bash
   # 上传修正的谱面到服务器
   tar -cf - -C qwwshs/plugins/bm/chart "small DENG kitchen IL" ... | \
     ssh admin@101.132.120.132 "cd /home/admin/nbbot/qwwshs/qwwshs/plugins/bm/chart && tar -xf -"
   # 提交定数表并部署
   git add qwwshs/plugins/bm/constexcel.xlsx
   git commit -m "定数表：人工补录 小登厨 / 蜜糖色的回响（曲师 AoiGroove真琴）"
   git push origin main
   ssh admin@101.132.120.132 "cd /home/admin/nbbot/qwwshs && bash scripts/restart-bot.sh"
   ```

4. **验证**：`/bmsong 小登厨` 能搜到、`/bmchart small deng` 出图、`/bmrating`
   中新曲成绩可计入（定数补上后）。

> 后续官方补丁发布正式 Info 后，`sync-constants.py` 会自动把这两首的定数
> （DLevel）与谱师补进补充表，无需重复人工操作。

## 7. 上传服务器与部署

代码变更（非 gitignore 素材）走 git：

```bash
# 本地
git add -A && git commit -m "..." && git push origin main
# 服务器自动拉取并重启（restart-bot.sh 内含：git pull → 定数同步 → 重启 bot）
ssh admin@101.132.120.132 "cd /home/admin/nbbot/qwwshs && bash scripts/restart-bot.sh"
```

素材（chart/ note/ images/ 均 gitignore）走 rsync/scp（见上文各节）。
或直接运行本地一键脚本 `deploy.bat`（推 GitHub → 服务器拉取 → 重启）。

**restart-bot.sh 的完整流程**：

1. `git pull` 拉取代码
2. `python3 scripts/sync-constants.py` 自动同步新谱面数据到定数表（失败不阻塞）
3. 重启 screen 会话 `nb`（`nb run`）

## 8. 更新后验证

| 检查项 | 命令 | 预期 |
| --- | --- | --- |
| 版本号 | `/bmbotversion` | 显示最新版本号 |
| 新曲检索 | `/bmsong 新曲名` | 能找到曲目 |
| 新曲定数 | `/bmrating`（绑定含新曲成绩的存档） | 新曲计入定数 |
| 谱面预览 | `/bmchart 新曲名` → 选难度 | 生成预览图 |
| 皮肤 | `/bmskin` 切换 → 指定皮肤的新谱 | 素材显示正常 |
| 曲绘 | `/bmsong 新曲名` | 显示新封面 |
| 头像 | `/bmrating`（绑定新角色的存档） | 显示新角色头像 |
| 定数同步日志 | 服务器 `screen -r nb` | 出现「已合并 N 首自动同步的新曲目」 |
| 全量定数表 | `/bmchartlist all` | 秒回缓存图，新曲已包含在内 |

## 9. 常见问题

- **新曲搜不到 / 定数为空**：`Info` 未更新——谱面更新时必须同时上传新的
  `Info` 文件（定数 / 谱师 / 曲师都来自它）。
- **头像不显示 / 显示默认头像**：新角色头像未放入 `images\`，或
  `CHAR_AVATAR_FILES`（render.py）缺少存档 Path → 文件名的映射；上传后需重启 bot。
- **git pull 冲突**：`data/` 与 `chart/`、`note/`、`images/` 均已 gitignore，
  正常不会冲突；若提示本地修改，多为误提交了 ignore 目录，`git checkout -- <file>` 后重试。
- **皮肤不生效**：同名纹理多皮肤共用时需按 `Material/*.json` 的 `_MainTex` PathID
  从 `data.unity3d` 提取对应版本（参考 `Phi_Tap.png` / `flick2.png` 的提取过程）。
- **CLI 解包很慢**：APK 约 1.7GB，首次全量解包需要几分钟；后续更新同类型会跳过
  已存在文件（增量），速度快很多。
- **定数表补充条目未生效**：确认 `data/bm/constants_extra.json` 存在且 JSON 合法，
  重启 bot 后 `get_song_constants` 会打印合并数量。
