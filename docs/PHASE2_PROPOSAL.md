# FASE 2 — Memory + Context (proposta técnica)

**Status:** proposta para revisão. **Nada implementado.**
**Data:** 2026-09-30 · **Pré-requisito:** Fase 1 aprovada e commitada.

## Objetivo

Dar ao JARVIS memória tipada e contexto de longo prazo sobre o Lucas, sem quebrar
nenhum invariante da Fase 1: deny-by-default, audit append-only, NEXUS read-only
via HTTP, local-first, single-user.

## Escopo proposto (3 slices verticais)

### Slice 1 — Memória episódica tipada

- Novas tabelas `memory_items`
  (`id`, `session_id`, `kind`, `subject`, `content`, `confidence`, `source`,
  `created_at`, `superseded_by`) + `memory_links`.
- Kinds fechados: `fact`, `preference`, `commitment`, `project_note`.
- Escrita de memória passa pelo pipeline de policy existente:
  nova capability `memory.write` (default **DENY**), `memory.read` ALLOW
  para o próprio usuário. Toda leitura/escrita auditada.
- **Sem vector DB na Fase 2a:** busca por FTS5 do SQLite (suficiente para
  single-user local). Embeddings ficam para 2b, somente se o recall exigir
  (regra: medir antes de adicionar infra).
- Correção/remoção: `superseded_by` (nunca DELETE lógico cego); usuário pode
  inspecionar e remover memórias via endpoint dedicado auditado.

### Slice 2 — Contexto de sessão enriquecido

- `ContextService` passa a montar o contexto do LLM com: resumo da sessão atual
  + top-k memórias relevantes (FTS5) + estado do NEXUS quando perguntado.
- Contrato `ContextSnapshot` versionado; o que entra no prompt é registrado em
  `context.assembled` (audit, com redação de segredos).
- Orçamento de tokens no snapshot, com truncamento por prioridade
  (relevância × confiança × recência).

### Slice 3 — Compromissos com cobrança (ponte com Clone Cobrador)

- Kind `commitment` com `due_at`; job diário (cron local, **não** Celery)
  lista compromissos vencidos; o orquestrador os inclui proativamente na
  próxima interação ("você prometeu X").
- Sem proatividade push na Fase 2 — só dentro da conversa.

## Anti-escopo Fase 2

Voz, visão, escrita no NEXUS, multi-agente, cloud, embeddings obrigatórios,
Redis/Kafka, UI nova (além do necessário para inspecionar memória).

## Riscos e mitigações

| Risco | Mitigação |
|---|---|
| Alucinação de memória | toda memória carrega `source` (utterance id) + `confidence`; proveniência exposta na API |
| Crescimento do prompt | orçamento de tokens + truncamento por prioridade no `ContextSnapshot` |
| Privacidade | tudo local, no mesmo SQLite; export/limpeza via endpoint auditado |
| Policy bypass via conteúdo | memória é dado, nunca instrução (mesma regra do web content) |

## Critérios de aceite

1. 3 slices com testes, incluindo "memória nunca inventa fato sem `source`".
2. Policy `memory.write` DENY por default (teste: escrita sem regra → negada + auditada).
3. Audit cobrindo leitura e escrita de memória.
4. Migration `0003` reversível (up/down/up em DB temporário).
5. Smoke do NEXUS inalterado; nenhum invariante da Fase 1 quebrado.

## Decisões que o Lucas precisa tomar

1. Aprovar o escopo dos 3 slices (ou cortar/alterar).
2. FTS5 agora vs. embeddings já (recomendação: FTS5 primeiro).
3. Compromissos com cobrança entram na Fase 2 ou ficam só no Clone Cobrador?
