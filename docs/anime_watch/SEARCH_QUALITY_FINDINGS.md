# 搜索质量实测报告（2026 春季番真样本）

> 测试日期 2026-07-07（Phase 4 收口、enabled=true 之后）。目的：拿**真实当季新番 + 用户口吻 query** 检验 resolver + mikan 搜索链路的命中率，为 Phase 5 搜索 hardening 定优先级。
> 方法：真实链路 `parse_query → MikanRssSource.search（真网络 mikanani.me）→ resolver.resolve（纯函数）`，**只 resolve 不下载**（不碰 materialize 下游 / qbt / yt-dlp）。每 case 一次 RSS 请求 + 1.2s 节流，共约 16 次请求。
> 复跑：`python docs/anime_watch/probes/probe_search_quality.py`（一次性 recon 脚本，不进 spica/，同 Phase 0 探针惯例）。
> 样本来源：mikan 官方 2026 春季列表（`/Home/BangumiCoverFlowByDayOfWeek?year=2026&seasonStr=春`，92 部），抽 10 部覆盖：中文数字季/集、长标题全称、常用简称、部分标题子串、LATEST、全角标点、非标准季标（第x部分）、短歧义词、绝对编号发布。

---

## 1. 结果矩阵：4 命中 / 3 歧义 / 3 失败

| # | Query（用户口吻） | parse_query | 结果 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 租借女友第五季第2集 | 租借女友 s5 e2 | ✅ matched s5e2（1080p） | 正确。绿茶组绝对编号 `[52]` 解析为 s=None/e=52，按 §0.3 安全非匹配跳过（**设计验证通过**） |
| 2 | 关于我转生变成史莱姆这档事第四季第1集 | 全称 s4 e1 | ❌ `episode 1 not found` | **RSS 窗口滚动**：搜索只返回最近约 100 条（当时播到 13 集），4 月的第 1 集已滚出窗口 |
| 3 | 转生史莱姆第四季第1集 | 转生史莱姆 s4 e1 | ❌ `season not offered` | **简称不命中**：「转生史莱姆」不是「关于我**转生**变成**史莱姆**这档事」的子串，`name_matches` 离线确认 False |
| 4 | 实力至上主义教室第四季最新一集 | 子串 s4 LATEST | ✅ matched s4e16 | 正确（e16 当时仅 2160p 释出，选 2160p 合理） |
| 5 | Re从零开始的异世界生活第四季第3集 | s4 e3 | ❌ **search 0 候选** | **server-side 标点敏感**：官方标题为 `Re：从零开始的异世界生活`（全角冒号），见 §2.3 变体实测 |
| 6 | 转生最新一集 | 转生 s=None LATEST | ⚠ ambiguous（seasons [1,3,4]） | review P2-1 的场景本次被 season-spread 拦下→候选确认；但候选实为**三部不同番**（落第贤者/无职转生/史莱姆）被标成「多个季」，归因表述错位 |
| 7 | 石纪元第2集 | 石纪元 s=None e2 | ⚠ ambiguous（seasons [2,3,4]） | 半合理：各字幕组把同一部标成 第二部分/第3部分/第四季，走确认符合 §0.3 不折算 cour 原则 |
| 8 | 异兽魔都第二季第1集 | s2 e1 | ✅ matched s2e1 | 正确（`S2` 标记） |
| 9 | 尖帽子的魔法工房第一集 | s=None e1 | ✅ matched e1 | 正确（中文数字集号） |
| 10 | 欺诈游戏最新一集 | s=None LATEST | ⚠ **假歧义** `multiple distinct titles matched` | 两候选实为同一部（喵萌奶茶屋两种发布形制），聚类分裂，白问用户一次 |

主链路结论：**标准全称+季/集、子串部分标题、LATEST、S 标记、中文数字在真实数据上是稳的**；失败集中在查询构造（标点）、别名（简称）、数据窗口（老集数）三个明确边缘。

---

## 2. 根因（全部离线/变体复现钉死）

### 2.1 假歧义（case 10）：`【组】★促销★[标题/别名]` 形制名称抽取错位

```
t1 = "[喵萌奶茶屋&LoliHouse] 欺诈游戏 / 诈欺游戏 / LIAR GAME - 13 [...]"
     → name_zh='欺诈游戏' ✓
t2 = "【喵萌奶茶屋】★04月新番★[欺诈游戏 / 诈欺游戏 / LIAR GAME][13][1080p][繁日双语]"
     → name_zh='喵萌奶茶屋 ★04月新番★' ✗（组名+促销语被当成番名；真标题在第二层方括号里）
_cluster_by_title([t1, t2]) → 2 clusters（应为 1）
```

名称**过滤**靠 `name_matches` 检查 raw 兜住了 t2（所以它能进 named pool），但**聚类**用 name_zh → 分裂 → 假歧义。

### 2.2 双编号（case 2 的一半）：`[13(85)]` → episode=None

```
"【豌豆字幕组】[关于我转生变成史莱姆这档事 第四季 / ... S4][13(85)][繁体][1080P][MP4]"
 → season=4, episode=None（应为 e=13：季内号 13、绝对号 85）
```

