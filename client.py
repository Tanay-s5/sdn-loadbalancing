import argparse
import json
import socket
import struct
import time

HEADER = struct.Struct("!Id")
END_MARKER = 0xFFFFFFFF
END_HEADER = struct.Struct("!IdI")


def parse_profile(text, default_pps):
    """Parse "t:pps,t:pps,..." into a sorted [(seconds, pps), ...] step list.

    Example: "0:150,10:450,30:150" sends 150 pkt/s, jumps to 450 pkt/s after
    10 s (controlled congestion burst) and drops back to 150 pkt/s after 30 s.
    If the first step does not start at 0, --pps is used until it does.
    """
    steps = []
    for item in text.split(","):
        t, pps = item.split(":")
        steps.append((float(t), float(pps)))
    steps.sort()
    if any(t < 0 or pps <= 0 for t, pps in steps):
        raise ValueError("profile times must be >= 0 and rates > 0")
    if steps[0][0] > 0:
        steps.insert(0, (0.0, default_pps))
    return steps


def pps_at(profile, elapsed):
    pps = profile[0][1]
    for t, rate in profile:
        if elapsed >= t:
            pps = rate
        else:
            break
    return pps


def main():
    parser = argparse.ArgumentParser(description="UDP traffic generator")
    parser.add_argument("--server", required=True, help="Server IP address")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--pps", type=float, default=500,
                        help="Packets per second")
    parser.add_argument("--size", type=int, default=1200,
                        help="UDP payload size in bytes")
    parser.add_argument("--profile", default=None,
                        help="Time-varying rate as 't:pps,t:pps,...' "
                             "(seconds since start). Overrides --pps after "
                             "the first step. Used to create a controlled "
                             "congestion burst.")
    parser.add_argument("--summary-json", default=None,
                        help="Write start/end wall-clock times and counters "
                             "to this file (used by the analysis).")
    args = parser.parse_args()

    if args.duration <= 0 or args.pps <= 0:
        parser.error("duration and pps must be positive")
    if args.size < HEADER.size or args.size > 65507:
        parser.error("size must be between 12 and 65507 bytes")

    try:
        profile = (parse_profile(args.profile, args.pps) if args.profile
                   else [(0.0, args.pps)])
    except ValueError as exc:
        parser.error(f"bad --profile: {exc}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    destination = (args.server, args.port)

    padding = b"x" * (args.size - HEADER.size)

    sent = 0
    start_wall = time.time()
    start = time.monotonic()
    deadline = start + args.duration
    next_send = start

    print(f"Sending UDP traffic to {destination}")
    if args.profile:
        print(f"Rate profile (t[s]:pkt/s)={args.profile}, size={args.size} bytes")
    else:
        print(f"Rate={args.pps} packets/s, size={args.size} bytes")
    print(f"Duration={args.duration} seconds", flush=True)

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()

            if now < next_send:
                time.sleep(next_send - now)
                continue

            packet = HEADER.pack(sent, time.time()) + padding
            sock.sendto(packet, destination)
            sent += 1
            next_send += 1.0 / pps_at(profile, now - start)

            # If the sender falls behind, don't send a huge burst
            # to catch up.
            if next_send < time.monotonic():
                next_send = time.monotonic()

    finally:
        elapsed = time.monotonic() - start
        end_wall = time.time()

        # Repeat the end marker to improve the chance of delivery.
        end_packet = END_HEADER.pack(0xFFFFFFFF, time.time(), sent)
        for _ in range(5):
            sock.sendto(end_packet, destination)
            time.sleep(0.02)

        sock.close()

        if args.summary_json:
            with open(args.summary_json, "w") as f:
                json.dump({
                    "server": args.server, "port": args.port,
                    "size": args.size, "duration": args.duration,
                    "profile": profile, "start_wall": start_wall,
                    "end_wall": end_wall, "sent": sent,
                    "elapsed": elapsed,
                }, f, indent=2)

    print(f"Packets sent: {sent}")
    print(f"Elapsed time: {elapsed:.2f} seconds")
    print(f"Application payload rate: {sent * args.size * 8 / max(elapsed, 1e-9) / 1e6:.3f} Mbps")


if __name__ == "__main__":
    main()
