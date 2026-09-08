# QuantGT 改版 / 抓取失败 / 邮件未送达 —— 排障检查清单

本文档用于 quantcheck（抓取 quantgt.io 选股数据、邮件推送给订阅者的服务）出现下面两类问题时的排障：**quantgt.io 页面改版导致抓取/解析报错**，或者**抓取看起来正常但订阅者反馈没收到邮件**。不管排障的是人还是 agent，都应该从上到下按顺序走一遍，不要跳步骤、不要重新摸索。A1-A6 每一条都来自 2026-08-30 一次真实发生过的排障（细节和 commit 见文末「本文档的来源」），不是通用运维建议——如果你发现现象对不上、或者代码已经变了，请更新本文档，不要让它烂掉。

---

## 第一步：先定位到层，再查那一层

出问题先别急着看代码，先按症状对到下表的层，再去对应文件里查：

| 症状 | 最可能的层 | 首查文件/函数 |
|---|---|---|
| 异常/日志里出现 `did not produce parsable picks rows`（完整信息形如 `{mode} page did not produce parsable picks rows after {attempts} attempts`） | 抓取/解析 | `quantcheck/scrape_parse.py`；`quantcheck/picks_report.py` 里 `rows_from_table` 的卡片过滤 JS 和 `wait_for_parsable_picks_rows` |
| 异常里出现 `incomplete detail rows`（完整信息形如 `logged-in watchlist validation failed: incomplete detail rows: ...`） | 详情弹窗抓取 | `quantcheck/picks_report.py: expand_watchlist_and_attach_details`；校验逻辑在 `quantcheck/validation.py: validate_member_picks_data` |
| `pick_date` 变成 `Unknown` | 日期解析 | `quantcheck/scrape_parse.py: extract_pick_date` |
| 字段静默变空（不报错，但 diff 里出现类似 `"held_since": "" ` 这种变化，或某一列整体消失） | 表头别名 | `quantcheck/scrape_parse.py: HEADER_ALIASES` |
| 抓取本身正常（`state/raw/` 有新快照、`health.json` 无报错）但订阅者说没收到邮件 | 通知/投递层 | `quantcheck/notify_routes.py`；`logs/email_delivery_ledger.jsonl` |
| 同一封邮件被发了两遍 | 去重状态落盘时机 / 并发锁 | `quantcheck/official_mail_forwarder.py`（`_record_forward` 何时落盘、`LOCK_FILE`） |

---

## 第二步：五分钟诊断命令（按顺序跑）

```bash
cd /opt/quantcheck
systemctl status quantcheck.service
tail -100 logs/quantcheck_scheduler.log
tail -100 logs/quantgt_monitor.log
ls -lt state/raw/ | head -10          # 最近一次成功抓取是什么时候，断档从哪天开始
.venv/bin/python3 -m quantcheck.picks_check --mode baseline --force --no-random
```

**必须记住的坑**：一定要用 `.venv/bin/python3`，不要用系统 `python3`。用系统 `python3` 会直接报：

```
ModuleNotFoundError: No module named 'pandas_market_calendars'
```

**跑上面最后一条命令前先想一下第五步第 2 条**：`--mode baseline` 只是把新抓的数据写进 `state/latest_picks.json`（旧的挪成 `state/previous_picks.json`），不会做任何 diff/notify。如果你怀疑故障期间发生过真实变化、之后还要用它来对比，**不要**在没有备份的情况下先跑这条命令，否则会把 `state/previous_picks.json` 覆盖成故障后的数据，故障期间的真实变化就永远比不出来了。诊断阶段只是想看抓取能不能跑通，可以跑；但跑之前先确认第六步的四步检查你不会跳过。

---

## 第三步：对照已知改版模式（2026-08-30 一次同时踩到四个）

### 3.1 Weekly Watchlist 卡片不再带 `$价格`

