"""Provision PipOS LAN access and run its two separate SSH listeners."""

import argparse
import base64
import binascii
import ipaddress
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .network_policy import INTERNET, PAIRED_LAN, unique_object
from .storage import atomic_write, sync_directory
from .wifi import INTERFACE, networkd_config, owner_change


MIND = Path('/mind')
OBSERVE_PORT = 2222
DEV_PORT = 2223
SSH = '/usr/sbin/sshd'
NETWORK = '/usr/sbin/being-network'
DEV_SERVICE = 'being-lab-dev.service'
LAN_NETWORKS = tuple(map(ipaddress.IPv4Network, ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')))
ED25519_HEADER = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'


def public_key(line):
    words = line.split()
    if len(words) < 2 or words[0] != 'ssh-ed25519' or len(line.splitlines()) != 1:
        raise ValueError('Expected one OpenSSH Ed25519 public key.')
    try:
        key = base64.b64decode(words[1], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('Invalid Ed25519 public key.') from None
    if not key.startswith(ED25519_HEADER) or len(key) != len(ED25519_HEADER) + 32:
        raise ValueError('Invalid Ed25519 public key.')
    return f'ssh-ed25519 {base64.b64encode(key).decode("ascii")}'


def lan_address(value):
    if not isinstance(value, str):
        raise ValueError('Use a reserved private IPv4 address for each device.')
    address = ipaddress.IPv4Address(value)
    if not any(address in network for network in LAN_NETWORKS):
        raise ValueError('Use a reserved private IPv4 address for each device.')
    return str(address)


def pair_addresses(address, controller, ethernet):
    address, controller = lan_address(address), lan_address(controller)
    if address == controller:
        raise ValueError('Use different IPv4 addresses for the laptop and controller.')
    if ethernet is not None and (not isinstance(ethernet, str) or not INTERFACE.fullmatch(ethernet)):
        raise ValueError('Invalid Ethernet interface name.')
    return dict(address=address, controller=controller, ethernet=ethernet)


def validated_enrollment(state):
    pair = pair_addresses(state['address'], state['controller'], state.get('ethernet'))
    observe, dev = state['observe'], state.get('dev')
    if not isinstance(observe, str) or (dev is not None and not isinstance(dev, str)):
        raise ValueError('Unreadable pairing keys.')
    observe = public_key(observe)
    dev = public_key(dev) if dev is not None else None
    if observe == dev:
        raise ValueError('Use different observation and development keys.')
    return dict(pair, dev_enabled=dev is not None), observe, dev


def load_pair(mind_dir):
    """Read the authorized endpoints for the firewall without exposing any key material."""
    directory = Path(mind_dir) / 'lab-access'
    try:
        folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(folder)
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                return None
            fd = os.open('access.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    return None
                contents = source.read(4097)
        finally:
            os.close(folder)
        if len(contents) > 4096:
            return None
        pair, _, _ = validated_enrollment(json.loads(contents, object_pairs_hook=unique_object))
        return pair
    except (KeyError, OSError, TypeError, ValueError, RecursionError):
        return None


def enroll(mind, address, observe, dev, controller, ethernet=None):
    pair = pair_addresses(address, controller, ethernet)
    observe = public_key(observe.read_text(encoding='utf-8'))
    dev = public_key(dev.read_text(encoding='utf-8')) if dev else None
    if observe == dev:
        raise ValueError('Use different observation and development keys.')
    state = mind / 'lab-access'
    access = state / 'access.json'
    key_path = state / 'ssh_host_ed25519_key'
    with owner_change(access):
        if not key_path.exists():
            # Generate in the private directory and publish only the complete key.
            with tempfile.TemporaryDirectory(dir=state) as temporary:
                key = Path(temporary) / 'host'
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
                key.replace(key_path)
                sync_directory(state)
        host_key = subprocess.check_output(['ssh-keygen', '-y', '-f', str(key_path)], text=True).strip()
        config = dict(pair, observe=observe, dev=dev)
        atomic_write(access, json.dumps(config) + '\n')
    for port in (OBSERVE_PORT, DEV_PORT) if dev else (OBSERVE_PORT,):
        print(f'[{pair["address"]}]:{port} {public_key(host_key)}')


def ssh_config(mode, runtime, credentials):
    state = json.loads((credentials / 'access').read_bytes(), object_pairs_hook=unique_object)
    pair, observe, dev = validated_enrollment(state)
    address = pair['address']
    port = OBSERVE_PORT if mode == 'observe' else DEV_PORT
    source = f'from="{pair["controller"]}",'
    users = []
    root_keys = runtime / 'authorized-root'
    root_keys.unlink(missing_ok=True)
    if mode == 'observe':
        users.append('being-observe')
        atomic_write(runtime / 'authorized-being-observe',
                     f'{source}restrict,command="/usr/sbin/being-lab-access metrics" {observe}\n', 0o644)
    if dev:
        users.append('root')
        options = ('restrict,command="/usr/sbin/being-lab-access control"' if mode == 'observe'
                   else 'restrict,pty')
        atomic_write(root_keys, f'{source}{options} {dev}\n', 0o644)
    if not users:
        return None
    return f'''AddressFamily inet
ListenAddress {address}:{port}
PidFile none
HostKey {credentials / 'host-key'}
HostKeyAlgorithms ssh-ed25519
AuthorizedKeysFile {runtime / 'authorized-%u'}
AllowUsers {' '.join(users)}
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PermitRootLogin {'forced-commands-only' if mode == 'observe' else 'prohibit-password'}
PermitTTY {'no' if mode == 'observe' else 'yes'}
PermitUserRC no
PermitUserEnvironment no
DisableForwarding yes
StrictModes yes
ClientAliveInterval 30
ClientAliveCountMax 2
LoginGraceTime 15
MaxAuthTries 2
MaxSessions 1
MaxStartups 4:50:8
LogLevel ERROR
'''


def listen(mode):
    runtime = Path(f'/run/being-lab-{mode}')
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
    config = ssh_config(mode, runtime, credentials)
    if config is None:
        return
    path = runtime / 'sshd_config'
    atomic_write(path, config)
    os.execv(SSH, [SSH, '-D', '-e', '-f', str(path)])


ETHERNET_NETWORK = Path('/run/systemd/network/01-being-lab-ethernet.network')


def configure_ethernet(pair, mode):
    """Prepare the selected wired link before the policy brings networkd up."""
    name = pair.get('ethernet') if pair is not None else None
    if mode not in (PAIRED_LAN, INTERNET) or not name:
        ETHERNET_NETWORK.unlink(missing_ok=True)
        return
    ETHERNET_NETWORK.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(ETHERNET_NETWORK, networkd_config(name, mode, ethernet=True), 0o644)


def control(original):
    command = {'dev-on': 'start', 'dev-off': 'stop'}.get(original)
    if command is None:
        raise ValueError('Expected dev-on or dev-off.')
    subprocess.run(['/usr/bin/systemctl', command, DEV_SERVICE], check=True, timeout=15)
    if command == 'start':
        subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', DEV_SERVICE], check=True, timeout=3)
    print('Development window ' + ('is open (maximum 30 minutes per window).' if command == 'start' else 'closed.'))


def wait_dev():
    credentials = Path(os.environ['CREDENTIALS_DIRECTORY'])
    state = json.loads((credentials / 'access').read_bytes(), object_pairs_hook=unique_object)
    pair, _, _ = validated_enrollment(state)
    address = pair['address']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, DEV_PORT), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise ValueError('The development listener did not start at the reserved address.')


def metrics(original):
    from .hardware_metrics import main as measure

    args = shlex.split(original)
    if len(args) != 5 or args[0] != 'metrics' or args[1] != '--seconds' or args[3] != '--interval':
        raise ValueError('Expected metrics --seconds N --interval N.')
    if any(not arg.isascii() or not arg.isdecimal() or len(arg) > 3 for arg in (args[2], args[4])):
        raise ValueError('Invalid measurement window.')
    measure(args[1:])


def main():
    parser = argparse.ArgumentParser(prog='being-lab-access')
    commands = parser.add_subparsers(dest='command', required=True)
    provision = commands.add_parser('enroll', help='enroll public keys and print host-key pins')
    provision.add_argument('address')
    provision.add_argument('--observe-key', type=Path, required=True)
    provision.add_argument('--dev-key', type=Path)
    provision.add_argument('--controller-ip', required=True, help='reserved private IPv4 address of the paired controller')
    provision.add_argument('--ethernet', help='use DHCP on this Ethernet interface')
    provision.add_argument('--offline-mind', type=Path)
    revoke = commands.add_parser('revoke', help='disable both listeners and remove authorized public keys')
    revoke.add_argument('--offline-mind', type=Path)
    server = commands.add_parser('listen')
    server.add_argument('mode', choices=('observe', 'dev'))
    commands.add_parser('control')
    commands.add_parser('wait-dev')
    commands.add_parser('metrics')
    args = parser.parse_args()
    try:
        if args.command == 'metrics':
            metrics(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
            return
        if os.geteuid() != 0:
            parser.error('Run as root.')
        if args.command == 'listen':
            listen(args.mode)
        elif args.command == 'wait-dev':
            wait_dev()
        elif args.command == 'control':
            control(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
        else:
            from .wifi import offline_mind_mount

            mind = offline_mind_mount(args.offline_mind) if args.offline_mind else MIND
            if not mind.is_mount():
                raise ValueError('Mount the installed mind partition first.')
            if args.command == 'enroll':
                enroll(mind, args.address, args.observe_key, args.dev_key, args.controller_ip, args.ethernet)
            else:
                state = mind / 'lab-access'
                if state.exists():
                    access = state / 'access.json'
                    with owner_change(access):
                        access.unlink(missing_ok=True)
                        sync_directory(state)
            if args.offline_mind is None:
                subprocess.run([NETWORK, 'refresh-pair'], check=True, timeout=10)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit(f'being-lab-access: {error}')


if __name__ == '__main__':
    main()
