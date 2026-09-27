# Carpool Fare Splitter · 拼车行程费用分摊 API

基于 FastAPI 的拼车费用分摊服务：按顺序路段读取整数分费用与每位乘客的上下车站点，
逐段均摊费用，输出逐人总额与逐路段明细；还可从最多 12 名候选乘客中**自动遴选**一组
上车（座位与预算双约束，先求入选人数最多、再求入选者应付总额最小）；在提供每人
实际付款记录后，自动生成**转账笔数全局最少**的结清方案；在路段费用修订后，
按所有修订应用顺序计算每个变动路段对每人应付额的**平均边际影响（Shapley 归因）**；
以及在恰有两段费用未定（各给整数分上下界）、全程总价固定时，给出目标乘客应付额的
**精确最小值 / 最大值及完整路段费用见证**（`POST /fare-envelope`）。
金额全程使用整数分与精确分数计算，总额严格守恒，且乘客与付款记录的输入顺序不影响结果。

## 快速启动

单条命令（构建镜像、安装依赖、启动服务全部自动完成）：

```bash
docker compose up --build
```

启动后访问：

| 地址 | 说明 |
| --- | --- |
| http://localhost:8000/split | 分摊接口（POST） |
| http://localhost:8000/admit | 候选乘客遴选 + 分摊接口（POST） |
| http://localhost:8000/settle | 分摊 + 结清转账接口（POST） |
| http://localhost:8000/revision-impact | 路段费用修订的边际影响归因接口（POST） |
| http://localhost:8000/fare-envelope | 两段费用待定时目标乘客应付额的最小/最大包络（POST） |
| http://localhost:8000/docs | Swagger 交互式文档（各 POST 接口可在此直接发请求） |
| http://localhost:8000/healthz | 健康检查 |

以上 `/split`、`/admit`、`/settle`、`/revision-impact`、`/fare-envelope` 均为 **POST** 接口，浏览器地址栏直接访问只适用于 GET；命令行可用下文的 `curl` 示例（带 `-d` 请求体即按 POST 发送），或打开 `/docs` 在页面上交互式调用。

后台运行可加 `-d`；停止用 `docker compose down`。

## 配置

无需任何必需配置（无数据库、无环境变量）。默认端口 **8000**；
如需修改宿主机端口，编辑 `compose.yaml` 中的端口映射即可，例如 `"8080:8000"`。

## API：`POST /split`

### 请求体

```json
{
  "segments": [100, 251, 80],
  "passengers": [
    {"id": "alice", "board": 0, "alight": 3},
    {"id": "bob",   "board": 1, "alight": 3},
    {"id": "carol", "board": 0, "alight": 1}
  ]
}
```

- `segments`：按行程顺序排列的各路段费用，整数，单位分，必须 ≥ 0，至少 1 段。
- `passengers[].id`：乘客 ID，字符串或整数，全程唯一。
- `board` / `alight`：上/下车站点编号。`N` 个路段对应站点 `0..N`；
  乘客在车区间为 `[board, alight)`，即乘坐第 `board` 至 `alight-1` 段，须满足 `0 ≤ board < alight ≤ N`。

### 响应（200）

```json
{
  "total_cost": 431,
  "totals": {"alice": 216, "bob": 165, "carol": 50},
  "segments": [
    {"index": 0, "cost": 100, "passengers": ["alice", "carol"], "shares": {"alice": 50, "carol": 50}},
    {"index": 1, "cost": 251, "passengers": ["alice", "bob"],   "shares": {"alice": 126, "bob": 125}},
    {"index": 2, "cost": 80,  "passengers": ["alice", "bob"],   "shares": {"alice": 40, "bob": 40}}
  ]
}
```

- `totals`：逐人应付总额（分），总和恒等于 `total_cost`。
- `segments[]`：逐路段明细，`passengers` 为该段在车乘客，`shares` 为该段分摊结果（每段 `shares` 之和恒等于该段 `cost`）。

### 示例

