# JARVIS — Fase 3: Proposta Arquitetural

**Status:** proposta (não implementada).
**Data:** 2026-09-30.
**Base:** estado real do código em `~/workspace/jarvis` após a Fase 2.1
(HEAD `3a1c9bf` + hardening F1; 265 testes verdes).

---

## 1. Objetivo

**Tornar o JARVIS conversável e utilizável localmente.**

O primeiro grande milestone é o fluxo completo:

```text
abrir navegador
      ↓
interface local do JARVIS
      ↓
criar sessão
      ↓
enviar mensagem
      ↓
JARVIS processa
      ↓
responder
```

A primeira interface deve ser **deliberadamente simples**: uma página,
uma caixa de texto, um histórico. Nada de orb 3D, voz, ou automações.

---

## 2. Ponto de partida real (o que já existe)

A Fase 3 não começa do zero. O backend já expõe tudo que a UI precisa:

| Recurso | Endpoint existente | Arquivo |
|---|---|---|
| Criar sessão | `POST /api/v1/sessions` | `src/jarvis/api/main.py:269` |
| Enviar mensagem | `POST /api/v1/sessions/{id}/messages` | `src/jarvis/api/main.py:275` |
| Cancelar task | `POST /api/v1/tasks/{id}/cancel` | `src/jarvis/api/main.py:289` |
| Memória (CRUD/lifecycle) | `GET/POST/DELETE /api/v1/memory...` | `src/jarvis/api/main.py:305-387` |
| Commitments | `GET/POST /api/v1/commitments...` | `src/jarvis/api/main.py:407-448` |
| Health/readiness | `GET /health`, `GET /ready`, `GET /version` | `src/jarvis/api/main.py:248-257` |

- Servidor local: `127.0.0.1:8123` (`JARVIS_PORT`, `src/jarvis/config.py`).
- Pipeline completo: `Orchestrator` → `Policy` → `NexusIntegration` (HTTP) →
  `ContextBuilder` → `LLMProvider` → renderer determinístico pt-BR.
- `FakeLLMProvider` funciona hoje; `OpenAIProvider` existe mas nunca foi
  exercitado com chave real (pendência operacional, não arquitetural).
- Segurança da Fase 2/2.1: deny-by-default, `scan_for_secrets` pré-persistência
  em memórias **e** commitments, audit append-only, memória rotulada
  não-verificada (`MemoryCtx.verified=False`), NEXUS somente GET loopback.

**Decisão central da Fase 3:** a UI é uma camada fina de apresentação.
Toda inteligência, memória, policy e contexto continuam no backend existente.

---

## 3. Arquitetura proposta

```text
┌─────────────┐      HTTP (loopback)      ┌──────────────────────────────┐
│  Web UI     │ ───────────────────────▶ │  JARVIS API (existente)      │
│  (estática) │ ◀─────────────────────── │  127.0.0.1:8123              │
└─────────────┘      JSON                 └──────────────┬───────────────┘
                                                       │
                                              ┌────────▼────────┐
                                              │  Application    │
                                              │  Orchestrator   │
                                              │  Policy / Audit │
                                              │  Memory/Context │
                                              └────────┬────────┘
                                                       │  (port)
                                              ┌────────▼────────┐
                                              │  LLM Provider   │
                                              │  (fake → real)  │
                                              └─────────────────┘
```

Regras inegociáveis:

1. A UI **nunca** fala com o provider de LLM diretamente.
2. A UI **nunca** acessa o banco, o NEXUS ou o filesystem diretamente.
3. A UI **nunca** possui API key do provider.
4. A UI **não** duplica lógica de backend: ela chama os endpoints
   existentes e renderiza as respostas.
5. Toda a segurança das Fases 1–2.1 continua valendo sem exceção
   (deny-by-default, scan de secrets, audit, memória ≠ policy).

---

## 4. Interface web local (F3.1)

UI mínima servida pelo próprio processo JARVIS (arquivos estáticos,
sem build step, sem framework pesado):

