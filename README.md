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
- Fully containerized using Docker

---

## 📁 Project Structure

```

dv-router-project/
├── router.py
├── Dockerfile
├── README.md
└── report.md

````

---

## ⚙️ Prerequisites

- Docker installed
- Linux / WSL / Ubuntu recommended

Check Docker:

```bash
docker --version
````

---

# 🚀 Complete Setup & Execution

## 1️⃣ Build Docker Image

```bash
docker build --no-cache -t my-router .
```

---

## 2️⃣ Clean Previous Setup (IMPORTANT)

```bash
docker rm -f router_a router_b router_c
docker network rm net_ab net_bc net_ac
```

(Errors like "No such container" are fine)

---

## 3️⃣ Create Networks

```bash
docker network create --subnet=172.28.1.0/24 net_ab
docker network create --subnet=172.28.2.0/24 net_bc
docker network create --subnet=172.28.3.0/24 net_ac
```

---

## 4️⃣ Start Routers

### 🔹 Router A

```bash
docker run -d --name router_a --privileged \
--network net_ab --ip 172.28.1.10 \
-e MY_IP=172.28.1.10 \
-e DIRECT_NETWORKS=172.28.1.0/24,172.28.3.0/24 \
-e NEIGHBORS=172.28.1.20,172.28.3.30 \
my-router

docker network connect net_ac router_a --ip 172.28.3.10
```

---

### 🔹 Router B

```bash
docker run -d --name router_b --privileged \
--network net_ab --ip 172.28.1.20 \
-e MY_IP=172.28.1.20 \
-e DIRECT_NETWORKS=172.28.1.0/24,172.28.2.0/24 \
-e NEIGHBORS=172.28.1.10,172.28.2.30 \
my-router

docker network connect net_bc router_b --ip 172.28.2.20
```

---

### 🔹 Router C

```bash
docker run -d --name router_c --privileged \
--network net_bc --ip 172.28.2.30 \
-e MY_IP=172.28.2.30 \
-e DIRECT_NETWORKS=172.28.2.0/24,172.28.3.0/24 \
-e NEIGHBORS=172.28.2.20,172.28.3.10 \
my-router

docker network connect net_ac router_c --ip 172.28.3.30
```

---

## 5️⃣ Verify Running Containers

```bash
docker ps
```

You should see:

* router_a
* router_b
* router_c

---

## 🔍 Viewing Logs

### Router A

```bash
docker logs -f router_a
```

### Router B

```bash
docker logs -f router_b
```

### Router C

```bash
docker logs -f router_c
```

---

# 🧪 Testing Scenarios

---

## ✅ Test 1 — Route Learning

Routers automatically learn new networks.

Example output:

```
[ADD] 172.28.2.0/24 via 172.28.1.20 (cost 1)
```

---

## ✅ Test 2 — Failure Handling

Stop a router:

```bash
docker stop router_b
```

After ~15 seconds:

```
[EXPIRE] Removed stale route 172.28.2.0/24
```

---

## ✅ Test 3 — Alternate Path Discovery

If one router fails, another path is chosen:

```
[ADD] 172.28.2.0/24 via 172.28.3.30 (cost 1)
```

---

## ✅ Test 4 — Restart Router

```bash
docker start router_b
```

Router rejoins and exchanges updates again.

---

## 🛡️ Loop Prevention (Split Horizon)

Routes learned from a neighbor are **not sent back to the same neighbor**, preventing routing loops.

---

# 🧹 Cleanup

```bash
docker rm -f router_a router_b router_c
docker network rm net_ab net_bc net_ac
```

---

## 📌 Notes

* Direct routes always remain
* Route timeout = 15 seconds
* Update interval = 5 seconds
* Logs show dynamic convergence clearly

---

## ✅ Conclusion

This project successfully demonstrates:

* Dynamic route discovery
* Shortest path calculation
* Failure recovery
* Loop prevention
* Realistic router behavior using Docker