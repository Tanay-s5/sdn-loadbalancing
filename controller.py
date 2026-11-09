"""Ryu controller for the diamond topology: static baseline + adaptive rerouting.

Same file, two behaviours, chosen with the ROUTING_MODE environment variable:

  ROUTING_MODE=static    fixed client->path assignment (baseline). The
                         controller still *monitors* and logs congestion so
                         the baseline can be plotted, but never reroutes.
  ROUTING_MODE=adaptive  (default) closed loop:
                           1. poll flow + port statistics from s1
                           2. turn byte counters into per-client rates (Mbps)
                           3. compare per-path load with the path capacity
                           4. if a path stays above the congestion threshold
                              for CONFIRM_SAMPLES polls, move one client to
                              the other path (rules re-pushed on s1 and s4)
                           5. hold off for COOLDOWN seconds (no flapping)

Start it with:
    ROUTING_MODE=adaptive ryu-manager --ofp-tcp-listen-port 6653 controller.py

Tunables (environment variables, all optional):
    POLL_INTERVAL        seconds between stats polls            (0.5)
    CONGESTION_THRESHOLD path load / capacity that counts as    (0.85)
                         "overloaded"
    CONFIRM_SAMPLES      consecutive polls before acting         (2)
    REROUTE_COOLDOWN     seconds to wait after a reroute         (5.0)
    RESULTS_DIR          where the logs below are written        (results)

Logs written to RESULTS_DIR:
    controller_stats.csv     one row per poll: offered load, utilisation,
                             carried load and per-client rate / path
    controller_events.jsonl  congestion_start / congestion_detected /
                             reroute / congestion_cleared / no_reroute_possible
"""
import csv
import json
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (CONFIG_DISPATCHER, DEAD_DISPATCHER,
                                    MAIN_DISPATCHER, set_ev_cls)
from ryu.lib import hub
from ryu.ofproto import ofproto_v1_3


# Must match the MACs / ports in topo.py.
HOSTS = {
    1: {"mac": "00:00:00:00:00:01", "switch": 1, "port": 1},
    2: {"mac": "00:00:00:00:00:02", "switch": 1, "port": 2},
    3: {"mac": "00:00:00:00:00:03", "switch": 1, "port": 3},
    4: {"mac": "00:00:00:00:00:04", "switch": 4, "port": 1},
    5: {"mac": "00:00:00:00:00:05", "switch": 4, "port": 2},
    6: {"mac": "00:00:00:00:00:06", "switch": 4, "port": 3},
}
CLIENTS = (1, 2, 3)   # on s1
SERVERS = (4, 5, 6)   # on s4

# Uplink ports. Path A = s1 <-> s2 <-> s4, Path B = s1 <-> s3 <-> s4.
# Same numbering on s1 and s4 (see topo.py).
UPLINK = {"A": 4, "B": 5}

# Link capacity of each path in Mbps. Must match the bw= values in topo.py.
PATH_CAPACITY_MBPS = {"A": 5.0, "B": 10.0}

# Which path each client starts on (static baseline). The adaptive mode
# changes self.client_path at run time and re-pushes the s1 / s4 rules.
CLIENT_PATH = {1: "A", 2: "B", 3: "A"}

PRIORITY = 200

# s1 sees every client->server packet first, so its counters give the
# per-client offered load and the uplink port counters give per-path load.
MONITORED_DPID = 1

# Flows slower than this are never worth moving (Mbps).
MIN_FLOW_MBPS = 0.05

SERVER_MACS = {HOSTS[s]["mac"] for s in SERVERS}
CLIENT_BY_PORT = {HOSTS[c]["port"]: c for c in CLIENTS}     # s1 port -> client
UPLINK_PORT_TO_PATH = {port: path for path, port in UPLINK.items()}


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


