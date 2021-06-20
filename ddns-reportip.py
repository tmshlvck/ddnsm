#!/usr/bin/env python3
#
# DDNS
# (C) 2015-2019 Tomas Hlavacek (tmshlvck@gmail.com)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import sys
import getopt
import os
import syslog
import subprocess
import re
import ipaddress
import time
import yaml

config_file = '/etc/ddns/ddns.yaml'
config = {'debug': False,}

def d(message):
    if config['debug']:
        print(message)

def log(message):
    if config['debug']:
        print(message)
        syslog.syslog(message)
    else:
        syslog.syslog(message)


def read_config(cfgfile):
    global config
    config.update(yaml.safe_load(open(cfgfile, 'r')))


def run_ipaddr(dev=None):
    devregexp=re.compile(r'^\s*[0-9]+:\s+([0-9a-zA-Z]+)(:|@.+)\s+<([^>]+)>')
    ipv4regexp=re.compile(r'^\s+inet\s+(([0-9]{1,3}\.){3}[0-9]{1,3})(/[0-9]{1,2})?\s+(.+)$')
    ipv6regexp=re.compile(r'^\s+inet6\s+(([0-9a-fA-F]{0,4}:){0,7}[0-9a-fA-F]{0,4})/[0-9]{1,3}\s+(.+)$')

    if dev:
        c = [config['bin_ip'], 'address', 'show', 'dev', dev]
    else:
        c = [config['bin_ip'], 'address', 'show']

    p=subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (stdoutdata, stderrdata)=p.communicate()

    if stdoutdata:
        dev = None
        for l in stdoutdata.decode().split('\n'):
            m = devregexp.match(l)
            if m:
                dev = m.group(1)
                flags = m.group(3).split(',')

                if not ('UP' in flags and 'LOWER_UP' in flags):
                    dev = None

            m = ipv4regexp.match(l)
            if m and dev:
                yield(m.group(1), 4, dev, m.group(4).strip().split(' '))

            m = ipv6regexp.match(l)
            if m and dev:
                yield(m.group(1), 6, dev, m.group(3).strip().split(' '))



def get_dev_ipaddr(ifaces=None):
    """
        in ifaces = ["eth0", "tap1", "br0"]
        return [(str address, int ipversion, str dev, [str flag,...])]
    """

    def filter_dev(fltr, dev):
        if dev == 'lo':
            return False

        if dev in fltr:
            return True
        if ('-%s' % dev) in fltr:
            return False
        if '*' in fltr:
            return True

        return False


    if ifaces:
        if type(ifaces) is str:
            dev_list = [ifaces]
        else:
            dev_list = ifaces
    else:
        dev_list = ['*']

    for addr, ipv, dev, flags in run_ipaddr():
        d(f"considering IP address {addr} (IPv{ipv}) from interface {dev} with flags {str(flags)}")
        if filter_dev(dev_list, dev):
            d("    -> allowed by interface filter")
            yield (addr, ipv, dev, flags)
        else:
            d("    -> denied by interface filter")


def measure_ipv4(ipv4addr, flags):
    try:
        ipo = ipaddress.IPv4Address(ipv4addr)
    except:
        return 0

    d("IPv4 address: %s" % str(ipo))

    if ipo.is_multicast:
        d("  -> multicast")
        return 0
    if ipo.is_private:
        d("  -> private")
        return 0
    if ipo.is_unspecified:
        d("  -> unspecified")
        return 0
    if ipo.is_reserved:
        d("  -> reserved")
        return 0
    if ipo.is_loopback:
        d("  -> loopback")
        return 0
    if ipo.is_link_local:
        d("  -> link_local")
        return 0

    d("  -> global_unicast")
    return 1


def measure_ipv6(ipv6addr, flags):
    def is_eui64(ipa):
        if ipa.packed[11] == 0xff and ipa.packed[12] == 0xfe:
            return True
        else:
            return False

    try:
        ipo = ipaddress.IPv6Address(ipv6addr)
    except:
        return 0

    d("IPv6 address: %s" % str(ipo))
    if ipo.is_multicast:
        d("  -> multicast")
        return 0
    if ipo.is_private:
        d("  -> private")
        return 0
    if ipo.is_unspecified:
        d("  -> unspecified")
        return 0
    if ipo.is_reserved:
        d("  -> reserved")
        return 0
    if ipo.is_loopback:
        d("  -> loopback")
        return 0
    if ipo.is_link_local:
        d("  -> link_local")
        return 0
    if ipo.is_site_local:
        d("  -> site_local")
        return 0
    if is_eui64(ipo):
        d("  -> EUI64")
        return 3
    if 'mngtmpaddr' in flags:
        d("  -> stable privacy")
        return 2

    d("  -> global_unicast")
    return 1


def get_host_ipaddr(devs, enable_ipv4, enable_ipv6):
    """
    devs - None or list of str or str; str = name of interface(s)
    enable_ipv4 - boot = include IPv4 addresses in the result
    enable_ipv6 - bool = include IPv6 addresses in the result
    return ([str IPv4],[str IPv6]), str = IP addresses
    """

    ipv4 = None
    ipv4metric = 0
    ipv6 = None
    ipv6metric = 0

    for addr,proto,dev,flags in get_dev_ipaddr(devs):
        if proto == 4:
          if enable_ipv4:
            m = measure_ipv4(addr, flags)
            if m > 0 and m > ipv4metric:
              ipv4 = addr
              ipv4metric = m
        elif proto == 6:
          if enable_ipv6:
            m = measure_ipv6(addr, flags)
            if m > 0 and m > ipv6metric:
              ipv6 = addr
              ipv6metric = m
        else:
            raise Exception("Wrong protocol: %d" % proto)

    return (ipv4, ipv6)


