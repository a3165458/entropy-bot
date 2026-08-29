# entropy-bot

**This Python package is the real bot.** It talks only to official Hyperliquid HTTP + WebSocket. The Chrome Tampermonkey userscript (`entropy-desk.user.js`) is optional / legacy — do not depend on it for live MM.

EntropyIO HIP-3 perps on Hyperliquid: **`io:ANTH`** and **`io:SNDK`** only. DEX name is **`io`** (`fullName` = EntropyIO). `xyz:SNDK`, `vntl:ANTHROPIC` are other venues. `io:OAI` / `io:IONQ` are delisted. All refused.

---

## English

### What this is

A Python 3.11+ CLI on the official APIs only:

- HTTP: `https://api.hyperliquid.xyz` (`/info`, `/exchange`)
- WS: `wss://api.hyperliquid.xyz/ws`

No entropy.io scraping, no third-party order relays, no userscript bridge.

**Paper / shadow mode is the default.** Live signing happens only when `LIVE=1` **and** `HYPERLIQUID_PRIVATE_KEY` is set.

### Live MM (userscript 1.6.2 port)

Per coin: at most **1 buy + 1 sell**. Isolated-only. Builder = Entropy `0xcD254d2A328f7f67C7c6FEf930A4757516F7b601` fee **0**. Tick is inferred from the live bid/ask increment (usually 0.1 at ANTH ~1985) — not `tick_size(mid)`, which returns 1.0 at 5 sigfigs.

**FLAT (`szi==0`)** — two-sided ALO, `reduceOnly=false`:

- Spread ≤ 2 ticks: join exact bid/ask.
- Spread > 2 ticks: `buy=floorToTick(mid-tick)`, `sell=ceilToTick(mid+tick)`, clamp inside the book, `buy < sell`. Example `1985.00 / 1986.90 / tick 0.1` → buy **1985.8** sell **1986.1**.
- Same-price rests older than 45s: cancel and replace at fresh BBO/mid.

**IN POSITION** — flatten only, never add. Size `min(notionalSz, |szi|)`. Age clock starts when `|szi|` first becomes nonzero; resets on flat.

- Long: only reduce-only sell. Short: only reduce-only buy.
- `<6s`: reduce-only ALO at the far touch (long sell@ask, short buy@bid).
- `≥6s`: reduce-only ALO improved to mid (or 1 tick inside the near touch). Stay maker.
- Unfilled flatten ALO: after **90s** at the same price, cancel-and-replace the ALO (same maker path). Never IOC / taker flatten. Prefer cancel over taking.

ALO / Bad Alo Px / post-only reject: log and requote; the loop does not stop. Stage / TIF / price change: cancel that coin's rests (local cache + `frontendOpenOrders` with `dex:"io"`) then place — **throttled** by `MIN_REPLACE_S` (default 12s) per coin while a rest is live. A book tick alone does not cancel+replace until that interval passes. Flatten uses the same ALO cancel+replace path (no IOC bypass). Never place a side that already has an oid/cloid. HIP-3 oids exceed int32 — they are never truncated.

### Request weight / write throttle

Hyperliquid address-level cumulative request weight (`nRequestsUsed` vs `nRequestsCap`) scales with lifetime volume. **Waiting does not restore the cap.** After `Too many cumulative requests` / request-weight errors the bot logs **once** and backs off **all signed writes** for `max(MIN_REPLACE_S, 30s)`. It does not retry every book tick, does not buy `reserveRequestWeight`, and does not place taker-unlock spam.

Current ops: run **ANTH-only** until weight recovers and NY RTH for SNDK. The default `COINS` example still lists `io:ANTH,io:SNDK`.

WS `l2Book` for both coins. REST book only if WS is stale >15s. Inventory from `clearinghouseState` with `dex:"io"`. Open-order queries always send `dex:"io"`. A null/429 response does **not** wipe the local rest cache.

### Fill-rate / +3s markout (logging only)

Live prints greppable `FILL_DIAG {...}` JSON lines. Sizes and two-sided quote prices are unchanged. Flatten stays maker ALO (no IOC).

Per fill (after 3s, using the already-subscribed `l2Book`; no extra WS):

- `coin` (`io:ANTH` / `io:SNDK`), `side` (buy/sell), `fill_px`, `mid_at_fill`, `mid_3s`
- `markout_bps`: buy `(mid_3s - fill_px)/fill_px * 1e4`; sell `(fill_px - mid_3s)/fill_px * 1e4`. If the book is stale >15s at +3s, `mid_3s` is null and markout is skipped.
- `spread` (ask−bid) and `spread_bps` vs mid at fill time
- counters on the same row: `quotes`, `fills`, `fill_rate` (`fills/quotes`, null if no quotes)

ANTH and SNDK never share a bucket. SNDK rows also have `session=rth` or `session=ah`. Regular hours are Mon–Fri 09:30–16:00 `America/New_York`. **No holiday calendar** — a weekday cash holiday is still tagged `rth`. After-hours markout is dirty: it is logged and kept in the AH bucket only; it is never folded into an RTH average.