```bash
curl -s http://localhost:8000/split \
  -H 'Content-Type: application/json' \
  -d '{"segments":[100,251,80],"passengers":[{"id":"alice","board":0,"alight":3},{"id":"bob","board":1,"alight":3},{"id":"carol","board":0,"alight":1}]}'
```

## API：`POST /admit`

在既有行程（`segments` + 已在车的 `passengers`）基础上，从**最多 12 名候选乘客**中遴选一组加入。
每名候选乘客除 `id`、`board`、`alight` 外还给出 `max_fare`（整数分，愿意支付的上限）；
另有统一的每段座位上限 `capacity`。

一组候选入选当且仅当：

1. **座位约束**：每个路段上「既有乘客 + 入选候选」人数都不超过 `capacity`；
2. **预算约束**：将入选者并入完整名单后，按与 `/split` 完全相同的规则**重新分摊全程费用**，
   每名入选者的应付额不超过其 `max_fare`（既有乘客不受预算约束）。

遴选目标按以下顺序优化：

1. **入选人数最多**；
2. 人数相同时，**入选者应付总额最小**；
3. 仍相同时，取入选 ID 名单按规范顺序排序后字典序最小者
   （整数 ID 按数值升序、字符串 ID 按字典序升序，整数整体排在字符串前）。

### 请求体

```json
{
  "segments": [100, 251, 80],
  "passengers": [
    {"id": "alice", "board": 0, "alight": 3},
    {"id": "carol", "board": 0, "alight": 1}
  ],
  "capacity": 3,
  "candidates": [
    {"id": "bob", "board": 1, "alight": 3, "max_fare": 165},
    {"id": 7,     "board": 2, "alight": 3, "max_fare": 40}
  ]
}
```

- `capacity`：正整数，所有路段统一的座位人数上限；既有乘客人数在任何路段已超限即判 422。
- `candidates`：候选数组，**至多 12 名**（可为空数组）。候选 ID 不得彼此重复，
  也不得与既有乘客 ID 冲突；区间与站点规则同 `/split`。
- `max_fare`：非负整数分；约束针对**重新分摊后**的应付额，而非加入前的费用。
- 行程本身仍须满足 `/split` 的全部规则（如不得有既有乘客覆盖不到的空载收费路段）。

### 响应（200）

```json
{
  "admitted": [7, "bob"],
  "totals": {"7": 27, "alice": 202, "bob": 152, "carol": 50},
  "segments": [
    {"index": 0, "cost": 100, "passengers": ["alice", "carol"], "shares": {"alice": 50, "carol": 50}},
    {"index": 1, "cost": 251, "passengers": ["alice", "bob"],   "shares": {"alice": 126, "bob": 125}},
    {"index": 2, "cost": 80,  "passengers": [7, "alice", "bob"], "shares": {"7": 27, "alice": 26, "bob": 27}}
  ]
}
```

- `admitted`：入选候选 ID，按规范顺序排列（整数在前）；**无人可入选时为 `[]`**。
- `totals` / `segments`：入选后**完整名单**（既有 + 入选）的分摊结果，结构与 `/split` 完全一致。
- 无任何候选可以入选（座位或预算均不满足）时，`admitted` 为空，
  `totals`/`segments` 即**原行程的分摊结果**，与对同一行程调用 `/split` 一致。
- 无论 `passengers` 与 `candidates` 以什么顺序提交，响应完全一致。

### 示例

```bash
curl -s http://localhost:8000/admit \
  -H 'Content-Type: application/json' \
  -d '{"segments":[100,251,80],"passengers":[{"id":"alice","board":0,"alight":3},{"id":"carol","board":0,"alight":1}],"capacity":3,"candidates":[{"id":"bob","board":1,"alight":3,"max_fare":165},{"id":7,"board":2,"alight":3,"max_fare":40}]}'
```

## API：`POST /settle`

在 `/split` 的同一套 `segments`、`passengers` 基础上增加 `payments`：
每位乘客**恰好一条**已支付记录（整数分，允许 0，不允许负数）。
接口先算出每人应付额，再以 `余额 = 已付 − 应付` 生成欠款人到收款人的转账方案。

