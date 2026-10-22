---
name: central
description: Central CW Studios — pré-produção e acompanhamento da edição de vídeo para o painel da gestão. Use quando o editor for começar um vídeo de um card ou demanda ("vamos fazer o SCC-1762", "prepara a D-0012", "pega a próxima"), pedir "me conecta na central com o token cw_…", disser de qual card é o vídeo ("é do SCC-1762", "é a D-0012"), disser que terminou ou entregou um vídeo ("entreguei", "terminei o vídeo", "pode fechar"), corrigir título ou tipo do vídeo, perguntar "o que tem na fila?", ligar/desligar a estação do Escritório ("liga minha estação"), procurar ou puxar agentes/skills da biblioteca ("o que tem na biblioteca?", "puxa a skill X"), preparar ou compartilhar os agentes para o kit ("prepara meus agentes para o kit da Cardápio Web / do CW Cast", "compartilha meus agentes com a central"), usar as ferramentas da empresa (Higgsfield), mandar um corte pronto para publicar nas redes ("manda pra publicação", "posta esse corte"), pedir "pega a próxima"/"pega a D-0012", ou devolver uma demanda. Funciona com qualquer agente de edição; não edita vídeo.
---

# Central CW Studios

O plugin já acompanha a edição sozinho, com qualquer agente:
- **Abre o vídeo** na primeira vez que o Claude mexe num arquivo de vídeo (ou quando o editor cola o caminho).
- **Liga ao card** quando o editor cita `SCC-1762` (Jira) ou `D-0012` (central) na conversa.
- **Mostra a etapa ao vivo** (Preparo → Transcrição → Corte e seleção → Montagem → Legenda e motion → Render →
  Revisão → Entrega) pelo que está sendo feito.
- **Mede o tempo ativo** e manda tudo para o painel da gestão.

O que depende de você (Claude) é pouco. Os comandos ficam em `${CLAUDE_PLUGIN_ROOT}/scripts/`, rodam com o
`python3` do sistema e nunca devem travar a edição: se um falhar, avise em uma linha e siga.

## Conectar (uma vez por computador)
"Me conecta na central com o token cw_…" →
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/central.py" entrar --token <token>` e confirme o nome que voltou.
O token é pessoal: nunca grave em arquivo de projeto nem repita em mensagem para outra pessoa.
Para conferir: `central.py quem`.

## Começar um vídeo: preparar primeiro
O material de cada card ou demanda fica no Drive compartilhado, em
`CW Studios - Produção › 0. ENVIE AQUI - Demandas › <SCC-1762 ou D-0012>` (o Google Drive para computador sincroniza).
Quando o editor for começar ("vamos fazer o SCC-1762", "pega a D-0012"), rode antes de editar:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/preparar.py" <CHAVE>`
Ele copia o material para `~/CW-Work/preparo/<CHAVE>/bruto/` (forçando o download do Drive), mede cada arquivo,
transcreve se a máquina tiver o ambiente do cw-cortes (`transcricao/*.txt|.srt|.json`), escreve `BRIEF.md` (pedido,
ajuste pedido, regras do time, material) e abre o vídeo na central na etapa Preparo. Pode levar alguns minutos
(transcrição): rode em segundo plano se o seu ambiente permitir e avise o editor do andamento.
- Leia o `BRIEF.md` antes de editar. Card do Jira: se houver conector do Jira, leia a descrição do card e complete o
  brief; senão, pergunte ao editor só o que faltar (CTA, nomes, prazo).
- Saiu com código 2 = material não está na pasta: diga ao editor exatamente o caminho que o script mostrou.
- Use a transcrição pronta no seu fluxo em vez de transcrever de novo, quando o formato servir.

## Durante a edição
- O editor citou o card depois que o vídeo já abriu, ou o vídeo abriu com nome ruim (ex.: `IMG 6967`):
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/registro.py" vincular --jira SCC-1762 --titulo "<resumo do card>" --tipo reel`
  (tipos: reel, cortes, tutorial, youtube, anuncio, story, outro). Use o título que o gestor reconhece.
- Se souber o tipo e o título logo no começo, rode o `vincular` uma vez: o relatório compara tempo por tipo.
- Rode as etapas pelas ferramentas de verdade (whisper, ffmpeg, seus agentes): é isso que vira etapa no painel.

