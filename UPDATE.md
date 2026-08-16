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
- 处理：按需裁切/缩放后存为 `images\<曲名>.png`
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

## 6. 定数表自动同步

**机制**：`scripts/sync-constants.py` 扫描 `chart/Info`，把定数表中缺失的曲目
（含定数 / 谱师 / 曲师）写入 `data/bm/constants_extra.json`；bot 启动加载定数表时
（`constants.py` 的 `get_song_constants`）自动合并补充条目。**每次部署重启都会自动执行**。

- **新增曲目**：整条写入（定数来自 Info 的 `DLevel`，谱师来自 `Charter`，曲师来自 `Artist`）。
- **已有曲目**：只补充主表中缺失的难度定数 / 谱师，**不覆盖**已有字段。
- 运行结果在 gitignore 的 `data/` 下，不会污染仓库；控制台会输出新增/补充报告。
- 注意：**游戏更新后 Info 会扩大**（示例：旧版 5 首 → 新版 52 首），
  同步脚本会自动把新曲目全部补进定数表，无需手动维护。

**人工审核与正式入库**（可选，建议定期）：把补充条目合并进正式定数表
`qwwshs/plugins/bm/constexcel.xlsx`（列：曲名 / 原曲名 / 曲师 / RL·IL·TT 难度与谱师 /
追加谱面 / 别名），然后删除或保留 `data/bm/constants_extra.json`。

手动运行同步（本地或服务器，仓库根目录）：

```bash
python scripts/sync-constants.py
```

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