### 请求体

```json
{
  "segments": [100, 251, 80],
  "passengers": [
    {"id": "alice", "board": 0, "alight": 3},
    {"id": "bob",   "board": 1, "alight": 3},
    {"id": "carol", "board": 0, "alight": 1}
  ],
  "payments": [
    {"id": "alice", "amount": 331},
    {"id": "bob",   "amount": 100},
    {"id": "carol", "amount": 0}
  ]
}
```

- `payments[].id`：乘客 ID，必须与 `passengers` 一一对应：**不缺、不重、无多余**。
- `payments[].amount`：该乘客已付金额，整数分，`>= 0`。
- `payments` 金额之和必须**恰好等于**行程总额（`sum(segments)`）。

### 响应（200）

```json
{
  "owed": {"alice": 216, "bob": 165, "carol": 50},
  "paid": {"alice": 331, "bob": 100, "carol": 0},
  "balance": {"alice": 115, "bob": -65, "carol": -50},
  "transfers": [
    {"from": "bob", "to": "alice", "amount": 65},
    {"from": "carol", "to": "alice", "amount": 50}
  ]
}
```

- `owed`：每人应付额（与 `/split` 的 `totals` 完全一致）。
- `paid`：每人已付额（按请求 `payments` 汇总）。
- `balance`：`paid - owed`，正数为应收、负数为应付，总和恒为 0。
- `transfers`：结清全部余额的转账列表；`from` 为欠款人、`to` 为收款人。
  - 非零余额参与者**不超过 12 人**时，求解**转账笔数全局最少**的方案
    （零和子集划分 + 桥接割动态规划，精确枚举）；
  - 最少方案不唯一时，将 `(from, to, amount)` 序列按乘客 ID 规范顺序
    （整数 ID 按数值、字符串 ID 按字典序，整数排在字符串前）排序后取字典序最小者；
  - 因此无论 `passengers` 与 `payments` 以什么顺序提交，响应完全一致；
  - 超过 12 个非零余额时改用确定性贪心结算（至多 `n-1` 笔）。

### 示例

```bash
curl -s http://localhost:8000/settle \
  -H 'Content-Type: application/json' \
  -d '{"segments":[100,251,80],"passengers":[{"id":"alice","board":0,"alight":3},{"id":"bob","board":1,"alight":3},{"id":"carol","board":0,"alight":1}],"payments":[{"id":"alice","amount":331},{"id":"bob","amount":100},{"id":"carol","amount":0}]}'
```

## API：`POST /revision-impact`

同一份乘客名单、同一次行程，只有部分路段的费用发生修订：给出**原始 `segments`** 与**等长的
`revised_segments`**，接口复用 `/split` 的全部校验与「逐段累计欠额分摊」算法，输出修订前后每人
应付额、差额，并把每人的差额**归因到每个费用发生变化的路段**。

### 归因口径：所有应用顺序上的平均边际影响

把 m 个变动路段逐个从原价切换为修订价。给定某个应用顺序，一条路段 j 的**边际影响**是：在它之前
已经切换的路段集合为 S 时，先只应用 S、再应用 S∪{j}，两次全程重新分摊后每人应付额之差。
由于欠额分摊是逐段累计的（某段余数补给谁取决于此前各段），中间状态必须对「S 用修订价、其余用
原价」的费用数组**从头重新分摊**。对所有 m! 个应用顺序取平均，即变动 j 对每人的 Shapley 贡献：

```
contribution(j, p) = Σ_{S ⊆ 其他变动路段} |S|!·(m-1-|S|)!/m! ·
                     (应付_p(S∪{j}) − 应付_p(S))
```

实现上枚举 2^m 个子集（最多 8 个变动，即 256 次分摊），全程精确分数（`fractions.Fraction`）：

