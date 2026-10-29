from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, set_ev_cls
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

# Which path each client uses (static baseline). The adaptive version
# will just change this dict and re-push the s1 / s4 rules.
CLIENT_PATH = {1: "A", 2: "B", 3: "A"}

PRIORITY = 200


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


class SimplePathController(app_manager.RyuApp):
    """Static, loop-free forwarding for the diamond topology (baseline)."""

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

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
        for match_kwargs, out_port in build_rules(dp.id):
            self.add_flow(dp, PRIORITY, parser.OFPMatch(**match_kwargs),
                          [parser.OFPActionOutput(out_port)])

        self.logger.info("s%d: installed %d flows", dp.id,
                         len(build_rules(dp.id)))