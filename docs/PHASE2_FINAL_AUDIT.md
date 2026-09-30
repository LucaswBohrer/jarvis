# JARVIS — Fase 2: Auditoria Final Independente

**Data:** 2026-09-30 (America/Sao_Paulo, -03)
**Escopo:** implementação da Fase 2 (Memory + Context + Commitments), commit `3a1c9bf`, branch `master`
**Natureza:** somente leitura e análise. Nenhum código, migration, banco, API, NEXUS ou Clone Cobrador foi alterado.
**Método:** código-fonte + migrations + testes + histórico Git confrontados diretamente. Relatórios anteriores (PHASE1_AUDIT_REPORT.md, PHASE2_ARCHITECTURE_REVIEW.md, PHASE2_PROPOSAL.md) foram tratados como alegações, nunca como prova.

**Runs próprios desta auditoria (2026-09-30):**

| Verificação | Resultado |
|---|---|
| `pytest tests/ -q` | **255 passed** em 44.96s (111 unit + 134 integration + 10 e2e) |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 108 files already formatted |
| `mypy src` | Success: no issues found in 62 source files |
| alembic upgrade 0002→0004 em DB temporário | OK |
| alembic downgrade 0004→0003 em DB temporário | OK |
| alembic re-upgrade → head em DB temporário | OK |
| DB real `data/jarvis.db` | intocado, permanece na revisão 0002 |

---

## 1. Executive Summary

A Fase 2 implementa memória tipada, montagem de contexto rotulada e compromissos (commitments) sobre a base da Fase 1, mantendo todas as garantias arquiteturais: política deny-by-default, auditoria append-only, LLM fora do caminho de autorização, fronteira rígida com o NEXUS e ausência de ações externas autônomas.

**Verificação dos números publicados:** os 255 testes foram reproduzidos nesta auditoria (255 passed). A decomposição "98 Fase 1 + 157 novos" **não pôde ser verificada via Git** — o repositório contém um único commit (`3a1c9bf`), sem histórico que separe Fase 1 de Fase 2. O total de 255 e a ausência de regressões (todos os testes de comportamento da Fase 1 verdes) foram confirmados empiricamente.

**Achados:** nenhum achado de severidade HIGH. Um achado MEDIUM (commitments não passam por `scan_for_secrets`, com caminho de vazamento via cauda de conversa para o provider LLM), cinco achados LOW e três INFO. Nenhum deles invalida a arquitetura.

**Vereditos por área:** 14 áreas PASS, 4 áreas PASS WITH FOLLOW-UP (§6 Retrieval/FTS5, §9 Commitments, §14 Test/Guarantee, §16 Complexity), 0 FAIL.

---

## 2. Repository State

- Branch: `master`. HEAD: `3a1c9bf61a55c52cdbcb18f40e6425c9b5d3eb37` ("feat: implement phase 2 memory context and commitments").
- `git status --porcelain`: limpo antes e depois desta auditoria (única adição final: este relatório).
- 114 arquivos rastreados. `git ls-files` não contém `.env`, `.db`, `.venv`, chaves ou certificados — nenhum secret versionado.
- **Limitação de verificação:** há apenas 1 commit no histórico; não existe linhagem Git que isole o que é "Fase 1" vs "Fase 2". A alegação "98 + 157" é aceita apenas como relato da implementação, não como fato auditável.
- Fronteiras externas intactas: NEXUS não tocado (verificação da missão anterior, HEAD `f69a857`); Clone Cobrador sem integração real (ver §10).

---

## 3. Architecture Compliance

Confrontado contra `docs/PHASE2_ARCHITECTURE_REVIEW.md` (§12, passos T1–T20) e o código real.

**4 regras de memória — todas implementadas:**
1. Contratos: `MemoryKind`/`Provenance`/`MemoryStatus`/`Sensitivity` como enums fechados; `MemoryItem`/`MemoryQuery`/`MemoryHit` frozen Pydantic v2 com `extra="forbid"` (`domain/contracts/memory.py`); `TERMINAL_STATUSES` declarado.
2. `scan_for_secrets()` pré-persistência no pipeline do `MemoryService` (`application/memory_service.py`).
3. Lifecycle de 6 estados com mapa de transições validado (`_TRANSITIONS`, `domain/contracts/memory.py`).
4. `origin="user_explicit_command"` exigido por literal no service e por `require_origin` em `config/policy.toml`.