- 每个变动路段对**每位乘客**都给出一个**最简分数字符串**（整数输出为 `"3"`，真分数为 `"1/2"`）；
- 贡献**可能为负**（例如某段降价，或余数 1 分的归属被其他路段改变），负值保留 `-` 号；
- **逐人精确守恒**：每位乘客在所有变动路段上的贡献之和，精确等于其 `after − before` 差额；
- 无任何路段费用变化时，`changes` 为空数组，每人差额为 0。

### 请求体

```json
{
  "segments": [100, 251, 80],
  "revised_segments": [120, 240, 90],
  "passengers": [
    {"id": "alice", "board": 0, "alight": 3},
    {"id": "bob",   "board": 1, "alight": 3},
    {"id": "carol", "board": 0, "alight": 1}
  ]
}
```

- `segments` / `revised_segments`：原始与修订的逐段费用，**等长、同序**，元素均为非负整数分；
  两份列表各自都必须满足 `/split` 的全部规则（不允许空载收费段等）。
- `passengers`：两份费用共用的**同一份名单**（上下车区间不变，只有路段费用变）。
- 费用发生变化（两列表同位置数值不等）的路段**不得超过 8 个**，否则返回 422。

### 响应（200）

```json
{
  "before": {"alice": 216, "bob": 165, "carol": 50},
  "after": {"alice": 225, "bob": 165, "carol": 60},
  "deltas": {"alice": 9, "bob": 0, "carol": 10},
  "changes": [
    {"index": 0, "original_cost": 100, "revised_cost": 120, "contributions": {"alice": "10", "bob": "0", "carol": "10"}},
    {"index": 1, "original_cost": 251, "revised_cost": 240, "contributions": {"alice": "-6", "bob": "-5", "carol": "0"}},
    {"index": 2, "original_cost": 80,  "revised_cost": 90,  "contributions": {"alice": "5",  "bob": "5", "carol": "0"}}
  ]
}
```

- `before` / `after`：原始 / 修订费用下每人应付额（分别等同于对两份费用调用 `/split` 的 `totals`）。
- `deltas`：`after − before`，每人修订后的应付变化（整数分，负数表示少付）。
- `changes[]`：每个变动路段一项，按 `index` 升序；`contributions` 给出该路段对**每位**乘客的平均
  边际影响（最简分数字符串，负数带符号）。逐人求和恒等于其 `deltas`，例如
  `alice: 10 + (-6) + 5 = 9`、`carol: 10 + 0 + 0 = 10`；段 1 降价使 `alice`/`bob` 的贡献为负。

当余数分摊在不同应用顺序下归属不同乘客时，贡献会是真分数。下例两段费用一加一减互相抵消、
每人总差额为 0，但每段的平均边际影响都是 ±1/2：

```json
{
  "before": {"alice": 7, "bob": 4, "carol": 2},
  "after": {"alice": 7, "bob": 4, "carol": 2},
  "deltas": {"alice": 0, "bob": 0, "carol": 0},
  "changes": [
    {"index": 0, "original_cost": 8, "revised_cost": 9, "contributions": {"alice": "1/2", "bob": "0", "carol": "1/2"}},
    {"index": 1, "original_cost": 5, "revised_cost": 4, "contributions": {"alice": "-1/2", "bob": "0", "carol": "-1/2"}}
  ]
}
```

（名单：alice 乘坐全程 2 段，bob 只坐第 0 段，carol 只坐第 1 段。）

### 示例

```bash
curl -s http://localhost:8000/revision-impact \
  -H 'Content-Type: application/json' \
  -d '{"segments":[100,251,80],"revised_segments":[120,240,90],"passengers":[{"id":"alice","board":0,"alight":3},{"id":"bob","board":1,"alight":3},{"id":"carol","board":0,"alight":1}]}'
```

## API：`POST /fare-envelope`

沿用 `/split` 的乘客上下车结构，但 `segments` 中**恰有两段费用为 `null`**（待定路段）。
为这两段分别给出**整数分上下界**，再给出全程总价 `total_cost` 与目标乘客 `target`；
其余路段费用固定。接口枚举所有满足：

