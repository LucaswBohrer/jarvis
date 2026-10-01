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