- **症状**：卡片文本从 `"SNDK Sandisk Corporation $2,032.22 Electronic Technology"` 变成 `"CORT Corcept Therapeutics Incorporated Health Technology"`——价格字段整个消失了。
- **根因**：`scrape_parse.py` 里的 `WATCHLIST_CARD_RE` 要求文本里必须有 `$价格` 才能把 symbol/company/price/sector 切分开；改版后卡片里没有价格了，这个正则整体不匹配，导致该行被当成解析失败。
- **修法**：加了一条新正则 `WATCHLIST_CARD_SYMBOL_ONLY_RE`（`^(?P<symbol>[A-Z][A-Z0-9.]{0,5})(?:\s+\S+){2,}$`），只无歧义地提取 symbol；company/sector/current_price 改为从认证 Watchlist API `/api/proxy/api/weekly/stocks`（`picks_report.py: fetch_watchlist_api_rows`）的响应字段 `name`/`sector`/`sell_price`（没有 `sell_price` 就退回 `price`）回填，回填逻辑在 `picks_report.py: merge_watchlist_api_scores`。已有字段（卡片文本给出的）不会被 API 值覆盖。
- **对应测试**：`tests/test_scrape_parse.py::test_rows_from_watchlist_card_layout_without_price`、`test_merge_watchlist_api_scores_backfills_missing_company_sector_price`、`test_merge_watchlist_api_scores_does_not_override_card_supplied_fields`。
- **commit**：`bc7ca93`

### 3.2 月度日期丢了年份，且和 MTD 徽标粘连无空格

- **症状**：monthly 页面文本变成 `"Latest holdings Updated August 1MTD +6.25%"`——日期数字后面直接接了徽标文字，中间没有空格。
- **根因**：`extract_pick_date`（`scrape_parse.py`）原本 monthly 分支的前两条规则都要求「`月 日, 年`」（带逗号和四位年份）；这里既没有年份，日期数字后面也不是空格/标点而是直接跟字母 `M`，所以两条规则都不匹配，最终退化成 `"Unknown"`。
- **修法**：在原有两条规则后面加了第三条兜底：`\bUpdated\s+(?:{MONTHS})\s+\d{1,2}(?!\d)`，用负向前瞻 `(?!\d)` 给日期数字收尾（只要求后面不是另一个数字），而不是要求 `\b` 词边界，这样就能兼容日期后面直接接字母的场景。
- **对应测试**：`tests/test_scrape_parse.py::test_monthly_updated_date_without_year_is_supported`（断言 `extract_pick_date(text, "monthly") == "Updated August 1"`）。
- **commit**：`bc7ca93`

### 3.3 详情弹窗点击被页面框架元素撞车

- **症状**：股票代码是单字母 `U` 一类的短 ticker 时，详情弹窗抓取失败或抓到错误内容。
- **根因**：`expand_watchlist_and_attach_details`（`picks_report.py`）原来用 `page.get_by_text(symbol, exact=True)` 做**全页面**查找；账号头像上的缩写 `<span data-slot="avatar-fallback">U</span>` 和 ticker `"U"` 的文本完全相同，会被同时命中，点到了错误的元素。
- **修法**：改成 `page.locator("main").get_by_text(symbol, exact=True)`，把查找范围限定在 `<main>` 容器内，排除页面头部/侧边栏等框架元素。
- **诊断手法（值得记住，比看代码更快）**：对比 `page.get_by_text(sym, exact=True).count()` 和 `page.locator("main").get_by_text(sym, exact=True).count()` 这两个数量。如果全页面的数量比 `main` 里的数量多，就说明页面框架里有重复文本，需要缩小查找范围。
- **对应测试**：这一条改动没有对应的单元测试（Playwright 页面交互不好用普通单测覆盖），只能靠上面的诊断手法在活体验证里确认。
- **commit**：`bc7ca93`

### 3.4 月度 Portfolio 表头 `Held Since` 改名 `Entry Date`

这一条最阴险，务必单独记住：

- **症状**：**不报错**。`validate_member_picks_data`（`validation.py`）的 `required_monthly_fields` 是 `["symbol", "company", "current_price", "return", "sector", "gt_score", "next_earnings", "analyst_signal"]`——**`held_since` 不在这个列表里**，所以就算这一列整列丢失，validation 也不会拦截。后果是下一次真实检查会把整个 Portfolio 的 `held_since` 从「有值」变成「缺失」，被 diff 当成「全部持仓都变了」误报出去。
- **根因**：`HEADER_ALIASES`（`scrape_parse.py`）里没有 `entry_date` 这个 key。表头文本 `"Entry Date"` 经 `normalize_header` 处理后变成 `"entry_date"`，在 `HEADER_ALIASES` 里查不到映射，`canonical_header` 返回 `None`，整列被 `rows_from_matrix` 丢弃。
- **修法**：`HEADER_ALIASES` 加一行 `"entry_date": "held_since"`。
- **对应测试**：`tests/test_scrape_parse.py::test_rows_from_monthly_table_header_using_entry_date_label`。
- **commit**：`b027758`

**举一反三**：任何页面改版把某一列表头换了个新名字，只要新名字没进 `HEADER_ALIASES`，都会重演这个模式——先查新表头文本，`normalize_header()` 一下，看结果在不在 `HEADER_ALIASES` 里。

