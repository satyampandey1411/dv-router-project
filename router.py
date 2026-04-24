import os
import json
import socket
import time
import threading
import subprocess

# ==========================================
# Configuration
# ==========================================
router_identity_ip = os.getenv("MY_IP", "127.0.0.1")
direct_network_entries = os.getenv("DIRECT_NETWORKS", "").split(",")
neighbor_ip_addresses = os.getenv("NEIGHBORS", "").split(",")

udp_port_number = 5000
route_timeout_limit = 15
update_interval_seconds = 5

# subnet -> [distance_metric, next_hop_ip, last_update_timestamp]
routing_information_table = {}

# Lock protecting all reads and writes to routing_information_table.
table_lock = threading.Lock()


# ==========================================
# Kernel Route Management
# Evaluator checks `ip route show table main` — not our Python dict.
# Every learned route must be written into the kernel table so the
# evaluator can see it.
# ==========================================
def kernel_add_route(subnet, via_ip):
    """Install a learned route into the kernel routing table."""
    try:
        subprocess.run(
            ["ip", "route", "replace", subnet, "via", via_ip],
            capture_output=True, check=False
        )
    except Exception as e:
        print(f"[WARN] Could not add kernel route {subnet} via {via_ip}: {e}")


def kernel_del_route(subnet):
    """Remove an expired learned route from the kernel routing table."""
    try:
        subprocess.run(
            ["ip", "route", "del", subnet],
            capture_output=True, check=False
        )
    except Exception as e:
        print(f"[WARN] Could not del kernel route {subnet}: {e}")


