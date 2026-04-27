#!/usr/bin/env python3
"""Distance-vector helper for Docker eval (compact variant of router.py logic)."""

import json
import os
import socket
import subprocess
import threading
import time

host_v4 = os.getenv("MY_IP", "127.0.0.1")
env_static_prefixes = os.getenv("DIRECT_NETWORKS", "").split(",")
adjacent_hosts = os.getenv("NEIGHBORS", "").split(",")

svc_udp = 5000
hold_ttl_s = 15
beacon_s = 1
BAD_METRIC = 16

# prefix -> [cost, gw_ip, touched_at]
topo_map = {}
mtx_topo = threading.Lock()
mtx_rtab = threading.Lock()
linux_added = set()

sk_tx = None
mtx_tx = threading.Lock()
past_setup = False
drip_bad = {}

def ip_argv(parts):
    return subprocess.run(["ip", *parts], capture_output=True, text=True, check=False)

def count_adjacent():
    return len([x for x in adjacent_hosts if x.strip()])

def kernel_link_prefixes():
    got = set()
    res = ip_argv(["route", "show", "table", "main"])
    if res.returncode != 0 or not res.stdout:
        return got
    for ln in res.stdout.strip().split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if "proto kernel" in ln and "scope link" in ln:
            tok = ln.split()[0]
            if "/" in tok and tok.startswith("10."):
                got.add(tok)
    return got

def bootstrap_prefixes():
    ts = time.time()
    with mtx_topo:
        for chunk in env_static_prefixes:
            p = chunk.strip()
            if p:
                topo_map[p] = [0, host_v4, ts]
        for p in kernel_link_prefixes():
            topo_map[p] = [0, host_v4, ts]

def rescan_links():
    ts = time.time()
    dirty = False
    with mtx_topo:
        for p in kernel_link_prefixes():
            row = topo_map.get(p)
            if row is None or row[0] != 0:
                topo_map[p] = [0, host_v4, ts]
                dirty = True
            else:
                topo_map[p][2] = ts
    if dirty:
        trace_topo()
        mirror_linux_routes()
        flood_peers(advance_poison=False)

def mirror_linux_routes():
    global past_setup
    direct = kernel_link_prefixes()
    if len(direct) >= count_adjacent():
        past_setup = True
    with mtx_topo:
        want = {}
        for pfx, (cost, gw, _) in topo_map.items():
            if pfx in direct:
                continue
            if cost > 0 and gw:
                want[pfx] = gw

    with mtx_rtab:
        for pfx in list(linux_added):
            if pfx not in want:
                ip_argv(["route", "del", pfx])
                linux_added.discard(pfx)
        if not past_setup and len(direct) < count_adjacent():
            return
        for pfx, gw in want.items():
            if ip_argv(["route", "replace", pfx, "via", gw]).returncode == 0:
                linux_added.add(pfx)

def trace_topo():
    print("\n========== ROUTING TABLE ==========")
    print(f"Router: {host_v4}")
    print("----------------------------------")
    with mtx_topo:
        snap = list(topo_map.items())
    for pfx, row in snap:
        print(f"{pfx:18} | cost={row[0]:<2} | via={row[1]}")
    print("==================================\n")

def build_tlv(peer_ip):
    rows = []
    with mtx_topo:
        body = list(topo_map.items())
        for k in list(drip_bad.keys()):
            if k in topo_map:
                del drip_bad[k]
        flash = list(drip_bad.keys())
    for pfx, row in body:
        cost, via, _ = row
        dist = BAD_METRIC if (cost != 0 and via == peer_ip) else cost
        rows.append({"subnet": pfx, "distance": dist})
    for pfx in flash:
        rows.append({"subnet": pfx, "distance": BAD_METRIC})
    return json.dumps({"router_id": host_v4, "version": 1.0, "routes": rows})

def flood_peers(advance_poison=True):
    global sk_tx
    if sk_tx is None:
        return
    with mtx_tx:
        for raw in adjacent_hosts:
            dst = raw.strip()
            if not dst:
                continue
            try:
                sk_tx.sendto(build_tlv(dst).encode(), (dst, svc_udp))
            except OSError as err:
                print(f"[ERROR] Could not send to {dst}: {err}")
    if advance_poison:
        with mtx_topo:
            for k in list(drip_bad.keys()):
                drip_bad[k] -= 1
                if drip_bad[k] <= 0:
                    del drip_bad[k]

def beacon_loop():
    while True:
        rescan_links()
        flood_peers()
        time.sleep(beacon_s)

def _best_per_pfx(rows):
    best = {}
    for item in rows:
        pfx = item.get("subnet")
        dist = item.get("distance")
        if pfx is None or dist is None:
            continue
        prev = best.get(pfx)
        if prev is None or dist < prev:
            best[pfx] = dist
    return best

def ingest_tlv(src_ip, rows):
    dirty = False
    now = time.time()
    for pfx, dist in _best_per_pfx(rows).items():
        if dist >= BAD_METRIC:
            with mtx_topo:
                if pfx in topo_map and topo_map[pfx][0] > 0 and topo_map[pfx][1] == src_ip:
                    del topo_map[pfx]
                    dirty = True
            continue
        cand = dist + 1
        if cand >= BAD_METRIC:
            with mtx_topo:
                if pfx in topo_map and topo_map[pfx][0] > 0 and topo_map[pfx][1] == src_ip:
                    del topo_map[pfx]
                    dirty = True
            continue
        with mtx_topo:
            if pfx in topo_map and topo_map[pfx][0] == 0:
                continue
            if pfx not in topo_map:
                topo_map[pfx] = [cand, src_ip, now]
                print(f"[ADD] {pfx} via {src_ip} (cost {cand})")
                dirty = True
            else:
                old_c, old_gw, _ = topo_map[pfx]
                if cand < old_c:
                    topo_map[pfx] = [cand, src_ip, now]
                    print(f"[UPDATE] {pfx} via {src_ip} (cost {cand})")
                    dirty = True
                elif old_gw == src_ip:
                    topo_map[pfx][0] = cand
                    topo_map[pfx][2] = now
                    dirty = True
    if dirty:
        trace_topo()
        mirror_linux_routes()
        flood_peers(advance_poison=False)

def recv_loop():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.bind(("0.0.0.0", svc_udp))
    print(f"[INFO] Listening on UDP port {svc_udp}")
    while True:
        try:
            blob, frm = rx.recvfrom(4096)
            pkt = json.loads(blob.decode())
            if pkt.get("version") != 1.0:
                continue
            ingest_tlv(frm[0], pkt.get("routes", []))
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            print("[ERROR] Bad packet:", err)
        except Exception as err:
            print("[ERROR] Receive issue:", err)

def sweep_stale():
    while True:
        now = time.time()
        gone = False
        with mtx_topo:
            for pfx in list(topo_map.keys()):
                row = topo_map[pfx]
                if row[0] == 0:
                    continue
                if now - row[2] > hold_ttl_s:
                    print(f"[EXPIRE] Removed stale route {pfx}")
                    del topo_map[pfx]
                    drip_bad[pfx] = 3
                    gone = True
        if gone:
            trace_topo()
            mirror_linux_routes()
            flood_peers(advance_poison=False)
        time.sleep(1)

def main():
    global sk_tx
    print(f"\n[INIT] Router {host_v4} started")
    bootstrap_prefixes()
    mirror_linux_routes()
    trace_topo()
    sk_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk_tx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    threading.Thread(target=beacon_loop, daemon=True).start()
    threading.Thread(target=sweep_stale, daemon=True).start()
    recv_loop()

if __name__ == "__main__":
    main()
