import socket
import threading
import time
import json
import sys
from datetime import datetime
import pickle
import random
import os, uuid
import struct  # <-- necessário para IP_ADD_MEMBERSHIP no Windows

class ChatNode:
    def __init__(self, multicast_group='224.0.0.1', multicast_port=5007, tcp_port=None):
        self.multicast_group = multicast_group
        self.multicast_port = multicast_port
        self.tcp_port = tcp_port if tcp_port else self._find_free_port()

        # Identidade / papéis
        self.node_id = None
        self.is_coordinator = False
        self.coordinator_id = None
        self.coordinator_addr = None

        # Tabela de nós: {node_id: (ip, tcp_port, last_seen)}
        self.nodes = {}
        # Dono anterior conhecido (opcional, p/ reforçar devolução de ID)
        self.last_owner = {}

        # Mensagens / locks
        self.message_history = []
        self.message_lock = threading.Lock()

        # Heartbeats / presença
        self.last_heartbeat = time.time()
        self.heartbeat_interval = 2.0           # envio de HB do coordenador
        self.peer_alive_interval = 2.0          # envio de NODE_ALIVE por todos
        self.heartbeat_timeout = 5.0            # timeout p/ detectar coord morto
        self.peer_timeout = 8.0                 # timeout p/ peer “morto” (kill)

        # Eleição (Bully + round id)
        self.election_in_progress = False
        self.election_responses = []
        self.current_election_id = None
        self.seen_elections = set()             # rounds já respondidos
        self.last_election_try = 0.0
        self.wait_new_coordinator = 4.0         # aguardo por COORDINATOR após OK

        # Sockets / controle
        self.multicast_sock = None
        self.tcp_sock = None
        self.running = True
        self.local_ip = self._get_local_ip()

        # Persistência leve (identidade e histórico local)
        self.state_path   = f"state_{self.tcp_port}.json"     # identidade e último node_id
        self.history_path = f"history_{self.tcp_port}.jsonl"  # histórico local
        self.node_uuid    = None
        self.desired_id   = None
        self.last_delivered_ts = 0.0
        self._load_state()

    # ------------------------ persistência ------------------------
    def _load_state(self):
        try:
            if os.path.exists(self.state_path):
                st = json.load(open(self.state_path, "r", encoding="utf-8"))
                self.node_uuid  = st.get("node_uuid")
                self.desired_id = st.get("last_node_id")
            if not self.node_uuid:
                self.node_uuid = str(uuid.uuid4())
                self.desired_id = None
                self._save_state()
        except Exception:
            self.node_uuid = str(uuid.uuid4())
            self.desired_id = None
            self._save_state()

    def _save_state(self):
        try:
            json.dump(
                {"node_uuid": self.node_uuid, "last_node_id": self.node_id},
                open(self.state_path, "w", encoding="utf-8"),
                ensure_ascii=False, indent=2
            )
        except Exception:
            pass

    def _append_history_local(self, msg):
        try:
            with open(self.history_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.last_delivered_ts = max(self.last_delivered_ts, msg.get("timestamp", 0.0))
        except Exception:
            pass

    # ------------------------ util ------------------------
    def _get_local_ip(self):
        # Permite forçar a interface via variável de ambiente (útil no Windows/VPN):
        #   set CHAT_IFACE=192.168.x.x
        forced = os.getenv("CHAT_IFACE")
        if forced and forced.strip():
            return forced.strip()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith("127."):
                raise RuntimeError("loopback")
            return ip
        except:
            # último recurso — pode falhar para multicast em alguns hosts
            return "0.0.0.0"

    def _find_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            return s.getsockname()[1]

    def _now_str(self):
        return datetime.now().strftime("%H:%M:%S")

    def _mark_seen(self, nid, ts=None):
        if nid in self.nodes:
            ip, port, _ = self.nodes[nid]
            self.nodes[nid] = (ip, port, ts if ts is not None else time.time())

    def _is_alive(self, nid):
        if nid not in self.nodes:
            return False
        _, _, last_seen = self.nodes[nid]
        return (time.time() - last_seen) <= self.peer_timeout

    def _prune_stale_nodes_local(self):
        # poda local (defensiva) — útil no /status
        to_remove = []
        now = time.time()
        for nid, (_, _, last_seen) in list(self.nodes.items()):
            if (now - last_seen) > self.peer_timeout:
                to_remove.append(nid)
        for nid in to_remove:
            del self.nodes[nid]

    # ------------------------ sockets ------------------------
    def setup_multicast(self):
        # Socket UDP para multicast
        self.multicast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.multicast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind na porta do grupo (todas interfaces)
        self.multicast_sock.bind(('', self.multicast_port))

        # Escolha/descoberta de interface de saída
        iface_ip = self.local_ip
        if not iface_ip or iface_ip.startswith("0.") or iface_ip.startswith("127."):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("224.0.0.1", 7))
                iface_ip = s.getsockname()[0]
                s.close()
            except:
                iface_ip = "0.0.0.0"

        group = socket.inet_aton(self.multicast_group)
        iface = socket.inet_aton(iface_ip)

        # Define interface para ENVIAR multicast
        try:
            self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
        except OSError:
            pass

        # Entra no grupo na interface escolhida (Windows precisa disso)
        try:
            mreq = struct.pack('=4s4s', group, iface)
            self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            # Fallback: tenta wildcard 0.0.0.0
            mreq_any = struct.pack('=4s4s', group, socket.inet_aton('0.0.0.0'))
            self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq_any)

        # TTL e loopback (úteis em testes locais)
        try:
            self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass

        print(f"[INFO] Multicast na interface {iface_ip} → {self.multicast_group}:{self.multicast_port}")

    def setup_tcp(self):
        self.tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_sock.bind(('0.0.0.0', self.tcp_port))
        self.tcp_sock.listen(5)

    def send_multicast(self, message):
        try:
            data = json.dumps(message).encode('utf-8')
            self.multicast_sock.sendto(data, (self.multicast_group, self.multicast_port))
        except Exception as e:
            print(f"[ERRO] Falha ao enviar multicast: {e}")

    def send_tcp(self, ip, port, message):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(3)
                s.connect((ip, port))
                data = pickle.dumps(message)
                s.sendall(data)
                return True
        except Exception:
            return False

    # ------------------------ join / coord ------------------------
    def join_network(self):
        print(f"\n[INFO] Tentando entrar na rede via multicast {self.multicast_group}:{self.multicast_port}")
        print(f"[INFO] Meu endereço TCP: {self.local_ip}:{self.tcp_port}")

        join_request = {
            'type': 'JOIN_REQUEST',
            'ip': self.local_ip,
            'tcp_port': self.tcp_port,
            'timestamp': time.time(),
            'desired_id': self.desired_id,   # tentar recuperar o mesmo node_id
            'node_uuid': self.node_uuid
        }
        self.send_multicast(join_request)

        start_time = time.time()
        while time.time() - start_time < 3:
            time.sleep(0.1)
            if self.node_id is not None:
                print(f"[SUCESSO] Entrei na rede com ID: {self.node_id}")
                print(f"[INFO] Coordenador: Node {self.coordinator_id} ({self.coordinator_addr})")
                return True

        print("[INFO] Nenhum coordenador encontrado. Tornando-me coordenador...")
        self.become_coordinator()
        return True

    def become_coordinator(self):
        self.is_coordinator = True
        self.node_id = 1
        self._save_state()
        self.coordinator_id = self.node_id
        self.coordinator_addr = (self.local_ip, self.tcp_port)
        self.nodes[self.node_id] = (self.local_ip, self.tcp_port, time.time())
        self.last_owner[self.node_id] = self.node_uuid

        print(f"\n{'='*50}")
        print(f"[COORDENADOR] Eu sou o coordenador (Node {self.node_id})")
        print(f"{'='*50}\n")

        self.announce_coordinator()

    def announce_coordinator(self):
        announcement = {
            'type': 'COORDINATOR_ANNOUNCEMENT',
            'coordinator_id': self.coordinator_id,
            'coordinator_ip': self.local_ip,
            'coordinator_port': self.tcp_port,
            'nodes': {nid: (ip, port, ts) for nid, (ip, port, ts) in self.nodes.items()}
        }
        self.send_multicast(announcement)

    # ------------------------ multicast handling ------------------------
    def handle_multicast_messages(self):
        while self.running:
            try:
                data, addr = self.multicast_sock.recvfrom(8192)
                message = json.loads(data.decode('utf-8'))
                msg_type = message.get('type')

                if msg_type == 'JOIN_REQUEST' and self.is_coordinator:
                    self.handle_join_request(message)
                elif msg_type == 'COORDINATOR_ANNOUNCEMENT':
                    self.handle_coordinator_announcement(message)
                elif msg_type == 'HEARTBEAT':
                    self.handle_heartbeat(message)
                elif msg_type == 'NODE_EXIT':
                    self.handle_node_exit(message)
                elif msg_type == 'NODE_JOINED':
                    self.handle_node_joined(message)
                elif msg_type == 'NODE_ALIVE':
                    self.handle_node_alive(message)
                elif msg_type == 'ELECTION':
                    self.handle_election_message(message)
                elif msg_type == 'ELECTION_OK':
                    self.handle_election_ok(message)
                elif msg_type == 'COORDINATOR':
                    self.handle_new_coordinator(message)

            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[ERRO] Erro ao processar multicast: {e}")

    def _coordinator_prune_before_assign(self):
        # libera IDs “mortos” antes de conceder (faxina leve)
        now = time.time()
        to_remove = []
        for nid, (ip, port, last_seen) in list(self.nodes.items()):
            if nid == self.node_id:
                continue
            if (now - last_seen) > self.peer_timeout:
                to_remove.append((nid, ip, port))
        for nid, ip, port in to_remove:
            if nid in self.nodes:
                del self.nodes[nid]
                print(f"[{self._now_str()}][COORD] Peer inativo removido: Node {nid} ({ip}:{port})")
                self.send_multicast({'type': 'NODE_EXIT', 'node_id': nid, 'timestamp': time.time()})

    def handle_join_request(self, message):
        if not self.is_coordinator:
            return

        self._coordinator_prune_before_assign()

        desired = message.get("desired_id")
        req_uuid = message.get("node_uuid")
        new_ip   = message["ip"]
        new_port = message["tcp_port"]

        # tenta conceder desired_id se livre (ou antigo dono)
        can_assign_desired = False
        if desired and isinstance(desired, int) and desired > 0 and (desired not in self.nodes):
            owner_ok = (self.last_owner.get(desired) in (None, req_uuid))
            if owner_ok:
                new_id = desired
                can_assign_desired = True

        if not can_assign_desired:
            new_id = max(self.nodes.keys()) + 1 if self.nodes else 1

        self.nodes[new_id] = (new_ip, new_port, time.time())
        self.last_owner[new_id] = req_uuid

        print(f"[{self._now_str()}][COORD] Nó entrando: Node {new_id} ({new_ip}:{new_port})"
              + (f" (reclamando ID antigo {desired})" if can_assign_desired else ""))

        join_response = {
            "type": "JOIN_RESPONSE",
            "node_id": new_id,
            "coordinator_id": self.coordinator_id,
            "coordinator_ip": self.local_ip,
            "coordinator_port": self.tcp_port,
            "nodes": {nid: (ip, port, ts) for nid, (ip, port, ts) in self.nodes.items()},
            "message_history": self.message_history
        }
        if not self.send_tcp(new_ip, new_port, join_response):
            del self.nodes[new_id]
            print(f"[{self._now_str()}][COORD] Falha no JOIN com Node {new_id}, revertendo.")
            return

        self.send_multicast({
            "type": "NODE_JOINED",
            "node_id": new_id,
            "ip": new_ip,
            "port": new_port,
            "timestamp": time.time()
        })

    def handle_coordinator_announcement(self, message):
        self.coordinator_id = message['coordinator_id']
        self.coordinator_addr = (message['coordinator_ip'], message['coordinator_port'])
        self.last_heartbeat = time.time()
        if 'nodes' in message:
            # usar ts enviado, não time.time()
            for nid_key, triple in message['nodes'].items():
                nid = int(nid_key) if isinstance(nid_key, str) else int(nid_key)
                ip, port, ts = triple
                self.nodes[nid] = (ip, port, float(ts))

    def handle_heartbeat(self, message):
        if message.get('coordinator_id') == self.coordinator_id:
            self.last_heartbeat = time.time()
            if 'nodes' in message:
                for nid_key, triple in message['nodes'].items():
                    nid = int(nid_key) if isinstance(nid_key, str) else int(nid_key)
                    ip, port, ts = triple
                    self.nodes[nid] = (ip, port, float(ts))

    def handle_node_joined(self, message):
        nid = int(message.get('node_id'))
        ip = message.get('ip')
        port = int(message.get('port'))
        self.nodes[nid] = (ip, port, time.time())
        if self.is_coordinator:
            print(f"[{self._now_str()}][COORD] Confirmação de entrada: Node {nid} ({ip}:{port})")

    def handle_node_exit(self, message):
        exit_node_id = int(message.get('node_id'))
        if exit_node_id in self.nodes:
            ip, port, _ = self.nodes[exit_node_id]
            del self.nodes[exit_node_id]
            if self.is_coordinator:
                print(f"[{self._now_str()}][COORD] Nó saiu: Node {exit_node_id} ({ip}:{port})")
            else:
                print(f"\n[INFO] Node {exit_node_id} saiu da rede")

    def handle_node_alive(self, message):
        nid = int(message.get('node_id'))
        ip = message.get('ip')
        port = int(message.get('port'))
        # se não conhecia, adiciona passivamente; se conhecia, atualiza last_seen
        if nid not in self.nodes:
            self.nodes[nid] = (ip, port, time.time())
        else:
            self._mark_seen(nid)

    # ------------------------ tcp handling ------------------------
    def handle_tcp_connections(self):
        while self.running:
            try:
                self.tcp_sock.settimeout(1.0)
                conn, addr = self.tcp_sock.accept()
                threading.Thread(target=self.handle_tcp_client, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[ERRO] Erro ao aceitar conexão TCP: {e}")

    def handle_tcp_client(self, conn, addr):
        try:
            data = b''
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                try:
                    message = pickle.loads(data)
                    break
                except:
                    continue

            if data:
                message = pickle.loads(data)
                msg_type = message.get('type')

                if msg_type == 'JOIN_RESPONSE':
                    self.handle_join_response(message)
                elif msg_type == 'CHAT_MESSAGE':
                    self.handle_chat_message(message)
                elif msg_type == 'HISTORY_REQUEST':
                    self.handle_history_request(conn, message)
                elif msg_type == 'HISTORY_RESPONSE':
                    self.handle_history_response(message)

        except Exception as e:
            print(f"[ERRO] Erro ao processar cliente TCP: {e}")
        finally:
            try:
                conn.close()
            except:
                pass

    def handle_join_response(self, message):
        self.node_id = message['node_id']
        self._save_state()
        self.coordinator_id = message['coordinator_id']
        self.coordinator_addr = (message['coordinator_ip'], message['coordinator_port'])
        for nid_key, triple in message['nodes'].items():
            nid = int(nid_key) if isinstance(nid_key, str) else int(nid_key)
            ip, port, ts = triple
            self.nodes[nid] = (ip, port, float(ts))
        with self.message_lock:
            self.message_history = message.get('message_history', [])
        self.last_heartbeat = time.time()

    def handle_chat_message(self, message):
        sender_id = message.get('sender_id')
        if sender_id:
            self._mark_seen(sender_id)
        with self.message_lock:
            if not any(m['sender_id'] == message['sender_id'] and m['timestamp'] == message['timestamp']
                       for m in self.message_history):
                self.message_history.append(message)
                self._append_history_local(message)
                sender_id = message['sender_id']
                text = message['text']
                timestamp = datetime.fromtimestamp(message['timestamp']).strftime('%H:%M:%S')
                print(f"\n[Node {sender_id}] ({timestamp}): {text}")
                print("> ", end='', flush=True)

    def handle_history_request(self, conn, message):
        response = {
            'type': 'HISTORY_RESPONSE',
            'message_history': self.message_history
        }
        try:
            data = pickle.dumps(response)
            conn.sendall(data)
        except Exception as e:
            print(f"[ERRO] Erro ao enviar histórico: {e}")

    def handle_history_response(self, message):
        peer_history = message.get('message_history', [])
        with self.message_lock:
            for msg in peer_history:
                if not any(m['sender_id'] == msg['sender_id'] and m['timestamp'] == msg['timestamp']
                           for m in self.message_history):
                    self.message_history.append(msg)
                    self._append_history_local(msg)
            self.message_history.sort(key=lambda x: x['timestamp'])

    # ------------------------ heartbeats / peers / election ------------------------
    def send_heartbeat(self):
        while self.running:
            if self.is_coordinator:
                heartbeat = {
                    'type': 'HEARTBEAT',
                    'coordinator_id': self.coordinator_id,
                    'timestamp': time.time(),
                    'nodes': {nid: (ip, port, ts) for nid, (ip, port, ts) in self.nodes.items()}
                }
                self.send_multicast(heartbeat)
            time.sleep(self.heartbeat_interval)

    def send_node_alive(self):
        # Todos os nós emitem presença periódica; isso cobre “kill terminal”
        while self.running:
            if self.node_id is not None:
                self.send_multicast({
                    'type': 'NODE_ALIVE',
                    'node_id': self.node_id,
                    'ip': self.local_ip,
                    'port': self.tcp_port,
                    'timestamp': time.time()
                })
            time.sleep(self.peer_alive_interval)

    def coordinator_watch_peers(self):
        # Coordenador remove peers “mortos” e anuncia NODE_EXIT
        while self.running:
            if self.is_coordinator:
                now = time.time()
                to_remove = []
                for nid, (ip, port, last_seen) in list(self.nodes.items()):
                    if nid == self.node_id:
                        continue
                    if (now - last_seen) > self.peer_timeout:
                        to_remove.append((nid, ip, port))
                for nid, ip, port in to_remove:
                    if nid in self.nodes:
                        del self.nodes[nid]
                        print(f"[{self._now_str()}][COORD] Peer inativo removido: Node {nid} ({ip}:{port})")
                        self.send_multicast({'type': 'NODE_EXIT', 'node_id': nid, 'timestamp': time.time()})
            time.sleep(1.0)

    def monitor_coordinator(self):
        while self.running:
            time.sleep(0.5)
            if not self.is_coordinator and self.node_id:
                if (time.time() - self.last_heartbeat) > self.heartbeat_timeout:
                    # Debounce: evita disparos contínuos
                    if (time.time() - self.last_election_try) < (self.wait_new_coordinator + 1.0):
                        continue
                    print(f"\n[ALERTA] Coordenador não responde há {(time.time()-self.last_heartbeat):.1f}s")
                    print("[INFO] Iniciando eleição...")
                    self.start_election()

    def _make_election_id(self):
        # identificador único por round
        return f"{self.node_id}-{time.time():.6f}-{random.randint(0, 1_000_000)}"

    def start_election(self):
        if self.election_in_progress:
            return
        self.last_election_try = time.time()
        self.election_in_progress = True
        self.election_responses = []
        self.current_election_id = self._make_election_id()

        higher_alive = sorted([nid for nid in self.nodes if nid > self.node_id and self._is_alive(nid)])
        if not higher_alive:
            self.win_election()
            return

        print(f"[ELEIÇÃO] Pedindo OK aos nós superiores vivos: {higher_alive}")
        election_msg = {
            'type': 'ELECTION',
            'sender_id': self.node_id,
            'election_id': self.current_election_id,
            'timestamp': time.time()
        }
        self.send_multicast(election_msg)

        # aguarda respostas por um curto período
        time.sleep(2.0)

        if not self.election_responses:
            # ninguém respondeu — assumimos coordenação
            self.win_election()
        else:
            # alguém respondeu: aguarda anúncio do novo coordenador
            wait_until = time.time() + self.wait_new_coordinator
            while self.election_in_progress and time.time() < wait_until:
                time.sleep(0.2)
            if self.election_in_progress:
                print("[ELEIÇÃO] Nenhum COORDINATOR anunciado no prazo. Vou tentar vencer.")
                self.win_election()

    def handle_election_message(self, message):
        sender_id = int(message['sender_id'])
        election_id = message.get('election_id', '')
        if not election_id:
            election_id = f"legacy-{sender_id}"

        if election_id in self.seen_elections:
            return

        # regra Bully: só responde OK se meu ID for maior (sou “superior”)
        if sender_id < self.node_id:
            ok_msg = {
                'type': 'ELECTION_OK',
                'sender_id': self.node_id,
                'election_id': election_id,
                'timestamp': time.time()
            }
            self.send_multicast(ok_msg)
            self.seen_elections.add(election_id)
            if not self.election_in_progress:
                print(f"[ELEIÇÃO] Recebi eleição de Node {sender_id}, iniciando minha própria.")
                threading.Thread(target=self.start_election, daemon=True).start()

    def handle_election_ok(self, message):
        if not self.election_in_progress:
            return
        if message.get('election_id') != self.current_election_id:
            return
        sender_id = int(message['sender_id'])
        if sender_id not in self.election_responses:
            self.election_responses.append(sender_id)
            print(f"[ELEIÇÃO] Recebi OK de Node {sender_id}")

    def win_election(self):
        print(f"\n{'='*50}")
        print(f"[ELEIÇÃO] Venci a eleição! Tornando-me coordenador...")
        print(f"{'='*50}\n")

        self.is_coordinator = True
        self.coordinator_id = self.node_id
        self.coordinator_addr = (self.local_ip, self.tcp_port)
        self.last_heartbeat = time.time()
        self.election_in_progress = False

        coordinator_msg = {
            'type': 'COORDINATOR',
            'coordinator_id': self.coordinator_id,
            'coordinator_ip': self.local_ip,
            'coordinator_port': self.tcp_port,
            'nodes': {nid: (ip, port, ts) for nid, (ip, port, ts) in self.nodes.items()}
        }
        self.send_multicast(coordinator_msg)
        self.sync_history_with_peers()

    def handle_new_coordinator(self, message):
        new_coord_id = int(message['coordinator_id'])
        print(f"\n[INFO] Novo coordenador eleito: Node {new_coord_id}")
        self.coordinator_id = new_coord_id
        self.coordinator_addr = (message['coordinator_ip'], message['coordinator_port'])
        self.last_heartbeat = time.time()
        self.is_coordinator = (new_coord_id == self.node_id)
        self.election_in_progress = False
        if 'nodes' in message:
            for nid_key, triple in message['nodes'].items():
                nid = int(nid_key) if isinstance(nid_key, str) else int(nid_key)
                ip, port, ts = triple
                self.nodes[nid] = (ip, port, float(ts))

    def sync_history_with_peers(self):
        for node_id, (ip, port, _) in list(self.nodes.items()):
            if node_id != self.node_id:
                request = {'type': 'HISTORY_REQUEST'}
                self.send_tcp(ip, port, request)

    # ------------------------ chat / status / exit ------------------------
    def send_message(self, text):
        if not self.node_id:
            print("[ERRO] Não estou conectado à rede!")
            return
        message = {
            'type': 'CHAT_MESSAGE',
            'sender_id': self.node_id,
            'text': text,
            'timestamp': time.time()
        }
        with self.message_lock:
            self.message_history.append(message)
            self._append_history_local(message)
        for node_id, (ip, port, _) in list(self.nodes.items()):
            if node_id != self.node_id:
                _ = self.send_tcp(ip, port, message)

    def show_status(self):
        self._prune_stale_nodes_local()
        print(f"\n{'='*60}")
        print(f"STATUS DA REDE")
        print(f"{'='*60}")
        print(f"Meu ID: Node {self.node_id}")
        print(f"Meu IP: {self.local_ip}:{self.tcp_port}")
        print(f"Coordenador: Node {self.coordinator_id} {'(EU)' if self.is_coordinator else ''}")
        print(f"Nós ativos: {len(self.nodes)}")
        now = time.time()
        for nid, (ip, port, last_seen) in sorted(self.nodes.items()):
            marker = " (EU)" if nid == self.node_id else ""
            marker += " (COORD)" if nid == self.coordinator_id else ""
            last = datetime.fromtimestamp(last_seen).strftime('%H:%M:%S')
            alive = "OK" if (now - last_seen) <= self.peer_timeout else "STALE"
            print(f"  - Node {nid}: {ip}:{port}{marker}  [visto: {last} | {alive}]")
        print(f"Mensagens no histórico: {len(self.message_history)}")
        print(f"{'='*60}\n")

    def show_history(self):
        print(f"\n{'='*60}")
        print(f"HISTÓRICO DE MENSAGENS ({len(self.message_history)} mensagens)")
        print(f"{'='*60}")
        with self.message_lock:
            for msg in self.message_history:
                sender = msg['sender_id']
                text = msg['text']
                timestamp = datetime.fromtimestamp(msg['timestamp']).strftime('%H:%M:%S')
                print(f"[Node {sender}] ({timestamp}): {text}")
        print(f"{'='*60}\n")

    def exit_network(self):
        # anuncia saída (fluxo limpo)
        if self.node_id is not None:
            exit_msg = {
                'type': 'NODE_EXIT',
                'node_id': self.node_id,
                'timestamp': time.time()
            }
            self.send_multicast(exit_msg)
            if self.is_coordinator:
                print(f"[INFO] Coordenador saindo; rede elegerá novo coordenador.")

        self.running = False

        try:
            if self.multicast_sock:
                self.multicast_sock.close()
        except:
            pass
        try:
            if self.tcp_sock:
                self.tcp_sock.close()
        except:
            pass

    # ------------------------ main loop ------------------------
    def run(self):
        try:
            self.setup_multicast()
            self.setup_tcp()

            threading.Thread(target=self.handle_multicast_messages, daemon=True).start()
            threading.Thread(target=self.handle_tcp_connections, daemon=True).start()
            threading.Thread(target=self.send_heartbeat, daemon=True).start()
            threading.Thread(target=self.send_node_alive, daemon=True).start()
            threading.Thread(target=self.monitor_coordinator, daemon=True).start()
            threading.Thread(target=self.coordinator_watch_peers, daemon=True).start()

            self.join_network()

            print(f"\n{'='*60}")
            print("SISTEMA DE CHAT DISTRIBUÍDO")
            print(f"{'='*60}")
            print("Comandos disponíveis:")
            print("  /status  - Mostra status da rede")
            print("  /history - Mostra histórico de mensagens")
            print("  /exit    - Sair da rede")
            print("  <mensagem> - Envia mensagem para todos")
            print(f"{'='*60}\n")

            while self.running:
                try:
                    message = input("> ")
                    if message == '/exit':
                        self.exit_network()
                        break
                    elif message == '/status':
                        self.show_status()
                    elif message == '/history':
                        self.show_history()
                    elif message.strip():
                        self.send_message(message)
                except KeyboardInterrupt:
                    print("\n[INFO] Saindo...")
                    self.exit_network()
                    break
                except Exception as e:
                    print(f"[ERRO] {e}")
        finally:
            self.exit_network()
            print("[INFO] Chat encerrado.")


def main():
    if len(sys.argv) > 1:
        tcp_port = int(sys.argv[1])
        node = ChatNode(tcp_port=tcp_port)
    else:
        node = ChatNode()
    node.run()

if __name__ == "__main__":
    main()
