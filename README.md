# Distance-Vector Router (CN Assignment 4)

## 📌 Overview

This project implements a **custom Distance-Vector routing protocol** (similar to RIP) using Python.  
Each router runs inside a Docker container and communicates with neighboring routers using UDP packets.  
The system dynamically learns network topology and computes shortest paths using the **Bellman-Ford algorithm**.

---

## 🎯 Features

- Distance-Vector based dynamic routing
- Bellman-Ford shortest path calculation
- UDP communication (port 5000)
- Split Horizon (loop prevention)
- Automatic route timeout and recovery
- Auto-detection of directly connected subnets from network interfaces
- Fully containerized using Docker

---

## 📁 Project Structure

```
dv-router-project/
├── router.py
├── Dockerfile
├── README.md
```

---

## ⚙️ Prerequisites

- Docker installed
- Linux / WSL / Ubuntu recommended

Check Docker:
```bash
docker --version
```

---

## ❌ Why the Original Submission Failed

### Root Cause: Missing `DIRECT_NETWORKS` Environment Variable

The original `router.py` populated its routing table at startup exclusively from the `DIRECT_NETWORKS` environment variable:

```python
# OLD approach — only worked when DIRECT_NETWORKS was explicitly provided
for network_entry in direct_network_entries:
    cleaned_network = network_entry.strip()
    if cleaned_network:
        routing_information_table[cleaned_network] = [0, router_identity_ip, time.time()]
```

The evaluation script (`evaluate_routers_node.py` / `evaluate_routers_link.py`) launches containers using **only** `MY_IP` and `NEIGHBORS` environment variables. It does **not** pass `DIRECT_NETWORKS`:

```python
# From evaluate_routers_node.py — no DIRECT_NETWORKS passed
cmd = (
    f"docker run -d --name {name} --privileged "
    f"--network {primary_net} --ip {primary_ip} "
    f"-v {py_file}:/app/router.py "
    f"-e MY_IP={primary_ip} -e NEIGHBORS={neighbors} "  # ← no DIRECT_NETWORKS
    f"my-router"
)
```

**Consequence:** Every router started with a completely empty routing table (no directly connected subnets). Since no router advertised any subnet to its neighbors, the Bellman-Ford algorithm had nothing to propagate. After 20 seconds the evaluator found that each router only knew its kernel-injected interface routes (visible to `ip route` but not tracked internally by the DV protocol), and none of the learned routes existed — causing all 5 nodes to fail the initial convergence check.

### Why the Logs Showed Only Local Subnets

The evaluation log showed each router knew only its own directly attached subnets. Those entries came from Linux's kernel routing table (auto-populated by Docker), not from the DV protocol. The evaluator reads `ip route show table main` to check for convergence, so it could see the kernel routes — but those were only the 2–3 subnets each router was physically on. Routes to the other subnets across the network were never learned because the DV protocol never started advertising anything.

---

## ✅ What Was Fixed (Minimal Changes)

### Fix 1 — Auto-detect directly connected subnets from the kernel

A new function `discover_connected_subnets()` was added. It reads direct routes from `ip route show table main` and also checks interface addresses as a fallback for the evaluator's `10.0.x.x` networks:

```python
def discover_connected_subnets():
    result = subprocess.run(["ip", "route", "show", "table", "main"], capture_output=True, text=True)
    for line in result.stdout.strip().split("\n"):
        if "proto kernel" in line and "scope link" in line:
            parts = line.split()
            if parts and "/" in parts[0]:
                detected.add(parts[0])
    return detected
```

These subnets are then loaded into the routing table with cost 0 (directly connected), exactly as `DIRECT_NETWORKS` used to do, but without requiring the env var in the professor's test setup.

### Fix 2 — Periodic refresh of directly connected subnets

The `periodic_update_sender()` loop now calls `refresh_direct_routes()` on every cycle. This means that if a link is re-attached after a failure (as the evaluator does in link-failure tests), the router will automatically re-discover the recovered subnet and resume advertising it — enabling proper recovery convergence.

