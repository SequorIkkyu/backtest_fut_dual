# 回测基础设施用户指南

**日期：** 2026-08-09
**契约版本：** `0.13.0` | **遥测 schema：** `0.6.0`

## 概述

这是一个面向双订单簿期货策略的**做市-对冲回测基础**——一个报价合约（被动做市）与一个相关联的对冲合约（主动对冲）。它提供两条执行路径：

| 路径 | 模块 | 状态 | 用途 |
|------|--------|--------|---------|
| **生产重放** | `production_replay.py` | 受支持的 S0 | 严格因果重放、研究遥测、经济资格 |
| **旧版** | `backtest.py` / `market.py` / `strategy.py` | 冻结兼容 | 历史策略、示例驱动 |

生产重放路径是**唯一的 S0 证据路径**。它绝不导入旧版路径。旧版代码被保留以便现有策略仍可运行，但不能产生 S0 经济、压力或晋升证据。

### 运行时

- **Python：** 3.10.13（`py310` conda 环境）
- **PYTHONPATH：** `D:\OneDrive\Python\Fut_HFT\backtest`

```powershell
$env:PYTHONPATH = "D:\OneDrive\Python\Fut_HFT\backtest"
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run_acceptance
```

### 关键依赖

`pandas`、`numpy`、`matplotlib`、`plotly`、`polars`、`pyarrow`、`fastparquet`、`joblib`、`numba`

---

## 架构

```
                  ProductionReplayAdapter.run()
  (sole S0 evidence runner — production_replay.py)
                            │
    ┌───────────────────────┼───────────────────────┐
    │                       │                       │
    ▼                       ▼                       │
ValidatedMarketData   CausalIngress       ProductionMakerHedgePolicy
(foundation_loader)   (ingress.py)        (user-supplied, two methods)
    │            declared availability              │
    │                  ordering,                    │
[apply_ingress_stress] hash-addressed               │
    │                  snapshots                    │
    │                       │                       │
    └───────────┬───────────┘                       │
                │                                   │
          IngressBatch loop ────────────────────────┘
                │        schedule_decision →
                │        select_signal_ids → propose
                │
                ▼
     DualBookFoundation  (foundation_api.py — public S0 facade)
     ┌──────────────────────────────────────────────────┐
     │  PassiveMatchingService  (passive_matching.py)   │
     │    price-time maker fill allocation              │
     │                                                  │
     │  DepthExecutionService   (execution.py)          │
     │    aggressive hedge + EOD depth consumption      │
     │                                                  │
     │  IntentLifecycleService  (lifecycle.py)          │
     │    order state machine, capacity reservations    │
     │                                                  │
     │  DualLegLedger           (ledger.py)             │
     │    positions, residual risk, fill-cost tracking  │
     │                                                  │
     │  TelemetryEmitter ──► canonical JSONL artifacts  │
     │    (telemetry.py)                                │
     └──────────────────────────────────────────────────┘
                │
                ▼  (after calendar EOD)
     ┌───────────────────────────────────────────────────┐
     │  PnlAttributionService   (pnl_attribution.py)     │
     │    maker-capture / leg-price / waterfall P&L      │
     │                         │                         │
     │  ResearchTelemetryEmitter (research_telemetry.py) │
     │    cross-table validation + manifest hashing      │
     └───────────────────────────────────────────────────┘
                │
                ▼
     OperationalReplayResult
       .economics_eligible  (fail-closed conjunction)
       .telemetry           (canonical run result)
       .pnl_attribution     (reconciled waterfall)
       .research_telemetry  (cross-table result + manifest hash)

Legacy path (compatibility only, frozen):
  backtest.py ──► Market (market.py) ──► Strategy (strategy.py) ──► PnL / reporting
```

### 模块映射