**4 regras de contexto — todas implementadas:**
1. `ContextSnapshot` imutável com digest SHA-256 sobre payload canônico excluindo ids/timestamps (`application/context_builder.py`).
2. Memórias rotuladas como não-verificadas: `MemoryCtx.verified: Literal[False]` travado no tipo (`domain/contracts/context.py`).
3. Sensíveis fora do objeto real: `sections_for_llm` exclui `sensitive`; `build_llm_request` omite a seção sensível do `LLMRequest` (`application/context_service.py`).
4. Builder sem tools: `ContextBuilder` é montador puro, sem I/O; T9 usa mocks explosivos (`tests/unit/test_context_builder.py::test_t9_builder_never_touches_tools_or_io`).

**4 regras de commitments — todas implementadas:**
1. Lifecycle `open → fulfilled/expired/cancelled`, terminais imutáveis (`domain/contracts/commitments.py`, `transition_commitment` pura).
2. Sweep idempotente com cooldown de 1h em `service_meta` (`application/commitments.py::sweep`).
3. Surface-only in-conversation: sem ação externa; `mark_surfaced` na mesma tx da resposta (D42).
4. Somente `LocalCommitmentSource` (`ports/commitments.py`, `adapters/commitments/local.py`).

**3 regras NEXUS — todas implementadas:**
1. `nexus` config inalterada (host default `127.0.0.1` com validador `_host_loopback`).
2. Adapter somente GET sob `/api/v1/`, `follow_redirects=False`, `trust_env=False`.
3. Guards D22: `database.py` rejeita qualquer URL que referencie o banco do NEXUS; zero `import nexus` em `src/` e `tests/`.

**policy.toml:** `policy_version="2"`, exatamente 3 regras (`nexus-status-read` inalterada, `memory-write-local` com `require_origin="user_explicit_command"`, `memory-read-local`). Nenhum sentinel novo; método/URL sentinel `INTERNAL` para tools locais. Deny-by-default confirmado em `security/policy.py` (sem match = DENY; precedência DENY > CONFIRM > ALLOW).

**Arquivos previstos:** `memory_service.py` contém `scan_for_secrets()` no pipeline; `context_builder.py` e `fts.py` criados; `MemoryKind`/`Provenance` em `domain/contracts/memory.py`.

**Veredito da seção: PASS** (ressalvas menores registradas nas seções específicas).

---

## 4. Memory Audit

Verificado em `domain/contracts/memory.py`, `application/memory_service.py`, `adapters/persistence/repositories.py`.

1. **Contratos frozen:** `MemoryItem`, `MemoryQuery`, `MemoryHit` são Pydantic v2 frozen com `extra="forbid"` (via `FrozenModel`); `AwareDatetime`; `schema_version=2`.
2. **Sem mutação pelo LLM:** o único caminho de escrita é `MemoryService.create`/`set_status`/`supersede`/`confirm`/`revoke`/`delete`/`purge`, invocados pelo orquestrador a partir de parse determinístico do texto do usuário ou pelos endpoints da API. Nenhum caminho aceita item construído pelo LLM.
3. **Transições validadas:** `_TRANSITIONS` permite `active↔pending`, `active→superseded/expired/revoked/deleted`, `pending→revoked`; terminais nunca transitam (`TERMINAL_STATUSES`).
4. **`set_status` check-and-set real:** `UPDATE ... WHERE status IN (expected_from)`; 0 linhas afetadas → erro `MEMORY_CONFLICT`. Race de dois writers resulta em um vencedor, sem overwrite silencioso.
5. **`supersede` não-destrutivo:** insere o sucessor (com `superseded_by` do alvo) e só então linka o alvo, na mesma transação; conteúdo do alvo preservado.
6. **`purge` exige tombstone:** `purge` só aceita item com status `deleted`; itens revogados nunca podem ser apagados fisicamente (D32 — revogação é evidência, mantida para sempre).
7. **`expire` automático:** sweep expira `active` com `valid_until <= now` (limit 500/passada).
8. **`confirm` com provenance:** `pending → active` exige `USER_EXPLICIT`; proveniência `LLM_INFERRED` não pode ser confirmada para `USER_EXPLICIT`.
9. **`revoke`/`delete` com auditoria:** `revoke` mantém o registro (tombstone lógico); `delete` é tombstone, não remoção física; ambos emitem eventos.
10. **Sem "ressuscitar":** nenhum caminho permite sair de status terminal; `purge` exige `deleted`; `set_status` rejeita terminais.

