# entropy-bot

Paper-first Hyperliquid bot for **EntropyIO HIP-3** markets: `io:ANTH` and `io:SNDK`.

EntropyIO 的 HIP-3 永续在 Hyperliquid 上的 DEX 名是 **`io`**（`fullName` = EntropyIO）。本仓库只交易这两个币对。`xyz:SNDK`、`vntl:ANTHROPIC` 是别的场，`io:OAI` / `io:IONQ` 已下架，全部拒绝。

---

## English

### What this is

A small Python 3.11+ CLI that talks **only** to the official Hyperliquid HTTP + WebSocket APIs:

- HTTP: `https://api.hyperliquid.xyz`
- WS: `wss://api.hyperliquid.xyz/ws`

No entropy.io scraping, no third-party order relays.

**Paper / shadow mode is the default.** Live signing happens only when `LIVE=1` **and** `HYPERLIQUID_PRIVATE_KEY` is set.

### Cheap path (WS + ALO + growth mode)

1. **One WebSocket** multiplexes `l2Book` for both coins. REST is used for `perpDexs`, `meta` / `metaAndAssetCtxs` with `dex: "io"`, and isolated user state — then we stay on WS.
2. **Default TIF is ALO** (post-only / add-liquidity-only). The bot never defaults to IOC or market.
3. Both listings have `growthMode=enabled` and `deployerFeeScale=1.0`. Official HIP-3 math:

   `scaleIfHip3 = 2` (because scale ≥ 1 → `deployerFeeScale * 2`), then growth mode `× 0.1`.

   Default fee model is **volume tier 4** (14d weighted volume > $500M): official perp base **taker 0.028% / maker 0.000%**. After HIP-3 ×2 and growth ×0.1 that is **taker 0.0056% / maker 0.000%**. ALO is the only default TIF so quotes stay on the maker/rebate side. Optional `REFERRAL_DISCOUNT` multiplies taker only (default 0 — 返佣 is not hardcoded). Optional `MAKER_REBATE_BPS` credits ALO fills (official maker-share tiers: 0.1 / 0.2 / 0.3 bp). The live command prints this estimate before every signed order.

4. Markets are **isolated-only** (`onlyIsolated`, `marginMode=strictIsolated`). Collateral is Hyperliquid USDC (`collateralToken` 0). Live mode sets isolated leverage per coin and never uses cross.

### HIP-3 asset IDs

Looked up every run (do not hardcode forever):

```
asset_id = 100000 + perp_dex_index * 10000 + index_in_meta
```

- `perp_dex_index` = index of `{"name":"io"}` in `{"type":"perpDexs"}` (EntropyIO is currently **10**).
- `index_in_meta` = index in `{"type":"meta","dex":"io"}` (ANTH=1, SNDK=2; skip delisted OAI/IONQ).
- Names are case-sensitive: `io:ANTH`, `io:SNDK`.

Writes use standard signed L1 `order` / `cancel` / `cancelByCloid`.

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

`status` prints io meta, mark / oracle / mid, funding, open interest, 24h volume, best bid/ask, spread in bps, fee tier, growth mode, all-in taker/maker, and that default TIF is ALO — for **both** `io:ANTH` and `io:SNDK`.

### Config (env / `.env`)

| Variable | Default | Notes |
| --- | --- | --- |
| `LIVE` | `0` | Live only if `1` **and** a private key is set |
| `HYPERLIQUID_PRIVATE_KEY` | unset | Required for `live` / `cancel` |
| `HYPERLIQUID_ACCOUNT` | derived from key | Optional; `status` can show isolated positions |
| `COINS` | `io:ANTH,io:SNDK` | Case-sensitive; foreign venues rejected |
| `QUOTE_NOTIONAL_USD` | `50` | Paper notional per side |
| `QUOTE_OFFSET_TICKS` | `2` | ALO distance behind the touch |
| `MAX_LEVERAGE` | `2` | Capped below each market max (ANTH 3 / SNDK 10) |
| `FEE_TIER` | `4` | Official perp volume tier (tier 4 = 0.028% / 0%) |
| `REFERRAL_DISCOUNT` | `0` | Taker-only: `taker * (1 - discount)`. Do not invent 返佣 |
| `MAKER_REBATE_BPS` | unset | Optional ALO rebate in bp (0.1 = -0.001%). Else maker = 0 |
| `HYPERLIQUID_API_URL` | official mainnet | Official host required |
| `HYPERLIQUID_WS_URL` | official mainnet | Official host required |

Live quotes use a **tiny** notional (`min(QUOTE_NOTIONAL_USD, 15)`, at least the $10 exchange minimum).

### Tests

```bash
pytest
```

Coverage includes coin-name resolution, **tier-4 + growth-mode fee math**, optional referral/rebate layers, ALO payloads with asset IDs from mocked `perpDexs`+`meta`, live-mode refusal without a key, and a hard ban on emitting `xyz:SNDK`.

---

## 中文

### 这是什么

面向 Hyperliquid 上 **EntropyIO HIP-3** 的小型 Python 机器人，只交易：

- `io:ANTH`（Anthropic 预上市永续，最高杠杆 3，仅逐仓）
- `io:SNDK`（SanDisk 永续，最高杠杆 10，仅逐仓）

DEX 名称是 **`io`**，全称 EntropyIO。不要用 `xyz` 或 `vntl`。

只走官方接口：`https://api.hyperliquid.xyz` 与 `wss://api.hyperliquid.xyz/ws`。不爬 entropy.io，也不走任何第三方报单中继。

### 低成本路径（必须遵守）

1. **一条 WebSocket** 同时订 `io:ANTH` 和 `io:SNDK` 的 `l2Book`；REST 只用于 `perpDexs`、带 `dex:"io"` 的 `meta` / `metaAndAssetCtxs`、以及用户逐仓状态。REST 要克制。
2. **默认 TIF 是 ALO**（只挂不吃）。不会默认 IOC / 市价。
3. **Growth Mode + volume 4 档**：`deployerFeeScale=1` → HIP-3 `scaleIfHip3=2`，再乘 growth `0.1`。官方永续 4 档底价 taker 0.028% / maker 0%。全部算完约 **taker 0.0056% / maker 0%**。默认只挂 **ALO**，走 maker/返佣一侧。`REFERRAL_DISCOUNT` 只乘 taker（默认 0，不写死返佣）。`MAKER_REBATE_BPS` 可选，设置后才把 maker 返佣记到 ALO 成交上。实盘下单前会打印预估手续费。
4. **默认纸交易 / 影子模式**。只有 `LIVE=1` 且设置了 `HYPERLIQUID_PRIVATE_KEY` 才会签名实盘单。
5. 这两个市场都是 **strictIsolated**。保证金是 Hyperliquid USDC（`collateralToken` 0）。实盘按币设置逐仓杠杆。

### 资产 ID

每次运行都重新解析，不要写死：

```
asset_id = 100000 + perp_dex_index * 10000 + index_in_meta
```

`perpDexs` 里 `io` 目前是 index **10**（以当场查询为准）。`meta` 用 `{"type":"meta","dex":"io"}`。币名大小写敏感。

### 命令

```bash
python -m entropy_bot status   # 公共行情，不需要私钥
python -m entropy_bot paper    # 纸面 ALO 报价，不签名
python -m entropy_bot live     # 实盘；缺 LIVE 或私钥会直接拒绝
python -m entropy_bot cancel   # 撤销本机器人的 cloid
```

### 安全

- 仓库里没有密钥。把私钥只放在本地 `.env`。
- `live` 没有密钥会退出。
- 载荷里不会出现 `xyz:SNDK` / `vntl:ANTHROPIC`。