| 模块 | 职责 |
|--------|------|
| `foundation_contracts.py` | 不可变词汇表：配置、快照、意图、证据、错误 |
| `foundation_api.py` | 公开的 `DualBookFoundation` 门面 + `ProductionMakerHedgePolicy` 协议 |
| `foundation_loader.py` | 严格行情数据校验 → `ValidatedMarketData` |
| `ingress.py` | `CausalIngress`：已声明可用性排序（`recv_ts`）、哈希寻址快照 |
| `production_replay.py` | `ProductionReplayAdapter`：串联加载器 → 接入 → 门面 → 遥测 |
| `execution.py` | `DepthExecutionService`：主动对冲/EOD 深度消耗 |
| `lifecycle.py` | `IntentLifecycleService`：订单状态机、容量预留 |
| `ledger.py` | `DualLegLedger`：带符号仓位、残余风险、成交成本跟踪 |
| `passive_matching.py` | `PassiveMatchingService`：价格-时间做市成交分配 |
| `pnl_attribution.py` | `PnlAttributionService`：做市捕获 / 腿价 / 瀑布盈亏 |
| `telemetry.py` | `TelemetryEmitter`：规范工件写入、不变量检查 |
| `research_telemetry.py` | 研究 schema 导出、跨表校验、清单哈希 |
| `stress.py` | `StressScenario`：延迟、波动率、费用、参与率、基差维度 |
| `reporting.py` | 由规范表生成的事后报告 |
| `cycles.py` | 周期规范化器：分桶成交记录、盈亏汇总 |
| `sessions.py` | 会话日历：日盘/夜盘窗口、交易日滚动 |
| `grid.py` | 信号准备与网格基础设施 |
| `market.py` | 旧版撮合引擎（冻结兼容） |
| `strategy.py` | 旧版盈亏引擎（冻结兼容） |
| `backtest.py` | 旧版回测运行器（冻结兼容） |

---

## 快速开始：运行测试

```powershell
# 完整验收套件（旧版 + 基础）
$env:PYTHONPATH = "D:\OneDrive\Python\Fut_HFT\backtest"
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run_acceptance

# 仅旧版套件
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.run

# 仅基础套件
& C:\Users\sgjia\miniconda3\envs\py310\python.exe -B -m common.tests.foundation.run

# 使用 pytest（若已安装；从 common/ 运行）
pytest tests/
```

预期：**213 项测试通过**（72 旧版、141 基础）。任何失败都意味着引擎原语发生了变更——在信任结果之前应先排查。

---

## 路径 1：生产重放（受支持的 S0）

这是产生 S0 经济证据的唯一路径。它遵循一条严格的流水线：

1. **校验**行情数据，通过严格加载器
2. **接入**事件，通过已声明可用性的因果排序
3. **决策**，通过用户提供的策略，在每个实质事件上
4. **执行**，通过公开的 `DualBookFoundation` 门面
5. **平仓**，在日历 EOD
6. **归因**盈亏，使用独立证明的输入
7. **封存**研究遥测，通过跨表校验

### 可用性时钟与原始快照状态

`recv_ts` 是基础的已声明可用性字段。当某来源拥有经认证的接收时钟时，该字段就携带它。对于 `E:\FinData\HFT\ticks` 中被接受的五档原始快照，`RawSnapshotAdapterConfig` 同时保留两个原始时钟，并将可用性推导为 `max(exchtime, timestamp)`；这是一种重放约定，而非延迟测量。

对已声明文件使用 `read_raw_snapshot_market_data()`，对内存内确定性输入使用 `adapt_raw_snapshot_frames()`。该适配器要求每个已配置合约有一个或多个已声明来源，保留每个原始文件哈希与行序号，校验五档 tick 网格，并按 `(可用时间, source_id, 行序号)` 分配运行全局 `source_seq`。其撮合器持有的成交被标记为 `snapshot_interval_proxy_evidence`，而非观测成交。它们仅在已声明的 `snapshot_interval_queue_proxy_v1` / `bid_then_ask_v1` 模型下有效；它们不作出实盘成交或物理延迟的声明。该代理的波动率压力目前被拒绝，而非静默地变换区间证据。

### 第 1 步：配置运行

