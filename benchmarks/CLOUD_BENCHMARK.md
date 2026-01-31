# Cloud Benchmark: Geo-Distributed Consensus Measurement

Measure real cross-region coordination latency to get legitimate geo-distributed performance numbers.

## Overview

The benchmark measures Rhizo's local algebraic commit (0.001ms) against real coordination protocols running across cloud regions. This gives us the true speedup for geo-distributed deployments.

**Expected results:**
- Cross-region 2PC: 100-300ms (depending on regions) → 100,000x+ vs Rhizo
- Cross-region Redis WAIT: similar to 2PC
- Cross-region etcd: similar to 2PC (Raft consensus)

## Quick Start (2PC — Cheapest Option)

This uses our custom 2PC protocol, requiring only Python on each VM. No Redis or etcd installation needed.

### 1. Provision 3 VMs in Different Regions

Pick any cloud provider. Use the smallest instance available.

**AWS Free Tier example:**
- `us-east-1`: t2.micro (Virginia)
- `eu-west-1`: t2.micro (Ireland)
- `ap-southeast-1`: t2.micro (Singapore)

**GCP Free Tier example:**
- `us-central1`: e2-micro (Iowa)
- `europe-west1`: e2-micro (Belgium)
- `asia-east1`: e2-micro (Taiwan)

### 2. Open Firewall Port

Allow TCP port 9000 inbound from your coordinator machine (or from the other VMs).

**AWS Security Group:**
```
Inbound rule: TCP 9000, Source: your-ip/32
```

**GCP Firewall:**
```bash
gcloud compute firewall-rules create allow-2pc \
  --allow tcp:9000 --source-ranges your-ip/32
```

### 3. Deploy Participant Server on Each Remote VM

SSH into each remote VM and run:

```bash
# Option A: Copy just the server script (no dependencies needed)
scp benchmarks/2pc_participant_server.py user@remote-vm:~/

# On the remote VM:
python3 2pc_participant_server.py --port 9000
```

The participant server is a single file with no dependencies beyond Python 3.6+.

### 4. Run the Benchmark from Your Machine

```bash
python benchmarks/real_consensus_benchmark.py \
  --remote-2pc 52.1.2.3:9000,34.5.6.7:9000 \
  --output benchmarks/CLOUD_BENCHMARK_RESULTS.json
```

This will:
1. Measure TCP RTT to each participant
2. Run Rhizo algebraic commits locally
3. Run localhost baselines (SQLite, localhost 2PC)
4. Run remote 2PC across all participants
5. Print comparison and save results to JSON

### 5. Clean Up

Terminate the VMs when done. Total runtime: ~5 minutes.

## Redis Option (Optional)

If you want Redis numbers too:

### Deploy Redis on One VM

```bash
# On the remote VM
sudo apt-get install redis-server
sudo sed -i 's/bind 127.0.0.1/bind 0.0.0.0/' /etc/redis/redis.conf
sudo systemctl restart redis
```

### Run with Redis

```bash
python benchmarks/real_consensus_benchmark.py \
  --remote-2pc host1:9000,host2:9000 \
  --remote-redis redis-host:6379
```

### Redis with Replication (Consensus-Like)

For the strongest comparison, set up Redis with replicas across regions and use WAIT for synchronous replication:

```bash
# On primary VM
redis-server --bind 0.0.0.0

# On replica VMs
redis-server --bind 0.0.0.0 --replicaof primary-ip 6379
```

The benchmark automatically detects replicas and uses `WAIT` for synchronous replication.

## etcd Option (Optional)

For real Raft consensus numbers:

### Deploy etcd Cluster

```bash
# On each VM (adjust --name and IPs)
etcd --name node1 \
  --initial-advertise-peer-urls http://this-ip:2380 \
  --listen-peer-urls http://0.0.0.0:2380 \
  --listen-client-urls http://0.0.0.0:2379 \
  --advertise-client-urls http://this-ip:2379 \
  --initial-cluster node1=http://ip1:2380,node2=http://ip2:2380,node3=http://ip3:2380
```

### Run with etcd

```bash
pip install etcd3  # on your benchmark machine
python benchmarks/real_consensus_benchmark.py \
  --remote-2pc host1:9000,host2:9000 \
  --remote-etcd etcd-host:2379
```

## Cost Estimate

| Provider | Instance | Cost (3 VMs, 1 hour) |
|----------|----------|----------------------|
| AWS t2.micro | Free tier | $0.00 |
| AWS t3.micro | On-demand | ~$0.03 |
| GCP e2-micro | Free tier | $0.00 |
| GCP e2-micro | On-demand | ~$0.02 |
| Oracle ARM | Always free | $0.00 |

Total benchmark runtime is ~5 minutes. Cost is negligible.

## Interpreting Results

The JSON output includes:

```json
{
  "mode": "cloud",
  "endpoint_rtts_ms": {
    "52.1.2.3:9000": 85.2,
    "34.5.6.7:9000": 142.7
  },
  "measured_speedups": {
    "Localhost 2PC (3 nodes)": 59.1,
    "Remote 2PC (3 nodes: ...)": 95432.1
  }
}
```

- **endpoint_rtts_ms**: Raw TCP round-trip time to each participant
- **measured_speedups**: All speedups vs Rhizo algebraic commit

The remote 2PC speedup is the headline number for geo-distributed claims.

## Measured Results (January 2026)

Benchmarks run from NYC to AWS EC2 t2.micro instances.

### Cross-Continent (NYC → Oregon + Ireland)

| Endpoint | Region | RTT |
|----------|--------|-----|
| 54.244.106.26 (Oregon) | us-west-2 | 92ms |
| 52.49.21.132 (Ireland) | eu-west-1 | 107ms |

| System | Mean | p50 | p95 | p99 | Speedup | N |
|--------|------|-----|-----|-----|---------|---|
| Rhizo algebraic (ADD) | 0.001ms | 0.001ms | 0.002ms | 0.002ms | baseline | 500 |
| Remote 2PC (3 machines) | 187.9ms | 188.1ms | 191.2ms | 194.3ms | **160,000x** | 500 |

### Same-Region (NYC → Virginia)

| Endpoint | Region | RTT |
|----------|--------|-----|
| 34.229.148.227 | us-east-1 | 17ms |
| 18.234.194.185 | us-east-1 | 19ms |

| System | Mean | Speedup | N |
|--------|------|---------|---|
| Rhizo algebraic (ADD) | 0.001ms | baseline | 500 |
| Remote 2PC (3 machines) | 33.3ms | **30,000x** | 500 |

### Why Rhizo is Always 0.001ms

Rhizo algebraic commits are **local operations** — they never touch the network. The math (commutativity + associativity) guarantees all nodes converge regardless of operation order. So the cost is constant: ~1 microsecond for a local memory operation. The further apart your nodes are, the more coordination costs — but Rhizo's cost stays flat.

Raw results: `CLOUD_MULTI_REGION_RESULTS.json`, `CLOUD_SAME_REGION_RESULTS.json`

---

## Local Testing

Test the remote 2PC flow locally before deploying to cloud:

```bash
# Terminal 1: Start participant
python benchmarks/2pc_participant_server.py --port 9000

# Terminal 2: Start another participant
python benchmarks/2pc_participant_server.py --port 9001

# Terminal 3: Run benchmark against them
python benchmarks/real_consensus_benchmark.py \
  --remote-2pc localhost:9000,localhost:9001
```
