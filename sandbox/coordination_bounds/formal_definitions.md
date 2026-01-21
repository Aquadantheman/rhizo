# Formal Definitions: Coordination Bounds for Algebraic Operations

## 1. System Model

### 1.1 Distributed System

We consider a distributed system with:
- $N$ nodes (replicas), each holding a copy of the data
- Asynchronous message passing (no timing assumptions)
- Reliable delivery (messages eventually arrive, no corruption)
- No Byzantine faults (nodes follow protocol)

### 1.2 Operations

An **operation** $o$ is a function that transforms state:

$$o: S \rightarrow S$$

where $S$ is the state space (e.g., the set of all possible table contents).

Operations are issued by clients to any node. The goal is for all nodes to eventually agree on the result of applying all operations.

---

## 2. Coordination Cost

### 2.1 Message Complexity

**Definition (Message Rounds):** The number of sequential message exchanges required before all nodes can safely apply an operation.

$$R(o) = \text{minimum rounds to commit } o \text{ across } N \text{ nodes}$$

**Definition (Message Count):** The total number of messages exchanged.

$$M(o) = \text{total messages to commit } o$$

### 2.2 Coordination Cost Function

**Definition (Coordination Cost):** For an operation $o$ in a system of $N$ nodes:

$$C(o, N) = R(o) \cdot M(o)$$

Alternatively, we can use just rounds $R(o)$ as the primary measure, since messages per round is typically $O(N)$ or $O(N^2)$ depending on topology.

### 2.3 Zero Coordination

**Definition:** An operation $o$ has **zero coordination cost** if:

$$C(o, N) = 0 \iff R(o) = 0$$

Meaning: a node can apply $o$ immediately upon receipt without waiting for any other node.

---

## 3. Algebraic Properties

### 3.1 Commutativity

**Definition:** Operations $o_1, o_2$ are **commutative** if:

$$o_1 \circ o_2 = o_2 \circ o_1$$

where $\circ$ denotes composition (apply $o_1$ then $o_2$).

**Equivalently:** For any state $s$:

$$o_2(o_1(s)) = o_1(o_2(s))$$

### 3.2 Associativity

**Definition:** Operations are **associative** if:

$$(o_1 \circ o_2) \circ o_3 = o_1 \circ (o_2 \circ o_3)$$

### 3.3 Idempotency

**Definition:** Operation $o$ is **idempotent** if:

$$o \circ o = o$$

### 3.4 Identity

**Definition:** Operation $e$ is an **identity** if for all $o$:

$$e \circ o = o \circ e = o$$

### 3.5 Inverse

**Definition:** Operation $o^{-1}$ is an **inverse** of $o$ if:

$$o \circ o^{-1} = o^{-1} \circ o = e$$

---

## 4. Algebraic Structures

### 4.1 Semilattice

A set of operations forms a **join-semilattice** if they satisfy:
- Commutativity: $a \sqcup b = b \sqcup a$
- Associativity: $(a \sqcup b) \sqcup c = a \sqcup (b \sqcup c)$
- Idempotency: $a \sqcup a = a$

**Examples:**
- `MAX(a, b)` - maximum of two values
- `MIN(a, b)` - minimum of two values
- `UNION(A, B)` - set union
- `OR(a, b)` - logical or

**Key Property:** The order of applying semilattice operations doesn't matter, AND applying the same operation twice has no additional effect.

### 4.2 Abelian Group

A set of operations forms an **Abelian group** if they satisfy:
- Commutativity: $a + b = b + a$
- Associativity: $(a + b) + c = a + (b + c)$
- Identity: $a + 0 = a$
- Inverse: $a + (-a) = 0$

**Examples:**
- `ADD(x, delta)` - increment by delta (inverse: subtract)
- `MULTIPLY(x, factor)` where factor ≠ 0 (inverse: divide)

**Key Property:** Order doesn't matter, and operations can be "undone."

### 4.3 Generic (Non-Algebraic)

Operations that don't satisfy commutativity.

**Examples:**
- `OVERWRITE(x, value)` - set to specific value
- `CAS(x, expected, new)` - compare-and-swap
- `APPEND(list, item)` - append to ordered list (order matters!)