- Iniciar sessão (`POST /api/v1/sessions`).
- Mostrar mensagens (histórico da sessão).
- Enviar mensagem (`POST /api/v1/sessions/{id}/messages`).
- Mostrar estado da requisição (ver §7).
- Mostrar erros (usar `message_key` + `code` do payload de erro da API).
- Cancelar processamento em andamento (`POST /api/v1/tasks/{id}/cancel`).
- Encerrar/reiniciar sessão (nova sessão = novo `session_id`).

**Fora do escopo da F3.1:** autenticação multi-usuário, persistência de UI,
temas, markdown rico, streaming (polling simples é suficiente no início).

---

## 5. Sessão (F3.2)

| Aspecto | Definição |
|---|---|
| Criação | `POST /api/v1/sessions` retorna `session_id`; a UI guarda em memória da página. |
| Identificação | `session_id` opaco (UUID); sem login na Fase 3. |
| Histórico | `GET` de mensagens da sessão (endpoint a confirmar/derivar do existente; se não existir, adicionar `GET /api/v1/sessions/{id}/messages` — leitura pura, sem policy). |
| Lifecycle | sessão = contêiner de mensagens; tasks vivem dentro da sessão e seguem a state machine existente. |
| Reload da página | a UI recria a sessão ou reanexa pelo `session_id` guardado (decisão de UX; o backend não exige nada). |
| Erro | payload `{error: {code, message_key, correlation_id}}`; a UI exibe texto amigável mapeado de `message_key`, nunca stack trace. |
| Cancelamento | botão "cancelar" → `POST /api/v1/tasks/{id}/cancel`; estado `CANCELLED` é terminal. |

---

## 6. LLM real (F3.4)

Separação de camadas (já existe no código; a Fase 3 só liga a chave):

```text
UI
 ↓  HTTP /api/v1/*
API
 ↓  chamadas internas
Application (Orchestrator, Policy, ContextBuilder)
 ↓  LLM port (LLMProvider)
Provider (FakeLLMProvider → OpenAIProvider com chave via Secure Vault)
```

- A UI nunca vê a chave; a chave vive na configuração do servidor.
- O provider real entra **atrás do port existente**, sem mudar o contrato.
- Critério de aceite: mesma conversa funciona com `fake` e com o provider
  real; nenhuma decisão de policy/tool passa pelo LLM (invariante 3).

---

## 7. Memória e contexto (F3.5)

- A UI **reutiliza** a memória existente: comandos como "lembre-se de X"
  continuam fluindo pelo intent `MEMORY_WRITE` → `MemoryService` (com
  `scan_for_secrets`).
- **Não criar** segundo banco, segundo mecanismo de memória, nem cache
  paralelo na UI.
- A UI pode exibir memórias via `GET /api/v1/memory` (somente leitura);
  mutações de lifecycle (confirmar/revogar) continuam exigindo o fluxo
  existente, nunca atalho da UI.
- Verificação: a conversa utiliza o `ContextSnapshot` já implementado
  (memórias rotuladas não-verificadas, budgets, digest).

---

## 8. Demonstração NEXUS (F3.6)

O primeiro fluxo real de ponta a ponta:

```text
Usuário: "Como está o NEXUS?"
  ↓
UI → POST /api/v1/sessions/{id}/messages
  ↓
Orchestrator → Policy (nexus.status.read) → NexusIntegration (GET /api/v1/*)
  ↓
Verification → Context → LLM → renderer determinístico
  ↓
UI exibe a resposta
```

- Reutiliza a infraestrutura existente; **nenhuma nova capability**.
- Nenhuma escrita no NEXUS (fora de escopo permanente da Fase 3).
- Se o NEXUS estiver fora do ar, a UI exibe "NEXUS indisponível"
  (fallback determinístico existente) — nunca dado inventado.

---

## 9. Observabilidade mínima da UI (F3.7)

Estados da requisição na interface (sem orb):

```text
IDLE → PROCESSING → COMPLETED
                ↘ ERROR
                ↘ CANCELLED
                ↘ WAITING (quando aplicável, ex.: aguardando NEXUS)
```