**Veredito da seção: PASS.**

---

## 5. Memory Security

1. **policy.toml:** 3 regras, `policy_version="2"`; escrita em memória exige `origin="user_explicit_command"`; deny-by-default para qualquer tool sem regra.
2. **`memory.write` deny-by-default:** `MemoryService._authorize` decide via policy antes de qualquer persistência; sem `origin` literal válido → exceção.
3. **Origin user-explicit:** o literal `"user_explicit_command"` é exigido no service (`_require_origin`) e fixado pelo orquestrador/API (`USER_EXPLICIT_ORIGIN`); o contrato `MemoryWriteArgs.origin` não tem default.
4. **`scan_for_secrets`:** pura, sem I/O; 8 padrões (openai_api_key, github_token, aws_access_key, aws_secret_key, slack_token, private_key, bearer_token, credential_assignment); retorna apenas categorias (`SecretHit.category`), nunca o valor; bloqueio antes da persistência com auditoria `memory.write_blocked` durável.
5. **LLM nunca autoriza tools:** o plano do LLM é `NexusResponsePlan` (fatos+recomendações), nunca `ToolRequest`; autorização é determinística no orquestrador/service.
6. **Destrutivas inexistentes:** não há comando de "apagar tudo"; `recognize_intent("apague todas as memórias")` retorna `None` (teste dedicado).
7. **D31:** decisões de policy e bloqueios de segredo são auditados em transação própria, fora da transação de escrita (ver §11).
8. **Validação desta seção:** adversarial coberta por `tests/integration/test_memory_authority.py` (7 testes, todos verdes nesta auditoria).

**Veredito da seção: PASS.**

---

## 6. Retrieval/FTS5 Audit

Verificado em `adapters/persistence/fts.py`, `adapters/persistence/repositories.py::retrieve`, `migrations/0004_memory_fts.py`.

1. **FTS5 ativo e íntegro:** `memory_fts(item_id, title, content, tokenize='unicode61 remove_diacritics 2')`; tabela externa de conteúdo; triggers `memory_fts_ai/ad/au` verificados no schema real após migração.
2. **`retrieve` read-only:** conexão dedicada com timeout; `CancelledError` re-levantado; qualquer outra falha retorna `[]` (fail-closed para contexto — nunca quebra o pipeline).
3. **Sanitização do MATCH:** o normalizador destrói sintaxe FTS5 do input (sem `\"`, `*`, `OR`, `NEAR` vindos do usuário); tokens truncados (máx 10 tokens, 32 chars cada).
4. **SQL default só `active`:** `retrieve` sempre filtra `status='active'`, sempre exclui `provenance='llm_inferred'` e itens expirados (`valid_until <= now` ou NULL); `revoked`/`deleted` são inalcançáveis pela query. Ordenação `bm25 + created_at DESC`.
5. **Fluxo adversarial — rastreado no código real, sem caminho de escalada:**
   - Conteúdo de memória nunca chega a `policy.decide` (D38: retrieval implícita usa o caminho read-only, não o tool autorizado).
   - Conteúdo nunca vira argumento de tool: `NexusStatusQuery` é construído só com `settings.nexus_equipment_code` (orchestrator.py:811); `MemoryWriteArgs` só a partir do parse do texto do usuário.
   - Conteúdo nunca vira estado de tarefa nem ação externa: intents vêm de `recognize_intent` sobre a mensagem atual do usuário; memória é inert data.
   - No LLM, o conteúdo chega rotulado como declarações do usuário (`MemoryCtx.verified=False`), e o system prompt (regras 6–7, estáticas) proíbe seu uso para fatos; `verify_plan` rejeita `selected_fact_ids` desconhecidos e exige `summary_key` derivada dos fatos verificados.
   - **Única influência memória→resposta permitida:** `preferred_detail_level` → `detail_level` do plano (D37, determinística, apresentação apenas).
   - Memórias sensíveis nunca alcançam o provider (`sections_for_llm` as exclui; `build_llm_request` omite a seção).
