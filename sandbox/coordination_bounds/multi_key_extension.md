# Multi-Key Extension: Coordination Bounds for Transactions

## Overview

The base proofs consider single-key operations. Real transactions often span multiple keys/tables. This document extends the theory to multi-key transactions.

---

## 1. Multi-Key System Model

### Definition 1.1 (Key Space)

Let $\mathcal{K} = \{k_1, k_2, ..., k_m\}$ be the set of keys (data items).

Each key $k$ has state $s_k \in \mathcal{D}_k$.

Global state: $S = \{s_{k_1}, s_{k_2}, ..., s_{k_m}\}$

### Definition 1.2 (Multi-Key Operation)

A **multi-key operation** $O$ is a set of single-key operations:

$$O = \{(k_1, o_1), (k_2, o_2), ..., (k_j, o_j)\}$$

where each $(k_i, o_i)$ applies operation $o_i$ to key $k_i$.

### Definition 1.3 (Transaction)

A **transaction** $T$ is a multi-key operation that must be applied atomically:
- All operations in $T$ apply, or none apply
- Other transactions see either all effects or no effects

### Definition 1.4 (Key Overlap)

Transactions $T_1, T_2$ have **key overlap** if:

$$\text{keys}(T_1) \cap \text{keys}(T_2) \neq \emptyset$$

---

## 2. Multi-Key Commutativity

### Definition 2.1 (Transaction Commutativity)

Transactions $T_1, T_2$ are **commutative** if:

$$T_1 \circ T_2 = T_2 \circ T_1$$

where composition applies all operations in order.

### Theorem 2.1 (Commutativity Decomposition)

Transactions $T_1, T_2$ are commutative if and only if:

1. $\text{keys}(T_1) \cap \text{keys}(T_2) = \emptyset$ (no overlap), OR
2. For all $k \in \text{keys}(T_1) \cap \text{keys}(T_2)$: $o_1^k \diamond o_2^k$ (overlapping operations commute)

### Proof

**Case 1: No key overlap**

If $\text{keys}(T_1) \cap \text{keys}(T_2) = \emptyset$, then $T_1$ and $T_2$ operate on disjoint state.

Applying $T_1$ then $T_2$:
- Keys in $T_1$: see effect of $o_1^k$
- Keys in $T_2$: see effect of $o_2^k$
- Other keys: unchanged

Applying $T_2$ then $T_1$: identical result.

Therefore $T_1 \diamond T_2$. $\square$

**Case 2: Key overlap with commutative operations**

Let $k \in \text{keys}(T_1) \cap \text{keys}(T_2)$.

Since $o_1^k \diamond o_2^k$:
$$o_1^k(o_2^k(s_k)) = o_2^k(o_1^k(s_k))$$

This holds for all overlapping keys.

Non-overlapping keys are independent (Case 1).

Therefore $T_1 \diamond T_2$. $\square$

**Case 3: Key overlap with non-commutative operations**

Let $k \in \text{keys}(T_1) \cap \text{keys}(T_2)$ with $\neg(o_1^k \diamond o_2^k)$.

Then:
$$o_1^k(o_2^k(s_k)) \neq o_2^k(o_1^k(s_k))$$

Therefore $T_1 \circ T_2 \neq T_2 \circ T_1$.

$T_1$ and $T_2$ do not commute. $\square$

$\blacksquare$

---

## 3. Multi-Key Coordination Bounds

### Theorem 3.1 (Multi-Key Lower Bound)

For transactions $T_1, T_2$ with:
- $\text{keys}(T_1) \cap \text{keys}(T_2) \neq \emptyset$
- $\exists k : \neg(o_1^k \diamond o_2^k)$

Coordination cost: $C^*(T) = \Omega(\log |N|)$

### Proof

By Theorem 2.1, $T_1$ and $T_2$ do not commute.

The reduction to consensus from Theorem 4.1 applies:
- Must agree on order of $T_1, T_2$
- Order agreement requires consensus
- Consensus requires $\Omega(\log |N|)$ rounds

$\blacksquare$

### Theorem 3.2 (Multi-Key Upper Bound)

For transaction $T$ where:
- All operations $o \in T$ have $\sigma(o) \in \{\text{SEMILATTICE}, \text{ABELIAN}\}$

Coordination cost: $C^*(T) = 0$

### Proof

Apply Protocol 5.1 to each key independently:
1. For each $(k, o) \in T$: apply $o$ to $s_k$ immediately
2. Mark entire transaction committed
3. Propagate asynchronously

Convergence follows from single-key proof applied to each key.

Atomicity is trivially satisfied in the absence of aborts (algebraic operations always succeed).

$\blacksquare$

### Corollary 3.1 (Transaction Classification)

A transaction $T$ can be coordination-free iff:

$$\forall (k, o) \in T : \sigma(o) \in \{\text{SEMILATTICE}, \text{ABELIAN}\}$$

A single generic operation forces the entire transaction to coordinate.

---

## 4. Cross-Table ACID Transactions

### 4.1 Application to Rhizo

Rhizo supports cross-table transactions. The coordination bounds apply:

**Coordination-free cross-table transaction:**
```sql
BEGIN;
  UPDATE metrics SET page_views = page_views + 1;     -- ADD (Abelian)
  UPDATE aggregates SET max_value = MAX(max_value, @v); -- MAX (Semilattice)
  UPDATE counters SET total = total + @delta;          -- ADD (Abelian)
COMMIT;  -- Immediate, no coordination
```

**Requires coordination:**
```sql
BEGIN;
  UPDATE users SET name = @new_name;                   -- OVERWRITE (Generic)
  UPDATE audit SET last_modified = NOW();              -- OVERWRITE (Generic)
COMMIT;  -- Requires coordination
```

### 4.2 Hybrid Transactions

A transaction mixing algebraic and generic operations:

```sql
BEGIN;
  UPDATE metrics SET count = count + 1;    -- ADD (Abelian) ✓
  UPDATE users SET status = 'active';      -- OVERWRITE (Generic) ✗
COMMIT;
```

**Coordination cost:** $\Omega(\log |N|)$ (dominated by generic operation)

### 4.3 Optimization: Split Transactions

If possible, split hybrid transactions:

```sql
-- Transaction 1: Coordination-free
UPDATE metrics SET count = count + 1;

-- Transaction 2: Coordinated
UPDATE users SET status = 'active';
```

Total latency: $L_{local} + O(\log |N|) \cdot L_{network}$

vs combined: $O(\log |N|) \cdot L_{network}$ (but atomic)

Trade-off: latency vs atomicity.

---

## 5. Key Partitioning Analysis

### 5.1 Independent Partitions

If keys can be partitioned into independent groups:

$$\mathcal{K} = \mathcal{K}_1 \cup \mathcal{K}_2 \cup ... \cup \mathcal{K}_p$$

with no transactions spanning partitions, each partition can be handled independently.

### 5.2 Coordination Within Partitions

For partition $\mathcal{K}_i$ with $|N_i|$ replicas:

$$C^*(\mathcal{K}_i) = \Omega(\log |N_i|)$$

Smaller partitions = lower coordination cost.

### 5.3 Hot Keys

A "hot key" accessed by many concurrent transactions:

- If all operations are algebraic: still $C = 0$
- If any operation is generic: serialization point

**Implication:** Hot keys should use algebraic operations where possible.

---

## 6. Conflict Analysis

### Definition 6.1 (Conflict Rate)

For workload $W$ with transactions $T_1, ..., T_n$:

$$\text{ConflictRate}(W) = \frac{|\{(T_i, T_j) : T_i, T_j \text{ conflict}\}|}{n^2}$$

### Theorem 6.1 (Expected Coordination)

For workload with:
- Fraction $\alpha$ algebraic transactions (no conflicts possible)
- Conflict rate $\rho$ among generic transactions

Expected coordination rounds:

$$E[C] = (1-\alpha) \cdot \rho \cdot \Omega(\log |N|)$$

### Implication

Reducing conflict rate (better key distribution) AND increasing algebraic fraction both reduce expected coordination.

---

## 7. Practical Recommendations

### 7.1 Schema Design for Coordination-Freedom

**Good:** Separate algebraic and generic columns
```sql
CREATE TABLE metrics (
  id INT PRIMARY KEY,           -- Generic (but rarely written)
  view_count INT,               -- ADD - algebraic
  max_score INT,                -- MAX - algebraic
  last_updated TIMESTAMP        -- OVERWRITE - generic (isolate to separate table?)
);
```

**Better:** Isolate generic updates
```sql
CREATE TABLE metrics_counters (
  id INT PRIMARY KEY,
  view_count INT,    -- ADD
  max_score INT      -- MAX
);  -- Fully algebraic!

CREATE TABLE metrics_metadata (
  id INT PRIMARY KEY,
  last_updated TIMESTAMP  -- OVERWRITE - coordinated separately
);
```

### 7.2 Transaction Design

1. **Prefer algebraic operations** where semantically equivalent
2. **Avoid mixing** algebraic and generic in same transaction
3. **Use MAX for timestamps** instead of OVERWRITE when possible
4. **Use ADD for counters** instead of read-modify-write

### 7.3 Monitoring

Track:
- Algebraic vs generic transaction ratio
- Conflict rate among generic transactions
- Coordination latency vs local commit latency

Optimize toward higher algebraic ratio.

---

## 8. Summary

### Multi-Key Coordination Bounds

| Transaction Type | Condition | Coordination Cost |
|------------------|-----------|-------------------|
| All algebraic | $\forall o : \sigma(o) \in \{\text{SL}, \text{AB}\}$ | $0$ |
| Any generic | $\exists o : \sigma(o) = \text{GEN}$ | $\Omega(\log \|N\|)$ |
| No key overlap | $\text{keys}(T_1) \cap \text{keys}(T_2) = \emptyset$ | $0$ |
| Overlap + commutative | Overlapping ops commute | $0$ |
| Overlap + non-commutative | Overlapping ops don't commute | $\Omega(\log \|N\|)$ |

### Key Insight

Multi-key transactions inherit the coordination cost of their "worst" operation. A single generic operation on a single key forces the entire transaction to coordinate.

This explains why:
- Cross-table algebraic transactions are just as fast as single-table
- A single OVERWRITE anywhere in a transaction forces coordination