def normalize_dns(name):
    return (name if name[-1] == '.' else "%s." % name)

def denormalize_dns(name):
    return (name[:-1] if name[-1] == '.' else name)


def query_dns():
    ipv4regexp = re.compile('([0-9]{1,3}\.){3}[0-9]{1,3}')
    ipv6regexp = re.compile('([0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4}')
    def q(name,qtype):
        regexpc=None
        addrregexp=None
        if qtype == 'A':
            regexp = '^%s\s+[0-9]+\s+IN\s+A\s+([0-9\.]+)$' % name.replace('.', '\.')
            regexpc = re.compile(regexp)
            addrregexp = ipv4regexp
        elif qtype == 'AAAA':
            regexp = '^%s\s+[0-9]+\s+IN\s+AAAA\s+([0-9a-fA-F:]+)$' % name.replace('.', '\.')
            regexpc = re.compile(regexp)
            addrregexp = ipv6regexp
        else:
            raise Exception("Unknown qtype: %s" % str(qtype))
                
        c=[config['bin_dig'], '@%s' % config['dns_server'], name, qtype]
        dig=subprocess.Popen(c,stdout=subprocess.PIPE)
        d("Running command: %s" % str(c))
        r=dig.communicate()
        d("Command finished. Returncode: %d. Output: %s" % (dig.returncode, str(r)))

        if r and r[0]:
            for l in r[0].decode().split('\n'):
                m = regexpc.match(l)
                if m and addrregexp.match(m.group(1)):
                    return m.group(1)
 
    dnsname = normalize_dns("%s.%s" % (config['hostname'], config['dns_zone']))
    return(q(dnsname,'A'), q(dnsname,'AAAA'))


def update_dns(ipv4=None, ipv6=None):
    dnsname = normalize_dns("%s.%s" % (config['hostname'], config['dns_zone']))
    commands = """server %s
zone %s
update del %s
""" % (config['dns_server'], denormalize_dns(config['dns_zone']), dnsname)

    if ipv6:
        commands += "update add %s %d AAAA %s\n" % (dnsname, config['rr_ttl'], ipv6)
    if ipv4:
        commands += "update add %s %d A %s\n" % (dnsname, config['rr_ttl'], ipv4)

    commands += "send\n"

    d("Running command %s -y <hidden>" % config['bin_nsupdate'])
    nsu=subprocess.Popen([config['bin_nsupdate'], "-y", config['nsupdate_key']],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    d("Feeding data: \n%s---------------" % commands)
    r=nsu.communicate(commands.encode())
    d("Update finished. Return code: %d. Output: %s" % (nsu.returncode, str(r)))


def main():
    global config, config_file

    def usage():
                print("""reportip.py by Tomas Hlavacek (tmshlvck@gmail.com)
  -d --debug : sets debugging output
  -h --help : prints this help message
  -q --query : just run DNS query and print status, do not update
  -f --force : force update
""")

    try:
        opts, args = getopt.getopt(sys.argv[1:], "hc:dqf", ["help", "config=", "debug", "query", "force"])
    except getopt.GetoptError as err:
        print(str(err))
        usage()
        sys.exit(2)

    force_update = False
    query = False
    debug_cmdln = False
    for o, a in opts:
        if o == '-d':
            debug_cmdln = True
        elif o == '-h':
            usage()
            sys.exit(0)
        elif o == '-q':
            query = True
        elif o == '-f':
            force_update = True

        elif o == '-c':
            config_file = a

        else:
            assert False, "Unhandled option"

    read_config(config_file)
    if debug_cmdln:
        config['debug'] = True

    # query only and stop
    if query:
        print("Hostname: %s, zone: %s, DNS server: %s" % (config['hostname'], config['dns_zone'], config['dns_server']))
        (ipv4,ipv6) = get_host_ipaddr(config['interfaces'], config['enable_ipv4'], config['enable_ipv6'])
        print("Local addresses: IPv4 %s, IPv6 %s" % (str(ipv4), str(ipv6)))
        (dns_ipv4,dns_ipv6) = query_dns()
        print("Addresses in DNS: IPv4 %s, IPv6 %s" % (str(dns_ipv4), str(dns_ipv6)))
        return

    # sleep before proceeding
    if config['sleep']:
        d("Sleeping for %d s." % config['sleep'])
        time.sleep(config['sleep'])
        d("Waking up...")

    # select addresses
    (ipv4,ipv6) = get_host_ipaddr(config['interfaces'], config['enable_ipv4'], config['enable_ipv6'])
    d("Local addresses: IPv4 %s, IPv6 %s" % (str(ipv4), str(ipv6)))
    if ipv4 == None and ipv6 == None:
        d("No connectivity, nothing to report. Noop. Finish.")
        return

    # query DNS
    (dns_ipv4,dns_ipv6) = query_dns()
    d("Addresses in DNS: IPv4 %s, IPv6 %s" % (str(dns_ipv4), str(dns_ipv6)))

    # if DNS differs from local address send update
    if ipv4 != dns_ipv4 or ipv6 != dns_ipv6 or force_update:
        d("Difference in local address and DNS. Sending update.")
        update_dns(ipv4, ipv6)
    else:
        d("No difference in local address and DNS. Noop.")

    d("Finish.")

         

if __name__ == '__main__':
    main()