6. **Superfície ao usuário:** notas de memória renderizadas como seção "Você me disse:" (rótulo de declaração do usuário, nunca de fato); `render_memory_write_response` ecoa só o título.

**Achado F3 (LOW):** o trigger de INSERT do FTS5 indexa toda linha independentemente de status, enquanto o de UPDATE exclui `deleted`. Inconsistência menor: sem caminho de produção que insira linhas não-ativas e com `retrieve()` filtrando status, o impacto prático é nulo. Follow-up: alinhar o trigger de INSERT ao filtro de status.

**Veredito da seção: PASS WITH FOLLOW-UP** (F3).

---

## 7. Context Audit

Verificado em `domain/contracts/context.py`, `application/context_builder.py`, `application/context_service.py`, `application/orchestrator.py`.

1. **6 campos verificados:** `ContextSnapshot` contém `session_id`, `tail` (últimas 20), `memories`, `nexus_facts`, `commitments`, `budgets` — todos presentes.
2. **Digest determinístico:** SHA-256 sobre payload canônico (JSON `sort_keys`, separadores compactos), excluindo ids e timestamps; `applied_chars` ordenado. Auditoria emite `context.assembled` com o digest.
3. **Memórias rotuladas não-verificadas:** `MemoryCtx.verified: Literal[False]` — impossível no tipo representar memória como verificada.
4. **Sem tools no builder:** `ContextBuilder.build_snapshot` recebe apenas dados; sem I/O, sem chamadas a tools (T9 com mocks explosivos, verde).
5. **Sensitive fora do objeto real:** `sections_for_llm` exclui memórias `sensitive` da seção `memories` e da seção `sensitive` (que é omitida por padrão em `build_llm_request`); `preferred_detail_level` é a única influência memória→plano (D37).
6. **NEXUS indisponível não quebra:** `nexus_facts=None` → `llm_permitted=False`; fallback determinístico responde sem fatos; renderer trata `UNAVAILABLE`/`ERROR`.

**Veredito da seção: PASS.**

---

## 8. NEXUS Boundary Audit

1. **Config inalterada:** `config.py` mantém host default `127.0.0.1` com validador `_host_loopback` que rejeita qualquer host não-loopback.
2. **Adapter GET-only:** `adapters/nexus/http.py` expõe somente GET sob `/api/v1/`; `follow_redirects=False`, `trust_env=False`.
3. **`validate_destination`:** rejeita userinfo, fragmentos, schemes não-http(s) e qualquer host que não resolva exclusivamente para loopback (`security/policy.py:58`).
4. **Guards D22:** `database.py::references_nexus_database` detecta e rejeita URLs que apontem para o banco do NEXUS; zero ocorrências de `import nexus` em `src/` e `tests/`.
5. **Smoke test:** o script `scripts/smoke_nexus_status.py` existe e está íntegro, mas o smoke live permanece **não executável** — NEXUS fora do ar em 127.0.0.1:8000 (constatado nas janelas de auditoria anteriores; esta auditoria não tentou conexão de rede com o NEXUS). Pendência operacional herdada, não regressão da Fase 2.

**Veredito da seção: PASS.**

---

## 9. Commitments Audit

Verificado em `domain/contracts/commitments.py`, `application/commitments.py`, `adapters/persistence/repositories.py` (SqlCommitmentRepository), `api/main.py`, `application/orchestrator.py`.