```python
from datetime import date

from common.foundation_contracts import ExecutionModelRef, TrialDeclaration
from common.production_replay import (
    EconomicReplayInputs, ProductionReplayAdapter, ProductionReplayConfig,
)
from common.stress import StressScenario

# 在配置运行之前，先构造已声明的契约对象。
config = ProductionReplayConfig(
    run_id="my-run-001",
    hedge_mapping=hedge_mapping,           # HedgeMappingSpec
    instrument_specs=(q_spec, h_spec),     # InstrumentSpec for each product
    execution_models=(depth_model,),       # ExecutionModelConfig
    default_execution_model=ExecutionModelRef("depth", "1.0.0"),
    capacity_envelopes=(envelope,),        # CapacityEnvelope
    artifact_root="./artifacts",
    session_date=date(2025, 1, 2),
    trial=TrialDeclaration(...),           # trial identity + provenance
    provenance_artifacts={
        "configuration": {"policy": "my-policy:1.0.0", "market_data": "sha256:..."},
        "code": "sha256:...",
    },
    # 可选：
    stress_scenario=StressScenario("latency", "1.0.0", market_data_delay_ms=5.0),
    economic_inputs=EconomicReplayInputs(...),  # 用于盈亏资格
    research_export=True,                       # 启用研究遥测
    registered_signal_ids=frozenset({"my-signal"}),
    max_execution_book_age_ms_by_product={"Q": 1_000.0, "H": 1_000.0},
)
```

`provenance_artifacts` 必须包含一个 `configuration` 条目。上述占位符代表完全构造好的契约值；它们不是由适配器推断出来的。

### 第 2 步：编写策略

实现 `ProductionMakerHedgePolicy` 协议——两个方法：

```python
from common.foundation_api import (
    ProductionMakerHedgePolicy, PolicyProposal, PolicyTrigger
)
from common.foundation_contracts import (
    DecisionContext, MakerHedgeIntentBatch,
    OrderIntent, OrderRole, OrderSide,
)

class MyPolicy:
    def select_signal_ids(self, available_signals):
        """声明要消费哪些信号 ID。"""
        return tuple(s.signal_id for s in available_signals if s.signal_id == "my-signal")

    def propose(self, context: DecisionContext) -> PolicyProposal:
        """仅使用 context 中的值返回一个做市/对冲批次。"""
        # 读取已绑定的信号值。该信号同时提供评分与策略的报价价格。
        score = 0.0
        quote_price = None
        for signal in context.consumed_signal_values:
            score = float(signal.payload.get("score", 0.0))
            quote_price = float(signal.payload["quote_price"])

        if score > 0.5:
            if quote_price is None:
                raise ValueError("my-signal must supply quote_price")
            maker = OrderIntent(
                intent_id=f"{context.decision_id}:maker",
                run_id=context.run_id,
                decision_id=context.decision_id,
                hedge_pair=context.hedge_pair,
                role=OrderRole.MAKER,
                side=OrderSide.BUY,
                product=context.quoted_product,
                requested_qty=1,
                limit_price=quote_price,
            )
            batch = MakerHedgeIntentBatch(
                maker_intent=maker,
                maker_capacity_envelope_id="q-maker-capacity",
            )
        else:
            batch = MakerHedgeIntentBatch()  # 显式的不行动

        return PolicyProposal(
            batch=batch,
            decision_attributes={"action": "quote" if score > 0.5 else "hold"},
            triggers=(PolicyTrigger(f"{context.decision_id}:trigger", {"score": score}),),
        )
```

**规则：**
- `select_signal_ids` 声明策略*将*消费哪些信号
- `propose` 仅通过 `consumed_signal_values` 与 `signal_value(ref)` 接收不可变的、绑定于上下文的值
- `BookSnapshotRef` 有意地不含深度；策略必须自行提供因果可得的 `limit_price`，例如一个已绑定的信号值
- 一个做市批次必须声明一个 `maker_capacity_envelope_id`，它须匹配已配置的报价合约容量包络之一
- 对一个显式的不行动决策，返回一个空的 `MakerHedgeIntentBatch()`
- 触发 ID 必须在每个决策内唯一

### 第 3 步：加载行情数据

```python
import pandas as pd

from common.foundation_loader import (
    MarketDataValidationConfig, validate_market_data
)

validation_config = MarketDataValidationConfig(
    declared_contract_universe=("Q", "H"),
    book_levels=1,
    source_timezone="Asia/Shanghai",  # 若时间戳已带时区，则为 None
)

market_data = validate_market_data(
    pd.DataFrame(rows),  # 列：contract, exchange_ts, recv_ts, source_seq,
    validation_config,   #          bidpx0, bidvol0, askpx0, askvol0,
)                        #          totalvol, totalvalue, passive_trades（可选）
```