Fills come from the official `userFills` WS on the same connection (snapshot ignored) with a REST `userFills` poll as backup. Quote counts increment after an accepted ALO rest (two-sided or flatten). There is no IOC flatten.

### Dead-man (account-wide)

While `live` is running the bot sends `scheduleCancel` at **now+20s** and refreshes **only when remaining time is under 8s** (not every few seconds). Official minimum is 5s. On 429 it falls back to **+6s** (clamped ≥5s). A dead-man failure caused by request weight is logged; it does not tight-loop.

**This is account-wide.** When the timer fires, Hyperliquid cancels **all** open orders on the master account — not just `io:ANTH` / `io:SNDK`.

On a clean stop the bot sends `scheduleCancel` **without** `time` (clears the timer) after cancelling its rests. If info is rate-limited it leaves a +6s cancel instead of assuming the book is empty.

### Agent / master signing

The userscript signs with Entropy's local IndexedDB **agent**, not `window.ethereum`. The VPS bot does the same job with env vars:

| Variable | Role |
| --- | --- |
| `HYPERLIQUID_PRIVATE_KEY` | Signing key. Master **or** an approved API/agent key. Never logged. |
| `HYPERLIQUID_ACCOUNT` | Master address that holds positions. Required when the key is an agent. |

Official SDK path (verified against `hyperliquid-python-sdk` + docs):

```text
Exchange(wallet=agent_key, account_address=master, vault_address=None)
sign_l1_action(..., vaultAddress=None)
```

`vaultAddress` is **only** for vault / subaccount trading and is hashed into the connection id. Putting the master there is wrong. An approved agent signs L1 actions with its own key; the exchange maps it to the master via `extraAgents`. Info queries (`clearinghouseState`, `frontendOpenOrders`) use `HYPERLIQUID_ACCOUNT`.