# --------------------------------------------------------------------------
# Pure helpers (no Ryu objects) so they can be unit-tested offline.
# --------------------------------------------------------------------------
def build_rules(dpid, client_path=CLIENT_PATH):
    """Return [(match_kwargs, out_port), ...] for one switch.

    Pure function (no Ryu objects) so the rules can be tested offline.
    """
    rules = []
    mac = lambda h: HOSTS[h]["mac"]

    if dpid == 1:
        # deliver to local clients (any in_port)
        for c in CLIENTS:
            rules.append(({"eth_dst": mac(c)}, HOSTS[c]["port"]))
        # client -> server goes up the client's assigned path
        for c in CLIENTS:
            for s in SERVERS:
                rules.append((
                    {"in_port": HOSTS[c]["port"], "eth_dst": mac(s)},
                    UPLINK[client_path[c]],
                ))

    elif dpid == 4:
        # deliver to local servers (any in_port)
        for s in SERVERS:
            rules.append(({"eth_dst": mac(s)}, HOSTS[s]["port"]))
        # server -> client goes back on the same path the client uses
        for s in SERVERS:
            for c in CLIENTS:
                rules.append((
                    {"in_port": HOSTS[s]["port"], "eth_dst": mac(c)},
                    UPLINK[client_path[c]],
                ))

    elif dpid in (2, 3):
        # Middle switches: port 1 faces s1, port 2 faces s4.
        # Path-agnostic, so rerouting only needs changes on s1 / s4.
        for s in SERVERS:
            rules.append(({"in_port": 1, "eth_dst": mac(s)}, 2))
        for c in CLIENTS:
            rules.append(({"in_port": 2, "eth_dst": mac(c)}, 1))

    return rules


def rules_as_dict(rules):
    """{hashable match: out_port} so two rule sets can be diffed."""
    return {tuple(sorted(match.items())): out for match, out in rules}


def path_loads(rates, client_path):
    """Sum per-client rates (Mbps) onto the path each client uses."""
    loads = {p: 0.0 for p in UPLINK}
    for client, path in client_path.items():
        loads[path] += rates.get(client, 0.0)
    return loads


def choose_reroute(rates, client_path, capacity=PATH_CAPACITY_MBPS,
                   threshold=0.85, min_rate=MIN_FLOW_MBPS):
    """Pick one client to move off the most overloaded path.

    Returns {"client", "from", "to", "rate", "projected_util"} or None.

    For every client on the overloaded path we simulate moving it to the other
    path and keep only moves that do not push the destination above the
    threshold. Among those we take the one that leaves the lowest worst-case
    utilisation (ties: move the smaller flow, it disturbs less).
    """
    loads = path_loads(rates, client_path)
    util = {p: loads[p] / capacity[p] for p in capacity}
    overloaded = [p for p in capacity if util[p] > threshold]
    if not overloaded:
        return None

    src = max(overloaded, key=lambda p: util[p])
    best = None
    for client, path in sorted(client_path.items()):
        rate = rates.get(client, 0.0)
        if path != src or rate < min_rate:
            continue
        for dst in capacity:
            if dst == src:
                continue
            new_loads = dict(loads)
            new_loads[src] -= rate
            new_loads[dst] += rate
            new_util = {p: new_loads[p] / capacity[p] for p in capacity}
            if new_util[dst] > threshold:
                continue                      # would just move the problem
            key = (max(new_util.values()), rate)
            if best is None or key < best[0]:
                best = (key, {"client": client, "from": src, "to": dst,
                              "rate": rate, "projected_util": new_util})
    return best[1] if best else None


def _match_field(match, name):
    try:
        return match[name]
    except KeyError:
        return None


