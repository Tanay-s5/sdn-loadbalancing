#!/usr/bin/env python3
"""Run the static-vs-adaptive congestion experiment end to end.

For each routing mode (static, then adaptive) this script
  1. starts Ryu with ROUTING_MODE=<mode>,
  2. builds the Mininet diamond topology and waits for the switches,
  3. starts the three UDP servers (h4, h5, h6),
  4. starts the three UDP clients (h1->h4, h2->h5, h3->h6) at the same time,
     all at a modest base rate; one client (h3 by default) jumps to a much
     higher rate for a fixed window -> *controlled congestion* on path A,
  5. collects per-packet logs + controller logs into results/<mode>/.
Then it runs analyze.py to compare the two runs.

Needs root (Mininet) and `ryu-manager` on root's PATH:
    sudo env "PATH=$PATH" python3 run_experiment.py
    sudo env "PATH=$PATH" python3 run_experiment.py --modes adaptive
    sudo env "PATH=$PATH" python3 run_experiment.py --duration 60 --burst-start 15 --burst-end 45

If a run is killed half way, clean up with:  sudo mn -c
"""
import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(HERE, "server.py")
CLIENT_PY = os.path.join(HERE, "client.py")

# (client host number, server host number): one flow per pair, all concurrent.
PAIRS = [(1, 4), (2, 5), (3, 6)]
UDP_PORT = 5000


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Static vs adaptive routing experiment (Mininet + Ryu)")
    p.add_argument("--modes", nargs="+", default=["static", "adaptive"],
                   choices=["static", "adaptive"])
    p.add_argument("--outdir", default=os.path.join(HERE, "results"))

    g = p.add_argument_group("traffic / controlled congestion")
    g.add_argument("--duration", type=float, default=40.0,
                   help="seconds each client sends (default 40)")
    g.add_argument("--base-pps", type=float, default=150,
                   help="normal rate of every client in packets/s "
                        "(150 pps x 1200 B ~ 1.5 Mbps)")
    g.add_argument("--burst-pps", type=float, default=450,
                   help="rate of the burst client during the burst "
                        "(450 pps ~ 4.5 Mbps)")
    g.add_argument("--burst-client", type=int, default=3, choices=[1, 2, 3])
    g.add_argument("--burst-start", type=float, default=10.0,
                   help="seconds after start when congestion begins")
    g.add_argument("--burst-end", type=float, default=30.0,
                   help="seconds after start when the burst stops")
    g.add_argument("--size", type=int, default=1200, help="UDP payload bytes")
    g.add_argument("--report-interval", type=float, default=0.5,
                   help="server console report interval (s)")

    c = p.add_argument_group("controller")
    c.add_argument("--ryu-cmd", default="ryu-manager",
                   help="command that starts Ryu (default: ryu-manager)")
    c.add_argument("--controller-wait", type=float, default=3.0,
                   help="seconds to give Ryu to start (default 3)")
    c.add_argument("--poll-interval", type=float, default=0.5)
    c.add_argument("--threshold", type=float, default=0.85,
                   help="congestion threshold, fraction of path capacity")
    c.add_argument("--confirm-samples", type=int, default=2)
    c.add_argument("--cooldown", type=float, default=5.0)

    p.add_argument("--delay-limit-ms", type=float, default=50.0,
                   help="packets later than this count as 'bad' when "
                        "measuring recovery (analysis)")
    p.add_argument("--no-analyze", action="store_true")
    args = p.parse_args(argv)

    if not (0 < args.burst_start < args.burst_end <= args.duration):
        p.error("need 0 < burst-start < burst-end <= duration")
    return args


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def wait_port_free(port, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if not port_in_use(port):
            return True
        time.sleep(0.5)
    return False


def start_controller(mode, run_dir, args, port):
    env = os.environ.copy()
    env.update({
        "ROUTING_MODE": mode,
        "RESULTS_DIR": run_dir,
        "POLL_INTERVAL": str(args.poll_interval),
        "CONGESTION_THRESHOLD": str(args.threshold),
        "CONFIRM_SAMPLES": str(args.confirm_samples),
        "REROUTE_COOLDOWN": str(args.cooldown),
    })
    cmd = shlex.split(args.ryu_cmd) + [
        "--ofp-tcp-listen-port", str(port),
        os.path.join(HERE, "controller.py")]
    log = open(os.path.join(run_dir, "ryu.log"), "w")
    try:
        proc = subprocess.Popen(cmd, env=env, stdout=log,
                                stderr=subprocess.STDOUT,
                                start_new_session=True)
    except FileNotFoundError:
        log.close()
        sys.exit("Could not run %r. Under sudo the PATH is reset; try:\n"
                 "  sudo env \"PATH=$PATH\" python3 run_experiment.py\n"
                 "or pass --ryu-cmd with the full path." % args.ryu_cmd)
    time.sleep(args.controller_wait)
    if proc.poll() is not None:
        log.close()
        sys.exit("Ryu exited immediately, see %s" %
                 os.path.join(run_dir, "ryu.log"))
    return proc, log


def stop_group(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def wait_for_switches(net, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        if all(s.connected() for s in net.switches):
            return
        time.sleep(0.5)
    raise RuntimeError("switches did not connect to the controller within "
                       "%.0f s (check ryu.log)" % timeout)


def check_connectivity(net):
    h1, h4 = net.get("h1"), net.get("h4")
    for _ in range(5):
        out = h1.cmd("ping -c 2 -W 1 %s" % h4.IP())
        if " 0% packet loss" in out:
            return
        time.sleep(1.0)
    raise RuntimeError("h1 cannot reach h4: flow rules were not installed "
                       "(check ryu.log)\n" + out)


# --------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------
def run_mode(mode, args):
    import topo                      # imports mininet; only needed here

    run_dir = os.path.join(args.outdir, mode)
    shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir)

    if not wait_port_free(topo.CONTROLLER_PORT):
        sys.exit("Port %d is already in use (a ryu-manager you started by "
                 "hand?). Stop it, this script starts its own controller."
                 % topo.CONTROLLER_PORT)

    print("\n=== %s routing ===" % mode.upper(), flush=True)
    ctrl, ctrl_log = start_controller(mode, run_dir, args,
                                      topo.CONTROLLER_PORT)
    net = None
    procs, files = [], []
    try:
        net = topo.make_net()
        net.start()
        net.staticArp()
        wait_for_switches(net)
        time.sleep(2.0)                           # let the flow rules land
        check_connectivity(net)

        for _, s in PAIRS:
            out = open(os.path.join(run_dir, "server_h%d.out" % s), "w")
            files.append(out)
            procs.append(net.get("h%d" % s).popen(
                [sys.executable, "-u", SERVER_PY,
                 "--port", str(UDP_PORT),
                 "--report-interval", str(args.report_interval),
                 "--log-prefix", os.path.join(run_dir, "h%d" % s)],
                stdout=out, stderr=subprocess.STDOUT))
        servers = list(procs)
        time.sleep(1.0)

        burst_profile = "0:%g,%g:%g,%g:%g" % (
            args.base_pps, args.burst_start, args.burst_pps,
            args.burst_end, args.base_pps)
        print("clients: %g pps each; h%d bursts to %g pps between t=%gs and "
              "t=%gs; total run %gs" % (
                  args.base_pps, args.burst_client, args.burst_pps,
                  args.burst_start, args.burst_end, args.duration),
              flush=True)

        clients = []
        for c, s in PAIRS:
            cmd = [sys.executable, "-u", CLIENT_PY,
                   "--server", "10.0.0.%d" % s, "--port", str(UDP_PORT),
                   "--duration", str(args.duration),
                   "--size", str(args.size), "--pps", str(args.base_pps),
                   "--summary-json",
                   os.path.join(run_dir, "client_h%d.json" % c)]
            if c == args.burst_client:
                cmd += ["--profile", burst_profile]
            out = open(os.path.join(run_dir, "client_h%d.out" % c), "w")
            files.append(out)
            clients.append(net.get("h%d" % c).popen(
                cmd, stdout=out, stderr=subprocess.STDOUT))
        procs.extend(clients)

        for p in clients:
            p.wait(timeout=args.duration + 30)
        time.sleep(1.5)                           # let the end markers land

        for p in servers:                         # normally already done
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.terminate()                     # server logs on SIGTERM
                p.wait(timeout=10)

        write_scenario(mode, run_dir, args)
        print("results written to %s" % run_dir, flush=True)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in files:
            f.close()
        if net is not None:
            net.stop()
        stop_group(ctrl)
        ctrl_log.close()
        time.sleep(1.0)


def write_scenario(mode, run_dir, args):
    starts = {}
    for c, _ in PAIRS:
        path = os.path.join(run_dir, "client_h%d.json" % c)
        try:
            with open(path) as f:
                starts[c] = json.load(f)["start_wall"]
        except (OSError, KeyError, ValueError):
            raise RuntimeError("client h%d left no summary; see %s" % (
                c, os.path.join(run_dir, "client_h%d.out" % c)))
    burst_start_wall = starts[args.burst_client]
    scenario = {
        "mode": mode,
        "duration_s": args.duration,
        "size": args.size,
        "base_pps": args.base_pps,
        "burst_pps": args.burst_pps,
        "burst_client": "h%d" % args.burst_client,
        "burst_start_s": args.burst_start,
        "burst_end_s": args.burst_end,
        "t0_wall": min(starts.values()),
        "onset_wall": burst_start_wall + args.burst_start,
        "burst_end_wall": burst_start_wall + args.burst_end,
        "threshold": args.threshold,
        "poll_interval_s": args.poll_interval,
        "cooldown_s": args.cooldown,
        "client_start_wall": {"h%d" % c: t for c, t in starts.items()},
        "flows": [{"client": "h%d" % c, "server": "h%d" % s}
                  for c, s in PAIRS],
    }
    with open(os.path.join(run_dir, "scenario.json"), "w") as f:
        json.dump(scenario, f, indent=2)


def give_files_back(path):
    """Under sudo, hand the results back to the invoking user."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return
    for root, dirs, names in os.walk(path):
        for n in dirs + names:
            try:
                os.chown(os.path.join(root, n), int(uid), int(gid))
            except OSError:
                pass
    try:
        os.chown(path, int(uid), int(gid))
    except OSError:
        pass


def main(argv=None):
    args = parse_args(argv)
    if os.geteuid() != 0:
        sys.exit("Mininet needs root:  sudo env \"PATH=$PATH\" python3 "
                 "run_experiment.py")
    os.makedirs(args.outdir, exist_ok=True)

    for mode in args.modes:
        run_mode(mode, args)

    if not args.no_analyze:
        import analyze
        analyze.main(["--results", args.outdir, "--modes", *args.modes,
                      "--delay-limit-ms", str(args.delay_limit_ms)])
    give_files_back(args.outdir)


if __name__ == "__main__":
    main()
