import socket
import threading
import time
import json
import sys
from datetime import datetime
import pickle

class ChatNode:
    def __init__(self, multicast_group='224.0.0.1', multicast_port=5007, tcp_port=None):
        self.multicast_group = multicast_group
        self.multicast_port = multicast_port
        self.tcp_port = tcp_port if tcp_port else self._find_free_port()
        
        # Node identity
        self.node_id = None
        self.is_coordinator = False
        
        # Network state
        self.coordinator_id = None
        self.coordinator_addr = None
        # {node_id: (ip, tcp_port, last_seen)}
        self.nodes = {}
        
        # Message history
        self.message_history = []
        self.message_lock = threading.Lock()
        
        # Heartbeat control
        self.last_heartbeat = time.time()
        self.heartbeat_timeout = 5.0
        self.heartbeat_interval = 2.0
        
        # Election control
        self.election_in_progress = False
        self.election_responses = []
        
        # Sockets
        self.multicast_sock = None
        self.tcp_sock = None
        self.running = True
        
        # Get local IP
        self.local_ip = self._get_local_ip()

    # ------------------------ util ------------------------
    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"
    
    def _find_free_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port

    def _now_str(self):
        return datetime.now().strftime("%H:%M:%S")

    def _mark_seen(self, nid):
        if nid in self.nodes:
            ip, port, _ = self.nodes[nid]
            self.nodes[nid] = (ip, port, time.time())

    # remove nós obviamente “mortos” quando formos exibir status
    def _prune_stale_nodes(self):
        # margem mais larga que o timeout de heartbeat do coordenador
        stale_limit = self.heartbeat_timeout * 3
        to_remove = []
        now = time.time()
        for nid, (_, _, last_seen) in list(self.nodes.items()):
            if (now - last_seen) > stale_limit and nid != self.coordinator_id:
                to_remove.append(nid)
        for nid in to_remove:
            del self.nodes[nid]
            if self.is_coordinator:
                print(f"[{self._now_str()}][COORD] Removido por inatividade: Node {nid}")

    # ------------------------ sockets ------------------------
    def setup_multicast(self):
        self.multicast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.multicast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.multicast_sock.bind(('', self.multicast_port))
        mreq = socket.inet_aton(self.multicast_group) + socket.inet_aton('0.0.0.0')
        self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self.multicast_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        
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
        except Exception as e:
            print(f"[ERRO] Falha ao enviar TCP para {ip}:{port}: {e}")
            return False

    # ------------------------ join / coord ------------------------
    def join_network(self):
        print(f"\n[INFO] Tentando entrar na rede via multicast {self.multicast_group}:{self.multicast_port}")
        print(f"[INFO] Meu endereço TCP: {self.local_ip}:{self.tcp_port}")
        
        join_request = {
            'type': 'JOIN_REQUEST',
            'ip': self.local_ip,
            'tcp_port': self.tcp_port,
            'timestamp': time.time()
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
        self.coordinator_id = self.node_id
        self.coordinator_addr = (self.local_ip, self.tcp_port)
        self.nodes[self.node_id] = (self.local_ip, self.tcp_port, time.time())
        
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
            'nodes': self.nodes
        }
        self.send_multicast(announcement)

    # ------------------------ multicast handling ------------------------
    def handle_multicast_messages(self):
        while self.running:
            try:
                data, addr = self.multicast_sock.recvfrom(4096)
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
    
    def handle_join_request(self, message):
        if not self.is_coordinator:
            return
        
        new_id = max(self.nodes.keys()) + 1 if self.nodes else 1
        new_ip = message['ip']
        new_port = message['tcp_port']
        
        self.nodes[new_id] = (new_ip, new_port, time.time())
        
        print(f"[{self._now_str()}][COORD] Novo nó entrando: Node {new_id} ({new_ip}:{new_port})")
        
        join_response = {
            'type': 'JOIN_RESPONSE',
            'node_id': new_id,
            'coordinator_id': self.coordinator_id,
            'coordinator_ip': self.local_ip,
            'coordinator_port': self.tcp_port,
            'nodes': self.nodes,
            'message_history': self.message_history
        }
        self.send_tcp(new_ip, new_port, join_response)
        
        # avisa a todos que este nó entrou
        announcement = {
            'type': 'NODE_JOINED',
            'node_id': new_id,
            'ip': new_ip,
            'port': new_port,
            'timestamp': time.time()
        }
        self.send_multicast(announcement)
    
    def handle_coordinator_announcement(self, message):
        self.coordinator_id = message['coordinator_id']
        self.coordinator_addr = (message['coordinator_ip'], message['coordinator_port'])
        self.last_heartbeat = time.time()
        
        if 'nodes' in message:
            for nid, (ip, port, _) in message['nodes'].items():
                self.nodes[int(nid)] = (ip, port, time.time())
    
    def handle_heartbeat(self, message):
        if message.get('coordinator_id') == self.coordinator_id:
            self.last_heartbeat = time.time()
            if 'nodes' in message:
                for nid, (ip, port, _) in message['nodes'].items():
                    self.nodes[int(nid)] = (ip, port, time.time())

    def handle_node_joined(self, message):
        # todos (inclusive o coordenador) atualizam
        nid = int(message.get('node_id'))
        ip = message.get('ip')
        port = int(message.get('port'))
        self.nodes[nid] = (ip, port, time.time())
        # Log amigável para o coordenador
        if self.is_coordinator:
            print(f"[{self._now_str()}][COORD] Confirmação de entrada: Node {nid} ({ip}:{port})")

    def handle_node_exit(self, message):
        exit_node_id = message.get('node_id')
        if exit_node_id in self.nodes:
            ip, port, _ = self.nodes[exit_node_id]
            del self.nodes[exit_node_id]
            if self.is_coordinator:
                print(f"[{self._now_str()}][COORD] Nó saiu: Node {exit_node_id} ({ip}:{port})")
            else:
                print(f"\n[INFO] Node {exit_node_id} saiu da rede")
    
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
            conn.close()
    
    def handle_join_response(self, message):
        self.node_id = message['node_id']
        self.coordinator_id = message['coordinator_id']
        self.coordinator_addr = (message['coordinator_ip'], message['coordinator_port'])
        
        for nid, (ip, port, _) in message['nodes'].items():
            self.nodes[int(nid)] = (ip, port, time.time())
        
        with self.message_lock:
            self.message_history = message.get('message_history', [])
        
        self.last_heartbeat = time.time()
    
    def handle_chat_message(self, message):
        sender_id = message.get('sender_id')
        if sender_id:
            self._mark_seen(sender_id)

        with self.message_lock:
            msg_id = (message['sender_id'], message['timestamp'])
            if not any(m['sender_id'] == message['sender_id'] and 
                      m['timestamp'] == message['timestamp'] 
                      for m in self.message_history):
                self.message_history.append(message)
                
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
                if not any(m['sender_id'] == msg['sender_id'] and 
                          m['timestamp'] == msg['timestamp'] 
                          for m in self.message_history):
                    self.message_history.append(msg)
            self.message_history.sort(key=lambda x: x['timestamp'])

    # ------------------------ heartbeats / election ------------------------
    def send_heartbeat(self):
        while self.running:
            if self.is_coordinator:
                heartbeat = {
                    'type': 'HEARTBEAT',
                    'coordinator_id': self.coordinator_id,
                    'timestamp': time.time(),
                    'nodes': self.nodes
                }
                self.send_multicast(heartbeat)
            time.sleep(self.heartbeat_interval)
    
    def monitor_coordinator(self):
        while self.running:
            time.sleep(1)
            if not self.is_coordinator and self.node_id:
                time_since_heartbeat = time.time() - self.last_heartbeat
                if time_since_heartbeat > self.heartbeat_timeout:
                    print(f"\n[ALERTA] Coordenador não responde há {time_since_heartbeat:.1f}s")
                    print("[INFO] Iniciando eleição...")
                    self.start_election()
    
    def start_election(self):
        if self.election_in_progress:
            return
        
        self.election_in_progress = True
        self.election_responses = []
        
        higher_nodes = [nid for nid in self.nodes.keys() if nid > self.node_id]
        if not higher_nodes:
            self.win_election()
            return
        
        print(f"[ELEIÇÃO] Enviando mensagem de eleição para nós superiores: {higher_nodes}")
        election_msg = {
            'type': 'ELECTION',
            'sender_id': self.node_id,
            'timestamp': time.time()
        }
        self.send_multicast(election_msg)
        
        time.sleep(2)
        if not self.election_responses:
            self.win_election()
        else:
            print(f"[ELEIÇÃO] Recebi respostas, aguardando novo coordenador...")
            self.election_in_progress = False
    
    def handle_election_message(self, message):
        sender_id = message['sender_id']
        if sender_id < self.node_id:
            ok_msg = {
                'type': 'ELECTION_OK',
                'sender_id': self.node_id,
                'timestamp': time.time()
            }
            self.send_multicast(ok_msg)
            if not self.election_in_progress:
                print(f"[ELEIÇÃO] Recebi eleição de Node {sender_id}, iniciando minha própria eleição")
                threading.Thread(target=self.start_election, daemon=True).start()
    
    def handle_election_ok(self, message):
        sender_id = message['sender_id']
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
            'nodes': self.nodes
        }
        self.send_multicast(coordinator_msg)
        self.sync_history_with_peers()
    
    def handle_new_coordinator(self, message):
        new_coord_id = message['coordinator_id']
        if new_coord_id >= self.node_id:
            print(f"\n[INFO] Novo coordenador eleito: Node {new_coord_id}")
            self.coordinator_id = new_coord_id
            self.coordinator_addr = (message['coordinator_ip'], message['coordinator_port'])
            self.last_heartbeat = time.time()
            self.election_in_progress = False
            self.is_coordinator = (new_coord_id == self.node_id)
            if 'nodes' in message:
                for nid, (ip, port, _) in message['nodes'].items():
                    self.nodes[int(nid)] = (ip, port, time.time())
    
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
        
        for node_id, (ip, port, _) in list(self.nodes.items()):
            if node_id != self.node_id:
                self.send_tcp(ip, port, message)
    
    def show_status(self):
        # Antes de exibir, removemos entradas obviamente defasadas
        self._prune_stale_nodes()

        print(f"\n{'='*60}")
        print(f"STATUS DA REDE")
        print(f"{'='*60}")
        print(f"Meu ID: Node {self.node_id}")
        print(f"Meu IP: {self.local_ip}:{self.tcp_port}")
        print(f"Coordenador: Node {self.coordinator_id} {'(EU)' if self.is_coordinator else ''}")
        print(f"Nós ativos: {len(self.nodes)}")
        for nid, (ip, port, last_seen) in sorted(self.nodes.items()):
            marker = " (EU)" if nid == self.node_id else ""
            marker += " (COORD)" if nid == self.coordinator_id else ""
            last = datetime.fromtimestamp(last_seen).strftime('%H:%M:%S')
            print(f"  - Node {nid}: {ip}:{port}{marker}  [visto: {last}]")
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
        # Todos anunciam saída (antes só o coordenador fazia)
        if self.node_id is not None:
            exit_msg = {
                'type': 'NODE_EXIT',
                'node_id': self.node_id,
                'timestamp': time.time()
            }
            self.send_multicast(exit_msg)
            if self.is_coordinator:
                print(f"[INFO] Coordenador saindo, rede irá eleger novo coordenador...")

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
            threading.Thread(target=self.monitor_coordinator, daemon=True).start()
            
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