# --------------------------------------------------------------------------
# The controller
# --------------------------------------------------------------------------
class AdaptivePathController(app_manager.RyuApp):
    """Static baseline or adaptive rerouting for the diamond topology."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.mode = os.environ.get("ROUTING_MODE", "adaptive").strip().lower()
        if self.mode not in ("static", "adaptive"):
            raise ValueError("ROUTING_MODE must be 'static' or 'adaptive', "
                             "got %r" % self.mode)
        self.poll_interval = _env_float("POLL_INTERVAL", 0.5)
        self.threshold = _env_float("CONGESTION_THRESHOLD", 0.85)
        self.confirm_samples = int(_env_float("CONFIRM_SAMPLES", 2))
        self.cooldown = _env_float("REROUTE_COOLDOWN", 5.0)
        self.results_dir = os.environ.get("RESULTS_DIR", "results")

        self.client_path = dict(CLIENT_PATH)
        self.datapaths = {}

        # monitoring state
        self.prev_flow_bytes = {}                       # client -> (bytes, t)
        self.prev_port_bytes = {}                       # path   -> (bytes, t)
        self.client_rate = {c: 0.0 for c in CLIENTS}    # Mbps offered by client
        self.tx_rate = {p: 0.0 for p in UPLINK}         # Mbps carried per path

        # detection / reroute state
        self.over_count = {p: 0 for p in UPLINK}
        self.under_count = {p: 0 for p in UPLINK}
        self.episodes = {}                              # path -> episode dict
        self.cooldown_until = 0.0

        self._open_logs()
        self.logger.info("mode=%s poll=%.2fs threshold=%.0f%% confirm=%d "
                         "cooldown=%.1fs results=%s", self.mode,
                         self.poll_interval, self.threshold * 100,
                         self.confirm_samples, self.cooldown, self.results_dir)
        self.monitor_thread = hub.spawn(self._monitor_loop)

    # ---- logging ---------------------------------------------------------
    def _open_logs(self):
        os.makedirs(self.results_dir, exist_ok=True)
        self.event_file = open(
            os.path.join(self.results_dir, "controller_events.jsonl"), "w",
            buffering=1)
        self.stats_file = open(
            os.path.join(self.results_dir, "controller_stats.csv"), "w",
            newline="")
        self.stats_writer = csv.writer(self.stats_file)
        self.stats_writer.writerow(
            ["t", "offered_A_mbps", "offered_B_mbps", "util_A", "util_B",
             "tx_A_mbps", "tx_B_mbps"]
            + ["rate_h%d" % c for c in CLIENTS]
            + ["path_h%d" % c for c in CLIENTS])
        self.stats_file.flush()

    def _log_event(self, event, **fields):
        record = {"t": round(time.time(), 4), "event": event,
                  "mode": self.mode}
        record.update(fields)
        self.event_file.write(json.dumps(record) + "\n")
        self.logger.info("EVENT %s", json.dumps(record))

    def _write_stats_row(self, now, loads, util):
        self.stats_writer.writerow(
            ["%.3f" % now,
             "%.4f" % loads["A"], "%.4f" % loads["B"],
             "%.4f" % util["A"], "%.4f" % util["B"],
             "%.4f" % self.tx_rate["A"], "%.4f" % self.tx_rate["B"]]
            + ["%.4f" % self.client_rate[c] for c in CLIENTS]
            + [self.client_path[c] for c in CLIENTS])
        self.stats_file.flush()

    # ---- flow table ------------------------------------------------------
    def add_flow(self, datapath, priority, match, actions):
        parser = datapath.ofproto_parser
        ofp = datapath.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        datapath.send_msg(parser.OFPFlowMod(
            datapath=datapath, priority=priority, match=match,
            instructions=inst))

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features(self, ev):
        dp = ev.msg.datapath
        parser = dp.ofproto_parser
        ofp = dp.ofproto

        # wipe old rules so a controller restart starts clean
        dp.send_msg(parser.OFPFlowMod(
            datapath=dp, table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
            out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY))

        # No table-miss rule: unknown traffic is dropped (never flooded,
        # so the diamond can't create a loop).
        rules = build_rules(dp.id, self.client_path)
        for match_kwargs, out_port in rules:
            self.add_flow(dp, PRIORITY, parser.OFPMatch(**match_kwargs),
                          [parser.OFPActionOutput(out_port)])

        self.logger.info("s%d: installed %d flows", dp.id, len(rules))

    @set_ev_cls(ofp_event.EventOFPStateChange,
                [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.datapaths.pop(dp.id, None)

    # ---- monitoring ------------------------------------------------------
    def _monitor_loop(self):
        while True:
            dp = self.datapaths.get(MONITORED_DPID)
            if dp is not None:
                self._request_stats(dp)
            hub.sleep(self.poll_interval)

    def _request_stats(self, dp):
        parser = dp.ofproto_parser
        ofp = dp.ofproto
        dp.send_msg(parser.OFPFlowStatsRequest(
            dp, flags=0, table_id=ofp.OFPTT_ALL, out_port=ofp.OFPP_ANY,
            out_group=ofp.OFPG_ANY, cookie=0, cookie_mask=0,
            match=parser.OFPMatch()))
        dp.send_msg(parser.OFPPortStatsRequest(
            dp, flags=0, port_no=ofp.OFPP_ANY))

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        """Carried load on the two uplinks (what actually left s1)."""
        if ev.msg.datapath.id != MONITORED_DPID:
            return
        now = time.time()
        for stat in ev.msg.body:
            path = UPLINK_PORT_TO_PATH.get(stat.port_no)
            if path is None:
                continue
            prev = self.prev_port_bytes.get(path)
            if prev and stat.tx_bytes >= prev[0] and now > prev[1]:
                self.tx_rate[path] = ((stat.tx_bytes - prev[0]) * 8
                                      / (now - prev[1]) / 1e6)
            self.prev_port_bytes[path] = (stat.tx_bytes, now)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply(self, ev):
        """Offered load per client, from the client->server flow entries on s1.

        (s1 has 12 flows, so the reply always fits in one multipart message.)
        """
        if ev.msg.datapath.id != MONITORED_DPID:
            return
        now = time.time()
        totals = {c: 0 for c in CLIENTS}
        for stat in ev.msg.body:
            if stat.priority != PRIORITY:
                continue
            client = CLIENT_BY_PORT.get(_match_field(stat.match, "in_port"))
            eth_dst = _match_field(stat.match, "eth_dst")
            if client is None or eth_dst is None:
                continue
            if eth_dst.lower() in SERVER_MACS:
                totals[client] += stat.byte_count
        self._update_rates(totals, now)
        self._evaluate(now)

    def _update_rates(self, totals, now):
        for client, total in totals.items():
            prev = self.prev_flow_bytes.get(client)
            if prev is not None:
                prev_bytes, prev_time = prev
                # total < prev_bytes means the entries were re-installed and
                # the counters restarted: keep the last rate for one sample.
                if total >= prev_bytes and now > prev_time:
                    self.client_rate[client] = ((total - prev_bytes) * 8
                                                / (now - prev_time) / 1e6)
            self.prev_flow_bytes[client] = (total, now)

    # ---- congestion detection -------------------------------------------
    def _evaluate(self, now):
        loads = path_loads(self.client_rate, self.client_path)
        util = {p: loads[p] / PATH_CAPACITY_MBPS[p] for p in UPLINK}
        self._write_stats_row(now, loads, util)

        for path in UPLINK:
            self._track_episode(path, util[path], loads[path], now)

        if self.mode == "adaptive":
            self._maybe_reroute(now)

    def _track_episode(self, path, util, load, now):
        """Book-keeping for 'path is overloaded' episodes (all modes)."""
        ep = self.episodes.get(path)
        if util > self.threshold:
            self.over_count[path] += 1
            self.under_count[path] = 0
            if ep is None:
                ep = self.episodes[path] = {
                    "start": now, "detected": None, "rerouted": None,
                    "warned": False}
                self._log_event("congestion_start", path=path,
                                util=round(util, 3), load_mbps=round(load, 3))
            if ep["detected"] is None and \
                    self.over_count[path] >= self.confirm_samples:
                ep["detected"] = now
                self._log_event("congestion_detected", path=path,
                                util=round(util, 3), load_mbps=round(load, 3),
                                detection_delay_s=round(now - ep["start"], 3))
        else:
            self.over_count[path] = 0
            if ep is not None:
                self.under_count[path] += 1
                if self.under_count[path] >= self.confirm_samples:
                    self._log_event(
                        "congestion_cleared", path=path, util=round(util, 3),
                        duration_s=round(now - ep["start"], 3),
                        rerouted=ep["rerouted"] is not None)
                    del self.episodes[path]
                    self.under_count[path] = 0

    # ---- adaptive rerouting ---------------------------------------------
    def _maybe_reroute(self, now):
        if now < self.cooldown_until:
            return
        if not any(self.over_count[p] >= self.confirm_samples for p in UPLINK):
            return

        move = choose_reroute(self.client_rate, self.client_path,
                              PATH_CAPACITY_MBPS, self.threshold,
                              MIN_FLOW_MBPS)
        if move is None:
            for path, ep in self.episodes.items():
                if not ep["warned"]:
                    ep["warned"] = True
                    self._log_event("no_reroute_possible", path=path)
            return

        decided = time.time()
        self._reroute(move["client"], move["to"])
        installed = time.time()

        self.cooldown_until = now + self.cooldown
        self.over_count = {p: 0 for p in UPLINK}
        src_ep = self.episodes.get(move["from"])
        if src_ep is not None:
            src_ep["rerouted"] = installed
        self._log_event(
            "reroute", client="h%d" % move["client"],
            from_path=move["from"], to_path=move["to"],
            rate_mbps=round(move["rate"], 3),
            projected_util={p: round(u, 3)
                            for p, u in move["projected_util"].items()},
            install_time_s=round(installed - decided, 4))

    def _reroute(self, client, new_path):
        """Move one client to new_path: re-push only the rules that change.

        s4 (return direction) first, then s1 (forward direction). The middle
        switches are path-agnostic, so they never need touching.
        """
        old_map = dict(self.client_path)
        new_map = dict(old_map)
        new_map[client] = new_path

        for dpid in (4, 1):
            dp = self.datapaths.get(dpid)
            if dp is None:
                self.logger.warning("s%d not connected, cannot reroute", dpid)
                continue
            parser = dp.ofproto_parser
            old = rules_as_dict(build_rules(dpid, old_map))
            new = rules_as_dict(build_rules(dpid, new_map))
            for key, out_port in new.items():
                if old.get(key) != out_port:
                    self.add_flow(dp, PRIORITY,
                                  parser.OFPMatch(**dict(key)),
                                  [parser.OFPActionOutput(out_port)])
        self.client_path = new_map