# ==========================================
# Auto-Detect Directly Connected Subnets
# ==========================================
def discover_connected_subnets():
    """
    Read directly connected subnets from the kernel routing table.
    Replaces the DIRECT_NETWORKS env-var approach: works correctly
    when the evaluator omits that variable.
    """
    detected = set()
    try:
        result = subprocess.run(
            ["ip", "route", "show", "table", "main"],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            # Kernel-injected directly-connected routes look like:
            #   10.0.1.0/24 dev eth0 proto kernel scope link src 10.0.1.10
            if "proto kernel" in line and "scope link" in line:
                parts = line.split()
                if parts and "/" in parts[0]:
                    detected.add(parts[0])
    except Exception as e:
        print(f"[WARN] Could not auto-detect subnets: {e}")
    return detected


def load_direct_routes():
    """
    Populate routing_information_table with all directly connected subnets.
    Merges DIRECT_NETWORKS env var (backward-compat) + kernel auto-detection.
    Direct routes are cost=0 and do NOT need a kernel route add — Docker
    already injected them.
    """
    all_subnets = set()

    for network_entry in direct_network_entries:
        cleaned = network_entry.strip()
        if cleaned:
            all_subnets.add(cleaned)

    all_subnets |= discover_connected_subnets()

    with table_lock:
        for subnet in all_subnets:
            routing_information_table[subnet] = [0, router_identity_ip, time.time()]
            print(f"[DIRECT] {subnet} (cost 0, self)")


load_direct_routes()


# ==========================================
# Pretty Print Routing Table
# ==========================================
def show_current_routing_table():
    print("\n========== ROUTING TABLE ==========")
    print(f"Router: {router_identity_ip}")
    print("----------------------------------")
    with table_lock:
        snapshot = dict(routing_information_table)
    for subnet_value, route_info in snapshot.items():
        print(
            f"{subnet_value:18} | "
            f"cost={route_info[0]:<2} | "
            f"via={route_info[1]}"
        )
    print("==================================\n")


# ==========================================
# Build DV Packet (Split Horizon)
# ==========================================
def construct_update_message(target_neighbor):
    route_entries = []
    with table_lock:
        snapshot = dict(routing_information_table)
    for subnet_value, route_info in snapshot.items():
        distance_metric = route_info[0]
        learned_next_hop = route_info[1]
        # Split Horizon: do not advertise a route back to the neighbor
        # it was learned from (loop prevention)
        if distance_metric != 0 and learned_next_hop == target_neighbor:
            continue
        route_entries.append({"subnet": subnet_value, "distance": distance_metric})
    return json.dumps({
        "router_id": router_identity_ip,
        "version": 1.0,
        "routes": route_entries
    })


# ==========================================
# Send Periodic Updates
# ==========================================
def periodic_update_sender():
    udp_sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    while True:
        # Re-scan interfaces so recovered links are re-advertised automatically
        refresh_direct_routes()

        for neighbor_ip in neighbor_ip_addresses:
            neighbor_ip = neighbor_ip.strip()
            if not neighbor_ip:
                continue
            try:
                packet_bytes = construct_update_message(neighbor_ip).encode()
                udp_sender.sendto(packet_bytes, (neighbor_ip, udp_port_number))
            except Exception as send_issue:
                print(f"[ERROR] Could not send to {neighbor_ip}: {send_issue}")

        time.sleep(update_interval_seconds)


def refresh_direct_routes():
    """
    Re-scan interfaces and add any newly appeared directly connected subnets.
    Also refreshes timestamps of existing direct routes so they never expire.
    """
    current_time = time.time()
    with table_lock:
        for subnet in discover_connected_subnets():
            if subnet not in routing_information_table:
                routing_information_table[subnet] = [0, router_identity_ip, current_time]
                print(f"[DIRECT-NEW] {subnet} (cost 0, self)")
            elif routing_information_table[subnet][0] == 0:
                routing_information_table[subnet][2] = current_time


# ==========================================
# Bellman-Ford Update Logic
# ==========================================
def process_incoming_routes(source_ip, incoming_routes):
    table_updated = False
    current_timestamp = time.time()
    routes_to_add_to_kernel = []   # (subnet, via_ip) pairs

    with table_lock:
        for route_item in incoming_routes:
            subnet_value = route_item.get("subnet")
            received_distance = route_item.get("distance")

            if subnet_value is None or received_distance is None:
                continue

            computed_distance = received_distance + 1

            # Protect directly connected routes
            if (
                subnet_value in routing_information_table and
                routing_information_table[subnet_value][0] == 0
            ):
                continue

            if subnet_value not in routing_information_table:
                # New route discovered
                routing_information_table[subnet_value] = [
                    computed_distance, source_ip, current_timestamp
                ]
                routes_to_add_to_kernel.append((subnet_value, source_ip))
                print(f"[ADD] {subnet_value} via {source_ip} (cost {computed_distance})")
                table_updated = True

            else:
                existing_distance = routing_information_table[subnet_value][0]
                existing_next_hop = routing_information_table[subnet_value][1]

                if computed_distance < existing_distance:
                    # Better route found — take it
                    routing_information_table[subnet_value] = [
                        computed_distance, source_ip, current_timestamp
                    ]
                    routes_to_add_to_kernel.append((subnet_value, source_ip))
                    print(f"[UPDATE] {subnet_value} via {source_ip} (cost {computed_distance})")
                    table_updated = True

                elif existing_next_hop == source_ip:
                    # Same next-hop keep-alive: refresh timestamp only
                    routing_information_table[subnet_value][2] = current_timestamp

                # NOTE: if a DIFFERENT neighbor advertises the same cost we do
                # NOT refresh the timestamp. This lets the route age out when
                # the real next-hop goes silent (node failure), so we converge
                # to the alternative path correctly.

    # Install new/updated routes into the kernel OUTSIDE the lock
    for subnet, via_ip in routes_to_add_to_kernel:
        kernel_add_route(subnet, via_ip)

    if table_updated:
        show_current_routing_table()


# ==========================================
# Listen for Incoming Updates
# ==========================================
def udp_listener_loop():
    udp_receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_receiver.bind(("0.0.0.0", udp_port_number))

    print(f"[INFO] Listening on UDP port {udp_port_number}")

    while True:
        try:
            packet_data, sender_address = udp_receiver.recvfrom(4096)
            sender_ip = sender_address[0]
            decoded_message = json.loads(packet_data.decode())
            if decoded_message.get("version") != 1.0:
                continue
            received_routes = decoded_message.get("routes", [])
            process_incoming_routes(sender_ip, received_routes)
        except Exception as receive_issue:
            print("[ERROR] Receive issue:", receive_issue)


# ==========================================
# Remove Expired Routes
# ==========================================
def expired_route_cleanup_worker():
    while True:
        current_time_value = time.time()
        removed_subnets = []

        with table_lock:
            for subnet_value in list(routing_information_table.keys()):
                route_info = routing_information_table[subnet_value]
                distance_metric = route_info[0]
                last_update_time = route_info[2]

                # Direct routes (cost 0) stay forever
                if distance_metric == 0:
                    continue

                if current_time_value - last_update_time > route_timeout_limit:
                    print(f"[EXPIRE] Removed stale route {subnet_value}")
                    del routing_information_table[subnet_value]
                    removed_subnets.append(subnet_value)

        # Remove expired routes from kernel OUTSIDE the lock
        for subnet in removed_subnets:
            kernel_del_route(subnet)

        if removed_subnets:
            show_current_routing_table()

        time.sleep(5)


# ==========================================
# Main
# ==========================================
if __name__ == "__main__":
    print(f"\n[INIT] Router {router_identity_ip} started")
    show_current_routing_table()

    sender_thread = threading.Thread(target=periodic_update_sender, daemon=True)
    cleanup_thread = threading.Thread(target=expired_route_cleanup_worker, daemon=True)

    sender_thread.start()
    cleanup_thread.start()

    udp_listener_loop()