每一行都必须有：`contract`、`exchange_ts`、`recv_ts`、`source_seq`、每一档的买/卖价格与成交量，以及累计 `totalvol`/`totalvalue`。`recv_ts` 是已声明可用性，合并后的输入必须按该值排序，并具有严格单调、运行全局的 `source_seq`。来源适配器必须单独保留原始来源身份与行序号。

`passive_trades` 是可选的；当提供时，它是报价合约主动方成交的列表，用于经验证的被动做市成交。它用于逐笔成交级别的输入，不得从快照累计流量推断。校验器检查单调可用性顺序、正的非交叉价格、非负深度、非递减累计成交量，以及合约全集成员资格。

对于被接受的五档原始快照，请使用版本化适配器，而非手工构造规范行：

```python
from common.foundation_loader import (
    RawSnapshotAdapterConfig, RawSnapshotFile, read_raw_snapshot_market_data,
)

raw_config = RawSnapshotAdapterConfig(
    declared_contract_universe=("Q", "H"),
    proxy_interval_contracts=("Q",),  # 被动做市 / 报价合约
    source_timezone="Asia/Shanghai",
    tick_by_contract={"Q": 0.2, "H": 0.2},
    multiplier_by_contract={"Q": 300.0, "H": 300.0},
)
market_data = read_raw_snapshot_market_data(
    (
        RawSnapshotFile("q-session", r"E:\FinData\HFT\ticks\Q.csv", "Q"),
        RawSnapshotFile("h-session", r"E:\FinData\HFT\ticks\H.csv", "H"),
    ),
    raw_config,
)
```

第一个来源行没有区间。只有已声明的 `proxy_interval_contracts` 产生累计流量区间；其他已声明来源仍提供其保留订单簿，用于对冲与估值决策。生产重放要求该集合恰好是该对中的报价合约。正数代理腿累计流量增量，会在当前快照的正数五档价格包络（最低买价至最高卖价）内，变成保量的、位于有效 tick 的区间桶。一个偏离深度的桶默认会拒绝该适配；`off_depth_interval_disposition="drop"` 会把违规来源行从重放中丢弃，使其无法产生代理成交。零成交量、重置以及其他无效区间处置均为显式配置。使用 `cumulative_reset_disposition="drop"` 时，重置行被丢弃，后续行开始一个新的来源限定累计纪元，因此累计校验不会把它们与此前的计数器纪元比较。每次原始适配器丢弃都会作为 `LoaderValidationIssue` 返回在 `ValidatedMarketData.issues` 中，附有其原始来源 ID、行序号、原因码与 `drop` 处置。

返回的 `source_provenance` 是一个类型化、不可变的清单。它记录完整的适配器配置，包括已声明全集、代理区间合约、tick 与乘数表、模型与可用性规则、各处置、来源身份、行范围、文件内容哈希权威，以及一个 `adapted_replay_hash`。文件读取器在内存中解析已经哈希过的字节；它不会重新打开路径。`adapted_replay_hash` 覆盖将被交给接入的确切规范订单簿事件负载（时序、序列、保留深度与任何代理区间）。生产会将该不可变事件元组物化一次，在重放前校验其哈希，然后重放这同一个元组。它还针对清单校验每个区间的来源 ID/哈希、合约、行序号与模型字段，并针对对应的 `InstrumentSpec` 校验每个适配器 tick/乘数。`adapt_raw_snapshot_frames()` 仍是确定性的内存内测试辅助；其由调用方提供的哈希会被生产重放拒绝。

### 第 4 步：运行

```python
adapter = ProductionReplayAdapter(config)
result = adapter.run(market_data, MyPolicy())

print(f"Decisions: {len(result.decision_ids)}")
print(f"EOD: {result.eod_completion.disposition.value}")
print(f"Canonical eligible: {result.telemetry.eligible}")
print(f"Research eligible: {result.research_telemetry.eligible}")
print(f"Economics eligible: {result.economics_eligible}")
```

### 第 5 步：读取结果

```python
from common.telemetry import load_canonical_table
from pathlib import Path

decisions = load_canonical_table(Path("./artifacts") / config.run_id, "decisions")
fills     = load_canonical_table(Path("./artifacts") / config.run_id, "fills")
outcomes  = load_canonical_table(Path("./artifacts") / config.run_id, "outcome_pnl")

for row in fills:
    print(f"{row['fill_id']}: {row['product']} qty={row['fill_qty']} @ {row['fill_price']}")
```