1. 两段费用均在各自闭区间内（非负整数，**金额与上界不超过 10¹² 分**）；
2. 全部路段费用之和**恰好等于** `total_cost`；

的费用组合，对每个组合复用 `/split` 的「累计欠分 + 余分」分摊规则，返回目标乘客应付额的
**精确最小值、最大值**，以及取到各极值时的**完整路段费用见证向量**（无 `null`，求和恒等于
`total_cost`）。同一极值对应多个费用向量时，取**费用向量字典序最小**者。

- 至多 **100 段**、**12 名乘客**；两段待定路段**都必须有乘客在车**（否则 422）；
- 固定路段仍须满足 `/split` 全部既有行程规则（正费用空载段非法、零费用空载段允许）；
- 不存在满足区间与总价的组合、`target` 不是车上乘客、或违反既有行程规则时返回 **422**。

### 余数周期压缩（不逐分遍历区间）

设两段待定路段为 `a`、`b`，在车人数分别为 `n_a`、`n_b`。把费用写成
`x = q·n_a + r`（`0 ≤ r < n_a`）、`y = q'·n_b + s`（`0 ≤ s < n_b`）。当某段费用在其
余数不变的前提下整体增减「在车人数的整数倍」时：

- 该段每位在车乘客只是**同等多地多收整分**，本段余数归属不变；
- 该段之后所有乘客的欠分状态**逐分相同**，因此第二段余分补给谁（含跨段携带的欠分）、
  以及第二段之后所有固定路段的分摊都**完全不变**。

所以目标乘客的应付额只取决于至多 `n_a·n_b ≤ 144` 个余数对 `(r, s)`。对每个余数对，
约束 `x + y = total_cost − 固定段之和` 变成一个二元同余方程组
（`x ≡ r (mod n_a)`、`x ≡ remaining − s (mod n_b)`），用中国剩余定理判定相容并得到
步长 `lcm(n_a, n_b)` 的等差可行序列；序列内目标乘客应付额关于 `x` 是仿射函数，
斜率为「目标是否在 a 段 / n_a − 目标是否在 b 段 / n_b」，故每个余数对只需评估
**序列两端**即可取到该余数类上的最小/最大值，再跨余数类比较。全程精确分数，
10¹² 量级区间也在毫秒内完成；跨段欠分影响由「余数对固定即整段下游分摊固定」自动保留。

### 请求体

```json
{
  "segments": [null, 5, null],
  "passengers": [
    {"id": "alice", "board": 0, "alight": 3},
    {"id": "bob",   "board": 1, "alight": 2},
    {"id": "carol", "board": 0, "alight": 1}
  ],
  "bounds": [
    {"index": 0, "min_cost": 0, "max_cost": 10},
    {"index": 2, "min_cost": 0, "max_cost": 10}
  ],
  "total_cost": 15,
  "target": "alice"
}
```

- `segments`：按行程顺序的路段费用，与 `/split` 相同，但**恰有两个元素为 `null`**。
- `bounds`：每个待定路段一条，`index` 必须指向某个 `null` 段，`min_cost ≤ max_cost`，
  均为非负整数且不超过 10¹² 分。
- `total_cost`：全程总价（非负整数，≤ 10¹² 分），任何可行组合的费用和都必须恰好等于它。
- `target`：目标乘客 ID（字符串或整数，须存在于 `passengers`）。

### 响应（200）

```json
{
  "target": "alice",
  "total_cost": 15,
  "minimum": {"amount": 8,  "segments": [9, 5, 1]},
  "maximum": {"amount": 13, "segments": [0, 5, 10]}
}
```

上例 `x + y = 10`，alice 在 11 个可行组合上的应付额依次为
`13,12,12,11,11,10,10,9,9,8,8`：最小 8 由两个向量同时取到（`x=9` 与 `x=10`），
取字典序更小的 `[9,5,1]`；最大 13 仅 `[0,5,10]` 取到。
注意只乘坐中间固定段的 bob：该段 5 分 / 2 人的余分归属取决于 alice 从第 0 段
**携带进来的欠分**，因此 bob 的应付额会随 `x` 的奇偶在 2 与 3 之间变化
（`minimum = [0,5,10] → 2`，`maximum = [1,5,9] → 3`），跨段欠分影响被完整保留。