**Key Property:** The order of operations affects the result.

---

## 5. Algebraic Signature

**Definition:** The **algebraic signature** $\sigma(o)$ of an operation $o$ is its classification:

$$\sigma(o) \in \{\text{Semilattice}, \text{Abelian}, \text{Generic}\}$$

More precisely:

$$\sigma(o) = \begin{cases}
\text{Semilattice} & \text{if } o \text{ is commutative, associative, idempotent} \\
\text{Abelian} & \text{if } o \text{ is commutative, associative, has identity and inverse} \\
\text{Generic} & \text{otherwise}
\end{cases}$$

**Note:** Semilattice and Abelian are not mutually exclusive in theory, but in practice:
- Semilattice ops (MAX, UNION) are idempotent but lack inverses
- Abelian ops (ADD) have inverses but are not idempotent

---

## 6. The Core Conjecture

### 6.1 Lower Bound Conjecture

**Conjecture:** For any operation $o$ with algebraic signature $\sigma(o)$:

$$C_{min}(\sigma) = \begin{cases}
0 & \text{if } \sigma \in \{\text{Semilattice}, \text{Abelian}\} \\
\Omega(\log N) & \text{if } \sigma = \text{Generic}
\end{cases}$$

**In words:**
- Commutative operations can be applied with zero coordination
- Non-commutative operations require at least $\Omega(\log N)$ coordination

### 6.2 Why $\Omega(\log N)$ for Generic?

**Intuition:** Without commutativity, nodes must agree on an order. Agreement among $N$ nodes requires information to propagate, which takes at least $\log N$ rounds in the best case (binary tree dissemination).

**More formally:** This relates to the **consensus lower bound**. Fischer-Lynch-Paterson (FLP) showed consensus is impossible in asynchronous systems with even one fault. With no faults, consensus requires $\Omega(\log N)$ rounds for $N$ nodes to all learn the outcome.

### 6.3 Why Zero for Algebraic?

**Intuition:** If operations commute, then every ordering produces the same result. Nodes don't need to agree on order - they can apply operations immediately and will converge to the same state.

**More formally:** This is the CRDT insight. If all operations satisfy semilattice or Abelian properties, the state forms a CRDT and convergence is guaranteed regardless of message ordering.

---

## 7. What We Need to Prove

### 7.1 Lower Bound (Impossibility)

**Theorem (Lower Bound):** For any protocol handling generic (non-commutative) operations, there exists an execution requiring $\Omega(\log N)$ message rounds before all nodes can safely commit.

**Proof approach:** Reduction to consensus. Show that if we could commit generic operations in $o(\log N)$ rounds, we could solve consensus faster than known lower bounds.

### 7.2 Upper Bound (Achievability)

**Theorem (Achievability):** For operations with $\sigma \in \{\text{Semilattice}, \text{Abelian}\}$, there exists a protocol achieving $C = 0$.

**Proof approach:** Constructive. Show that Rhizo's protocol (immediate local application + gossip propagation) achieves zero coordination for algebraic operations.

### 7.3 Optimality

**Corollary (Optimality):** Rhizo's algebraic classification achieves the minimum coordination cost for each operation type.

**Proof:** Combine lower and upper bounds. Show Rhizo matches the lower bound for both algebraic (achieves 0) and generic (achieves $O(\log N)$) operations.

---

## 8. Open Questions

1. **Partial commutativity:** What about operations that commute under certain conditions? Can we characterize the coordination cost as a function of "commutativity probability"?

2. **Mixed transactions:** A transaction with both algebraic and generic operations. Is the coordination cost determined by the "weakest" operation? (Likely yes.)

3. **Bounded counters:** ADD is Abelian, but bounded ADD (with overflow check) is not. How does this affect the analysis?

4. **Read-write interactions:** We've focused on write operations. How do reads factor into coordination cost?

---

## Next Steps

1. **Formalize the lower bound proof** → `lower_bound_proof.md`
2. **Formalize the achievability proof** → `achievability_proof.md`
3. **Map SQL operations to signatures** → `classification_mapping.py`
4. **Validate empirically** → `empirical_validation.py`
