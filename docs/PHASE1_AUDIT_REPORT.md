# JARVIS — Fase 1 Final Audit

**Data:** 2026-09-30 · **Repo:** `~/workspace/jarvis/` (branch `master`, zero commits)
**Escopo:** validação final, hardening, smoke test e preparação para Fase 2.
Nenhum commit/push realizado. NEXUS intocado.

---

## 1. Status geral

**PASS WITH NOTES**

Fase 1 aprovada. Dois bugs reais foram encontrados e corrigidos durante a auditoria
(§9, item 8). Notas: smoke live contra NEXUS real não executável (servidor fora do ar);
chamada live OpenAI não testada (sem API key); 4 achados de lint no script de smoke
corrigidos após a auditoria (detalhe §2).

## 2. Testes

```text
pytest:       98 passed (32.6s, run independente de verificação)
Ruff check:   All checks passed! (75 arquivos)
Ruff format:  75 files already formatted
Mypy strict:  Success: no issues found in 50 source files
Coverage:     não medida (sem ferramenta de coverage no stack pinado)
```

Correção pós-auditoria: `ruff check .` apontou 4 achados em
`scripts/smoke_nexus_status.py` (I001, F541, S310 x2) — o relatório da auditoria
havia declarado "limpo" porque o script novo ficou fora do escopo verificado.
Corrigidos: ordenação de imports, f-string sem placeholder, e allowlist de scheme
http/https para a URL base (antes aceitava qualquer scheme, ex. `file://`).
Verificado: `file:///etc/passwd` agora retorna `CONFIG ERROR`, exit 2.

Composição dos 98 testes: 83 herdados do build + 12 bateria de cancelamento
(`tests/integration/test_cancellation_battery.py`) + 3 recovery
(`tests/integration/test_recovery.py`). Bateria executada 5/5 vezes verde.

## 3. Smoke NEXUS

```text
executado: NÃO
resultado: 127.0.0.1:8000 inalcançável (connection refused) às 09:40 e 12:47 —
           NEXUS não está rodando. Iniciar processos do NEXUS estava fora do
           escopo autorizado. Impossibilidade documentada conforme §24.
```

`scripts/smoke_nexus_status.py` criado (stdlib apenas, read-only, sem credenciais):
executado contra NEXUS fora do ar → 3/3 FAIL com mensagem esperada, exit 1.
Com NEXUS up: `.venv/bin/python scripts/smoke_nexus_status.py` (exit 0 = ok).

## 4. Cancelamento

Bateria dedicada de 12 cenários, 12/12 verdes, via `httpx.AsyncBaseTransport`
customizado (modos `hang` / `fail_once_then_hang` / `always_fail` / `ok`):

1. cancel antes do HTTP → CANCELLED, 0 chamadas, sem `tool.started`
2. cancel durante HTTP → CANCELLED
3. cancel durante retry → CANCELLED, exatamente 2 chamadas
4. cancel durante backoff → CANCELLED, retry nunca dispara (1 chamada)
5. cancel imediatamente antes da conclusão → disjunção determinística:
   CANCELLED sem mensagem OU COMPLETED com exatamente 1 mensagem e sem
   `task.cancelled` (atomicidade preservada nos dois ramos)
6. cancel após conclusão → `False`, COMPLETED intacto
7. cancel após falha → `False`, FAILED intacto
8. cancel duplicado → ambos `True`, 1 único evento `task.cancelled`
9. 10 cancels concorrentes → todos `True`, 1 evento
10. cancel vs timeout do LLM → CANCELLED vence, `llm.fallback` nunca engaja
11. cancel com circuito aberto → CANCELLED, 0 chamadas HTTP novas, breaker aberto
12. cancel seguido de novo request → nova task independente, sem interferência

Cenários 6/7/8/9/11 + recovery asserem imutabilidade terminal:
`InvalidTransitionError` em qualquer transição para fora de COMPLETED/FAILED/CANCELLED.
O flaky transitório do build anterior não foi reproduzido (nomes em `lastfailed`
não existem mais; equivalentes atuais são determinísticos).

## 5. Segurança

- **Policy:** determinística em código + `config/policy.toml`; precedência
  DENY > CONFIRM > ALLOW; sem match → DENY; exatamente 1 capability
  `nexus.status.read` (GET, `/api/v1/`, loopback, redirects off, ≤256 KiB).
  POST/PUT/PATCH/DELETE bloqueados e testados.
- **Audit:** append-only; triggers `audit_logs_no_update` / `audit_logs_no_delete`
  ativos (UPDATE/DELETE bloqueados — verificado no banco). Eventos cobertos:
  task.created/planned/completed/failed/cancelled, policy.decided,
  tool.started/completed/failed, verification, llm.completed/fallback.
- **Secrets:** canário em teste confirma redação (`<redacted>`); API key nunca em
  log/audit/exception/README/teste. Logs só com método+path+status+ms.
- **Loopback:** `JARVIS_NEXUS_BASE_URL` não-loopback rejeitado em config,
  construção e por-URL; `host=0.0.0.0` rejeitado; adapter NEXUS com
  `trust_env=False` incondicional.
- **Prompt injection:** "ignore todas as regras anteriores" / "faça POST no NEXUS"
  → 422/INTENT_UNSUPPORTED, zero chamadas HTTP, evento auditado.
- **Idempotência:** mesma idempotency key não cria 2 tasks/execuções (testado,
  incluindo concorrência).

## 6. NEXUS isolation