豌豆组这一形制的候选全军覆没。case 2 的主因仍是 RSS 窗口（e=1 确实不在返回集），双编号是叠加伤害。

### 2.3 server-side 标点敏感（case 5）：searchstr 变体实测

| searchstr | 候选数 |
| --- | --- |
| `Re从零开始的异世界生活`（用户口吻，无冒号） | **0** |
| `Re：从零开始的异世界生活`（全角冒号，官方形态） | 94 |
| `从零开始的异世界生活`（去 ASCII 头，最长中文段） | **94** |
| `Re:从零开始的异世界生活`（半角冒号） | 67 |

失败发生在 mikan server-side 关键词匹配，**客户端 `name_matches` 的正规化根本没机会出手**——这是 adapter 层查询构造问题，不是 resolver 问题。

### 2.4 简称硬边界（case 3）

`name_matches("转生史莱姆", parse_source_title("[豌豆字幕组&LoliHouse] 关于我转生变成史莱姆这档事 第四季 / ..."))` → **False**。子串匹配对「热门简称 ⊄ 全称」无解——正是 PLAN §5.1「不建大别名库，不够用挂账」的挂账到期：本季人气最高的续作直接挂。

---

## 3. 优化清单（按 ROI 排序，Phase 5 搜索 hardening 批次）

| 序 | 修什么 | 怎么修 | 落点 | 量级 |
| --- | --- | --- | --- | --- |
| 1 | 搜索零命中（§2.3） | search 返回 0 候选时，取 query 最长连续中文段重搜一次（失败才多一次请求） | `spica/adapters/anime_source/mikan.py`（或 coordinator 统一做，bilibili 同享） | ~15 行 |
| 2 | 简称不命中（§2.4） | 内置别名 map 加热门简称条目（转生史莱姆→关于我转生变成史莱姆这档事 等）；或加「query 各 2+ 字词均现于标题」弱匹配 + **强制走候选确认门**（禁静默，P1-10） | `spica/anime/resolver.py` 别名 map | 数据条目 / ~30 行 |
| 3 | 双编号解析（§2.2） | EP 提取加 `\[(\d{1,3})\((\d{1,4})\)\]` 模式，取季内号 | `spica/anime/resolver.py` `_EP_PATTERNS` | 1 正则 + golden |
| 4 | 假歧义（§2.1） | ① subgroup 剥离后若首段无 `/` 且后随方括号段含 `/`，取该方括号段为名称源；② `_cluster_by_title` 剥 `★…★` 促销 token、按别名段（`/` 切分）交集折叠 | `spica/anime/resolver.py` | ~40 行 |
| 5 | RSS 窗口滚动（case 2） | 短期：区分「找到该番但无此集」的 reason/播报文案（「这集可能太早，蜜柑最近列表里已经没有了」），别伪装成真 NOT_FOUND；中期：bangumi 页 HTML 降级（正是挂账的 P2-19） | `resolver.resolve` reason + `watch_flow._map_outcome`；中期 mikan adapter | 文案小 / HTML 中 |
| 6 | 歧义候选展示（case 6/7） | `multiple seasons` 的代表候选混着不同番时按 display_title 原文归因，别把不同番说成「多个季」；与 review **P2-1（LATEST 跨番门：max() 应在跨番聚类检查之后）**同点位一起修 | `spica/anime/resolver.py` resolve 步骤 3 | ~20 行 |

关联既有 review findings：P2-1（LATEST 跨番歧义旁路，本次 case 6 被 season-spread 侥幸拦下但机制仍在）、P3-8（`_quality_rank` 死分支 → quality 键 no-op，case 4 若配 720p 即暴露）。

---

## 4. 建议提为 resolver golden cases 的真样本

修复落地时把以下样本钉进 `tests/test_anime_resolver.py`：

```
# §2.1 名称抽取 + 聚类归并（期望：两条同番、1 个聚类、name_zh=欺诈游戏）
"[喵萌奶茶屋&LoliHouse] 欺诈游戏 / 诈欺游戏 / LIAR GAME - 13 [WebRip 1080p HEVC-10bit AAC][简繁日内封字幕]"
"【喵萌奶茶屋】★04月新番★[欺诈游戏 / 诈欺游戏 / LIAR GAME][13][1080p][繁日双语]"

# §2.2 双编号（期望：season=4, episode=13）
"【豌豆字幕组】[关于我转生变成史莱姆这档事 第四季 / Tensei Shitara Slime Datta Ken S4][13(85)][繁体][1080P][MP4]"

# §2.4 简称别名（期望：别名折叠后 name_matches=True 且走确认门）
query="转生史莱姆" vs "[豌豆字幕组&LoliHouse] 关于我转生变成史莱姆这档事 第四季 / Tensei Shitara Slime Datta Ken 4th Season - 13"

# case 1 绝对编号安全非匹配（期望：s=None/e=52，季 pin 查询不命中——已正确，防回归）
"[绿茶字幕组] 租借女友  / Kanojo Okarishimasu  [52][WebRip][1080p][简繁日内封]"

# §2.3 搜索变体（adapter 层测试：0 候选 → 最长中文段重试）
"Re从零开始的异世界生活" → 0；fallback "从零开始的异世界生活" → 命中
```
