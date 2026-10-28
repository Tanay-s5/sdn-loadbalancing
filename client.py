
import argparse
import socket
import struct
import time

HEADER = struct.Struct("!Id")
END_MARKER = 0xFFFFFFFF
END_HEADER = struct.Struct("!IdI")


def main():
    parser = argparse.ArgumentParser(description="UDP traffic generator")
    parser.add_argument("--server", required=True, help="Server IP address")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--pps", type=float, default=500,
                        help="Packets per second")
    parser.add_argument("--size", type=int, default=1200,
                        help="UDP payload size in bytes")
    args = parser.parse_args()

    if args.duration <= 0 or args.pps <= 0:
        parser.error("duration and pps must be positive")
    if args.size < HEADER.size or args.size > 65507:
        parser.error("size must be between 12 and 65507 bytes")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    destination = (args.server, args.port)

    padding = b"x" * (args.size - HEADER.size)

    sent = 0
    start = time.monotonic()
    deadline = start + args.duration
    next_send = start

    print(f"Sending UDP traffic to {destination}")
    print(f"Rate={args.pps} packets/s, size={args.size} bytes")
    print(f"Duration={args.duration} seconds")

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()

            if now < next_send:
                time.sleep(next_send - now)
                continue

            packet = HEADER.pack(sent, time.time()) + padding
            sock.sendto(packet, destination)
            sent += 1
            next_send += 1.0 / args.pps

            # If the sender falls behind, don't send a huge burst
            # to catch up.
            if next_send < time.monotonic():
                next_send = time.monotonic()

    finally:
        elapsed = time.monotonic() - start

        # Repeat the end marker to improve the chance of delivery.
        end_packet = END_HEADER.pack(0xFFFFFFFF, time.time(), sent)
        for _ in range(5):
            sock.sendto(end_packet, destination)
            time.sleep(0.02)

        sock.close()

    print(f"Packets sent: {sent}")
    print(f"Elapsed time: {elapsed:.2f} seconds")
    print(f"Application payload rate: {sent * args.size * 8 / max(elapsed, 1e-9) / 1e6:.3f} Mbps")


if __name__ == "__main__":
    main()
