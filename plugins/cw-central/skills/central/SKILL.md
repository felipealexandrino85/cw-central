---
name: central
description: Central CW Studios — pré-produção e acompanhamento da edição de vídeo para o painel da gestão. Use quando o editor for começar um vídeo de um card ou demanda ("vamos fazer o SCC-1762", "prepara a D-0012", "pega a próxima"), pedir "me conecta na central com o token cw_…", disser de qual card é o vídeo ("é do SCC-1762", "é a D-0012"), disser que terminou ou entregou um vídeo ("entreguei", "terminei o vídeo", "pode fechar"), corrigir título ou tipo do vídeo, perguntar "o que tem na fila?", ligar/desligar a estação do Escritório ("liga minha estação"), procurar ou puxar agentes/skills da biblioteca ("o que tem na biblioteca?", "puxa a skill X"), usar as ferramentas da empresa (Higgsfield), mandar um corte pronto para publicar nas redes ("manda pra publicação", "posta esse corte"), pedir "pega a próxima"/"pega a D-0012", ou devolver uma demanda. Funciona com qualquer agente de edição; não edita vídeo.
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

## Escritório (estação)
- "Liga minha estação do escritório" → confira que está conectado (`central.py quem`) e rode
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/estacao.py" ligar`. Explique em uma linha: a partir de agora ele pode
  escrever na central › Escritório e o Claude desta máquina executa, mesmo com esta janela fechada; o Claude age
  sozinho só dentro da pasta do vídeo e pede OK na página para o resto. A estação sobe sozinha ao ligar o Mac.
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