1. **Lifecycle 4 estados:** `open → fulfilled/expired/cancelled`; `transition_commitment` pura; terminais imutáveis.
2. **D40 respeitado:** mutações não passam por `policy.decide` (a lista de capabilities é fechada e não inclui commitments); autorização por `origin="user_explicit_command"` literal; intents reconhecidas do texto do usuário; planos com `capability_set=["none"]`.
3. **Sweep idempotente:** cooldown de 1h em `service_meta` (`commitments:sweep:last`); `due_at NULL` nunca expira; máx 500/passada; expiração via `set_status` check-and-set por id (vencedor único em race).
4. **Cooldown fora da tx (F4, INFO):** a checagem do cooldown ocorre fora da transação (TOCTOU); como o sweep é idempotente, o pior caso é uma passada duplicada com linhas de auditoria repetidas. Aceito.
5. **Surfacing in-conversation:** horizonte de 24h, dedup de 12h (`last_surfaced_at`), bloco "📌 Lembretes:" apenas no chat; `mark_surfaced` + auditoria `commitment.surfaced` na mesma tx da persistência da resposta (D42) — nunca exibido duas vezes nem perdido.
6. **D41:** parser pt-BR determinístico ("amanhã"/"hoje"/dias-da-semana/"dd/mm"); datas no fim-do-dia **UTC**.
7. **Sem scheduler (F5, LOW):** o sweep só executa via `POST /api/v1/internal/commitments/sweep` (loopback). O código documenta "the OS cron is the cadence" com exemplo de `curl`, mas **nenhum timer/cron está instalado** — compromissos podem permanecer `open` após o vencimento indefinidamente (sendo re-exibidos como atrasados). `force=True` é o default do endpoint, o que torna o cooldown de 1h inócuo para chamadas manuais (documentado como intencional para uso via cron).
8. **Clone Cobrador:** nenhuma integração real (ver §10).

**Achado F1 (MEDIUM):** `CommitmentService.create` aceita `title`/`detail` **sem** `scan_for_secrets`. Um segredo colado num comando de compromisso ("me cobre amanhã sobre ...sk-...") é persistido em claro no SQLite, indexado no FTS5, re-exibido no chat e — via cauda de conversa (`tail` → `sections_for_llm`) — pode alcançar o provider LLM externo. Probabilidade baixa (exige o usuário colar o segredo no comando), impacto médio (exfiltração para terceiro). Follow-up: aplicar `scan_for_secrets` a title/detail (bloquear ou redigir).

**Achado F2 (LOW):** due dates em fim-do-dia UTC com usuário em America/Sao_Paulo (UTC-3): "amanhã" expira às 20:59:59 locais — até 3h de deslocamento semântico. D41 documenta a escolha; follow-up: parsear no tz do usuário.

**Veredito da seção: PASS WITH FOLLOW-UP** (F1, F2, F5).

---

## 10. Clone Cobrador Audit

1. **Nenhuma integração real:** `ports/commitments.py` define apenas o protocolo `CommitmentSource`; o único adapter é `LocalCommitmentSource`.
2. **Nenhum compartilhamento de banco:** zero referências ao Clone Cobrador em `src/`; nenhum acesso a arquivos fora do repo JARVIS.
3. **Sem cross-imports:** nenhum import ou chamada entre os dois projetos no código.
4. **Superfície somente in-conversation:** compromissos são exibidos apenas no chat ("📌 Lembretes:"); nenhuma mensagem, webhook ou ação externa.

**Veredito da seção: PASS.**

---

## 11. D31 Transaction Audit

Verificado em `application/memory_service.py::_authorize` e `tests/integration/test_memory_audit.py`.

1. **Pipeline contrato → policy → scan → persist:** a ordem é essa; `_authorize` emite `policy.decided` (e o scan emite `memory.write_blocked`) em **transação própria**, comitada antes da transação de escrita.
2. **DENY/secret-block duráveis:** se a escrita posterior falhar ou a transação rolar back, o `policy.decided`/`memory.write_blocked` já está comitado — a negação deixa rastro (teste `test_policy_deny_leaves_durable_audit`, verde).
3. **ALLOW honesto:** um ALLOW cuja escrita subsequente falha registra a decisão sem o `memory.created` correspondente — o audit reflete o que aconteceu, não o que se pretendia.
4. **Sem overlap de transações:** a tx de auditoria comita e fecha antes da tx de escrita abrir (sem risco de `database is locked` entre elas no SQLite).

