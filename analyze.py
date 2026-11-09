#!/usr/bin/env python3
"""Compare static and adaptive routing runs written by run_experiment.py.

Reads results/<mode>/ (scenario.json, h4/h5/h6_packets.csv + _summary.json,
controller_events.jsonl, controller_stats.csv) and produces

  * a printed comparison table,
  * results/summary.json and results/summary.csv,
  * results/comparison.png (if matplotlib is installed).

Definitions
-----------
onset            wall-clock time the controlled congestion starts
                 (burst client start time + --burst-start).
congestion window   onset .. end of the burst.
bad packet       lost, or one-way delay above --delay-limit-ms.
recovery time    (send time of the LAST bad packet at or after onset) - onset,
                 worst case over all flows. After that instant every packet
                 is delivered on time again. "recovered before congestion
                 ended" is False when bad packets keep occurring until the
                 burst itself stops, i.e. the network only got better because
                 the load went away, not because routing reacted.
Lost packets have no receive time, so their send time is interpolated from
the sequence numbers / timestamps of the packets around them.
"""
import argparse
import bisect
import csv
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DELAY_LIMIT_MS = 50.0
RECOVERY_MARGIN_S = 1.0       # "recovered early" = last bad packet this much before burst end
PLOT_BIN_S = 0.5


# --------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------
def read_json(path):
    with open(path) as f:
        return json.load(f)


def read_packets(path):
    seqs, sent, recv = [], [], []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            seqs.append(int(row["seq"]))
            sent.append(float(row["sent_ts"]))
            recv.append(float(row["recv_ts"]))
    return seqs, sent, recv


def read_events(path):
    events = []
    if not os.path.exists(path):
        return events
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
    events.sort(key=lambda e: e.get("t", 0))
    return events