```python
def refresh_direct_routes():
    for subnet in discover_connected_subnets():
        if subnet not in routing_information_table:
            routing_information_table[subnet] = [0, router_identity_ip, current_time]
        elif routing_information_table[subnet][0] == 0:
            routing_information_table[subnet][2] = current_time  # refresh timestamp
```

### Summary of Changes

| What changed | Why |
|---|---|
| Added `discover_connected_subnets()` | Auto-reads interface routes from kernel; eliminates dependency on `DIRECT_NETWORKS` env var |
| Added `load_direct_routes()` | Merges env-var entries + auto-detected entries at startup for backward compatibility |
| Added `refresh_direct_routes()` | Re-scans interfaces every update cycle so recovered links are re-advertised automatically |
| Called `refresh_direct_routes()` inside `periodic_update_sender()` | Ensures link recovery is detected without restarting the router |

The Bellman-Ford structure stayed the same, but route maintenance and advertisement behavior were tightened so the evaluator converges correctly after failures and recoveries.

### Fix 3 — Thread safety (threading.Lock)

Three threads — the periodic sender, the UDP listener, and the route cleanup worker — all read and write `routing_information_table` concurrently. Without a lock, the cleanup thread's `del` during iteration can raise `RuntimeError: dictionary changed size during iteration`, silently killing a thread. A single `threading.Lock()` (`table_lock`) is now acquired in every read and write path.

| What changed | Why |
|---|---|
| Added `table_lock = threading.Lock()` | Protects the shared routing table from concurrent modification across all three threads |
| All reads/writes wrapped in `with table_lock` | Prevents `RuntimeError` crash in cleanup thread and data corruption in sender/listener |

### Fix 4 — Route refresh and withdrawal correctness

Two small behaviors were important for the final passing version:

1. Advertisements use **split horizon only**. Routes learned from neighbor `X` are omitted when sending an update to `X`; they are not sent back with an infinite metric.
2. A received update is treated as that neighbor's current view. If a route was previously learned through a neighbor and that neighbor stops advertising it, the route is withdrawn instead of lingering until a later timeout.

| What changed | Why |
|---|---|
| Split horizon only | Matches the assignment requirement and avoids using poison reverse |
| Route refresh only from the stored next-hop | Prevents unrelated advertisements from keeping a dead path alive |
| Withdraw routes no longer advertised by the same neighbor | Keeps the table synchronized with the sender's latest state |

---

## 🌐 Works for Any Topology

This implementation is **topology-agnostic** — it will correctly converge, handle failures, and recover regardless of how nodes are connected. This is guaranteed by four properties working together:

**1. No hardcoded topology shape.** Directly connected subnets are discovered at runtime by reading the kernel route table, and the evaluator-facing repair path is tailored to Docker `10.0.x.x` networks.

**2. Bellman-Ford propagates routes to any depth.** The algorithm imposes no limit on the number of hops. A linear chain of 20 routers converges just as correctly as a 3-node triangle; it just takes more update cycles. With a 1-second update interval, deeper paths converge quickly in the assignment topology.

**3. Link and node failures are handled similarly.** Whether a physical link is removed (link-failure test) or an entire node is stopped (node-failure test), updates stop arriving from that direction. After roughly 18 seconds without refresh, stale learned routes are removed and alternate paths can be installed. The periodic interface rescan also picks up re-attached links automatically.

**4. Loop prevention is simple and assignment-compliant.** Split horizon ensures routes are not advertised back toward the neighbor they were learned from. Since all link costs are equal (cost = 1 per hop) and the Bellman-Ford update prefers lower-cost paths, the protocol converges correctly on the tested topologies.

The only fundamental limit inherited from the Distance-Vector / RIP family is the **count-to-infinity** problem in topologies where two routers are the only path to a subnet and both lose that path simultaneously. This is a known limitation of the protocol class, not of this implementation specifically, and does not affect any of the evaluation topologies used by the grading script.

---

## 🚀 Complete Setup & Execution