**Veredito da seção: PASS.**

---

## 12. Database/Migration Audit

Verificado empiricamente em DB temporário com `JARVIS_DATABASE_URL` explícito (o aviso operacional sobre `migrations/env.py` foi respeitado; o DB real permanece em 0002, intocado).

1. **0001–0004 íntegras:** upgrade 0002→0003→0004 OK; tabelas `memory_items`, `commitments`, `service_meta`, `memory_fts` (+ shadow tables) criadas.
2. **Downgrade reversível:** 0004→0003 OK; re-upgrade → head OK; revisão final `0004 (head)`.
3. **Triggers FTS5 presentes:** `memory_fts_ai/ad/au` + `audit_logs_no_delete/no_update` confirmados no schema real.
4. **Tokenizer:** `unicode61 remove_diacritics 2` (forma com espaço exigida pelo SQLite 3.45.1 — diferença legítima vs. o desenho, documentada).
5. **D39:** a reescrita do DDL de `commitments` na 0003 foi pré-release (zero commits publicados à época, DB real ainda em 0002) — correção legítima, não violação de imutabilidade de migration.
6. **Sync testado:** triggers validados por testes de sincronia FTS (suíte verde).

**Nota F8 (INFO):** DDL no SQLite é não-transacional — o downgrade da 0004 é best-effort; uma falha no meio poderia deixar estado parcial. Verificado funcionando; aceitável para o estágio local.

**Veredito da seção: PASS.**

---

## 13. API Audit

Verificado em `src/jarvis/api/main.py`.

1. **Sem bypass:** todos os endpoints de memória e commitments passam pelo service (policy + scan + auditoria); nenhum endpoint escreve direto no repositório.
2. **Origin fixado:** mutações via API usam `USER_EXPLICIT_ORIGIN` constante; o cliente HTTP não pode forjar `origin`.
3. **Memória passa por scan:** `POST /api/v1/memory` → `state.memory.create` → pipeline com `scan_for_secrets`.
4. **Sweep loopback:** `POST /api/v1/internal/commitments/sweep` é interno/loopback; idempotente.
5. **Leitura sem vazamento:** `GET /api/v1/memory` usa o caminho autorizado com policy check; itens sensíveis/excluídos não vazam (filtros do `retrieve`).
6. **Erros honestos:** códigos de erro estáveis (`ErrorCode`), sem stack traces para o cliente; indisponibilidade do NEXUS é explícita.

**Veredito da seção: PASS.**

---

## 14. Test/Guarantee Audit

Todos executados nesta auditoria: **255 passed**.

1. **Autoridade T8/T12** (`test_memory_authority.py`): 7 testes — injection armazenada como dado inerte, recuperada como dado (não comando), sem comando "apagar tudo", utterances não-suportadas rejeitadas sem side effects, slice de memória nunca toca NEXUS. Verdes.
2. **T9 mocks explosivos** (`test_context_builder.py:83`): builder nunca toca tools/I/O. Verde.
3. **T10 digests** (`test_context_assembly.py`): digest determinístico, muda com conteúdo, ignora ids/timestamps. Verdes.
4. **T12 injection adversarial:** coberto em (1). Verde.
5. **T13 sweep idempotência** (`test_commitments.py`): duas passadas, sem duplicatas; cooldown respeitado. Verdes.
6. **T18 surfacing** (`test_commitment_charge.py`, e2e): bloco 📌, dedup 12h, sem re-exibição. Verdes.

**Flake conhecido `test_11_cancel_while_circuit_open`:** passou nesta auditoria. Causa provável: race entre as transições de estado do orquestrador e o polling do teste (`_wait_task` aguardando `PENDING`/`PLANNING`) — sob carga do suíte completo, a tarefa pode ultrapassar esses estados antes do waiter observar. É fragilidade do teste (timing), não do produto: a invariante de cancelamento é provada pelos outros 11 testes da bateria. **Não é regressão.**