def read_stats(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            parsed = {}
            for k, v in row.items():
                try:
                    parsed[k] = float(v)
                except (TypeError, ValueError):
                    parsed[k] = v
            rows.append(parsed)
    return rows


# --------------------------------------------------------------------------
# maths
# --------------------------------------------------------------------------
def percentile(values, q):
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * q / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def mean(values):
    return sum(values) / len(values) if values else float("nan")


def summarise(records):
    """records: [(send_time, delay_seconds_or_None), ...]"""
    n = len(records)
    delays = [d * 1000.0 for _, d in records if d is not None]
    lost = n - len(delays)
    return {
        "sent": n,
        "received": len(delays),
        "lost": lost,
        "loss_pct": 100.0 * lost / n if n else 0.0,
        "mean_delay_ms": mean(delays),
        "p95_delay_ms": percentile(delays, 95),
        "max_delay_ms": max(delays) if delays else float("nan"),
    }


# --------------------------------------------------------------------------
# per flow / per run
# --------------------------------------------------------------------------
def build_records(summary, seqs, sent, recv):
    """One (send_time, delay|None) per packet the client sent."""
    total_sent = summary.get("total_sent")
    if total_sent is None:                       # end marker never arrived
        total_sent = (max(seqs) + 1) if seqs else 0
    pairs = sorted(zip(seqs, sent, recv))
    anchor_seq = [p[0] for p in pairs]
    anchor_ts = [p[1] for p in pairs]
    end_ts = summary.get("end_ts")
    if end_ts is not None and (not anchor_seq or total_sent > anchor_seq[-1]):
        anchor_seq.append(total_sent)
        anchor_ts.append(end_ts)
    got = {s: (st, rt) for s, st, rt in pairs}

    def estimate(n):
        i = bisect.bisect_left(anchor_seq, n)
        if i == 0:
            return anchor_ts[0]
        if i >= len(anchor_seq):
            return anchor_ts[-1]
        s0, s1 = anchor_seq[i - 1], anchor_seq[i]
        t0, t1 = anchor_ts[i - 1], anchor_ts[i]
        return t0 + (t1 - t0) * (n - s0) / (s1 - s0)

    records = []
    if not anchor_seq:
        return records, total_sent
    for n in range(total_sent):
        if n in got:
            st, rt = got[n]
            records.append((st, rt - st))
        else:
            records.append((estimate(n), None))
    return records, total_sent


def analyse_flow(run_dir, flow, onset, burst_end, delay_limit_s):
    prefix = os.path.join(run_dir, flow["server"])
    seqs, sent, recv = read_packets(prefix + "_packets.csv")
    summary = read_json(prefix + "_summary.json")
    records, total_sent = build_records(summary, seqs, sent, recv)

    window = [r for r in records if onset <= r[0] < burst_end]
    bad = [t for t, d in records
           if t >= onset and (d is None or d > delay_limit_s)]
    last_bad = max(bad) if bad else None

    result = {"client": flow["client"], "server": flow["server"],
              "all": summarise(records), "window": summarise(window),
              "recovery_s": (last_bad - onset) if last_bad is not None else 0.0,
              "last_bad_time": last_bad}
    plot = {"recv": [r for r in recv], "delay_ms": [(r - s) * 1000.0
                                                    for s, r in zip(sent, recv)]}
    return result, records, plot


def controller_timings(events, onset):
    def first(name):
        for e in events:
            if e.get("event") == name and e.get("t", 0) >= onset - 0.5:
                return e
        return None

    def rel(e):
        return round(e["t"] - onset, 3) if e else None

    seen, detected = first("congestion_start"), first("congestion_detected")
    reroute, cleared = first("reroute"), first("congestion_cleared")
    return {
        "controller_seen_s": rel(seen),
        "controller_detection_s": rel(detected),
        "controller_reroute_s": rel(reroute),
        "controller_cleared_s": rel(cleared),
        "reroute_install_ms": (reroute["install_time_s"] * 1000.0
                               if reroute and "install_time_s" in reroute
                               else None),
        "reroute_detail": (
            "%s %s->%s" % (reroute.get("client"), reroute.get("from_path"),
                           reroute.get("to_path")) if reroute else None),
    }


def analyse_run(run_dir, delay_limit_ms):
    scenario = read_json(os.path.join(run_dir, "scenario.json"))
    onset, burst_end = scenario["onset_wall"], scenario["burst_end_wall"]
    limit_s = delay_limit_ms / 1000.0

    flows, all_records, win_records, plots = [], [], [], {}
    for flow in scenario["flows"]:
        res, records, plot = analyse_flow(run_dir, flow, onset, burst_end,
                                          limit_s)
        flows.append(res)
        all_records.extend(records)
        win_records.extend(r for r in records if onset <= r[0] < burst_end)
        plots[flow["client"] + "->" + flow["server"]] = plot

    overall, window = summarise(all_records), summarise(win_records)
    last_bad = [f["last_bad_time"] for f in flows
                if f["last_bad_time"] is not None]
    recovery = (max(last_bad) - onset) if last_bad else 0.0
    recovered_early = (not last_bad) or \
        max(last_bad) < burst_end - RECOVERY_MARGIN_S

    events = read_events(os.path.join(run_dir, "controller_events.jsonl"))
    stats = read_stats(os.path.join(run_dir, "controller_stats.csv"))

    metrics = {
        "mode": scenario["mode"],
        "sent_total": overall["sent"],
        "loss_pct": overall["loss_pct"],
        "win_loss_pct": window["loss_pct"],
        "win_mean_delay_ms": window["mean_delay_ms"],
        "win_p95_delay_ms": window["p95_delay_ms"],
        "win_max_delay_ms": window["max_delay_ms"],
        "recovery_s": recovery,
        "recovered_early": recovered_early,
        "congestion_window_s": burst_end - onset,
    }
    metrics.update(controller_timings(events, onset))
    return {
        "metrics": metrics,
        "flows": flows,
        "scenario": scenario,
        "_plot": {"stats": stats, "events": events, "flows": plots,
                  "t0": scenario["t0_wall"], "onset": onset,
                  "burst_end": burst_end,
                  "threshold": scenario.get("threshold", 0.85),
                  "size": scenario.get("size", 1200)},
    }


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
ROWS = [
    ("Packets sent (all flows)",                   "sent_total",             "{:.0f}"),
    ("Overall packet loss (%)",                    "loss_pct",               "{:.2f}"),
    ("Packet loss during congestion window (%)",   "win_loss_pct",           "{:.2f}"),
    ("Mean one-way delay in window (ms)",          "win_mean_delay_ms",      "{:.1f}"),
    ("95th pct one-way delay in window (ms)",      "win_p95_delay_ms",       "{:.1f}"),
    ("Max one-way delay in window (ms)",           "win_max_delay_ms",       "{:.1f}"),
    ("Controller: congestion detected after (s)",  "controller_detection_s", "{:.2f}"),
    ("Controller: reroute issued after (s)",       "controller_reroute_s",   "{:.2f}"),
    ("Controller: rule install time (ms)",         "reroute_install_ms",     "{:.1f}"),
    ("Controller: overload cleared after (s)",     "controller_cleared_s",   "{:.2f}"),
    ("RECOVERY TIME, data plane (s)",              "recovery_s",             "{:.2f}"),
    ("Recovered before congestion ended",          "recovered_early",        None),
    ("Length of congestion window (s)",            "congestion_window_s",    "{:.1f}"),
]


def fmt(value, pattern):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return pattern.format(value) if pattern else str(value)


def print_comparison(runs):
    modes = list(runs)
    width = max(len(r[0]) for r in ROWS) + 2
    head = "".ljust(width) + "".join(m.rjust(12) for m in modes)
    print("\n" + head)
    print("-" * len(head))
    for label, key, pattern in ROWS:
        line = label.ljust(width)
        for m in modes:
            line += fmt(runs[m]["metrics"].get(key), pattern).rjust(12)
        print(line)

    print("\nPer flow, during the congestion window (loss %, mean delay ms, "
          "recovery s):")
    for m in modes:
        for f in runs[m]["flows"]:
            w = f["window"]
            print("  %-9s %s->%s   loss %6.2f %%   delay %7.1f ms   "
                  "recovery %6.2f s" % (m, f["client"], f["server"],
                                        w["loss_pct"], w["mean_delay_ms"]
                                        if not math.isnan(w["mean_delay_ms"])
                                        else 0.0, f["recovery_s"]))

    if "static" in runs and "adaptive" in runs:
        s, a = runs["static"]["metrics"], runs["adaptive"]["metrics"]
        print("\nAdaptive vs static:")
        print("  loss in congestion window: %.2f %% -> %.2f %%"
              % (s["win_loss_pct"], a["win_loss_pct"]))
        print("  recovery time:             %.2f s -> %.2f s"
              % (s["recovery_s"], a["recovery_s"]))
        if not s["recovered_early"]:
            print("  (static never recovered by itself: it only improved when "
                  "the burst stopped after %.1f s)" % s["congestion_window_s"])


def write_summaries(runs, out_dir):
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump({m: {"metrics": r["metrics"], "flows": r["flows"]}
                   for m, r in runs.items()}, f, indent=2)
    with open(os.path.join(out_dir, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric"] + list(runs))
        for label, key, pattern in ROWS:
            w.writerow([label] + [fmt(runs[m]["metrics"].get(key), pattern)
                                  for m in runs])


def make_plots(runs, out_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: skipping comparison.png "
              "(pip install matplotlib)")
        return False

    modes = list(runs)
    fig, axes = plt.subplots(3, len(modes), figsize=(6.5 * len(modes), 9.5),
                             sharex="col", squeeze=False)
    colours = ["tab:blue", "tab:orange", "tab:green"]

    for col, mode in enumerate(modes):
        pl = runs[mode]["_plot"]
        t0, onset, end = pl["t0"], pl["onset"], pl["burst_end"]

        def decorate(ax, first):
            ax.axvspan(onset - t0, end - t0, color="red", alpha=0.08,
                       label="controlled congestion" if first else None)
            done = False
            for e in pl["events"]:
                if e.get("event") == "reroute":
                    ax.axvline(e["t"] - t0, color="k", ls="--", lw=1,
                               label="reroute" if first and not done else None)
                    done = True
            ax.grid(alpha=0.3)

        # row 0: path load seen by the controller
        ax = axes[0][col]
        rows = pl["stats"]
        if rows:
            ts = [r["t"] - t0 for r in rows]
            ax.plot(ts, [100 * r["util_A"] for r in rows], label="path A load")
            ax.plot(ts, [100 * r["util_B"] for r in rows], label="path B load")
        ax.axhline(100 * pl["threshold"], color="gray", ls=":",
                   label="threshold")
        ax.set_ylabel("offered load / capacity (%)")
        ax.set_title("%s routing" % mode)
        decorate(ax, True)
        ax.legend(fontsize=8, loc="upper left")

        # row 1: goodput per flow
        ax = axes[1][col]
        for colour, (name, data) in zip(colours, sorted(pl["flows"].items())):
            if not data["recv"]:
                continue
            bins = {}
            for r in data["recv"]:
                b = int((r - t0) / PLOT_BIN_S)
                bins[b] = bins.get(b, 0) + 1
            xs = sorted(bins)
            ax.plot([(b + 0.5) * PLOT_BIN_S for b in xs],
                    [bins[b] * pl["size"] * 8 / PLOT_BIN_S / 1e6 for b in xs],
                    color=colour, label=name)
        ax.set_ylabel("received goodput (Mbps)")
        decorate(ax, False)
        ax.legend(fontsize=8, loc="upper left")

        # row 2: one-way delay per packet
        ax = axes[2][col]
        for colour, (name, data) in zip(colours, sorted(pl["flows"].items())):
            ax.scatter([r - t0 for r in data["recv"]], data["delay_ms"],
                       s=1.5, alpha=0.4, color=colour, label=name)
        ax.set_ylabel("one-way delay (ms)")
        ax.set_xlabel("time since start (s)")
        decorate(ax, False)
        ax.legend(fontsize=8, loc="upper left", markerscale=6)

    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    print("plot written to %s" % out_path)
    return True


# --------------------------------------------------------------------------
def main(argv=None):
    p = argparse.ArgumentParser(description="Compare static vs adaptive runs")
    p.add_argument("--results", default=os.path.join(HERE, "results"))
    p.add_argument("--modes", nargs="+", default=None,
                   help="run folders to compare (default: static adaptive)")
    p.add_argument("--delay-limit-ms", type=float,
                   default=DEFAULT_DELAY_LIMIT_MS)
    p.add_argument("--no-plot", action="store_true")
    args = p.parse_args(argv)

    modes = args.modes or [m for m in ("static", "adaptive")
                           if os.path.exists(os.path.join(
                               args.results, m, "scenario.json"))]
    if not modes:
        sys.exit("no runs found in %s (run run_experiment.py first)"
                 % args.results)

    runs = {}
    for m in modes:
        run_dir = os.path.join(args.results, m)
        try:
            runs[m] = analyse_run(run_dir, args.delay_limit_ms)
        except (OSError, KeyError, ValueError) as exc:
            print("skipping %s: %s" % (m, exc), file=sys.stderr)
    if not runs:
        sys.exit("nothing to analyse")

    print_comparison(runs)
    write_summaries(runs, args.results)
    print("\nsummary written to %s/summary.{json,csv}" % args.results)
    if not args.no_plot:
        make_plots(runs, os.path.join(args.results, "comparison.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
