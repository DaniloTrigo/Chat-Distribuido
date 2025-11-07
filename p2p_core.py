import socket, threading, uuid, json, time
from copy import deepcopy

BUF = 65536
MIN_DEGREE = 2   # alvo mínimo de conexões

def _pack(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")

def _readlines(sock):
    buff = b""
    while True:
        data = sock.recv(BUF)
        if not data: break
        buff += data
        while b"\n" in buff:
            line, buff = buff.split(b"\n", 1)
            yield line

class Peer:
    """
    Nó P2P com:
      - difusão com deduplicação
      - descoberta/expansão de vizinhança
      - coordenação (IDs + anúncios)
      - eleição (Bully "light")
      - tolerância a falhas (anúncio de LEAVE e re-expansão)
      - histórico consistente com ordem causal (Relógio Vetorial + buffer + anti-entropia)
    """
    def __init__(self, host="127.0.0.1", port=0, peer_id=None):
        self.host = host
        self.port = port
        self.peer_id = peer_id or f"{host}:{port}"

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((host, port))
        self.port = self.server.getsockname()[1]  # resolve se port=0
        self.peer_id = peer_id or f"{host}:{self.port}"

        self.lock = threading.RLock()
        self.connections = set()
        self.seen = set()
        self.running = False

        # Mapas auxiliares
        self.sock2peer = {}        # socket -> peer_id
        self.peer2sock = {}        # peer_id -> socket

        # ===== Coordenador (Req.3) =====
        self.role = "node"
        self.members = {}
        self._id_seq = 0
        self.assigned_id = None

        # ===== Eleição (Req.4) =====
        self.current_coord = None
        self._election_lock = threading.RLock()
        self._election_timer = None
        self._heard_alive = False
        self._participating = False
        self._on_became_coordinator = None
        self._on_new_coordinator = None

        # ===== Histórico consistente (Req.6) =====
        # Relógio vetorial: dict peer_id -> contagem
        self.clock = {}                   # estado local
        self.buffer = {}                  # message_id -> msg (ainda não caus. pronto)
        self.history = []                 # lista de mensagens TEXT entregues (na ordem causal local)

    # ---------- callbacks ----------
    def set_on_became_coordinator(self, fn): self._on_became_coordinator = fn
    def set_on_new_coordinator(self, fn): self._on_new_coordinator = fn

    # ---------- util ----------
    def set_role(self, role: str): self.role = role
    def _next_id(self) -> int: self._id_seq += 1; return self._id_seq
    def _priority(self) -> int: return int(self.assigned_id) if self.assigned_id is not None else int(self.port)

    # VC helpers
    def _vc_inc_local(self):
        self.clock[self.peer_id] = self.clock.get(self.peer_id, 0) + 1

    def _vc_merge(self, other_vc: dict):
        for k, v in other_vc.items():
            if v > self.clock.get(k, 0):
                self.clock[k] = v

    def _causal_ready(self, msg_vc: dict, src: str) -> bool:
        # Regra: para todo p != src: VCm[p] <= VClocal[p]
        #        e para p == src:    VCm[src] == VClocal[src] + 1
        local = self.clock
        for p, v in msg_vc.items():
            if p == src:
                if v != local.get(p, 0) + 1:
                    return False
            else:
                if v > local.get(p, 0):
                    return False
        # também checar peers não mencionados no msg_vc? Não é necessário.
        return True

    # ---------- ciclo ----------
    def start(self):
        self.running = True
        self.server.listen()
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self.running = False
        try: self.server.close()
        except: pass
        with self.lock:
            for c in list(self.connections):
                try: c.close()
                except: pass
            self.connections.clear()
            self.sock2peer.clear()
            self.peer2sock.clear()
        with self._election_lock:
            if self._election_timer:
                try: self._election_timer.cancel()
                except: pass
            self._election_timer = None
            self._participating = False
            self._heard_alive = False

    # ---------- rede ----------
    def _accept_loop(self):
        while self.running:
            try:
                sock, _ = self.server.accept()
                with self.lock: self.connections.add(sock)
                try: sock.sendall(_pack({"type":"HELLO","from":self.peer_id}))
                except: pass
                threading.Thread(target=self._reader, args=(sock,), daemon=True).start()
            except OSError:
                break

    def connect(self, ip, port, timeout=3.0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        with self.lock: self.connections.add(s)
        try:
            s.sendall(_pack({"type":"HELLO","from":self.peer_id}))
            s.sendall(_pack({"type":"REQ_PEERS"}))
        except: pass
        threading.Thread(target=self._reader, args=(s,), daemon=True).start()

    def _reader(self, sock):
        try:
            for raw in _readlines(sock):
                try: msg = json.loads(raw.decode("utf-8"))
                except Exception: continue
                self._handle(sock, msg)
        except Exception:
            pass
        finally:
            lost_peer = None
            with self.lock:
                if sock in self.connections: self.connections.remove(sock)
                lost_peer = self.sock2peer.pop(sock, None)
                if lost_peer and self.peer2sock.get(lost_peer) is sock:
                    self.peer2sock.pop(lost_peer, None)
            try: sock.close()
            except: pass
            self._on_disconnect(lost_peer)

    # ---------- falha/desconexão ----------
    def _on_disconnect(self, lost_peer_id: str):
        if not lost_peer_id:
            self._maybe_expand_neighbors()
            return

        if self.current_coord and lost_peer_id == self.current_coord:
            print(f"[FALHA] Perdi a conexão com o coordenador ({lost_peer_id}). Iniciando eleição...")
            self.start_election(trigger="coord_disconnect")

        if self.role == "coord":
            left_id = None
            for nid, info in list(self.members.items()):
                if info.get("peer_id") == lost_peer_id:
                    left_id = nid; del self.members[nid]; break
            announce = {
                "type":"MSG","id": str(uuid.uuid4()),"from": self.peer_id,
                "ts": time.time(),"kind":"CTRL","sub":"ANNOUNCE_LEAVE",
                "payload": {"node_id": left_id, "peer_id": lost_peer_id}
            }
            self._handle(None, announce)

        self._maybe_expand_neighbors()

    def _maybe_expand_neighbors(self):
        with self.lock:
            deg = len(self.connections)
        if deg >= MIN_DEGREE: return
        self._broadcast_req_peers()

    def _broadcast_req_peers(self):
        msg = {"type":"REQ_PEERS"}
        with self.lock:
            for c in list(self.connections):
                try: c.sendall(_pack(msg))
                except: pass

    # ---------- protocolo ----------
    def _handle(self, sock, msg):
        t = msg.get("type")

        if t == "ASSIGNED":
            self.assigned_id = msg.get("node_id")
            print(f"[INFO] ID atribuído pelo coordenador: {self.assigned_id}")
            return

        if t == "HELLO":
            pid = msg.get("from")
            if pid:
                with self.lock:
                    if sock: self.sock2peer[sock] = pid
                    self.peer2sock[pid] = sock
            return

        # ===== Anti-entropia =====
        if t == "REQ_SYNC":
            # Manda dump completo do histórico (simples, suficiente p/ trabalho)
            dump = {"type":"SYNC_DUMP","items": self.history[-5000:]}  # cap de segurança
            try: sock and sock.sendall(_pack(dump))
            except: pass
            return

        if t == "SYNC_DUMP":
            # Integra mensagens recebidas (dedup + causal)
            for m in msg.get("items", []):
                mid = m.get("id")
                if not mid or mid in self.seen:  # já vista ou inválida
                    continue
                # processa como se tivesse chegado via rede
                self._receive_text(m, forward=False)  # não repropaga dump
            # tenta drenar buffer após integrar
            self._drain_buffer()
            return

        if t == "REQ_PEERS":
            items = []
            with self.lock:
                for c in self.connections:
                    try:
                        ip, port = c.getpeername()
                        items.append([ip, port])
                    except: pass
            try: sock and sock.sendall(_pack({"type":"PEERS","items":items}))
            except: pass
            return

        if t == "PEERS":
            for ip, port in msg.get("items", [])[:4]:
                try:
                    already = False
                    with self.lock:
                        for c in self.connections:
                            try:
                                if c.getpeername() == (ip, port): already = True; break
                            except: pass
                    if already or (ip == self.host and port == self.port): continue
                    threading.Thread(target=self.connect, args=(ip, port), daemon=True).start()
                except: pass
            return

        if t == "MSG":
            kind = msg.get("kind", "TEXT")
            mid = msg.get("id")

            # Dedup geral por id (vale para todos os kinds que tragam 'id')
            if mid is not None:
                with self.lock:
                    if mid in self.seen: return
                    self.seen.add(mid)

            # ----- chat: entrega causal -----
            if kind == "TEXT":
                self._receive_text(msg, forward=True)
                return

            # ----- JOIN (coordenador) -----
            if kind == "JOIN" and self.role == "coord":
                nick = msg.get("nick","anon")
                peer_id = msg.get("peer_id","?")
                node_id = self._next_id()
                self.members[node_id] = {"peer_id": peer_id, "nick": nick}
                try: sock and sock.sendall(_pack({"type":"ASSIGNED","node_id":node_id}))
                except: pass
                announce = {
                    "type":"MSG","id": str(uuid.uuid4()),"from": self.peer_id,
                    "ts": time.time(),"kind":"CTRL","sub":"ANNOUNCE_JOIN",
                    "payload": {"node_id": node_id, "peer_id": peer_id, "nick": nick}
                }
                self._handle(None, announce)
                return

            # ----- LEAVE (coordenador) -----
            if kind == "LEAVE" and self.role == "coord":
                peer_id = msg.get("peer_id","?")
                left_id = None
                for nid, info in list(self.members.items()):
                    if info.get("peer_id") == peer_id:
                        left_id = nid; del self.members[nid]; break
                announce = {
                    "type":"MSG","id": str(uuid.uuid4()),"from": self.peer_id,
                    "ts": time.time(),"kind":"CTRL","sub":"ANNOUNCE_LEAVE",
                    "payload": {"node_id": left_id, "peer_id": peer_id}
                }
                self._handle(None, announce)
                return

            # ----- CTRL (anúncios do coordenador) -----
            if kind == "CTRL":
                sub = msg.get("sub","?")
                p = msg.get("payload",{})
                if sub == "ANNOUNCE_JOIN":
                    print(f"[COORD] Entrou: id={p.get('node_id')} nick={p.get('nick')} peer={p.get('peer_id')}")
                elif sub == "ANNOUNCE_LEAVE":
                    print(f"[COORD] Saiu: id={p.get('node_id')} peer={p.get('peer_id')}")
                self._forward(sock, msg)
                return

            # ----- ELEIÇÃO -----
            if kind == "ELECTION":
                other_prio = int(msg.get("prio", -1))
                my_prio = self._priority()
                if my_prio > other_prio:
                    alive = {
                        "type":"MSG","id": str(uuid.uuid4()),"from": self.peer_id,
                        "ts": time.time(),"kind":"ALIVE",
                        "from_prio": my_prio, "from_peer": self.peer_id
                    }
                    self._handle(None, alive)
                    self.start_election(trigger="heard_lower")
                self._forward(sock, msg)
                return

            if kind == "ALIVE":
                with self._election_lock:
                    self._heard_alive = True
                self._forward(sock, msg)
                return

            if kind == "COORDINATOR":
                new_coord = msg.get("peer_id")
                prio = int(msg.get("prio", -1))
                self.current_coord = new_coord
                if new_coord != self.peer_id:
                    self.set_role("node")
                with self._election_lock:
                    self._participating = False
                    self._heard_alive = False
                    if self._election_timer:
                        try: self._election_timer.cancel()
                        except: pass
                        self._election_timer = None
                print(f"[ELEIÇÃO] Novo coordenador: {new_coord} (prio {prio})")
                if self._on_new_coordinator:
                    try: self._on_new_coordinator(new_coord)
                    except: pass
                self._forward(sock, msg)
                self._maybe_expand_neighbors()
                return

    # ----- tratamento de TEXT com causalidade -----
    def _receive_text(self, msg, forward: bool):
        src = msg.get("from","?")
        vc = msg.get("vc", {})
        mid = msg.get("id")
        text = msg.get("text","")

        # Se não estiver causalmente pronto, guarda em buffer mas ainda assim propaga.
        if not self._causal_ready(vc, src):
            self.buffer[mid] = msg
            if forward: self._forward(None, msg)
            return

        # Entrega: atualiza relógio e histórico
        self.clock[src] = self.clock.get(src, 0) + 1
        self.history.append({
            "id": mid, "from": src, "ts": msg.get("ts"), "vc": vc, "text": text
        })
        print(f"[{src}] {text}")
        if forward: self._forward(None, msg)

        # Merge conservador (opcional, útil se vierem VC maiores para outros peers)
        self._vc_merge(vc)

        # Após uma entrega, tente drenar mensagens que ficaram prontas
        self._drain_buffer()

    def _drain_buffer(self):
        changed = True
        while changed:
            changed = False
            # Copiar chaves para evitar mutação durante iteração
            for mid in list(self.buffer.keys()):
                m = self.buffer[mid]
                src = m.get("from","?")
                if self._causal_ready(m.get("vc", {}), src):
                    # "forward=False" pois ela já foi propagada antes
                    self._receive_text(m, forward=False)
                    del self.buffer[mid]
                    changed = True

    def _forward(self, origin_sock, msg):
        data = _pack(msg)
        with self.lock:
            for c in list(self.connections):
                if c is origin_sock: continue
                try: c.sendall(data)
                except: pass

    # ---------- app ----------
    def broadcast_text(self, text: str):
        # Gera VC local e anexa na mensagem
        self._vc_inc_local()
        msg = {
            "type":"MSG",
            "id": str(uuid.uuid4()),
            "from": self.peer_id,
            "ts": time.time(),
            "kind":"TEXT",
            "text": text,
            "vc": deepcopy(self.clock)  # snapshot do VC
        }
        # Trata local (entrega causal) e propaga
        self._receive_text(msg, forward=True)

    def send_join(self, sock, nick: str):
        j = {"type":"MSG","kind":"JOIN","nick":nick,"peer_id":self.peer_id}
        try: sock and sock.sendall(_pack(j))
        except: pass

    def send_leave(self):
        msg = {"type":"MSG","id": str(uuid.uuid4()),"from": self.peer_id,
               "ts": time.time(),"kind":"LEAVE","peer_id": self.peer_id}
        self._handle(None, msg)

    # ---------- eleição ----------
    def start_election(self, trigger="timeout"):
        with self._election_lock:
            if self._participating:
                return
            self._participating = True
            self._heard_alive = False
            prio = self._priority()
            print(f"[ELEIÇÃO] Iniciada (motivo: {trigger}) com prioridade {prio}")

            msg = {"type":"MSG","id": str(uuid.uuid4()),"from": self.peer_id,
                   "ts": time.time(),"kind":"ELECTION","prio": prio,"origin": self.peer_id}
            self._handle(None, msg)

            def check():
                with self._election_lock:
                    if self._heard_alive:
                        print("[ELEIÇÃO] Recebi ALIVE de processo com prioridade maior. Aguardando COORDINATOR...")
                        self._participating = False
                        self._election_timer = None
                        return
                    self._declare_as_coordinator()
                    self._election_timer = None
            self._election_timer = threading.Timer(3.0, check)
            self._election_timer.daemon = True
            self._election_timer.start()

    def _declare_as_coordinator(self):
        self.set_role("coord")
        self.current_coord = self.peer_id
        prio = self._priority()
        print(f"[ELEIÇÃO] Eu sou o novo coordenador (prio {prio}).")
        msg = {"type":"MSG","id": str(uuid.uuid4()),"from": self.peer_id,
               "ts": time.time(),"kind":"COORDINATOR","peer_id": self.peer_id,"prio": prio}
        self._handle(None, msg)
        if self._on_became_coordinator:
            try: self._on_became_coordinator()
            except: pass
