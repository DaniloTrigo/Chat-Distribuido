# 🕸️ Trabalho Final — Sistemas Distribuídos 2025.2  
## Chat P2P com Eleição Automática e Histórico Consistente  

---

### 👩‍💻 Aluno
**Danilo Trigo**  
**Curso:** Ciência da Computação — UERJ  
**Disciplina:** Sistemas Distribuídos  
**Linguagem:** Python 3.13.1  
**Ambiente de execução:** Windows 10 / VS Code  

---

## ⚙️ Requisitos Atendidos

| Nº | Requisito | Implementação | Evidência |
|:-:|:------------|:----------------|:-----------|
| 1 | Arquitetura **peer-to-peer**, sem servidor central | Cada nó atua como cliente e servidor TCP | `peer_core.py` cria conexões bidirecionais entre peers |
| 2 | Entrada na rede via **IP multicast** | `discovery.py` usa 224.1.1.1:5007 (`DISCOVER`/`COORD`) | Nós se conectam ao coordenador sem IP prévio |
| 3 | Coordenador atribui IDs e envia **heartbeats** | `coord_node.py` responde DISCOVER e envia `HB` periódico | `[COORD] ouvindo DISCOVER + heartbeat @ 224.1.1.1:5007` |
| 4 | Eleição automática de novo coordenador | Implementado via **algoritmo Bully light** com prioridade = ID/porta | `[ELEIÇÃO] Eu sou o novo coordenador (prio X)` |
| 5 | Tolerância a falhas (reorganização automática) | Heartbeat monitorado; eleição em caso de falha | `[ALERTA] Coordenador inativo (sem heartbeat)` |
| 6 | Histórico consistente em todos os nós | Broadcast causal com **Relógio Vetorial + Anti-entropia** | `/history` idêntico entre nós |
| 7 | Demonstração prática (≥ 4 nós simultâneos) | Executado em 4 terminais locais via loopback | Logs e prints anexados na seção de testes |

---

## 📂 Estrutura do Projeto

TrabalhoFinal_SD/
├── peer_core.py ← Núcleo P2P (TCP + VC + eleição + histórico causal)
├── discovery.py ← Multicast (entrada na rede + heartbeat)
├── coord_node.py ← Inicializa o coordenador
├── node_mcast.py ← Inicializa um nó peer comum
└── README.md ← Este guia completo


---

## ▶️ Execução Local (4 Terminais no VS Code)

Cada processo será um nó da rede P2P.

### 🖥️ Coordenador (Terminal 1)
```powershell
& "E:\Usuários\Danilo Trigo\Desktop\Trabalho Final\.venv\bin\python.exe" -u coord_node.py --host 127.0.0.1 --port 6000 --mcast-ip 224.1.1.1 --mcast-port 5007

Saída esperada:

[COORD] TCP em 127.0.0.1:6000
[COORD] ouvindo DISCOVER + heartbeat @ 224.1.1.1:5007

💬 Nó A (Terminal 2):

& "...\python.exe" -u node_mcast.py --host 127.0.0.1 --nick A --mcast-ip 224.1.1.1 --mcast-port 5007

💬 Nó B (Terminal 3):

& "...\python.exe" -u node_mcast.py --host 127.0.0.1 --nick B --mcast-ip 224.1.1.1 --mcast-port 5007

💬 Nó C (Terminal 4):

& "...\python.exe" -u node_mcast.py --host 127.0.0.1 --nick C --mcast-ip 224.1.1.1 --mcast-port 5007

Saída esperada em cada nó:

Coordenador: 127.0.0.1:6000
Nó local: 127.0.0.1:550xx
[INFO] ID atribuído pelo coordenador: n
[COORD] Entrou: id=n nick=X peer=127.0.0.1:550xx

💬 Comandos disponíveis:

| Comando     | Função                               |
| :---------- | :----------------------------------- |
| texto comum | envia mensagem para todos os nós     |
| `/history`  | exibe o histórico causal convergente |
| `/elect`    | força uma eleição manual             |
| `/leave`    | encerra o nó local                   |



