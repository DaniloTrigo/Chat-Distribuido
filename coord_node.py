import argparse, threading
from p2p_core import Peer
from discovery import coordinator_listen_and_reply, coordinator_heartbeat

def main():
    ap = argparse.ArgumentParser(description="Coordenador P2P + Multicast + Heartbeat")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6000)
    ap.add_argument("--mcast-ip", default="224.1.1.1")
    ap.add_argument("--mcast-port", type=int, default=5007)
    args = ap.parse_args()

    p = Peer(args.host, args.port)
    p.set_role("coord")
    p.start()
    print(f"[COORD] TCP em {p.peer_id}")

    def get_addr(): return (args.host, p.port)
    threading.Thread(target=coordinator_listen_and_reply, args=(get_addr,args.mcast_ip,args.mcast_port),daemon=True).start()
    threading.Thread(target=coordinator_heartbeat, args=(args.mcast_ip,args.mcast_port,2.0),daemon=True).start()

    print(f"[COORD] ouvindo DISCOVER + heartbeat @ {args.mcast_ip}:{args.mcast_port}")
    try:
        while True: pass
    except KeyboardInterrupt: pass
    finally: p.stop()

if __name__ == "__main__":
    main()
