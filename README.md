# Chat Distribuído P2P (Multicast + TCP) com Eleição Bully

Um chat **peer-to-peer** simples (sem servidor central) que:
- Descobre nós via **UDP Multicast**
- Entrega mensagens e sincroniza histórico via **TCP**
- Elege automaticamente um **coordenador** (algoritmo **Bully**)
- Detecta **quedas abruptas** (kill/fechar terminal) e remove nós inativos
- Mantém **estado e histórico por nó** em disco (reutiliza a identidade e tenta recuperar o mesmo `node_id`)

> Projeto acadêmico: foco em clareza e robustez suficiente para laboratório/local.

---

## Sumário
- [Arquitetura](#arquitetura)
- [Requisitos](#requisitos)
- [Como Executar](#como-executar)
- [Comandos](#comandos)
- [Persistência em Disco](#persistência-em-disco)
- [Protocolo](#protocolo)
- [Eleição (Bully)](#eleição-bully)
- [Tolerância a Falhas](#tolerância-a-falhas)
- [Cenários de Teste](#cenários-de-teste)
- [Parâmetros e Timeouts](#parâmetros-e-timeouts)
- [Boas Práticas (.gitignore)](#boas-práticas-gitignore)
- [Troubleshooting](#troubleshooting)
- [Roadmap (idéias de melhoria)](#roadmap-idéias-de-melhoria)
- [Licença](#licença)

---

## Arquitetura

- **Multicast (UDP)**  
  Descoberta e controle: `JOIN_REQUEST`, `HEARTBEAT`, `NODE_ALIVE`, `NODE_JOINED`, `NODE_EXIT`, `ELECTION`, `ELECTION_OK`, `COORDINATOR`.

- **TCP**  
  Canal confiável ponto-a-ponto para: `JOIN_RESPONSE`, `CHAT_MESSAGE`, `HISTORY_REQUEST`, `HISTORY_RESPONSE`.

- **Coordenador**  
  Mantém a tabela de nós (ID → `ip:porta:last_seen`), envia **heartbeats**, remove **peers inativos** e anuncia saídas.

- **Nós comuns**  
  Enviam presença periódica (**`NODE_ALIVE`**) e conversam via TCP.

---

## Requisitos

- **Python 3.10+** (usa só biblioteca padrão)
- Multicast habilitado na rede/host (em Windows/Linux/macOS)
- Um terminal por nó (para simular vários peers no mesmo computador)

---

## Como Executar

Abra um terminal por nó e rode com uma **porta TCP** (recomendado):
```bash
# Nó 1 (pode virar coordenador)
python chat_node.py 6000

# Nó 2
python chat_node.py 6001

# Nó 3
python chat_node.py 6002
```

Sem argumento, a aplicação seleciona uma porta livre automaticamente:
```bash
python chat_node.py
```

> Dica (Windows PowerShell): execute como “Administrador” se tiver problemas com multicast.

---

## Comandos

Dentro do programa (prompt `> `):

- **Texto livre** → envia mensagem para todos
- **`/status`** → mostra nós ativos com “visto” (timestamp) e status **OK/STALE**
- **`/history`** → imprime o histórico local conhecido
- **`/exit`** → saída limpa (anuncia `NODE_EXIT`)

---

## Persistência em Disco

Cada nó mantém **identidade** e **histórico** no disco, por nó (baseado em **UUID**):

```
./data/<node_uuid>/
  ├─ state.json        # { "node_uuid": "...", "last_node_id": N }
  └─ history.jsonl     # mensagens (1 linha JSON por mensagem)
```

- Ao reiniciar na **mesma porta**, o nó envia `desired_id = last_node_id` e tenta **reclamar o mesmo ID**.  
- Se o coordenador confirmar que aquele ID pertence ao mesmo `node_uuid` (ou está livre), ele **atribui de volta**; caso contrário, o nó recebe um novo ID.

> Existe migração automática de formatos legados `state_<porta>.json`/`history_<porta>.jsonl` para a estrutura nova (por pasta).

---

## Protocolo

### Mensagens Multicast (JSON)
- `JOIN_REQUEST { ip, tcp_port, desired_id, node_uuid, timestamp }`
- `COORDINATOR_ANNOUNCEMENT { coordinator_id, coordinator_ip, coordinator_port, nodes }`
- `HEARTBEAT { coordinator_id, nodes, timestamp }`
- `NODE_ALIVE { node_id, ip, port, timestamp }`
- `NODE_JOINED { node_id, ip, port, timestamp }`
- `NODE_EXIT { node_id, timestamp }`
- `ELECTION { sender_id, election_id, timestamp }`
- `ELECTION_OK { sender_id, election_id, timestamp }`
- `COORDINATOR { coordinator_id, coordinator_ip, coordinator_port, nodes }`

### Mensagens TCP (pickle)
- `JOIN_RESPONSE { node_id, coordinator_*, nodes, message_history }`
- `CHAT_MESSAGE { sender_id, text, timestamp }`
- `HISTORY_REQUEST / HISTORY_RESPONSE { message_history }`

---

## Eleição (Bully)

1. Se um nó **não recebe heartbeat** do coordenador por `heartbeat_timeout` (padrão: **5s**), inicia eleição.
2. Envia `ELECTION` para **nós com ID maior**.
3. Quem for “superior” responde `ELECTION_OK` e inicia sua própria eleição.
4. Sem respostas, o iniciador **vence** e anuncia `COORDINATOR`.

Proteções:
- **`election_id`** por rodada para evitar loops/duplicações
- **Debounce** para não disparar eleições em sequência

---

## Tolerância a Falhas

- **Saída limpa**: `/exit` emite `NODE_EXIT`; todos atualizam a view.
- **Queda abrupta** (kill/fechar terminal): ausência de `NODE_ALIVE`/atualizações leva:
  - **Coordenador** a remover o nó após `peer_timeout` (padrão **8s**) e anunciar `NODE_EXIT`.
  - **Nós não coordenadores** exibem **STALE** no `/status` até a tabela ser atualizada por heartbeat/anúncio.
- **Reentrada**: ao voltar, o nó tenta **reclamar o ID anterior** (por `node_uuid` + `desired_id`).

---

## Cenários de Teste

1) **Entrada/descoberta**  
Abra 2–3 nós; veja o coordenador e IDs atribuídos com `/status`.

2) **Mensagens**  
Envie mensagens entre nós; confira no `/history`.

3) **Queda do coordenador**  
Mate (kill) o terminal do coordenador → após ~5s inicia eleição; um novo coordenador é anunciado.

4) **Queda de um nó comum**  
Mate um nó não coordenador → após ~8s o coordenador remove e anuncia `NODE_EXIT`; `/status` não deve mostrá-lo como **OK**.

5) **Reentrada com o mesmo ID**  
Reabra o nó na **mesma porta** → ele tenta recuperar o `node_id` anterior (se o coordenador permitir, pela checagem de `node_uuid`).

---

## Parâmetros e Timeouts (valores padrão)

```text
heartbeat_interval   = 2.0s   # Coordenador envia HEARTBEAT
heartbeat_timeout    = 5.0s   # Detectar coordenador inativo
peer_alive_interval  = 2.0s   # Todos enviam NODE_ALIVE
peer_timeout         = 8.0s   # Remover peer inativo
```

> Ajustes ficam no próprio `chat_node.py` (procure pelas variáveis acima).

---

## Boas Práticas (.gitignore)

Para evitar subir dados locais/ambiente:
```gitignore
__pycache__/
.venv/
.env
data/
logs/
state_*.json
history_*.jsonl
```

---

## Troubleshooting

- **“Nada aparece/ninguém se encontra”**  
  Verifique firewall e suporte a **UDP Multicast** na máquina/rede. Teste tudo no mesmo host primeiro.

- **Eleições em loop**  
  Confira se os relógios do sistema não estão muito fora e se os timeouts não foram reduzidos demais.

- **Latência lenta para remover nós**  
  Diminua `peer_timeout` (com cuidado para não gerar falsos positivos).

- **Conflitos de ID após reentrada**  
  Confirme que a pasta do nó (em `./data/<node_uuid>/state.json`) aponta para o `last_node_id` correto. O coordenador só concede de volta se o `node_uuid` bater.

---

## Roadmap (idéias de melhoria)

- `./data/<uuid>/node.db` com **SQLite** (histórico/estado com consultas e atomicidade)
- **Criptografia** no TCP (TLS) e assinatura de mensagens
- **Resend/ACK** para *at-least-once* e “buracos” de mensagens
- Flags de CLI para timeouts/endereços multicast

---

## Licença

Uso acadêmico/educacional. Ajuste para a licença que preferir (MIT/BSD/GPL).