### 3.5 Watchlist 详情弹窗里「合法缺失」字段被公司简介吞掉

- **症状**：`incomplete detail rows: <SYMBOL> missing analyst_signal`，连续多天同一个股票报同一个错，其它股票都正常。
- **根因**：`parse_watchlist_dialog_text`（`scrape_parse.py`）用 `value_after(label, next_labels)` 按「找下一个已知标签出现的位置」来截断字段值。正常情况下 "Analyst Consensus" 后面紧跟 "Momentum"，边界很紧。但如果这只股票的 Momentum/Relative Strength **完全没有数据**，Quant GT 连标签本身都不渲染（不是显示"—"，是整个区块消失）——这时 `value_after` 找不到 "Momentum"/"Relative Strength"，只能继续找下一个存在的标签，很可能是隔着一整段公司简介之后的 "More Headlines"，于是把整段简介误当成 `analyst_signal` 的原始值。后果是「合法缺失」检测（`analyst_signal_raw.upper() in unavailable_values`）失效——因为被检测的不再是干净的 `"—"`，而是一大段文字，永远不会精确等于 `"—"`。
- **修法**：加了 `first_token_after(label)`，只取标签后面第一个空格分隔的 token（不做多标签边界搜索），专门用来判断这个字段是不是「合法缺失」；原本的 `value_after` 继续用来提取真实存在的值（如 "Buy +0.24"），两者并行、互不影响。
- **诊断手法**：不要只看 validation 报错字符串，要活体抓一次真实的 dialog 原始文本（`dialog.inner_text()`），肉眼确认到底是文本里根本没有这个字段，还是提取边界算错了。本次是后者。
- **对应测试**：`tests/test_scrape_parse.py::test_watchlist_dialog_unavailable_analyst_consensus_not_swallowed_by_company_blurb`（用真实抓到的 APGE 文本做的 fixture）。
- **commit**：`_(见 git log，2026-09-08 修复)_`

**举一反三**：任何「某字段值缺失时连标签本身都不渲染」的场景，都可能让 `value_after` 式的边界搜索越界吞掉后面一大段无关内容。判断「合法缺失」时优先用窄范围的 `first_token_after` 式检测，不要依赖宽范围提取的返回值恰好等于某个哨兵字符串。

---

## 第四步：两个容易复发的代码陷阱

1. **JS 字符串缺 `r` 前缀**：代码里大量用三引号字符串内嵌 JS 传给 `page.evaluate` / `page.wait_for_function`。如果这段 JS 里含有 `\b`（正则词边界）而字符串没写成 `r"""..."""`，Python 会把 `\b` 解释成退格符（0x08 字节），而不是字面的反斜杠+b。结果是传给浏览器的 JS 正则永远匹配不上意图中的分支，而且**不会抛出任何异常**——因为语法仍然合法，只是语义错了。2026-08-30 这天在 `picks_report.py: is_watchlist_page` 和 `picks_check.py: _wait_for_screenshot_ready` 各踩了一次，两处都是漏了 `r` 前缀。**排查方法**：`grep -n '"""() =>' quantcheck/*.py` 之类，把所有内嵌 JS 的三引号字符串找出来，只要里面有 `\b`（哪怕只有一处），一律确认外面是 `r"""`。
2. **页面级文本匹配**：任何 `page.get_by_text(...)`、`page.get_by_role(...)` 不加容器限定的写法，都要先问一句「页面框架里（导航栏/头像/侧边栏）会不会有一样的文本」。默认应该限定在具体容器（如 `page.locator("main")`）内查找，参考 3.3。

---

## 第五步：排障纪律（2026-08-30 验证有效的方法论）

1. **单测通过 ≠ 修好了**。当天错误 3.1/3.2/3.3 全都是在跑真实活体验证（`--mode baseline` / `--test-email`）时才依次暴露的——修好一个，才会露出下一个。修完必须跑真实流程，而且要一直跑到干净为止，不能改完代码、单测绿了就收工。
2. **不要用自己跑出来的 baseline 结果当比较基准**。重复跑 `--mode baseline` 会覆盖 `state/previous_picks.json`，之后再拿它去 diff，得到的是「无变化」的假结果。要对比时，用故障发生**之前**留下的 `state/raw/picks_raw_*.json` 做基准，而不是刚刚修完后跑出来的东西。
3. **单测不能污染生产日志/状态**。当天发现 `tests/test_official_mail_forwarder.py` 里有测试没有 patch `LOG_FILE`，导致跑单测时把 `admin@example.com`、`friend@example.com` 之类的 fixture 地址真实写进了生产用的 `logs/official_mail_forwarder.log`，干扰了当时的日志分析。修复（`a935d18`）里把几乎所有相关测试的 `LOG_FILE` / `LOCK_FILE` / `STATE_FILE` 都 patch 到了 `tempfile.TemporaryDirectory()`。**新增测试必须照此惯例**：凡是被测代码里有模块级 `LOG_FILE` / `STATE_FILE` / `LOCK_FILE` 这类路径常量，测试里一律用 `patch("quantcheck.xxx.LOG_FILE", ...)` 之类指向 tmp 目录。

