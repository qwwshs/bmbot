# Berry Melody Bot 更新指南

> 本文档面向**人类维护者与 AI 代理**：游戏更新（新谱面 / 新皮肤素材）后，
> 如何把素材同步到服务器，并自动把新谱面的数据（定数 / 谱师 / 曲师）写入定数表。

## 目录

1. [素材来源与解包](#1-素材来源与解包)
2. [谱面更新流程](#2-谱面更新流程)
3. [皮肤素材更新流程](#3-皮肤素材更新流程)
4. [定数表自动同步](#4-定数表自动同步)
5. [部署与重启](#5-部署与重启)
6. [更新后验证](#6-更新后验证)
7. [常见问题](#7-常见问题)

---

## 1. 素材来源与解包

游戏 APK 解包目录（本地 Windows，AssetStudio 导出）：

```
F:\AssetStudio-net10.0-win\base\
├── assets\bin\Data\data.unity3d   # Unity 主包（Sprite 九宫格边框等元数据）
├── ass\
│   ├── TextAsset\                 # 谱面文本（含 Info 对照文件）
│   ├── Sprite\                    # 精灵图（含皮肤素材）
│   ├── Texture2D\                 # 纹理
│   └── Material\                  # 材质（_MainTex 引用 → 皮肤↔素材对应关系）
└── lib\arm64-v8a\libil2cpp.so     # 游戏代码（存档加密等逆向分析用）
```

- **谱面**：`ass\TextAsset\` 下的文本文件，文件名形如 `曲名 难度`（难度：RL/IL/TT/RU/DM/FL），
  以及 `Info` 对照文件（`Song::{}` / `Chart::{}` 块，含曲名、曲师、谱师、定数 DLevel）。
- **皮肤素材**：`ass\Sprite\` 下的 PNG（如 `White_Tap.png`、`flick_bg.png`、`dy_mid.png`）；
  同一贴图名可能对应多个皮肤的版本，需按 `ass\Material\*.json` 的 `_MainTex` PathID
  从 `data.unity3d` 中按 PathID 提取正确版本。

## 2. 谱面更新流程

1. **本地**：把新谱面文件复制到 `qwwshs\plugins\bm\chart\`（文件名格式 `曲名 难度`，
   `.txt` 后缀可有可无），`Info` 文件一并放入同目录（**必须**，定数/谱师/曲师来源）。
   - 谱面目录与 `Info` 均已 gitignore，不会进仓库。
2. **上传服务器**（谱面目录在服务器：`/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/chart/`）：

   ```bash
   scp "qwwshs/plugins/bm/chart/新曲名 IL" admin@101.132.120.132:/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/chart/
   scp "qwwshs/plugins/bm/chart/Info" admin@101.132.120.132:/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/chart/
   # 或用 rsync 全量同步：
   # rsync -av "qwwshs/plugins/bm/chart/" admin@101.132.120.132:/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/chart/
   ```

3. **重启 bot**（触发定数表自动同步，见第 4 节）：

   ```bash
   ssh admin@101.132.120.132 "bash /home/admin/nbbot/qwwshs/scripts/restart-bot.sh"
   ```

## 3. 皮肤素材更新流程

1. **本地**：把新皮肤素材 PNG 复制到 `qwwshs\plugins\bm\note\`（该目录已 gitignore）。
2. **上传服务器**：

   ```bash
   scp "qwwshs/plugins/bm/note/新素材.png" admin@101.132.120.132:/home/admin/nbbot/qwwshs/qwwshs/plugins/bm/note/
   ```

3. **注册到皮肤表**：若新增了皮肤或素材文件名变化，修改
   `qwwshs/plugins/bm/chartpreview.py` 的 `SKIN_SETS`（皮肤名 → 各音符类型素材文件名），
   提交代码后部署（见第 5 节）。
4. **重启 bot** 使素材缓存刷新（`_note_image_cache` 在首次使用后缓存，缺素材会缓存 None）。

## 4. 定数表自动同步

**机制**：`scripts/sync-constants.py` 扫描 `chart/Info`，把定数表中缺失的曲目
（含定数 / 谱师 / 曲师）写入 `data/bm/constants_extra.json`；bot 启动加载定数表时
（`constants.py` 的 `get_song_constants`）自动合并补充条目。**每次部署重启都会自动执行**。

- **新增曲目**：整条写入（定数来自 Info 的 `DLevel`，谱师来自 `Charter`，曲师来自 `Artist`）。
- **已有曲目**：只补充主表中缺失的难度定数 / 谱师，**不覆盖**已有字段。
- 运行结果在 gitignore 的 `data/` 下，不会污染仓库；控制台会输出新增/补充报告。

**人工审核与正式入库**（可选，建议定期）：把补充条目合并进正式定数表
`qwwshs/plugins/bm/constexcel.xlsx`（列：曲名 / 原曲名 / 曲师 / RL·IL·TT 难度与谱师 /
追加谱面 / 别名），然后删除或保留 `data/bm/constants_extra.json`（保留则继续以补充表为准）。

手动运行同步（本地或服务器，仓库根目录）：

```bash
python scripts/sync-constants.py
```

## 5. 部署与重启

代码变更（非 gitignore 素材）走 git：

```bash
# 本地
git add -A && git commit -m "..." && git push origin main
# 服务器自动拉取并重启（restart-bot.sh 内含：git pull → 定数同步 → 重启 bot）
ssh admin@101.132.120.132 "cd /home/admin/nbbot/qwwshs && bash scripts/restart-bot.sh"
```

或直接运行本地的一键脚本 `deploy.bat`（推 GitHub → 服务器拉取 → 重启）。

**restart-bot.sh 的完整流程**：

1. `git pull` 拉取代码
2. `python3 scripts/sync-constants.py` 自动同步新谱面数据到定数表（失败不阻塞）
3. 重启 screen 会话 `nb`（`nb run`）

## 6. 更新后验证

| 检查项 | 命令 | 预期 |
| --- | --- | --- |
| 版本号 | `/bmbotversion` | 显示最新版本号 |
| 新曲检索 | `/bmsong 新曲名` | 能找到曲目 |
| 新曲定数 | `/bmrating`（绑定含新曲成绩的存档） | 新曲计入定数 |
| 谱面预览 | `/bmchart 新曲名` → 选难度 | 生成预览图 |
| 皮肤 | `/bmskin` 切换 → 指定皮肤的新谱 | 素材显示正常 |
| 定数同步日志 | 服务器 `screen -r nb` | 出现「已合并 N 首自动同步的新曲目」 |

## 7. 常见问题

- **新曲搜不到 / 定数为空**：`Info` 未上传或未更新——谱面更新时必须同时上传新的
  `Info` 文件（定数 / 谱师 / 曲师都来自它）。
- **git pull 冲突**：`data/` 与 `chart/`、`note/` 均已 gitignore，正常不会冲突；
  若提示本地修改，多为误提交了 ignore 目录，`git checkout -- <file>` 后重试。
- **皮肤不生效**：同名纹理多皮肤共用时需按 `Material/*.json` 的 `_MainTex` PathID
  从 `data.unity3d` 提取对应版本（参考 `Phi_Tap.png` / `flick2.png` 的提取过程）。
- **定数表补充条目未生效**：确认 `data/bm/constants_extra.json` 存在且 JSON 合法，
  重启 bot 后 `get_song_constants` 会打印合并数量。