## Acabamento no Premiere
Quando o vídeo sai do Claude para o Premiere (XML/EDL/FCPXML/.prproj gerado, Premiere aberto, ou o editor diz
"vou finalizar no Premiere"), o plugin muda a etapa para **Acabamento (Premiere)** e liga um vigia leve, só deste
vídeo: a cada minuto, se o Premiere está em primeiro plano conta como acabamento ativo (lê só o nome do app em frente,
sem permissão e sem ver a tela); um vídeo novo exportado na pasta do trabalho vira prévia na central; o `.prproj`
salvo vira passo. O vigia para sozinho quando o vídeo é fechado. Gere o XML/projeto na pasta do trabalho (ao lado do
material), para o vigia achar os exports. Ao terminar no Premiere, o editor diz "entreguei" como sempre.

## Ao terminar
Quando o editor disser que entregou, o plugin te lembra com o id. Confirme quantas peças e rode:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/registro.py" fim --status entregue --pecas <N>`
(`--status refazer` se voltou para ajuste, `--status cancelado` se foi abandonado; `--id` se houver mais de um vídeo).
Se a central abriu um vídeo numa conversa que não era edição (relatório, contagem, pesquisa: "isso não era edição"),
feche com `--status nao_edicao`: some do painel e não conta no relatório.
Se veio da central (D-0012), entregue também lá, com o link compartilhável do Drive:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/central.py" entregar --id D-0012 --link <link> --pecas <N> --nota "<o que vale saber>"`.

## Biblioteca (agentes e skills dos repositórios que a gestão conectou)
- "Que skills tem na biblioteca?" / "tem algum agente de legenda?" → `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/kit.py" biblioteca [termo]`.
- "Puxa a skill X" → confirme com o editor qual item (pelo ID da lista) e rode `kit.py puxar <ID>`; "tira o X" → `kit.py tirar <ID>`.
  Instala em ~/.claude com o prefixo `cw-bib-` e fica disponível na próxima sessão (diga isso ao editor). Também dá
  para escolher pela página: central › Biblioteca › "Puxar para meu Claude".
- "Instala o plugin X da biblioteca (ID n)" (o botão da página copia esse pedido) → `kit.py plugin <ID>`: adiciona o
  catálogo e instala o plugin. Depois diga ao editor para fechar e abrir o Claude.

## Kit do time
- "Compartilha meus agentes com a central" → primeiro mostre o que será enviado:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/kit.py" listar` e confirme com o editor (ele pode escolher só alguns);
  depois `kit.py compartilhar` (ou `--nomes a,b`). Vão só os arquivos de instrução (agente .md, SKILL.md e textos da
  skill), com tokens e chaves trocados por •••. A gestão avalia na aba Kit do time.
- O kit oficial chega sozinho ao abrir a sessão, com o prefixo `cw-kit-` (agentes em ~/.claude/agents, skills em
  ~/.claude/skills). Prefira esses fluxos ao editar. Para puxar agora: `kit.py sincronizar` (vale na próxima sessão).

### Transferir o meu jeito de editar para a IA do time (Titeu, FabI.Ano)
Pedido: "transfere meu jeito de editar para a IA" / "prepara meu estilo para o FabI.Ano (ou Titeu)". Faça o roteiro de
"Preparar o fluxo para o KIT" abaixo e, no MESMO pacote (`~/CW-Kit/<kit>/skills/<nome-do-estilo>/`), acrescente:
1. **APRENDIZADOS.md**: tudo o que este editor já ensinou ao Claude e vale para o estilo, em regras curtas e acionáveis:
   o CLAUDE.md dos projetos de edição dele, as memórias (`~/.claude/projects/*/memory/*.md` que falam de edição), correções
   que ele já fez ("não use…", "sempre…"), medidas aprovadas (tamanhos, cores, tempos, posições) e exemplos do que foi
   reprovado e por quê. Nada de dado pessoal, senha ou token. Mostre ao editor e confirme antes de enviar.
