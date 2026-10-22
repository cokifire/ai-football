# 数据源清单（可得性 + 覆盖盲区）

---

## Elo —— 几乎总是能拿到

**首选**：`https://euroranking.net/en/{队A}-vs-{队B}`
（队名小写、连字符，如 `real-madrid-vs-sevilla`、`getafe-vs-rayo-vallecano`、`sabah-vs-borussia-dortmund`）

一次拿到：双方 Elo + 欧洲排名、**该站自己的预测概率**、近期战绩（含每场 Elo 变动）、H2H、近 30 日 Elo 变化。

**备用**：
- `https://euroranking.net/en/{国家码}/{队名}`（如 `/en/esp/getafe`、`/en/ita/como`、`/en/aze/sabah`）
- `https://euroranking.net/en/power-ranking-elo`（总榜）
- `playerelo.football`、`worldclubratings.com`、`theyscored.com`（不同体系，可作交叉验证）

**已知问题**：
- `api.clubelo.com` 常返回空或不可达
- 直接用 `curl` 打 euroranking 会被 Cloudflare 拦（403）；用 WebFetch
- 队名 URL 需要试：Sabah 是 `/en/aze/sabah`，Como 是 `/en/ita/como`

---

## xG —— Understat，但有覆盖盲区

**联赛表**（一次拿全部球队）：
```
https://understat.com/league/{League}/2026
League ∈ {La_liga, EPL, Serie_A, Bundesliga, Ligue_1, Eredivisie, RFPL}
```
**球队页**（拿逐场明细 + 主客场拆分）：
```
https://understat.com/team/{Team}/2026
```

**覆盖盲区（重要）**：
| 联赛 | Understat 是否覆盖 |
|---|---|
| 英超 / 西甲 / 意甲 / 德甲 / 法甲 / 荷甲 / 俄超 | ✅ |
| **阿塞拜疆超级联赛** | ❌ |
| 葡超、土超、比甲、希超、捷甲、挪超等 | ❌ |
| 欧冠 / 欧联 / 欧会杯 本身 | ❌（只有各国联赛） |

**后果**：欧冠碰到非五大联赛球队（如萨巴赫、卡拉巴赫、博德闪耀、费耶诺德）时，其 xG 通常不可得 → 走单锚降级路径。

**解析要点（务必注意）**：
1. 联赛表行格式：`[名次][队名] M W D L G GA PTS xG (xG−G) xGA (xGA−GA) xPTS (xPTS−PTS)`
   - 括号内是**差值带符号**，如 `15.51+3.51` 表示 xG = 15.51 且 xG − G = +3.51 → G = 12
   - **要小心**：`12.21+3.213.73-4.27` 应读作 xG 12.21(+3.21)、xGA **3.73**(−4.27)，不要读成 13.73
2. 球队页的比分与 xG 是**主队在前**，不是本队在前（用一场已知客场的比赛验证）
3. 球队页底部汇总行：`[G][A][Sh90][KP90][xG][xG−G][xA][xA−A][xG90][xA90]`
4. 数据可能不完整（缺 1–2 场），**用逐场明细求和，不要用汇总行**
5. 汇总行与逐场明细有时不一致 → 采用与联赛表同口径的那个

**备用 xG 源**（质量参差，只作补充）：
- `scoutingstats.ai/clubs/{id}/{team}/performance` — 有 Field Tilt / PPDA，但很多队 404
- `statz.ai/competitions/{league}/stats/teams/...` — 控球率
- `footymetrics.com` — 控球率
- `theanalyst.com` / `footystats.org`

---

## 控球 / Field Tilt / PPDA —— 最难拿

优先级：
1. `scoutingstats.ai`（**唯一稳定的 FT + PPDA 源**，但覆盖率低，常 404）
2. `statz.ai`、`footymetrics.com`、`en.as.com/resultados/.../porcentaje-de-posesion/`（只给控球率）
3. 赛前预览站（theyscored、thefootballfaithful、thedailypunt）偶尔给

拿不到就写 UNAVAILABLE，**不要估算**。

---

## 赔率 —— 需要 ≥8 家才敢叫"市场"

**好用的聚合页**（一次给多家）：
- `legalbet.com/game-center/...`（还给"公平赔率"与去水概率，省事）
- `sportspundit.com`（8–39 家比价表）
- `thedailypunt.com`（含 1X2 / O/U / BTTS 三张表）
- `mightytips.com`
- `oddspedia.com`
- `livefootai.com`（**给 22 家欧洲机构的中位数**，质量高）
- `flashscore.com`（给 2 组报价 + Opta 胜率）

**要点**：
- 尽量取 ≥8 家，且**同一时间快照**（跨源混合会引入时滞）
- 单源只有 2–3 家时，不要叫"市场均值"，就叫"N 家报价均值"
- **异常值要剔除**：如 APWin 曾给平局 8.00（其他全部 3.20–3.75），明显是数据错误
- 亚盘 / 球队进球盘 / 角球盘的报价常常拿不到 → 不要编，直接说"未取得报价"

---

## 伤停与首发

至少两源交叉。**冲突是常态**。

- `fotmob.com/matches/...` — 有 unavailable 列表 + 预计首发 + 球员评分
- `onefootball.com`（Playmakerstats 供稿） — 有预计首发 + unavailable
- `whoscored.com/articles/...` — 有预计首发 + 伤停 + 自己的比分预测
- `legalbet.com`、`flashscore.com`
- 中文源（腾讯、搜狐）常给出更细的伤停与赛程信息

**处理**：冲突时在 §10 列表记录，并说明"本报告采用 X 源，若按 Y 源则结论变动约 ±Npp"。

---

## 裁判

多数站点不给。可得时优先 `legalbet.com`（会给执法场次、场均犯规、黄牌、红牌）。
拿不到写 UNAVAILABLE。

---

## 外部预测（S7 交叉验证用）

| 类型 | 来源 |
|---|---|
| Elo 模型 | euroranking、theyscored（自有 Elo） |
| 统计模型 | ratingbet（给数学模型概率 + 最可能比分）、PredictLix（给模型 xG）、SportSignals |
| 市场派生 | sportspundit、livefootai |
| 人工 | WhoScored、Sports Mole、calledItMate、Flashscore 投注角度 |
| 中文 | 搜狐、腾讯（常引 Whoscored / Sports Mole 的比分预测） |

**最有价值的是"历史基准"**（完全独立于任何模型）：
- UEFA 官方赛前数据包（`uefa.com/uefachampionsleague/news/...`）质量最高，常给"X 队对 Y 国球队 N 战 X 胜 X 平 X 负"这类分布
- Flashscore 的 "Hot Stats" 也常给

---

## 天气

`timeanddate.com/weather/{country}/{city}/ext`（逐小时，最可靠）。
备用：FotMob、PredictLix 的比赛页小字。
**多源常冲突**（温差 7°C、晴/雨不一）→ 写进 §10，不要强行裁决。