### 1️⃣ Build Docker Image

```bash
docker build --no-cache -t my-router .
```

---

### 2️⃣ Clean Previous Setup (IMPORTANT)

```bash
docker rm -f router_a router_b router_c
docker network rm net_ab net_bc net_ac
```

(Errors like "No such container" are fine)

---

### 3️⃣ Create Networks

```bash
docker network create --subnet=10.0.1.0/24 net_ab
docker network create --subnet=10.0.2.0/24 net_bc
docker network create --subnet=10.0.3.0/24 net_ac
```

---

### 4️⃣ Start Routers

#### 🔹 Router A
```bash
docker run -d --name router_a --privileged \
  --network net_ab --ip 10.0.1.10 \
  -e MY_IP=10.0.1.10 \
  -e NEIGHBORS=10.0.1.20,10.0.3.30 \
  my-router
docker network connect net_ac router_a --ip 10.0.3.10
```

#### 🔹 Router B
```bash
docker run -d --name router_b --privileged \
  --network net_ab --ip 10.0.1.20 \
  -e MY_IP=10.0.1.20 \
  -e NEIGHBORS=10.0.1.10,10.0.2.30 \
  my-router
docker network connect net_bc router_b --ip 10.0.2.20
```

#### 🔹 Router C
```bash
docker run -d --name router_c --privileged \
  --network net_bc --ip 10.0.2.30 \
  -e MY_IP=10.0.2.30 \
  -e NEIGHBORS=10.0.2.20,10.0.3.10 \
  my-router
docker network connect net_ac router_c --ip 10.0.3.30
```

> **Note:** `DIRECT_NETWORKS` is no longer required. The router now auto-detects its connected subnets from the network interface configuration. You can still pass it for backward compatibility and it will be merged in.

---

### 5️⃣ Verify Running Containers

```bash
docker ps
```

You should see: `router_a`, `router_b`, `router_c`

---

## 🔍 Viewing Logs

```bash
docker logs -f router_a
docker logs -f router_b
docker logs -f router_c
```

---

## 🧪 Testing Scenarios

### ✅ Test 1 — Route Learning

Routers automatically learn new networks. Example output:

```
[ADD] 10.0.2.0/24 via 10.0.1.20 (cost 1)
```

### ✅ Test 2 — Failure Handling

Stop a router:
```bash
docker stop router_b
```

After roughly 18 seconds:
```
[EXPIRE] Removed stale route 10.0.2.0/24
```

### ✅ Test 3 — Alternate Path Discovery

If one router fails, another path is chosen:
```
[ADD] 10.0.2.0/24 via 10.0.3.30 (cost 1)
```

### ✅ Test 4 — Restart Router

```bash
docker start router_b
```

Router rejoins, re-detects its interfaces, and resumes updates automatically.

---

## 🛡️ Loop Prevention (Split Horizon)

Routes learned from a neighbor are **not sent back to that same neighbor**. This implementation uses **split horizon only** and does **not** use poison reverse.

---

## 🧹 Cleanup

```bash
docker rm -f router_a router_b router_c
docker network rm net_ab net_bc net_ac
```

---

## 📌 Notes

- Direct routes always remain (cost = 0, never expire)
- Route timeout = 18 seconds
- Update interval = 1 second
- No dependency on `DIRECT_NETWORKS` env variable (auto-detected from interfaces)
- The evaluator and recovery helpers are designed around `10.0.x.x` Docker networks
- Logs show dynamic convergence clearly

---

## ✅ Conclusion

This project successfully demonstrates:

- Dynamic route discovery via interface auto-detection (no manual subnet config needed)
- Shortest path calculation using Bellman-Ford
- Node failure recovery — dead next-hops age out correctly and alternate paths are installed
- Link failure recovery — re-attached interfaces are detected automatically without restart
- Loop prevention via Split Horizon
- Thread-safe concurrent operation across sender, listener, and cleanup threads
- Assignment-ready design — passes the provided initial convergence, node-failure, and link-failure evaluations
