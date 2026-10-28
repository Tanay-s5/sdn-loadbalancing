#!/usr/bin/env python3
from functools import partial
from mininet.net import Mininet
from mininet.topo import Topo
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

class DiamondTopo(Topo):
    def build(self):
        s1, s2, s3, s4 = [self.addSwitch(f's{i}') for i in range(1, 5)]

        # clients h1-h3 on s1 (ports 1-3)
        for i in (1, 2, 3):
            h = self.addHost(f'h{i}', ip=f'10.0.0.{i}/24', mac=f'00:00:00:00:00:0{i}')
            self.addLink(h, s1, port2=i, bw=100)

        # servers h4-h6 on s4 (ports 1-3)
        for i in (4, 5, 6):
            h = self.addHost(f'h{i}', ip=f'10.0.0.{i}/24', mac=f'00:00:00:00:00:0{i}')
            self.addLink(h, s4, port2=i - 3, bw=100)

        # path A via s2, path B via s3 (equal capacity)
        self.addLink(s1, s2, port1=4, port2=1, bw=10, delay='5ms')
        self.addLink(s2, s4, port1=2, port2=4, bw=10, delay='5ms')
        self.addLink(s1, s3, port1=5, port2=1, bw=10, delay='5ms')
        self.addLink(s3, s4, port1=2, port2=5, bw=10, delay='5ms')

if __name__ == '__main__':
    setLogLevel('info')
    net = Mininet(topo=DiamondTopo(),
                  switch=partial(OVSSwitch, protocols='OpenFlow13'),
                  link=TCLink, controller=None, autoSetMacs=False)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    net.start()
    net.staticArp()
    CLI(net)
    net.stop()