**Lacuna F6 (LOW):** as garantias de performance T20 (retrieval p95 < 500ms com 10k memórias, montagem de snapshot < 100ms, sweep de 500 < 1s) **não têm teste**. Os budgets existem no código, mas a garantia não é provada. Follow-up: implementar o T20 ou remover a garantia do desenho.

**Veredito da seção: PASS WITH FOLLOW-UP** (F6).

---

## 15. Fase 1 Regression Audit

1. **Suíte completa verde:** 255 passed nesta auditoria, zero falhas — nenhum teste de comportamento da Fase 1 quebrou.
2. **Composição 98+157:** não auditável via Git (histórico de 1 commit); o total de 255 foi reproduzido e a ausência de regressões confirmada.
3. **Fronteira NEXUS preservada:** config, adapter e guards D22 inalterados em comportamento (ver §8).
4. **Bateria de cancelamento:** 12/12 verde; estados terminais imutáveis.
5. **Gates reproduzidos:** pytest 255 passed, ruff check passed, ruff format 108 files formatted, mypy 62 source files sem issues, migrations up/down/up OK.

**Veredito da seção: PASS.**

---

## 16. Complexity Audit

1. **Sem camadas especulativas:** nenhum framework de agentes, fila, cache externo ou serviço novo; stack inalterada (FastAPI, Pydantic v2, SQLAlchemy/Alembic, FTS5, httpx, pytest).
2. **Sem duplicação funcional:** `context_service.py` reduziu-se a `build_llm_request` (aditivo); `context_builder.py` é o montador puro — responsabilidades distintas, sem overlap.
3. **Interfaces com consumidores:** todos os Protocols em `ports/` têm implementação e uso (`MemoryRepository`, `MemoryRetriever`, `CommitmentSource`).
4. **Código morto (F7, INFO):** `SqlCommitmentRepository.expire_due` não tem chamadores — o service usa `set_status` check-and-set por id. Follow-up: remover ou conectar.

**Veredito da seção: PASS WITH FOLLOW-UP** (F7).

---

## 17. Scope Audit

Greps e leitura direta em `src/`:

1. **Sem voz:** nenhuma referência a voice/microfone/wake-word.
2. **Sem visão:** nenhuma referência a vision/webcam/screen.
3. **Sem browser:** nenhuma referência a browser/navegação web automatizada.
4. **Sem shell/filesystem irrestrito:** nenhum `subprocess`/`os.system`/escrita arbitrária em `application/` ou `domain/`; persistência limitada ao SQLite próprio via repositórios.
5. **Sem agentes/automações:** nenhum multi-agent; o sweep de commitments não tem scheduler (só endpoint loopback) — nenhuma proatividade autônoma.
6. **Sem exposição pública:** API em 127.0.0.1:8123; `validate_destination` impõe loopback; sem middleware de auth porque o modelo de ameaça da fase é "localhost = usuário único" (inalterado da Fase 1).

**Veredito da seção: PASS.**

---

## 18. Risk Matrix

| # | Risco | Severidade | Probabilidade | Impacto | Mitigação existente | Status |
|---|---|---|---|---|---|---|
| F1 | Commitments sem `scan_for_secrets`: segredo no título/detalhe é persistido em claro, indexado no FTS5 e pode alcançar o provider LLM via cauda de conversa | MEDIUM | Baixa | Médio | Origin user-explicit; single-user local | **Aberto** |
| F2 | Due dates em fim-do-dia UTC vs usuário em America/Sao_Paulo (deslocamento de até 3h) | LOW | Baixa | Baixo | D41 documentado; semântica determinística | Aceito (follow-up) |
| F3 | Trigger INSERT do FTS5 indexa qualquer status; UPDATE exclui `deleted` (inconsistência) | LOW | Baixa | Nulo (retrieve filtra) | Filtros de status no `retrieve()`; sem caminho que insira não-ativos | **Aberto** (menor) |
| F4 | Checagem do cooldown do sweep fora da transação (TOCTOU) | INFO | Baixa | Nulo | Sweep idempotente | Aceito |
| F5 | Sem scheduler para o sweep: compromissos podem ficar `open` após o vencimento até chamada manual do endpoint | LOW | Média | Baixo (re-exibição como atrasado) | Endpoint idempotente documentado para cron do SO | **Aberto** |
| F6 | Garantias de performance T20 sem teste (p95 retrieval, montagem, sweep) | LOW | Média | Baixo | Budgets implementados no código | **Aberto** |
| F7 | Código morto: `SqlCommitmentRepository.expire_due` sem chamadores | INFO | — | Nulo | — | Aceito (follow-up) |
| F8 | Downgrade 0004 best-effort (DDL não-transacional no SQLite) | INFO | Baixa | Baixo | Verificado funcionando up/down/up | Aceito |

