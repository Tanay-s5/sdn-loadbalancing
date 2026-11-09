#!/usr/bin/env python3
"""Diamond topology for the static-vs-adaptive routing experiment.

        h1 h2 h3                    h4 h5 h6
          \\ | /                      \\ | /
            s1 --- s2 (path A) --- s4
              \\--- s3 (path B) ---/

Run by hand (interactive CLI):
    ryu-manager --ofp-tcp-listen-port 6653 controller.py     # terminal 1
    sudo python3 topo.py                                      # terminal 2

Run the full experiment (starts Ryu + Mininet itself):
    sudo python3 run_experiment.py
"""
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.topo import Topo

CONTROLLER_IP = "127.0.0.1"
CONTROLLER_PORT = 6653

# Inter-switch link parameters. PATH_CAPACITY_MBPS in controller.py must match
# the bandwidths below (it is what the controller compares load against).
PATH_A_BW_MBPS = 5
PATH_A_DELAY = "5ms"      # per link -> 10 ms one-way
PATH_B_BW_MBPS = 10
PATH_B_DELAY = "2ms"      # per link ->  4 ms one-way

# Queue length (packets) on the inter-switch links. A short queue makes
# congestion show up as loss + bounded extra delay (100 pkts ~ 200 ms on
# path A) instead of seconds of bufferbloat, which keeps runs repeatable.
QUEUE_PACKETS = 100


class DiamondTopo(Topo):
    def build(self):
        # Set DPIDs explicitly because controller.py identifies switches by DPID.
        switches = {
            i: self.addSwitch(f"s{i}", dpid=f"{i:016x}")
            for i in range(1, 5)
        }
        s1, s2, s3, s4 = (switches[i] for i in range(1, 5))

        # Client hosts h1-h3 connect to s1 ports 1-3.
        for i in (1, 2, 3):
            host = self.addHost(
                f"h{i}",
                ip=f"10.0.0.{i}/24",
                mac=f"00:00:00:00:00:0{i}",
            )
            self.addLink(host, s1, port2=i, bw=100)

        # Server hosts h4-h6 connect to s4 ports 1-3.
        for i in (4, 5, 6):
            host = self.addHost(
                f"h{i}",
                ip=f"10.0.0.{i}/24",
                mac=f"00:00:00:00:00:0{i}",
            )
            self.addLink(host, s4, port2=i - 3, bw=100)

        # Asymmetric paths (port numbers match controller.py):
        #   Path A (via s2): slow  -> 5 Mbps,  5 ms per link (10 ms one-way)
        #   Path B (via s3): fast  -> 10 Mbps, 2 ms per link (4 ms one-way)
        # Path A: s1:4 <-> s2:1 <-> s2:2 <-> s4:4.
        self.addLink(s1, s2, port1=4, port2=1, bw=PATH_A_BW_MBPS,
                     delay=PATH_A_DELAY, max_queue_size=QUEUE_PACKETS)
        self.addLink(s2, s4, port1=2, port2=4, bw=PATH_A_BW_MBPS,
                     delay=PATH_A_DELAY, max_queue_size=QUEUE_PACKETS)

        # Path B: s1:5 <-> s3:1 <-> s3:2 <-> s4:5.
        self.addLink(s1, s3, port1=5, port2=1, bw=PATH_B_BW_MBPS,
                     delay=PATH_B_DELAY, max_queue_size=QUEUE_PACKETS)
        self.addLink(s3, s4, port1=2, port2=5, bw=PATH_B_BW_MBPS,
                     delay=PATH_B_DELAY, max_queue_size=QUEUE_PACKETS)


def make_net(controller_ip=CONTROLLER_IP, controller_port=CONTROLLER_PORT):
    """Build (but do not start) the Mininet network with a remote controller."""
    net = Mininet(
        topo=DiamondTopo(),
        switch=partial(OVSSwitch, protocols="OpenFlow13"),
        link=TCLink,
        controller=None,
        autoSetMacs=False,
    )
    # Start Ryu with: ryu-manager --ofp-tcp-listen-port 6653 controller.py
    net.addController(
        "c0", controller=RemoteController, ip=controller_ip, port=controller_port
    )
    return net


if __name__ == "__main__":
    setLogLevel("info")
    net = make_net()
    net.start()
    net.staticArp()
    CLI(net)
    net.stop()