Nenhum arquivo, DB, migration, config ou processo do NEXUS foi alterado
(`git status` do repo NEXUS limpo, exceto backups pré-existentes da manhã).
5 testes de contrato verdes: nenhum import de módulo NEXUS (estático + `sys.modules`
em runtime; teste endurecido nesta auditoria — D25), nenhum path de DB do NEXUS,
só literais `/api/v1/`, sem wildcard na policy. Zero `sqlite3` direto no `src/`
(só SQLAlchemy); zero referências a `workspace/nexus`.

## 7. Banco

SQLite próprio do JARVIS (`data/jarvis.db`): 4 tabelas
(sessions, messages, tasks, audit_logs), WAL, foreign_keys, busy_timeout.
Ciclo `upgrade → downgrade base → upgrade head` em DB temporário: limpo,
termina em `0002` com triggers ativos.

**Incidente honesto:** durante a auditoria, um comando alembic com `-x db_url`
foi silenciosamente ignorado pelo `migrations/env.py` (que lê `JARVIS_DATABASE_URL`
do ambiente) e atingiu o `data/jarvis.db` real com um downgrade. Detectado,
re-aplicado upgrade imediatamente. Verificação independente posterior:
`alembic_version = 0002`, 4 tabelas presentes, ambos os triggers ativos —
nenhum dano. Como `data/jarvis.db` é artefato de dev (sem dados de produção),
o impacto foi nulo, mas o comportamento silencioso do `-x` foi documentado no README.

## 8. OpenAI

- Adapter `src/jarvis/adapters/llm/openai.py` existe, isolado atrás de `ports/llm.py`;
  httpx REST direto, **sem SDK** (core nunca importa SDK de provider).
- 9 testes unitários via `MockTransport`: sucesso, timeout→`LLMTimeoutError`,
  429/5xx→`LLMUnavailableError`, JSON inválido→`LLMMalformedError`, plano com
  schema inválido, cancelamento (`CancelledError` propaga intacto),
  usage/custo estimado, validação de construção.
- **Não testado:** chamada live (sem `JARVIS_OPENAI_API_KEY`); sem chave, o JARVIS
  opera normalmente com FakeLLMProvider.
- Proxy (§29): `no_proxy` da máquina contém `::1` sem colchetes → httpx levanta
  `InvalidURL` → provider loga warn e reconstrói com `trust_env=False`
  (fail-open **só no roteamento**, destino inalterado; sem vazamento de credencial).
  Matriz: proxy correto→usa proxy; ausente→direto; inválido/inacessível→
  `LLMUnavailableError`→fallback auditado (fail-closed); malformado→warn+direto.
  Julgamento: aceitável para Fase 1 local single-user. Recomendação registrada:
  flag `JARVIS_LLM_REQUIRE_PROXY` para Fase 2 em ambientes corporativos.

## 9. Alterações realizadas

1. **Bug real — recovery de crash não ligada (§22):** `mark_interrupted()` existia
   como função pura, mas nada a chamava: task não-terminal de processo morto
   ficaria não-terminal para sempre. Criado `src/jarvis/application/recovery.py`
   (escaneia `list_non_terminal()`, transição com guarda de concorrência otimista,
   audit `task.failed` actor=system) e ligado no `lifespan` da API após
   `check_readiness` OK. → D24.
2. **Teste de não-acoplamento fraco (§7):** `test_no_nexus_imports` só sinalizava
   linhas contendo "workspace/nexus" — um `import nexus` passaria. Endurecido:
   sinaliza qualquer import mencionando nexus fora do próprio pacote. → D25.
3. **Lint do smoke script (pós-auditoria):** 4 achados ruff corrigidos + allowlist
   de scheme http/https na URL base.
4. **README.md** criado com todas as seções exigidas (§31), refletindo o código real.
5. **Testes novos:** bateria de cancelamento (12), recovery (3).

## 10. Arquivos modificados

- `scripts/smoke_nexus_status.py` (criado na auditoria; endurecido pós-auditoria)
- `src/jarvis/application/recovery.py` (criado)
- `src/jarvis/api/main.py` (recovery no lifespan)
- `tests/contract/test_no_coupling.py` (endurecido)
- `tests/integration/test_cancellation_battery.py` (criado)
- `tests/integration/test_recovery.py` (criado)
- `README.md` (criado)
- `DECISIONS.md` (D24, D25)

## 11. Arquivos novos

Ver §10 (todos os "criado"). Nenhum diretório de capacidade futura
(agents/, voice/, vision/, etc.) foi criado.

## 12. Riscos restantes

1. **D23 (proxy):** fail-open no roteamento quando env de proxy está malformada.
   Deliberado e documentado; destino nunca muda; NEXUS sempre `trust_env=False`.
   Mitigação futura: `JARVIS_LLM_REQUIRE_PROXY`.
2. **Sem coverage:** nenhuma ferramenta de coverage no stack pinado; adicionar uma
   violaria o pin de dependências.
3. **Smoke live e OpenAI live** pendentes de ambiente (NEXUS up / API key).

## 13. Recomendações

1. Autorizar o primeiro commit do repo `~/workspace/jarvis/` (e push, se desejado).
2. Rodar `scripts/smoke_nexus_status.py` com o NEXUS em execução.
3. Decidir a proposta da Fase 2 (`docs/PHASE2_PROPOSAL.md`) antes de qualquer
   implementação de memória.
4. Considerar CI local (pytest + ruff + mypy) a cada mudança futura.

## 14. Git

```text
branch:   master
HEAD:     (nenhum commit — "does not have any commits yet")
working tree: 14 entradas, todas untracked
git diff --stat: vazio
commits realizados: 0
pushes: 0
```