Nenhum risco HIGH. Nenhum risco exige bloqueio da Fase 2.

---

## 19. Final Technical Verdict

| Área | Veredito |
|---|---|
| 3. Architecture Compliance | PASS |
| 4. Memory Audit | PASS |
| 5. Memory Security | PASS |
| 6. Retrieval/FTS5 Audit | PASS WITH FOLLOW-UP |
| 7. Context Audit | PASS |
| 8. NEXUS Boundary Audit | PASS |
| 9. Commitments Audit | PASS WITH FOLLOW-UP |
| 10. Clone Cobrador Audit | PASS |
| 11. D31 Transaction Audit | PASS |
| 12. Database/Migration Audit | PASS |
| 13. API Audit | PASS |
| 14. Test/Guarantee Audit | PASS WITH FOLLOW-UP |
| 15. Fase 1 Regression Audit | PASS |
| 16. Complexity Audit | PASS WITH FOLLOW-UP |
| 17. Scope Audit | PASS |

**Veredito global: PASS WITH FOLLOW-UP.** A Fase 2 preserva todas as garantias da Fase 1, implementa os três slices conforme o desenho revisado e não introduz regressões. Os follow-ups são melhorias delimitadas, nenhum deles arquitetural.

---

## 20. Follow-up Items

Priorizados por severidade; nenhum bloqueia a Fase 2.

1. **[MEDIUM] Aplicar `scan_for_secrets` a commitments** — `CommitmentService.create`: varrer `title`/`detail` antes de persistir (bloquear ou redigir, com auditoria `commitment.write_blocked`). Elimina o F1.
2. **[LOW] Instalar a cadência do sweep** — systemd timer ou cron do SO chamando `POST http://127.0.0.1:8123/api/v1/internal/commitments/sweep` (horário). Elimina o F5; sem isso, expiração é manual.
3. **[LOW] Due dates no tz do usuário** — parsear "amanhã"/"hoje"/dias-da-semana em America/Sao_Paulo em vez de fim-do-dia UTC (D41). Elimina o F2.
4. **[LOW] Implementar o teste T20** — seed de 10k memórias sintéticas; asserts de p95 < 500ms (retrieval), < 100ms (snapshot), < 1s (sweep 500). Ou remover a garantia do desenho. Elimina o F6.
5. **[INFO] Alinhar trigger INSERT do FTS5** — indexar apenas linhas com status recuperável (ou documentar a divergência vs. UPDATE). Elimina o F3.
6. **[INFO] Remover `SqlCommitmentRepository.expire_due`** — sem chamadores; ou conectá-lo ao `sweep` substituindo o loop por-id. Elimina o F7.
7. **[Operacional] Smoke live do NEXUS** — segue pendente desde a Fase 1 (NEXUS fora do ar em 127.0.0.1:8000); rodar `scripts/smoke_nexus_status.py` quando o NEXUS estiver no ar.
8. **[Operacional] Aviso de migrations** — `migrations/env.py` continua ignorando `-x db_url` e lendo `JARVIS_DATABASE_URL` silenciosamente; manter o protocolo de DB temporário em qualquer auditoria futura (incidente da Fase 1 não se repetiu aqui).
