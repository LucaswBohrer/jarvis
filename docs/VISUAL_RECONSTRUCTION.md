# JARVIS — Visual Language Reconstruction: Design Direction
**2026-10-01 — Product Design, not Frontend Styling**

## The question
> "Como deveria ser a interface de um sistema de inteligência artificial proprietário chamado JARVIS?"

Não "como deixar mais bonito". A resposta: um **AI Operating Environment** — o usuário está *dentro* do sistema, não navegando páginas com cards.

## The core move: DISSOLVE THE SURFACES

Tudo que hoje é "seção delimitada" é candidato a desaparecer:

| Hoje (dashboard) | Vira (environment) |
|---|---|
| `.sys-panel` boxes (rail direito) | Telemetria flutuando — texto agrupado por proximidade |
| `.view-head` header bars | Títulos editoriais flutuando no espaço |
| Message bubbles | Eventos do sistema — tipografia + ritmo |
| Composer box | Linha de comando — energia + prompt, sem caixa |
| Badges em caixas | Estados via tipografia/intensidade/comportamento |
| Sidebar com labels | Rail mínimo — glifos, labels contextuais |
| Cards de view interna | Composições próprias por view |

**Regra:** se posso resolver com composição, tipografia, espaço, movimento ou hierarquia — não crio superfície.

## Space as primary material

- O vazio é o material mais caro. Grandes áreas respirando.
- Home: o ORB é o centro gravitacional. Status orbitam como microinformação. Composição assimétrica.
- Agrupamento por proximidade, escala, alinhamento — não por bordas.

## Typography as identity

- **Display**: greeting em escala grande, peso leve, tracking apertado.
- **Technical labels**: mono, uppercase, tracked, faint — o "whisper" do sistema.
- **Numbers**: elementos de design (sessão, turnos, timestamps).
- Hierarquia editorial: display → voz do sistema → microcopy → metadata.

## Orb-derived language

Do ORB extraímos: energia, ritmo, partículas, distorção, luminosidade, movimento, organicidade, profundidade, precisão.

| Propriedade do ORB | Tradução na interface |
|---|---|
| Energia | Energy line do composer; estados como comportamento |
| Ritmo | Stagger, respiração do idle |
| Partículas | Campo de fundo sutil |
| Distorção | Transições com blur/escala (deslocamento espacial) |
| Luminosidade | Foco = presença percebida |
| Movimento | Só com significado |
| Organicidade | Composição assimétrica |
| Profundidade | Camadas no espaço, sem caixas |
| Precisão | Hairlines, alinhamento, números |

**Brand mark**: o mini-orb atual compete com o ORB. Vira símbolo abstrato (não-orbital).

## Navigation as architecture

- Rail mínimo: glifos, labels só no hover/ativo ou micro-labels.
- Troca de view = **mudança de estado do sistema**: transição espacial com identidade (deslocamento + blur + escala), nunca fade genérico.

## Chat: abandon bubbles

- **User**: texto à direita, recuado, tipografia — sem caixa. Metadata mínima.
- **JARVIS**: evento do sistema — presença tipográfica total, linha de energia/sinal, metadata contextual. Não "mensagem dentro de card".
- Resposta = acontecimento no ambiente. O ORB responde visualmente.

## Internal views: compositions, not layouts

- **Sessions** → explorador: lista extremamente limpa, muito espaço, hover revela contexto.
- **Tasks** → monitor operacional: estados via intensidade/tipografia/movimento, sensação de timeline.
- **Activity** → stream viva: ritmo, timestamps discretos, pontos de atividade — acontecimentos, não tabela.
- **Audit** → execution trace: sequência técnica com conectores e timestamps.
- **Memory** → espaço de conhecimento: NÃO tabela. Prepara visualmente o Second Brain (entidades, conceitos — sem implementar nada).
- **NEXUS** → instrumento especializado dentro do OS.
- **System** → instrumentação interna.

## States as behavior (not color swap)

- **IDLE**: respira — orb lento, interface calma.
- **PROCESSING**: energia — orb ativo, command line varre.
- **OFFLINE**: silêncio — dim, parado.
- **ERROR**: ruptura — disrupção controlada de comportamento.