---

## 第六步（也是整份文档里最容易被忽略、代价最大的一条）：**修好抓取 ≠ 自动补发**

**`run_baseline()`（`quantcheck/picks_check.py`）在设计上不发通知。** 把它的代码通读一遍就知道：它只做三件事——抓取、把旧的 `LATEST` 挪成 `PREVIOUS`、把新数据写进 `LATEST`——从头到尾**没有调用 `compare()`，也没有调用 `notify()`**。它的语义是「确认现在这份数据就是新的起点」，不是「告诉订阅者这次和上次比多了什么」。这是有意的设计：baseline 模式本来就是用来处理「当前 state 已经不可信、需要强制重置」的场景，如果这时候顺手把 diff 发出去，抓取故障期间攒下的、很可能混杂着失败噪音的「变化」会被当成真实变化群发给所有订阅者。

**代价是**：如果修复流程的最后一步就是跑一次 `--mode baseline` 然后收工，那么故障窗口里真实发生的持仓变化会被永久静默吞掉——没有异常、没有日志报警、没有任何人会主动发现，直到订阅者自己来问「我怎么这周没收到邮件」。

**这不是假设，是 2026-08-30 当天真实发生的事**：

- 故障前最后一次成功抓取：`state/raw/picks_raw_2026-08-25_090042.json`，`weekly.pick_date = "Updated on Aug 21, 2026"`。
- 故障后第一次成功抓取：`state/raw/picks_raw_2026-08-30_214637.json`，`weekly.pick_date = "Updated on Aug 28, 2026"`。
- 这两份快照之间，`state/last_picks_change_notification.json`（`notify_dedupe.py` 维护的去重状态）里记录的最后一次真实通知，至今仍然停在 `weekly_date: "Updated on Aug 21, 2026"`、`at: "2026-08-25T12:30:53"`——也就是说 Aug 21 → Aug 28 这次变化从来没有真正走到 `run_check()` 的 notify 分支。
- 这次变化是真实的：新增 `APGE`、`U`，剔除 `ALAB`、`DDOG`，另有 8 行字段发生变化。是用户自己发现没收到邮件才追出来的。

### 修复流程最后一步必须做的四步检查（不是可选项）

1. **列出故障窗口内所有 `state/raw/picks_raw_*.json`**，确认「故障前最后一个成功快照」和「故障后第一个成功快照」分别是哪两个文件：
   ```bash
   ls -lt state/raw/picks_raw_*.json | tac   # 按时间正序看，找断档的位置
   ```
   **一个真实踩到的坑**：`run_baseline()`（`picks_check.py`）——也就是第二步诊断命令序列里用来验证修复的那条 `--mode baseline` 命令——**只写 `state/latest_picks.json`，不写 `state/raw/` 快照**。如果修复后是靠这条命令验证的，`state/raw/` 里"故障后第一个成功快照"这个文件根本不存在，下面第2步找不到端点。2026-09-08 这次的应对：`state/previous_picks.json`（故障前）和 `state/latest_picks.json`（跑完 baseline 后，故障修复后的当前状态）这对文件本身就是现成的端点——**前提是这期间只跑过一次 `--mode baseline`**（多跑会互相覆盖，参考第五步第2条）。这种情况下不必强求 `historical_resend.py` 能找到端点，可以：①备份当前 `state/latest_picks.json`；②把 `state/previous_picks.json` 的内容拷贝覆盖到 `state/latest_picks.json`（相当于把"latest"临时倒回故障前状态）；③跑一次 `quantcheck-admin ops run picks --force --confirm`（或等价的 `python -m quantcheck.picks_check --mode check --force`）——它会用当前真实的活体数据和刚刚"倒回去"的旧数据做 diff，检测到真实变化后自动走标准的 raw快照+Excel+截图+发信全流程，等价于把这次的 backfill 当成一次迟到的正常检查来处理，而不是用 `historical_resend.py` 重建。这条路径用的是系统里被验证最多的标准发信路径本身，不是新写的临时脚本。