### 示例

```bash
curl -s http://localhost:8000/fare-envelope \
  -H 'Content-Type: application/json' \
  -d '{"segments":[null,5,null],"passengers":[{"id":"alice","board":0,"alight":3},{"id":"bob","board":1,"alight":2},{"id":"carol","board":0,"alight":1}],"bounds":[{"index":0,"min_cost":0,"max_cost":10},{"index":2,"min_cost":0,"max_cost":10}],"total_cost":15,"target":"alice"}'
```

## 分摊规则

1. 按路段顺序处理，每段找出全部在车乘客均摊：`base = cost // n`，余数 `r = cost % n`。
2. 每位乘客维护**累计理论份额**（精确分数）与**已分金额**；欠额 = 理论份额 − 已分金额。
3. 无法整除时，余数 `r` 个 1 分优先补给**欠额最大**的乘客；欠额并列时按乘客 ID 升序兜底
   （整数 ID 按数值、字符串 ID 按字典序），除此之外 ID 不影响分摊。
4. 因此：总额严格守恒（`sum(totals) == sum(segments)`），且乘客在请求中的排列顺序不影响结果。

## 错误（422）

以下情况返回 `422 Unprocessable Entity`，响应 `detail` 中含具体原因：

- 空载收费路段：某段 `cost > 0` 但无任何乘客在车（费用为 0 的空载段允许）；
- 非法区间：`board >= alight`、站点越界（`alight > 路段数`）或站点为负；
- 乘客 ID 重复、费用为负、路段列表为空、字段类型错误或含未知字段；
- （仅 `/admit`）候选超过 12 名、候选 ID 与既有乘客或其他候选重复；
- （仅 `/admit`）既有乘客在某路段的人数已超过 `capacity`；
- （仅 `/admit`）`capacity` 非正整数，或 `max_fare` 为负/非整数；
- （仅 `/settle`）`payments` 总和不等于行程总额；
- （仅 `/settle`）`payments` 缺失某位乘客、存在未知 ID 或同一 ID 重复；
- （仅 `/settle`）付款金额为负或非整数（如小数）；
- （仅 `/revision-impact`）`segments` 与 `revised_segments` 长度不等；
- （仅 `/revision-impact`）费用发生变化的路段超过 8 个（归因需枚举全部 2^n 个子集）；
- （仅 `/fare-envelope`）`segments` 中 `null` 不是恰有两个；`bounds` 不是恰两条、
  指向了非 `null` 段、段号重复或 `min_cost > max_cost`；
- （仅 `/fare-envelope`）待定路段上没有任何乘客在车；
- （仅 `/fare-envelope`）`target` 不是车上乘客；
- （仅 `/fare-envelope`）没有任何费用组合同时满足两段整数区间与 `total_cost`；
- （仅 `/fare-envelope`）路段超过 100 段、乘客超过 12 人，或费用/上界/总价超过 10¹² 分。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/          # 全部测试（算法 + API）
python -m unittest tests.test_splitter   # 仅核心算法（纯标准库，无需安装依赖）
uvicorn app.main:app --reload    # 本地起服务，默认 http://127.0.0.1:8000
```

## 项目结构

```
app/
  main.py       # FastAPI 应用与路由（POST /split、/admit、/settle、/revision-impact、/fare-envelope、GET /healthz）
  models.py     # 请求/响应模型与 422 校验规则
  splitter.py   # 核心算法：分摊（精确分数）+ 候选遴选（子集枚举）+ 最少转账结清（子集 DP）+ 修订影响（Shapley 平均边际）+ 费用包络（余数周期/CRT 压缩搜索）
tests/          # 算法单元测试 + API 集成测试
Dockerfile      # 依赖与启动命令均固化在镜像内
compose.yaml    # 单服务编排，含健康检查
requirements.txt / requirements-dev.txt
```
