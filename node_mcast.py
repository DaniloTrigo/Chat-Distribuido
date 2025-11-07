import argparse, sys, socket, threading
from p2p_core import Peer
from discovery import discover_coordinator, HeartbeatWatcher, coordinator_listen_and_reply, coordinator_heartbeat

def main():
    ap = argparse.ArgumentParser(description="Nó P2P - Multicast + JOIN + HB + Eleição + Histórico causal")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--nick", default="anon")
    ap.add_argument("--mcast-ip", default="224.1.1.1")
    ap.add_argument("--mcast-port", type=int, default=5007)
    args = ap.parse_args()

    # Helpers para coordenador
    coord_threads = {"reply": None, "hb": None}
    stop_flags = {"running": False}

    def start_coord_services():
        if stop_flags.get("running"): return
        stop_flags["running"] = True
        def get_addr(): return (args.host, p.port)
        t1 = threading.Thread(target=coordinator_listen_and_reply,
                              args=(get_addr, args.mcast_ip, args.mcast_port), daemon=True)
        t1.start()
        t2 = threading.Thread(target=coordinator_heartbeat,
                              args=(args.mcast_ip, args.mcast_port, 2.0), daemon=True)
        t2.start()
        coord_threads["reply"] = t1
        coord_threads["hb"] = t2
        print("[COORD] Serviços iniciados (DISCOVER responder + HEARTBEAT).")

    def stop_coord_services():
        if stop_flags.get("running"):
            print("[COORD] (info) Alternância de papel: novo coordenador eleito; paro de atuar como coord.")
            stop_flags["running"] = False

    # 1) Descobrir coordenador
    info = discover_coordinator(args.nick, "127.0.0.1", mcast_grp=args.mcast_ip, mcast_port=args.mcast_port)
    if not info:
        print("Nenhum coordenador respondeu. Inicie um com coord_node.py.")
        sys.exit(1)
    coord_ip, coord_port = info
    print(f"Coordenador: {coord_ip}:{coord_port}")

    # 2) Sobe Peer local
    p = Peer(args.host, args.port)
    p.start()
    print(f"Nó local: {p.peer_id}")

    # 3) Conecta ao coordenador e envia JOIN
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((coord_ip, coord_port))
    p.connections.add(s)
    s.sendall((f'{{"type":"HELLO","from":"{p.peer_id}"}}' + "\n").encode())
    s.sendall(b'{"type":"REQ_PEERS"}\n')
    threading.Thread(target=p._reader, args=(s,), daemon=True).start()
    p.send_join(s, args.nick)

    # 4) Sincroniza histórico ao entrar (anti-entropia)
    try:
        s.sendall(b'{"type":"REQ_SYNC"}\n')
    except: pass

    # 5) Heartbeat watcher -> eleição
    def on_hb_timeout():
        print("\n[ALERTA] Coordenador inativo (sem heartbeat). Iniciando eleição...")
        p.start_election(trigger="heartbeat_timeout")
    hb = HeartbeatWatcher(on_hb_timeout, mcast_grp=args.mcast_ip, mcast_port=args.mcast_port)
    hb.start()

    # 6) callbacks de alternância
    p.set_on_became_coordinator(start_coord_services)
    p.set_on_new_coordinator(lambda new: stop_coord_services() if new != p.peer_id else None)

    print("Digite mensagens. Comandos: /leave | /elect | /history")
    try:
        for line in sys.stdin:
            line=line.strip()
            if not line: continue
            if line == "/leave":
                p.send_leave(); break
            if line == "/elect":
                p.start_election(trigger="manual"); continue
            if line == "/history":
                # imprime histórico local (ordem causal)
                for i, m in enumerate(p.history, 1):
                    print(f"{i:03d} [{m['from']}] {m['text']}")
                continue
            p.broadcast_text(line)
    except KeyboardInterrupt:
        pass
    finally:
        hb.stop(); p.stop()

if __name__ == "__main__":
    main()
