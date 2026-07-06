# Phase 0 探针结论（2026-07-06）

> 一次性探针脚本对三个来源做真机侦察的结论。样本存 `sample_mikan.json` / `sample_bilibili.json`。
> **探针脚本里的解析正则是天真版，故意留 bug 用来暴露 resolver 难点——不要把它当生产实现。**

## 环境事实

- Python 环境：`gptsovits`（conda，Python 3.11，numpy<2，生产环境），`requests` 可用。
- **yt-dlp / qbittorrent-nox 均未安装**（先决条件，见计划书 §12）。
- 网络：B 站 API 可直连；蜜柑**官方站 `mikanani.me` 可直连**；**`mikan.tangbai.cc` 镜像从本机连不上（http=000）**——D8 定官方站为主源正确，镜像放列表末位或去掉。

---

## 开放问题作答

### #1 该 space 视频命名规律 —— 已答

搬运号 `3493112693394137` 共 ~246 个视频，格式：`【标签】番名 [第X季] 集号（每周更新）`。
- 标签：`【7月/合集】`、`【4K超清/合集】`、`【7月】`、`【4K超清】`（季度/画质 + 合集标记）。
- **决定性发现**：搬运号是**一个 bvid = 整季合集分P**，不是一集一个视频。
  实测：`【4K超清】无职转生 第三季 01-02话 （全站最清，每周更新）`（BV1fmMP6NEvw）——S3 的 ep01/ep02 是同一 bvid 的两个分P。
- **对 B 站源设计的影响**：resolve 结果是 `(bvid, 分P序号)`，不是单个 URL。yt-dlp 按 `-I <part>` / `?p=N` 下指定集；matcher 要先定位「番+季」的合集视频，再把分P列表映射到集号。
- 集号位置不固定：有的标题把集号放最前（`【7月/合集】第1话 第三季 超超超超超喜欢你的100个女朋友`），季→番名→集号乱序，matcher 不能靠位置。

### #2 space API 风控强度 —— 已答

- 裸调 arc/search → `-403 访问权限不足`；仅 WBI 签名 → `-352 风控校验失败`。
- **加 buvid3/buvid4 指纹 cookie（`x/frontend/finger/spi`）+ dm_* WebGL 指纹参数 + WBI 签名 → 可成功，但概率性**：单页常要 1-3 次重试，翻多页时风控更凶。
- **结论**：匿名可爬，但 **① 重试逻辑必须有（re-seed buvid 重试）；② 登录 cookie（下载本来就要，D7）能显著稳住**——建议 B 站 adapter 有 cookie 就带上，既稳风控又解 1080p。
- 限速：翻 8 页无封禁，但每页多次重试；生产里页间加节流、能用 cookie 就别匿名硬刚。

### #3 蜜柑官方站 RSS 覆盖度与结构 —— 已答（RSS-only 成立）

- `GET https://mikanani.me/RSS/Search?searchstr=<番名>` → 标准 RSS，「无职转生」返回 **100 条**，过滤 batch 后 **76 条单集**。
- 每条 `<item>`：`title`（含字幕组/中日名/季/集/画质/字幕）、`enclosure@url`（.torrent）、`enclosure@length`（字节数）、`torrent/pubDate`。
- **infohash 假设确认**（`verify=True`，下载 .torrent 算 btih 与 URL token 逐字节一致）：`Download/<date>/<40hex>.torrent` 与 `Home/Episode/<40hex>` 里的 40 位十六进制**就是 btih**。
  → **能直接从 RSS 拼磁力 `magnet:?xt=urn:btih:<40hex>&dn=<title>`，无需下载 .torrent**。满足 P0-3 magnet-only、零 SSRF。
- **v1 RSS-only 足够**（P2-19 决策成立），HTML 解析不用进 v1。

### #4 qbt 免密 vs 密码 —— 待答（阻塞在安装）

`probe_qbt.py` 已就绪；需用户先 `sudo apt install qbittorrent-nox && sudo systemctl enable --now qbittorrent-nox`。脚本会报告 `LocalHostAuth` 是否可免密。

### #5 Totem 对字幕组 mkv 兼容性 —— 待手动验收

需真机放一集 LoliHouse 的 `1080p HEVC-10bit + ASS 内封` mkv 看 Totem 行不行。机器有 vlc 兜底；倾向 `player_command` 默认给 vlc（HEVC-10bit + ASS 字幕 Totem 常吃力）。装完 yt-dlp/qbt 下到真集后验。

### #6 「最新一集」数据来源 —— 已答

- 蜜柑 RSS 天然按 pubDate 倒序，最新 item = 最新集（但**多字幕组混排**，同集多版本，需先按番+季过滤再取最大集号）。
- B 站合集分P 的最后一个 part = 最新集。
- 季度歧义（如「无职转生II」cour 拆分、第2クール从 01 还是 13 编号）**必须暴露给候选确认，不静默选**（D10）。

---

## 对 resolver golden cases 的真实素材（来自 sample_mikan.json）

真实标题证明「季/集」解析在多字幕组命名下确实难，golden 必须覆盖：

| 真实标题片段 | 陷阱 |
| --- | --- |
| `无职转生 3期 / Mushoku Tensei S3 - 02` | 季有 `3期` 和 `S3` 两种写法并存 |
| `第3季 … - 02`（Skymoon） | **「第3季」的 3 会被天真正则误当集号**（探针实测 ep=3 错误） |
| `无职转生 第三季 … - 01`（沸班亚马） | 中文数字「三」，天真 SEASON_RE 漏识别 |
| `[ANi] … 第三季 - 02 [1080P][Baha][CHT]` | **CHT=纯繁体**，按简体/简繁偏好应**降权**（不是简繁内封） |
| `[LoliHouse] … 1080p HEVC-10bit … 简繁内封` | 理想匹配：1080p + 简繁内封 → 排序应胜出 |
| `… 2160p NVENC …` | 画质 2160p(4K) 也要识别 |

**结论**：resolver 必须先剥离字幕组 `[..]`/括号团/`【..】`，用中日双名 + 季度别名表（`N期`/`第N季`/`第N期`/`SN`/`Nrd Season`/罗马数字）规范化，季度标记先消费再抽集号（避免季号被当集号），同集多字幕组按（画质 + 字幕）偏好表排序取一。置信不足走候选确认。

---

## 需要写进计划书的设计增量

1. **B 站源 resolve 返回 `(bvid, part_index)`**，不是单 URL；adapter 用 yt-dlp 下指定分P。计划书 §5.2 补。
2. **B 站 adapter 必须有 risk-control 重试**（re-seed buvid + WBI 重签），并**优先带 cookie**（稳风控 + 解 1080p）。§5.2 + §7 补。
3. **蜜柑 magnet 直接由 RSS 的 infohash 拼**，不下 .torrent。§5.3 明确。
4. **镜像 `tangbai.cc` 本机不可达** → `mikan_base_urls` 默认官方站在前，镜像可留但不作主。§6/§0.2 已一致，标注镜像可达性存疑。