### `OperationalReplayResult`

| 字段 | 含义 |
|-------|---------|
| `telemetry.eligible` | 规范工件通过不变量检查 |
| `pnl_attribution` | 已对账的盈亏瀑布（若提供了 `economic_inputs`） |
| `research_telemetry.eligible` | 研究 schema 通过跨表校验 |
| `execution_freshness_eligible` | 所有执行使用的订单簿都在时限内 |
| `semantic_compliance_eligible` | 所有 S0 语义检查通过 |
| `economics_eligible` | **最终关口**：以上全部 + 经验证的经济证据 |

`economics_eligible` 是六个条件的失败关闭合取。它是授权 S0 经济声明的唯一关口。

---

## 路径 2：旧版回测（冻结兼容）

旧版路径被保留，以便历史策略继续运行。它不能产生 S0 证据。

```python
from common.backtest import Backtest
from common.market import Market
from common.strategy import Strategy

bt = Backtest()
bt.load_data("Q", "path/to/Q.csv", multiplier=10000, tick=0.005)
bt.load_data("H", "path/to/H.csv", multiplier=10000, tick=0.005)

# 定义策略（继承 Strategy）
class MyStrategy(Strategy):
    def on_step(self, market, pair_market):
        # 旧版决策逻辑
        ...

bt.run(MyStrategy)
```

**费用约定：** `FEE`（费率）与 `FEE_LOT`（每手固定美元）表示**完整往返**成本，在**平仓成交时一次性**收取。开仓不加收费用。

---

## 压力场景

`StressScenario` 应用可独立组合的压力维度。每个维度都是一次纯变换——基础场景的所有维度都处于中性值。

```python
from common.stress import StressScenario

scenario = StressScenario(
    scenario_id="latency-50ms",
    version="1.0.0",
    market_data_delay_ms=50.0,       # 延迟订单簿事件
    signal_delay_ms=30.0,            # 延迟信号事件
    action_submission_delay_ms=5.0,  # 延迟订单提交
    action_arrival_delay_ms=10.0,    # 到达前的额外延迟
    participation_multiplier=0.8,    # 降低成交参与率
    fee_multiplier=0.9,              # 降低费用影响
    basis_shift=0.5,                 # 平移决策中间价
    volatility_multiplier=0.7,       # 压缩订单簿价差
    opening_session_disposition="skip",  # "allow" 或 "skip"
)
```

在 `ProductionReplayConfig` 上设置 `stress_scenario` 以应用它。基础场景（`is_base = True`）的所有维度为 0/1.0/"allow"。

**tick 保持：** 当 `volatility_multiplier ≠ 1.0` 时，压力价格会舍入到合约 tick（买价向下、卖价向上）。偏离 tick 的变换订单簿会失败关闭。

---

## 经济资格

要声明 `economics_eligible=True`，需通过 `EconomicReplayInputs` 提供经济证据：

```python
from common.production_replay import EconomicReplayInputs
from common.foundation_contracts import (
    PnlViewEvidence, ValuationMarkEvidence, PnlAccountingView
)

inputs = EconomicReplayInputs(
    marks_by_product={"Q": 100.5, "H": 99.5},
    accounting_view=PnlAccountingView("accounting", total_pnl=-1.0),
    cycle_view=PnlAccountingView("cycle", total_pnl=-1.0),
    accounting_evidence=PnlViewEvidence(
        "acct-evidence", "accounting", -1.0,
        "general-ledger", "1.0.0", "gl-close",
        calculated_at=eod_ts,
        source_artifact=signed_json_bytes,  # HMAC 签名的规范 JSON
    ),
    cycle_evidence=PnlViewEvidence(...),
    mark_evidence_by_product={
        "Q": ValuationMarkEvidence(
            "q-mark", "Q", 100.5,
            "settlement", "1.0.0", "q-settlement",
            observed_at=obs_ts,
            source_artifact=signed_json_bytes,
        ),
        "H": ValuationMarkEvidence(...),
    },
)
```

### 权威注册表

经济来源工件必须由部署所有注册表中的权威进行 **HMAC-SHA256 签名**：