- Loading visível durante `PROCESSING`.
- Botão de cancelamento durante `PROCESSING`.
- Erros exibidos com `message_key` amigável + `correlation_id`
  (útil para depuração, sem vazar detalhes internos).

O orb visual (IDLE/LISTENING/THINKING/EXECUTING/SPEAKING/ERROR) continua
sendo ideia futura — **não** entra na Fase 3.

---

## 10. Segurança da UI

A UI não pode:

- executar shell ou acessar filesystem arbitrário;
- acessar o NEXUS diretamente (sempre via backend);
- acessar o SQLite do JARVIS diretamente;
- possuir a API key do provider;
- bypassar Policy ou Orchestrator;
- injetar instruções via memória (memória continua sendo dado, nunca
  instrução — invariantes 4/5/6).

Todo input da UI passa pelo mesmo pipeline validado das Fases 1–2.1,
incluindo `scan_for_secrets` em memórias e commitments.

---

## 11. Milestones

### F3.1 — Web shell
Servir página estática mínima pelo processo JARVIS; provar
navegador → `GET /health`.

### F3.2 — Session flow
Criar sessão e enviar a primeira mensagem via UI;
exibir a resposta do renderer determinístico.

### F3.3 — Conversation rendering
Histórico correto (ordem, papéis user/assistant, erros, cancelamentos).

### F3.4 — Real LLM provider
Ligar `OpenAIProvider` atrás do port com chave segura; mesma conversa
funciona nos dois providers; policy/tools intocados pelo LLM.

### F3.5 — Memory/context verification
"Lembre-se de X" pela UI → memória persiste → aparece rotulada como
não-verificada no contexto; `GET /api/v1/memory` exibe.

### F3.6 — NEXUS demonstration
"Como está o NEXUS?" de ponta a ponta pela UI, com NEXUS real no ar;
fallback honesto quando indisponível.

### F3.7 — UX hardening
Loading, cancelamento, erros amigáveis, estados da §9; sem orb.

Um milestone por vez. Nenhum começa antes do anterior estar verde
(testes + gates).

---

## 12. Riscos

| # | Risco | Mitigação |
|---|---|---|
| R1 | UI vira "segundo backend" (lógica duplicada) | regra: UI só chama endpoints e renderiza; revisão de diff por milestone |
| R2 | Tentação de atalhos de UX que bypassam policy | nenhum endpoint novo de escrita sem passar por `Orchestrator`/`MemoryService`/`CommitmentService` |
| R3 | Chave do provider vazando para o browser | chave só no servidor; UI nunca a recebe nem a referencia |
| R4 | Escopo inchando (voz, orb, agentes) | lista explícita do que NÃO será feito (§13); milestone travado |
| R5 | Smoke do NEXUS continua pendente | F3.6 exige NEXUS no ar; até lá, fallback determinístico é o comportamento correto |

---

## 13. O que NÃO será feito na Fase 3

- orb 3D ou qualquer interface visual além da página mínima;
- voz, speech-to-text, text-to-speech;
- visão / screen awareness;
- agentes ou multi-agent;
- browser automation;
- shell / filesystem tools;
- automações, proatividade, agendadores;
- plugins ou ferramentas arbitrárias;
- execução de comandos;
- exposição pública (continua `127.0.0.1`, sem túnel);
- autenticação multi-usuário;
- escrita no NEXUS;
- integração real com o Clone Cobrador;
- novo banco ou novo sistema de memória.

---

## 14. Critérios de aceite da Fase 3

1. Abrir `http://127.0.0.1:8123` (ou a porta configurada) mostra a UI.
2. Criar sessão, enviar "olá" e receber resposta — sem erros.
3. "Lembre-se de que eu gosto de café" → memória persiste e é
   reexibida como não-verificada (sem segredo, sem bypass).
4. "Como está o NEXUS?" com NEXUS no ar → dados reais verificados;
   com NEXUS fora do ar → "indisponível", nunca inventado.
5. Cancelar uma requisição em andamento funciona; estados terminais
   permanecem imutáveis.
6. Todos os gates verdes: `pytest`, `ruff check`, `ruff format --check`,
   `mypy strict`.
7. Nenhuma das proibições da §13 foi violada.
