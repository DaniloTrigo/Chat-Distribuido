import socket, struct, time, threading
from typing import Callable, Optional, Tuple

MCAST_GRP_DEFAULT = "224.1.1.1"
MCAST_PORT_DEFAULT = 5007

def _sock_mcast_receiver(mcast_grp, mcast_port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try: s.bind(("", mcast_port))
    except OSError: s.bind((mcast_grp, mcast_port))
    mreq = struct.pack("=4sl", socket.inet_aton(mcast_grp), socket.INADDR_ANY)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return s

def _sock_mcast_sender(ttl=1):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    return s

# ==== Req.2: descoberta ====
def coordinator_listen_and_reply(get_coord_addr, mcast_grp=MCAST_GRP_DEFAULT, mcast_port=MCAST_PORT_DEFAULT):
    recv = _sock_mcast_receiver(mcast_grp, mcast_port)
    while True:
        data, _ = recv.recvfrom(1024)
        try:
            parts = data.decode().strip().split()
            if len(parts) == 4 and parts[0] == "DISCOVER":
                cli_ip, cli_port = parts[2], int(parts[3])
                ipc, pc = get_coord_addr()
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                    u.sendto(f"COORD {ipc} {pc}".encode(), (cli_ip, cli_port))
        except Exception: pass

def discover_coordinator(nick, my_listen_ip, timeout_s=5.0,
                         mcast_grp=MCAST_GRP_DEFAULT, mcast_port=MCAST_PORT_DEFAULT) -> Optional[Tuple[str,int]]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rx:
        rx.bind((my_listen_ip, 0))
        _, my_port = rx.getsockname()
        rx.settimeout(timeout_s)
        tx = _sock_mcast_sender()
        tx.sendto(f"DISCOVER {nick} {my_listen_ip} {my_port}".encode(), (mcast_grp, mcast_port))
        try:
            data, _ = rx.recvfrom(1024)
            parts = data.decode().strip().split()
            if len(parts) == 3 and parts[0] == "COORD":
                return (parts[1], int(parts[2]))
        except socket.timeout:
            return None

# ==== Req.3: heartbeat ====
def coordinator_heartbeat(mcast_grp=MCAST_GRP_DEFAULT, mcast_port=MCAST_PORT_DEFAULT, interval_s=2.0):
    tx = _sock_mcast_sender()
    while True:
        tx.sendto(f"HB {int(time.time()*1000)}".encode(), (mcast_grp, mcast_port))
        time.sleep(interval_s)

class HeartbeatWatcher(threading.Thread):
    def __init__(self, on_timeout, mcast_grp=MCAST_GRP_DEFAULT, mcast_port=MCAST_PORT_DEFAULT, hb_timeout_s=6.0):
        super().__init__(daemon=True)
        self.on_timeout = on_timeout
        self.mcast_grp, self.mcast_port = mcast_grp, mcast_port
        self.hb_timeout_s = hb_timeout_s
        self._last = 0
        self._stop = False
    def run(self):
        rx = _sock_mcast_receiver(self.mcast_grp, self.mcast_port)
        rx.settimeout(1.0)
        while not self._stop:
            try:
                data, _ = rx.recvfrom(1024)
                p = data.decode().strip().split()
                if len(p)==2 and p[0]=="HB": self._last=time.time()
            except socket.timeout: pass
            if self._last and (time.time()-self._last)>self.hb_timeout_s:
                self.on_timeout(); self._last=0
    def stop(self): self._stop=True