2. **RECEITA.md**: como produzir UM vídeo neste estilo **sem ninguém olhando e sem app de desktop** (servidor Linux, sem
   Premiere/After/CapCut), com as seções `## EPISÓDIO` (opcional, o que se faz uma vez por material) e `## CORTE` (um vídeo).
   Diga a entrada (ex.: para reels, a pasta do bruto com a câmera e, se houver, a gravação de tela do OBS), cada comando ou
   script da skill na ordem, onde fica o final (`final`) e uma prévia leve (`previa`), e como conferir antes de entregar.
   Termine pedindo a resposta em JSON: `{"aprovado": true|false, "motivo": "...", "legenda_post": "...", "final": "caminho", "previa": "caminho"}`.
   Teste a receita a partir da cópia do pacote antes de enviar.
3. **EXEMPLOS/**: 2 ou 3 vídeos aprovados de referência (até 50 MB cada) ou quadros deles, para a IA comparar o resultado.
Depois envie com `kit.py compartilhar --pasta ~/CW-Kit/<kit> --kit <kit>` e diga ao editor que a gestão transforma o pacote
num Estilo da IA (aba do Titeu/FabI.Ano › Estilos).

### Preparar o fluxo para o KIT - Cardápio Web ou o KIT - CW Cast
Pedido: "prepara meus agentes para o kit da Cardápio Web" (ou "do CW Cast"). O objetivo é que o fluxo rode igual em
qualquer máquina do time e no servidor da nuvem (Linux, sem Premiere, Drive em `~/Drive`), com o mesmo estilo de edição.
Não mexa nos originais do editor: monte uma cópia em `~/CW-Kit/<kit>/` (kit = `cardapio-web` ou `cw-cast`).

1. **Levante o fluxo.** Pergunte qual é o fluxo daquela marca e ache tudo que ele usa: agentes (~/.claude/agents e
   `.claude/agents` do projeto), skills (~/.claude/skills, as do app em ~/.claude/skills/synced, as do projeto), o
   CLAUDE.md do projeto, scripts, e os arquivos de estilo (fontes, logos, vinhetas, trilhas, efeitos sonoros, LUTs,
   presets, templates do Remotion/HTML). Mostre a lista ao editor e confirme antes de seguir.
2. **Monte o pacote:** `skills/<nome>/SKILL.md` com tudo o que a skill usa dentro da própria pasta (scripts/, assets/,
   estilos/…) e `agents/<nome>.md`. Agente não leva arquivos: se ele precisa de arquivos, eles vão numa skill do pacote
   e o agente diz para usar a skill `cw-kit-<nome-da-skill>`. Regras do CLAUDE.md que fazem parte do estilo viram uma
   skill (ex.: `estilo-cw-cast`) ou vão para o texto do agente.
3. **Deixe portátil:**
   - nenhum caminho da máquina (`/Users/...`, `C:\Users\...`, `~/Library/CloudStorage/...`): arquivos da skill são
     referenciados a partir da pasta da skill ("a pasta desta skill"), e o Drive pelo caminho dentro do drive
     "CW Studios - Produção" (ex.: `CW Cast - Cortes/11. PUBLICAÇÃO/1. Para aprovar`), dizendo que a raiz do Drive é a
     do computador (no servidor, `~/Drive`);
   - nomes finais: o kit instala com o prefixo `cw-kit-`, então referências a outros itens do pacote usam `cw-kit-<nome>`;
   - nada de tokens, senhas ou chaves (a central troca por •••, mas não deixe nem chegar lá).
4. **Escreva `REQUISITOS.md`** na raiz do pacote (ou um por skill): programas (ffmpeg, Python e pacotes pip, Node e
   pacotes npm, fontes que precisam estar instaladas), conectores (MCP) e APIs usados, e marque com **[só no computador]**
   cada etapa que depende de app de desktop (Premiere, After Effects, CapCut): no servidor essa etapa é pulada ou
   entregue para o editor finalizar.
5. **Teste** pelo menos o começo do fluxo a partir da cópia do pacote (leitura dos arquivos, scripts rodando com caminhos
   relativos) e corrija o que quebrar.
6. **Envie**, depois do OK do editor:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/kit.py" compartilhar --pasta ~/CW-Kit/<kit> --kit <kit>`.
   Textos vão como texto; os demais arquivos (até 50 MB cada) vão como anexos guardados na central e chegam junto quando
   o item entra no kit. Diga ao editor o que foi enviado e o que ficou de fora (acima de 50 MB: avise para a gestão decidir).

## Escritório (estação)
- "Liga minha estação do escritório" → confira que está conectado (`central.py quem`) e rode
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/estacao.py" ligar`. Explique em uma linha: a partir de agora ele pode
  escrever na central › Escritório e o Claude desta máquina executa, mesmo com esta janela fechada; o Claude age
  sozinho só dentro da pasta do vídeo e pede OK na página para o resto. A estação sobe sozinha ao ligar o computador (Mac: serviço do macOS; Windows: item na Inicialização, sem janela). No Windows, se a estação não subir, confira se o Python está instalado (python.exe no PATH).
- "Desliga minha estação" → `estacao.py desligar`. "A estação está ligada?" → `estacao.py status`.
- Só o próprio editor manda pedidos e aprova ações na estação dele; a gestão só acompanha. Nunca ligue a estação
  de outra pessoa nem mude a cerca (`~/.cw-cortes/estacao/cerca.json`).

## Publicação (cortes prontos → Instagram, TikTok, YouTube Shorts)
- "Manda pra publicação" / "posta esse corte": para cada corte final (mp4 vertical), escreva a legenda no tom do CW Cast
  (gancho na 1ª linha, 1–3 frases, chamada para o episódio completo, 3–6 hashtags) e um título curto para o YouTube
  (até 100 caracteres). Mostre ao editor e ajuste se ele pedir. Depois rode, um por corte:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/publicar.py" enviar --arquivo <corte.mp4> --titulo "<título>" --legenda "<legenda>" --titulo-youtube "<título YT>" [--episodio EP52] [--jira SCC-…] [--agendar "21/10 18:00"]`
  O arquivo vai para a pasta "1. Para aprovar" do Drive e o card aparece na central para a produção aprovar.
  Nada é postado sem a aprovação. Só uma rede: `--redes instagram` (ou `tiktok`, `youtube`, separadas por vírgula).
- "Como estão meus envios?" → `publicar.py minhas` (aguardando, aprovado, postado com links, ou recusado com o motivo).
  Recusado: ajuste o que o motivo pede e envie de novo.

## Ferramentas da CW Studios (Higgsfield e outras APIs da empresa)
- O conector `cw-studios` (vem com este plugin) dá ao Claude as APIs da empresa sem chave nenhuma na máquina:
  `listar` (o que existe, link da documentação, gerações usadas no mês), `gerar` (PAGO, conta no limite mensal do editor),
  `status` (andamento; com `esperar_segundos` espera terminar) e `baixar` (traz o resultado para a pasta do trabalho).
- Antes de `gerar`, leia a documentação da ferramenta (ex.: Higgsfield `https://docs.higgsfield.ai/docs/llms.txt`) para
  achar o caminho do modelo e os parâmetros, e confirme com o editor o que vai gerar e quantas vezes. Prefira o
  conector da empresa à conta pessoal do editor. Passe `trabalho` (SCC-…/D-…) quando houver. Arquivos de entrada
  precisam ser URLs públicas. Os resultados ficam na API ~7 dias: baixe o que for usar.

## Fila da central
- "O que tem na fila?" → `central.py fila` (as do editor primeiro; a gestão pode ter atribuído alguma a ele).
- "Pega a próxima" / "pega a D-0012" → `central.py pegar [--id D-0012] --json` e, em seguida, `preparar.py D-0012`
  (o brief já traz o pedido, o ajuste pedido e as regras). Sem material na pasta: use o link da demanda, se houver.
- "Não vai dar" → `central.py devolver --id D-0012 --motivo "..."`.

## Espelho na central

Toda sessão deste Claude conectado à central aparece para a gestão em "Computador de <editor>", na seção "Direto no Claude": pedidos do editor, respostas e passos do Claude. Senhas e tokens são mascarados antes de sair da máquina, e tudo é apagado em 15 dias. O editor é avisado no começo de cada sessão. Se ele perguntar, confirme isso com franqueza. As sessões da estação do escritório não entram no espelho, porque já aparecem na página.

## Projeto da estação (agentes, skills e estilo do editor)

A estação abre o Claude na pasta do projeto do editor, se ela estiver definida. Assim ela usa o CLAUDE.md, os agentes, as skills e as fontes de sempre, e grava os arquivos na pasta do vídeo. Quando o editor pedir "usa a pasta X como projeto da estação" ou "a estação não está no meu estilo", rode:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/estacao.py" projeto "<pasta do projeto>"`
Use a pasta onde ele costuma abrir o Claude para editar. Na dúvida, use a pasta atual desta sessão, se ela tiver CLAUDE.md ou .claude/. Depois avise que vale para as conversas novas: ele precisa clicar em "Nova conversa" na central. Para voltar ao padrão: `estacao.py projeto --limpar`.

## Recomendar ferramentas e itens da biblioteca

No começo de cada sessão, a central manda o catálogo do time: as Ferramentas (APIs pagas da empresa, pelo conector cw-studios) e os itens da Biblioteca, dizendo quais já estão no Claude deste editor. Quando o pedido combinar com algum deles (gerar voz ou narração → voicestudio; imagem ou vídeo com IA → Higgsfield; motion em HTML → hyperframes), sugira em uma linha antes de improvisar. Se o editor aceitar um item que ainda não está instalado, adicione com `kit.py puxar <id>`: ele passa a valer na próxima mensagem da estação ou na próxima sessão. Para gerar com uma ferramenta paga, confirme antes o que vai ser gerado e o custo.

## Levar o padrão do editor para o time (CLAUDE.md e skills)

Quando o editor pedir "manda meu CLAUDE.md / minhas regras para o padrão do time":
1. Leia o CLAUDE.md da pasta do projeto dele e o `~/.claude/CLAUDE.md`, se existir.
2. Separe só o que é **padrão de edição** e vale para o time: estilo, fonte, legenda, motion, cortes, programas (CW Chefs, CW Cast…), o que evitar. Deixe de fora caminhos da máquina dele, senhas, contas e preferências pessoais de conversa.
3. Escreva uma regra por item, curta e direta (até 400 letras). Quando ela valer só para um tipo de vídeo, use o campo tipo: reel, cortes, tutorial, youtube, anuncio, story ou outro.
4. Mostre a lista ao editor e, se ele concordar, grave-a num JSON (`[{"texto": "...", "tipo": null}]`) e rode:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/central.py" sugerir-regras --arquivo regras.json --origem "CLAUDE.md de <pasta>"`
5. Avise que as regras ficam esperando a gestão aprovar em "Padrão do time" e, depois de aprovadas, entram em todo Claude do time.

Quando ele pedir "compartilha minhas skills e agentes no kit do time", rode `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/kit.py" listar`, confirme com ele quais vão e rode `kit.py compartilhar --nomes a,b`. A skill vai com as subpastas (agentes/, estilos/...). A gestão decide se o item vai para todo o time ou fica na Biblioteca.

Se o envio para publicação der "Permission denied" ou "não achei a pasta", não conclua sozinho que é falta de acesso. Rode `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/publicar.py" pasta` e mostre ao editor o resultado: ele lista cada letra ou pasta do Google Drive da máquina, se ela tem o CW Cast e se dá para gravar.

## Economia de limite: modelo barato para o trabalho braçal de texto

O conector cw-studios tem a ferramenta **texto**, que manda um texto longo para um modelo barato (OpenRouter, pago pela empresa, centavos) e devolve só a resposta. Ela não gasta o limite do Claude do editor. Use quando precisar **ler muito texto para tirar pouco**:
- triagem de momentos numa transcrição de episódio;
- resumo de reunião ou roteiro;
- lista de títulos ou headlines;
- conferência de legenda contra a transcrição.

Passe o **caminho** do arquivo em `arquivos` (o conector lê na máquina). Nunca cole o texto inteiro na chamada: isso gasta os tokens que queremos economizar. Modelo padrão: `anthropic/claude-haiku-5.5`. Use `anthropic/claude-sonnet-5.5` se a triagem precisar de mais julgamento. Para resposta longa, use `salvar_em` e leia só o que precisar. A decisão final (quais cortes, revisão editorial) continua com você.

Na estação, cada conversa pode ter um modelo próprio (escolhido pelo editor na página). Para produção mecânica, prefira o Sonnet.
