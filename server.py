
import argparse
import socket
import struct
import time

HEADER = struct.Struct("!Id")

END_MARKER = 0xFFFFFFFF
END_HEADER = struct.Struct("!IdI")


def main():
    parser = argparse.ArgumentParser(description="UDP traffic receiver")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--report-interval", type=float, default=1.0)
    args = parser.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(args.report_interval)

    received_sequences = set()
    total_bytes = 0
    interval_bytes = 0
    interval_packets = 0
    start_time = time.monotonic()
    interval_start = start_time

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

                seq, sent_timestamp = HEADER.unpack_from(packet)

                # End-of-test message contains the total sent count.
                if seq == END_MARKER:
                    if len(packet) >= END_HEADER.size:
                        _, _, total_sent = END_HEADER.unpack_from(packet)

                        received_count = len(received_sequences)
                        lost = max(0, total_sent - received_count)
                        loss_pct = (
                            100.0 * lost / total_sent
                            if total_sent else 0.0
                        )
                        elapsed = now - start_time
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
                    received_sequences.add(seq)
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


if __name__ == "__main__":
    main()
