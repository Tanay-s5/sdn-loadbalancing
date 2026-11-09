import argparse
import csv
import json
import os
import signal
import socket
import struct
import time

HEADER = struct.Struct("!Id")

END_MARKER = 0xFFFFFFFF
END_HEADER = struct.Struct("!IdI")


def _stop(signum, frame):
    # Turn SIGTERM into the same clean shutdown path as Ctrl-C so the
    # logs below are always written.
    raise KeyboardInterrupt


def write_logs(prefix, packets, summary):
    """<prefix>_packets.csv (one row per unique packet) + <prefix>_summary.json"""
    folder = os.path.dirname(prefix)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(prefix + "_packets.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["seq", "sent_ts", "recv_ts"])
        for seq, sent_ts, recv_ts in packets:
            writer.writerow([seq, "%.6f" % sent_ts, "%.6f" % recv_ts])
    with open(prefix + "_summary.json", "w") as f:
        json.dump(summary, f, indent=2)


def main():
    parser = argparse.ArgumentParser(description="UDP traffic receiver")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--bind", default="0.0.0.0",
                        help="Address to bind to (default: all interfaces)")
    parser.add_argument("--report-interval", type=float, default=1.0)
    parser.add_argument("--log-prefix", default=None,
                        help="Write <prefix>_packets.csv (per-packet send/"
                             "receive timestamps) and <prefix>_summary.json "
                             "for the analysis script.")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _stop)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Big receive buffer so loss in the results is network loss, not
        # the receiving Python process falling behind.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass
    sock.bind((args.bind, args.port))
    sock.settimeout(args.report_interval)

    received_sequences = set()
    packets = []                      # (seq, sent_ts, recv_ts)
    total_bytes = 0
    interval_bytes = 0
    interval_packets = 0
    start_time = time.monotonic()
    first_packet_time = None
    interval_start = start_time
    client_address = None
    end_info = None                   # (total_sent, end_ts) once the client says so

    print(f"UDP server listening on port {args.port}", flush=True)

    try:
        while True:
            try:
                packet, address = sock.recvfrom(65535)
            except socket.timeout:
                packet = None

            now = time.monotonic()

            if packet is not None:
                if len(packet) < HEADER.size:
                    continue

                recv_wall = time.time()
                seq, sent_timestamp = HEADER.unpack_from(packet)

                # End-of-test message contains the total sent count.
                if seq == END_MARKER:
                    if len(packet) >= END_HEADER.size:
                        _, end_ts, total_sent = END_HEADER.unpack_from(packet)
                        end_info = (total_sent, end_ts)

                        received_count = len(received_sequences)
                        lost = max(0, total_sent - received_count)
                        loss_pct = (
                            100.0 * lost / total_sent
                            if total_sent else 0.0
                        )
                        # Measure from the first data packet, not from server
                        # start-up, so idle time doesn't dilute the average.
                        elapsed = now - (first_packet_time or start_time)
                        throughput = (
                            total_bytes * 8 / elapsed / 1_000_000
                            if elapsed > 0 else 0.0
                        )

                        print("\n--- Final results ---")
                        print(f"Client: {address[0]}")
                        print(f"Packets sent: {total_sent}")
                        print(f"Unique packets received: {received_count}")
                        print(f"Estimated packets lost: {lost}")
                        print(f"Estimated packet loss: {loss_pct:.2f}%")
                        print(f"Total data received: {total_bytes} bytes")
                        print(f"Average throughput: {throughput:.3f} Mbps")
                        break
                    continue

                if seq not in received_sequences:
                    if first_packet_time is None:
                        first_packet_time = now
                    client_address = address[0]
                    received_sequences.add(seq)
                    packets.append((seq, sent_timestamp, recv_wall))
                    total_bytes += len(packet)
                    interval_bytes += len(packet)
                    interval_packets += 1

            if now - interval_start >= args.report_interval:
                duration = now - interval_start
                mbps = interval_bytes * 8 / duration / 1_000_000

                print(
                    f"Received={interval_packets} packets | "
                    f"Throughput={mbps:.3f} Mbps | "
                    f"Unique total={len(received_sequences)}",
                    flush=True,
                )

                interval_bytes = 0
                interval_packets = 0
                interval_start = now

    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        sock.close()
        if args.log_prefix:
            total_sent = end_info[0] if end_info else None
            received = len(received_sequences)
            lost = max(0, total_sent - received) if total_sent else None
            write_logs(args.log_prefix, packets, {
                "port": args.port,
                "client": client_address,
                "total_sent": total_sent,
                "end_ts": end_info[1] if end_info else None,
                "unique_received": received,
                "lost": lost,
                "loss_pct": (100.0 * lost / total_sent
                             if total_sent else None),
                "total_bytes": total_bytes,
                "ended_by": "end_marker" if end_info else "stopped",
            })


if __name__ == "__main__":
    main()