Approve the agent once on entropy.io / Hyperliquid (same as generating the userscript agent). The master must also have approved the Entropy builder at fee 0% (the userscript's `approveBuilderFee`).

### HIP-3 asset IDs

Looked up every run (do not hardcode forever):

```
asset_id = 100000 + perp_dex_index * 10000 + index_in_meta
```

- `perp_dex_index` = index of `{"name":"io"}` in `{"type":"perpDexs"}` (currently **10**).
- `index_in_meta` = index in `{"type":"meta","dex":"io"}` (ANTH=1, SNDK=2; skip delisted OAI/IONQ).
- Names are case-sensitive: `io:ANTH`, `io:SNDK`.

### Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"   # optional, for pytest

cp .env.example .env      # edit locally; never commit secrets

python -m entropy_bot status          # public API, no key
python -m entropy_bot paper           # WS quotes, paper fills, no signing
python -m entropy_bot paper --seconds 15
python -m entropy_bot live            # refuses unless LIVE=1 and a key
python -m entropy_bot cancel          # cancel this bot's cloIDs (needs key)
```

`status` prints io meta, books, Entropy Partner T4, self rebate, growth mode, and default TIF — no key required.

### VPS

```bash
# on the VPS, as a dedicated user
git clone https://github.com/a3165458/entropy-bot
cd entropy-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# set LIVE=1
# set HYPERLIQUID_PRIVATE_KEY to the API/agent key (0x…)
# set HYPERLIQUID_ACCOUNT to the master 0x… that holds ANTH/SNDK
# QUOTE_NOTIONAL_USD=50
# MIN_REPLACE_S=12
# Current ops until request weight recovers + NY RTH for SNDK:
# COINS=io:ANTH

# tmux (simple)
tmux new -s entropy
python -m entropy_bot live
# detach: Ctrl-b d

# or systemd — Restart=on-failure; the dead-man (+20s, account-wide) covers a hard kill
```

If the process dies without a clean stop, `scheduleCancel` fires and cancels **every** resting order on that master.

### Config (env / `.env`)

| Variable | Default | Notes |
| --- | --- | --- |
| `LIVE` | `0` | Live only if `1` **and** a private key is set |
| `HYPERLIQUID_PRIVATE_KEY` | unset | Agent or master key. Required for `live` / `cancel`. Never commit |
| `HYPERLIQUID_ACCOUNT` | derived from key | Master address. Set this when the key is an agent |
| `COINS` | `io:ANTH,io:SNDK` | Case-sensitive; foreign venues rejected. Current ops: ANTH-only until request weight recovers and NY RTH for SNDK |
| `MIN_REPLACE_S` | `12` | Per-coin seconds between cancel+replace while a rest is live. Flatten ALO uses the same gate |
| `QUOTE_NOTIONAL_USD` | `50` | Notional per side (live and paper), ≥ $10 |
| `QUOTE_OFFSET_TICKS` | `2` | Legacy. Live ignores it |
| `MAX_LEVERAGE` | `2` | Capped below each market max (ANTH 3 / SNDK 10) |
| `FEE_TIER` | `4` | Official HL perp volume tier (0.028% / 0%) |
| `ENTROPY_TIER` | `4` | Entropy Partner. Label only unless other rebate env overrides |
| `ENTROPY_SELF_REBATE` | `2.0` | 200% of Entropy's share of the HIP-3 fee (not the full user fee) |
| `ENTROPY_REFERRAL_REWARD` | `1.0` | Display / future invitee reward. Not applied to own fills |
| `ENTROPY_REFERRED_USER_BENEFIT` | `0` | Optional extra on own fills. Default off |
| `REFERRAL_DISCOUNT` | `0` | Optional HL-side gross taker multiplier |
| `MAKER_REBATE_BPS` | unset | Optional HL ALO maker-share rebate in bp |
| `HYPERLIQUID_API_URL` | official mainnet | Official host required |
| `HYPERLIQUID_WS_URL` | official mainnet | Official host required |

Markets are **isolated-only**. Live sets isolated leverage per coin and never uses cross. Default TIF is ALO for two-sided quotes and flatten/exit. IOC is refused.

### Tests

```bash
pytest
```

---

## 中文

### 这是什么

**真正的实盘机器人是这个 Python 包**，只走官方接口。Chrome 油猴脚本是可选/遗留，实盘做市不要再依赖它。

只交易 Hyperliquid 上 **EntropyIO HIP-3**：

- `io:ANTH`（逐仓，最高杠杆 3）
- `io:SNDK`（逐仓，最高杠杆 10）

DEX 名是 **`io`**。不要用 `xyz` 或 `vntl`。

接口：`https://api.hyperliquid.xyz` 与 `wss://api.hyperliquid.xyz/ws`。

### 实盘策略（从 userscript 1.6.2 移植）

每币最多 1 买 + 1 卖。空仓双边 ALO：价差 ≤2 tick 贴买卖一；价差 >2 tick 往中间贴（例 1985.00/1986.90/tick 0.1 → 买 1985.8 卖 1986.1）。有仓只减仓：`<6s` 远档 ALO，`≥6s` 中间 ALO，超时只撤换 ALO（90s），**不 IOC 吃单**。空仓同价挂 45s 无成交则重挂。ALO 被拒不停止。Builder 是 Entropy、费率 0。挂单还在时，盘口跳动不会立刻撤换；每币至少隔 `MIN_REPLACE_S`（默认 12 秒）才 cancel+replace。平仓走同一条 maker ALO 路径，没有 IOC 旁路。

请求权重（`Too many cumulative requests`）等不等都不会把额度补回来。命中后只打一次日志，签名写操作退避 `max(MIN_REPLACE_S, 30s)`，不会每个 book tick 重试，也不买 `reserveRequestWeight`、不加 taker-unlock。当前运维：权重恢复且 SNDK 进入纽约 RTH 之前只跑 **ANTH**；示例 `COINS` 仍列出两个币。

Tick 从盘口买卖价增量推断，不用 `tick_size(mid)`。

实盘只多打 `FILL_DIAG` JSON 行：成交价、成交时 mid、+3s mid、方向化 markout（买后 mid 涨为正）、价差、以及按币分开的报价次数 / 成交次数 / fill rate（含 maker 平仓成交）。SNDK 带 `session=rth|ah`（纽约时间周一至周五 09:30–16:00 为 rth，**不含节假日日历**）。AH markout 单独桶，不并进 RTH。报价次数在 ALO 挂单被接受后累加（双边和平仓侧都算）。

### 签名 / agent

`HYPERLIQUID_PRIVATE_KEY` 可以是主钱包，也可以是已批准的 API/agent 私钥（和网页 IndexedDB agent 同类）。`HYPERLIQUID_ACCOUNT` 是持仓的主地址。官方 SDK：用 agent 钥签名，`vaultAddress=None`；`vaultAddress` 只用于金库/子账户，**不要**填主地址。查询仓位和挂单用主地址。

### Dead-man（账户级）

运行中把 `scheduleCancel` 设到 **现在+20s**，只在剩余时间 **< 8s** 时续期（不要隔几秒就刷）。官方最短 5s；429 则 +6s。权重耗尽导致的续期失败只记日志，不 tight-loop。到点后会撤销该主账户上的**全部**挂单，不限于 ANTH/SNDK。干净退出时发送不带 `time` 的 `scheduleCancel` 以清除定时。

### 命令

```bash
python -m entropy_bot status   # 公共行情，不需要私钥
python -m entropy_bot paper    # 纸面报价，不签名
python -m entropy_bot live     # 实盘；缺 LIVE=1 或私钥会直接拒绝
python -m entropy_bot cancel   # 撤销本机器人的 cloid
```

VPS：`.env` 里设 `LIVE=1`、agent 私钥、主地址，用 tmux 或 systemd 跑 `python -m entropy_bot live`。进程被硬杀时靠账户级 dead-man 撤单。

### 安全

- 仓库里没有密钥。把私钥只放在本地 `.env`。
- 日志只打地址，不打印私钥。
- `live` 没有 `LIVE=1` + 密钥会退出。
- 载荷里不会出现 `xyz:SNDK` / `vntl:ANTHROPIC`。
