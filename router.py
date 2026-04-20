import os
import json
import socket
import time
import threading

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

# ==========================================
# Load Direct Networks
# ==========================================
for network_entry in direct_network_entries:
    cleaned_network = network_entry.strip()
    if cleaned_network:
        routing_information_table[cleaned_network] = [
            0,
            router_identity_ip,
            time.time()
        ]


# ==========================================
# Pretty Print Routing Table
# ==========================================
def show_current_routing_table():
    print("\n========== ROUTING TABLE ==========")
    print(f"Router: {router_identity_ip}")
    print("----------------------------------")

    for subnet_value, route_info in routing_information_table.items():
        metric_value = route_info[0]
        next_hop_value = route_info[1]

        print(
            f"{subnet_value:18} | "
            f"cost={metric_value:<2} | "
            f"via={next_hop_value}"
        )

    print("==================================\n")


# ==========================================
# Build DV Packet (Split Horizon)
# ==========================================
def construct_update_message(target_neighbor):
    route_entries = []

    for subnet_value, route_info in routing_information_table.items():
        distance_metric = route_info[0]
        learned_next_hop = route_info[1]

        # Split Horizon
        if distance_metric != 0 and learned_next_hop == target_neighbor:
            continue

        route_entries.append({
            "subnet": subnet_value,
            "distance": distance_metric
        })

    message_packet = {
        "router_id": router_identity_ip,
        "version": 1.0,
        "routes": route_entries
    }

    return json.dumps(message_packet)


# ==========================================
# Send Periodic Updates
# ==========================================
def periodic_update_sender():
    udp_sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sender.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    while True:
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


# ==========================================
# Bellman-Ford Update Logic
# ==========================================
def process_incoming_routes(source_ip, incoming_routes):
    table_updated = False
    current_timestamp = time.time()

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

        # New route
        if subnet_value not in routing_information_table:
            routing_information_table[subnet_value] = [
                computed_distance,
                source_ip,
                current_timestamp
            ]

            print(
                f"[ADD] {subnet_value} "
                f"via {source_ip} "
                f"(cost {computed_distance})"
            )

            table_updated = True

        else:
            existing_distance = routing_information_table[subnet_value][0]
            existing_next_hop = routing_information_table[subnet_value][1]

            # Better route
            if computed_distance < existing_distance:
                routing_information_table[subnet_value] = [
                    computed_distance,
                    source_ip,
                    current_timestamp
                ]

                print(
                    f"[UPDATE] {subnet_value} "
                    f"via {source_ip} "
                    f"(cost {computed_distance})"
                )

                table_updated = True

            # Same route refresh
            elif existing_next_hop == source_ip:
                routing_information_table[subnet_value][2] = current_timestamp

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
        removed_any_route = False

        for subnet_value in list(routing_information_table.keys()):
            route_info = routing_information_table[subnet_value]

            distance_metric = route_info[0]
            last_update_time = route_info[2]

            # Direct routes stay forever
            if distance_metric == 0:
                continue

            if current_time_value - last_update_time > route_timeout_limit:
                print(f"[EXPIRE] Removed stale route {subnet_value}")
                del routing_information_table[subnet_value]
                removed_any_route = True

        if removed_any_route:
            show_current_routing_table()

        time.sleep(5)


# ==========================================
# Main
# ==========================================
if __name__ == "__main__":
    print(f"\n[INIT] Router {router_identity_ip} started")
    show_current_routing_table()

    sender_thread = threading.Thread(
        target=periodic_update_sender,
        daemon=True
    )

    cleanup_thread = threading.Thread(
        target=expired_route_cleanup_worker,
        daemon=True
    )

    sender_thread.start()
    cleanup_thread.start()

    udp_listener_loop()