```python
from common.production_replay import DeploymentEvidenceAuthorityRegistry
from common.foundation_contracts import ApprovedEvidenceAuthority

registry = DeploymentEvidenceAuthorityRegistry((
    ApprovedEvidenceAuthority("accounting-dept", "v1", accounting_key),
    ApprovedEvidenceAuthority("cycle-dept", "v1", cycle_key),
    ApprovedEvidenceAuthority("valuation-desk", "v1", valuation_key),
))

adapter = ProductionReplayAdapter(config, authority_registry=registry)
result = adapter.run(market_data, policy)
```

请从部署的密钥管理器获取这些字节密钥；不要硬编码生产密钥材料。独立产生的来源工件必须是规范的 `s0-economic-evidence-v1` JSON，具有预期的权威选择器、已声明值与 HMAC 签名。

`economics_eligible` 要求：
1. 规范遥测通过不变量
2. 盈亏归因在容差内对账
3. 研究遥测通过跨表校验
4. 执行新鲜度（订单簿年龄 ≤ 每个合约配置的上限）
5. 语义合规（溯源清单存在、证据哈希匹配、研究 schema 版本匹配）
6. 经验证的经济证据（签名的来源工件、权威独立性、时序约束、内容到值的绑定）

---

## 研究遥测

当 `research_export=True` 时，重放会在 `<artifact_root>/research/<run_id>/tables/` 下以 JSONL 形式发出 **10 张研究表**：

| 表 | 键身份 | 内容 |
|-------|-------------|---------|
| `decisions` | `decision_id` | 策略输入、订单簿引用、信号哈希、属性 |
| `book_events` | `event_id` | 订单簿事件时序与来源顺序 |
| `book_snapshots` | `snapshot_id` | 关联决策的订单簿状态，含前 K 档 |
| `orders` | `order_id` | 已声明的意图、生命周期事件、预留 |
| `fills` | `fill_id` | 做市/吃单/EOD 成交记录 |
| `hedge_executions` | `hedge_id` | 对冲与 EOD 执行结果 |
| `trigger_evaluations` | `trigger_id` | 策略触发审计 |
| `signal_snapshots` | `signal_snapshot_id` | 已消费的信号负载 |
| `outcome_pnl` | `row_id` | 盈亏瀑布、路由、库存时长 |
| `inventory_series` | `row_id` | 仓位/敞口时间线 |

在封存时，`_cross_table_errors()` 校验：
- **重复检测：** 单例业务 ID（decision_id、snapshot_id、order_id、fill_id、hedge_id、trigger_id）必须唯一
- **决策→快照连接：** 每个决策关联两个 `snapshot_reason="decision"` 快照，产品与 book_seq 匹配
- **做市队列证据：** 每个做市订单必须有非空的 `queue_ahead_submit`
- **信号因果性：** `available_at ≤ dec_ts` 与成员资格哈希校验
- **成交/对冲连接：** 成交关联订单，对冲关联触发
- **库存对账：** 由权威成交/对冲事件重建的仓位、敞口与残余风险，与存储值比较
- **结果路由校验：** 路由步骤必须遵循合法顺序（quote→fill→inventory→hedge→eod），无虚假声明
- **时长重算：** `inventory_time` 由已对账敞口重新计算
- **瀑布算术：** `episode_total = maker_capture + quoted_leg + hedge_leg − shortfall − fees + rebates`

研究清单（`meta/research_manifest.json`）包含每张表的 SHA-256 哈希与最终结果。其自身的哈希被记录在规范溯源中。

---

## 关键契约参考

这是对公开类型的简明参考。构造参数按当前契约名展示；完整定义与校验规则见 `foundation_contracts.py`。

### 不可变身份类型

| 类型 | 关键字段 | 用途 |
|------|--------|---------|
| `HedgePairRef` | `pair_id`、`quoted_product`、`hedge_product`、`hedge_mapping_id`、`hedge_mapping_version` | 双订单簿身份 |
| `BookSnapshotRef` | `product`、`book_seq`、`feed_seq`、`event_id`、`recv_ts`、`available_at`、`snapshot_id`、`snapshot_hash` | 不可变订单簿引用（不含深度） |
| `SignalSnapshotRef` | `signal_id`、`product`、`feed_seq`、`event_id`、`snapshot_id`、`snapshot_hash`、`available_at` | 不可变信号引用 |
| `CausalSignalSnapshot` | `ref: SignalSnapshotRef`、`payload: Mapping[str, Any]` | 绑定到决策的带值信号 |