## Motion vocabulary

- Entrada de view: deslocamento espacial direcional.
- Foco: sistema percebe presença (luminosidade).
- Hover: resposta física (deslocamento/escala, não glow).
- Erro: ruptura controlada. Sucesso: resolução/relaxamento.
- Durações: instant 120ms / med 200ms / page 320ms / slow 500ms. Easing: expo-out.

## Background: one environment

Grid extremamente sutil + partículas + aurora controlada + grain. Tudo parte do mesmo ambiente, nunca wallpaper.

## Tokens

Espaçamento (escala 4/8/16/32/64), tipografia (display/ui/mono com papéis definidos), radius (só onde há matéria — quase zero), opacity (camadas de presença), motion (durações + easings + intensidades por estado), hierarquia (display/voz/whisper/metadata).

---

## Implementation log — 2026-10-01

Five semantic commits (local, no push):

1. `c8c77ba` — **Dissolve dashboard surfaces into environment.**
   Shell: 76px rail + environment (no sidebar grid). Topbar dissolved
   into floating presence elements. Brand mark → geometric diamond
   with energy line (non-orbital; the orb owns plasma). Nav → glyph
   rail with sliding teal indicator, contextual labels, no backgrounds.
   `.sys-panel` visually dissolved (no boxes/corner-cuts/decorative
   ticks). Telemetry → floating field, Home-only, hidden <1240px.
   View headers → editorial titles. Toolbars → quiet text actions.
   View transitions → lateral spatial shift + blur + subtle scale.

2. `9dfa48a` — **Reconstruct Home as command environment.**
   Single composition, orb as gravitational center. Greeting →
   display typography (large, light, tight tracking, generous space).
   Conversation → system events (user: compact right-aligned, no box;
   JARVIS: full-measure flat, larger light type; system: centered
   whisper; error: typographic red, no banner). Composer → command
   line (no box/background/blur; baseline hairline; energy filament;
   real idle/hover/focus/typing/busy states). Send → mono glyph.
   STOP stays a real red state. Keyboard hints → subtle text.
   Thread mask dissolves at top.

3. `ee9d036` — **Give each internal view its own composition.**
   Index language: entries (number, voice, metadata, state) grouped by
   alignment and space. No fills, no radius, no hover backgrounds —
   hover brightens text + index; clickable shifts 4px. States become
   typographic (dot + tracked mono). Sessions: calm index. Tasks:
   dense process monitor. Memory: airy archive. Activity: true
   timeline on a spine. Audit: dense mono ledger. NEXUS: mini-orb
   removed — display type + signal line instrument. System: ledgers
   (titled groups of ruled readouts). viewEnter retired (was
   overriding the spatial viewIn).

4. `9b6bdd6` — **Refine motion language and system states.**
   Fixed: `@keyframes rise` was referenced but never defined.
   Motion vocabulary documented in CSS (cause → effect; 160/260/420/
   520ms ladder; stagger capped 600ms). Ledgers enter as composed
   units with 80ms cascade. Primary button → instrument key
   (hairline, mono, teal; fills only on hover).

5. `a98d74e` — **Fix mobile nav/topbar overlap** (<=760px).

### Validation (2026-10-01)

- `node --check`: clean. `innerHTML`: 0. `style.display`: 0.
- `pytest`: 377 passed. `ruff check` + `ruff format --check`: clean.
- `mypy src`: clean. `alembic current`: 0004 (head).
- Chromium (real): 8 viewports (1920→390px), all 8 views — zero
  console errors, zero 4xx/5xx, zero overlaps. Screenshots inspected:
  Home (empty + thread), Sessions, Tasks, Memory, Activity (timeline),
  Audit (ledger), NEXUS (typographic instrument), System (ledgers).
- Bugs found and fixed in validation: dead `rise` keyframes; mobile
  topbar/nav overlap; `viewEnter` overriding spatial transition.
- NEXUS still down at 127.0.0.1:8000 — only the honest
  unavailability path was validated; the healthy path is untested.
- Final gate remains Lucas's real test on his Windows machine.