2. **用这两个端点做 diff——不是随手挑相邻两个文件比**，因为中间那些天可能完全没有成功快照。用 `quantcheck/historical_resend.py` 重建 diff，先不带 `--send` 跑一次，只打印 JSON 预览，不发送任何邮件：
   ```bash
   .venv/bin/python3 -m quantcheck.historical_resend --weekly-date "<故障后第一个快照的 weekly.pick_date>"
   ```
   2026-08-30 这次真实跑出来的结果（截至本文档撰写时，这组 `state/raw/`、`output/`、`screenshots/` 文件仍在，可以原样复现）：
   ```json
   {
     "target_weekly_date": "Updated on Aug 28, 2026",
     "raw": "/opt/quantcheck/state/raw/picks_raw_2026-08-30_214637.json",
     "previous_raw": "/opt/quantcheck/state/raw/picks_raw_2026-08-25_090042.json",
     "added": ["APGE", "U"],
     "removed": ["ALAB", "DDOG"],
     "changed_rows": 8
   }
   ```
3. **有真实变化（`added`/`removed`/`changed_rows` 不全为空）就必须补发**，一律用 `python -m quantcheck.historical_resend`，**绝不允许写临时发信脚本**——仓库安全文档明确禁止这一点。
4. **正式全量发送前，先用 `--recipient` 给一个 admin 地址发一份预览**，肉眼确认渲染出来的邮件正文和附件都对，而不是只信第 2 步的 JSON 摘要：
   ```bash
   .venv/bin/python3 -m quantcheck.historical_resend \
     --weekly-date "Updated on Aug 28, 2026" \
     --send --confirm-date "Updated on Aug 28, 2026" \
     --recipient admin@example.com
   ```
   `--recipient`（可重复传多次）会跳过 `notify_routes.subscriber_recipients()`，只发给显式指定的地址，返回的 JSON 里 `recipients_source` 会是 `"explicit_recipient"`。确认无误后，**去掉 `--recipient` 重新跑一次同样的命令**才会发给 `.env` 里 `NOTIFY_EMAIL_TO` / `NOTIFY_EMAIL_FILE` 配置的全量真实订阅者（这次 `recipients_source` 是 `"subscriber_route"`）——admin 预览和全量发送是两次独立的调用，预览那次不会自动带出全量发送。`--confirm-date` 必须和 `--weekly-date` 逐字符精确匹配，否则 `execute_resend` 会直接拒绝发送。

**两个容易忽略的补充提醒**：

- `historical_resend` 的预览和发送都依赖「同一次抓取」留下的三个文件同时存在：`output/quantgt_picks_report_<UTC时间戳>.xlsx`、`screenshots/portfolio_<NY时间戳>.png`、`screenshots/watchlist_<NY时间戳>.png`（对应逻辑在 `historical_resend.py: _same_run_attachments`）。这些文件会被 `prune_old_files` 按数量滚动清理（xlsx 保留最近 80 份、screenshot 保留 80~160 份不等），拖得越久越可能因为素材被清理掉而在这一步直接报错 `missing same-run attachment`。发现故障要尽快处理，不要拖到清理周期之后才想起来补发。
- 正式 `--send` 之前，建议先用 `logs/email_delivery_ledger.jsonl` 确认这批订阅者没有已经通过其它方式（比如手工邮件）收到过同样的变化，避免重复发送。

---

## 本文档的来源

本文档全部内容来自 2026-08-30 的一次真实排障（commit 时间戳为 UTC，落在 2026-08-31 00:00-02:00 之间，对应 America/New_York 时区的 2026-08-30 晚间）。当天在同一个 session 里连续发现并修复了四个问题：

| commit | 一句话说明 |
|---|---|
| `bc7ca93` | fix: restore weekly watchlist and monthly date parsing after QuantGT UI changes —— 对应第三步 3.1/3.2/3.3 三个改版模式，以及第四步的 `r` 前缀陷阱 |
| `a935d18` | fix: prevent duplicate official-mail forwarding sends —— 对应第一步「同一封邮件发了两遍」，以及第五步第 3 条（单测污染生产日志） |
| `4c79a13` | feat: add fail-closed historical picks resend tool —— 新增 `quantcheck/historical_resend.py`，对应第六步 |
| `b027758` | fix: recognize QuantGT's renamed monthly "Entry Date" column header —— 对应第三步 3.4 |

查看某个 commit 的完整改动：`git show <sha>`。