### 决策与执行

| 类型 | 关键字段 | 用途 |
|------|--------|---------|
| `DecisionContext` | `run_id`、`decision_id`、`dec_ts`、`feed_seq`、报价/对冲产品与订单簿、已消费信号、输入年龄 | 不可变策略输入 |
| `OrderIntent` | `intent_id`、`run_id`、`decision_id`、`hedge_pair`、`product`、`role`、`side`、`requested_qty`、`limit_price`、`execution_model_ref` | 做市/对冲批次的一条腿 |
| `MakerHedgeIntentBatch` | `maker_intent`、`hedge_intent`、`maker_capacity_envelope_id` | S0 批次：可选做市 + 可选对冲 |
| `PolicyProposal` | `batch`、`decision_attributes`、`triggers` | 策略返回值 |
| `ExecutionResult` | `execution_id`、`intent_id`、`decision_id`、`status`、`requested_qty`、`filled_qty`、`residual_qty`、`levels`、`vwap`、`execution_model_ref` | 主动执行结果 |

### 证据与盈亏

| 类型 | 关键字段 | 用途 |
|------|--------|---------|
| `PnlViewEvidence` | `evidence_id`、`view_id`、`total_pnl`、方法论与版本、`source_artifact_id`、`calculated_at`、`source_artifact` | 独立证明的盈亏视图 |
| `ValuationMarkEvidence` | `evidence_id`、`product`、`mark`、方法论与版本、`source_artifact_id`、`observed_at`、`source_artifact` | 独立证明的估值标记价 |
| `ApprovedEvidenceAuthority` | `authority_id`、`key_id`、`authentication_key` | HMAC-SHA256 签名权威 |
| `PnlAttributionResult` | `waterfall_total`、`maker_capture`、`quoted_leg_price_pnl`、`hedge_leg_price_pnl`、`hedge_execution_shortfall`、`fees`、`rebates`、`economics_eligible` | 已对账盈亏 |

### 日历与合约

| 类型 | 关键字段 | 用途 |
|------|--------|---------|
| `SessionCalendar` | `calendar_id`、`timezone`、`version`、`windows`、`trading_day_rollover`、`eod_time`、`holidays` | 带 EOD 的交易日历 |
| `InstrumentSpec` | `product`、`tick`、`multiplier`、`calendar`、`fee_model_id`、`roll_mapping_id` | 每合约配置 |
| `HedgeMappingSpec` | `hedge_pair`、`quoted_risk_weight`、`hedge_risk_weight`、`quantity_tolerance` | 风险权重映射 |

### 盈亏瀑布

对于带符号成交量 `q`、乘数 `k`、成交价 `F`、决策参考价 `R` 与核算标记价 `M`：

- **做市捕获：** `q × (R − F) × k`（仅报价被动成交）
- **报价腿价格盈亏：** `q × (M − R) × k`
- **对冲腿价格盈亏：** `q × (M − R) × k`
- **对冲执行滑点：** `q × (F − R) × k`（被减）
- **净盈亏：** `maker_capture + quoted_leg + hedge_leg − shortfall − fees + rebates`

---

## 添加测试

将一个 `test_*` 函数放入匹配的测试文件。使用普通的 `assert`。

```python
def test_my_new_behavior():
    result = some_function(known_input)
    assert result == expected_output
```

从 `common/` 使用 pytest 运行：`pytest tests/`

---

## 延伸阅读

- [基础边界与兼容性策略](archive_2608/maker-hedger-foundation-boundary_26-08-08.md) ——
  模块所有权、契约版本化、日历/生命周期/执行细节
- [S0 运维补救计划](archive_2608/maker-hedger-s0-operational-remediation-plan_26-08-08.md) ——
  S0 验收关口、补救历史、已知注意事项
- [遥测 Schema](../contracts/telemetry_schema.md) ——
  规范表定义、工件布局、溯源要求
- [测试 README](../tests/README.md) ——
  旧版测试套件约定、辅助工具、故障排查
- [CLAUDE.md](../../CLAUDE.md) ——
  费用约定、项目笔记
