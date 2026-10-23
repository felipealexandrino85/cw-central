#!/usr/bin/env python3
"""Registro de produção do hub de cortes: quem editou o quê, quanto tempo levou e quanto custou.

Só biblioteca padrão (roda com o python3 do sistema, 3.9+), para funcionar antes do venv existir.

Comandos:
  inicio   --tipo reel|cortes|tutorial|outro --titulo "..." [--area X] [--entrada caminho] [--demanda D-0012]
           abre um trabalho e liga à sessão atual; imprime o id
  retomar  --id ID          liga um trabalho já aberto à sessão atual (continuação noutro dia)
  fim      --id ID --status entregue|refazer|cancelado [--pecas N] [--saida caminho] [--nota "..."]
  sessao   [--evento Stop|SessionEnd]   chamado pelos hooks; lê o JSON do hook no stdin
  enviar                          manda à central o histórico de vídeos (180 dias); roda sozinho no início/fim
  atividade                       hook PostToolUse: traduz skill/agente/script em etapa da edição (ao vivo) e abre
                                  o vídeo sozinho quando o Claude mexe num arquivo de vídeo pela primeira vez
  abertura                        hook SessionStart: sinal de vida e regras do time como contexto do Claude
  prompt                          hook UserPromptSubmit: liga o vídeo ao card citado (SCC-1762, D-0012) e lembra o
                                  Claude de fechar o vídeo quando o editor diz que entregou
  vigia-premiere --trabalho ID --pasta DIR [--sessao SID]   enquanto o vídeo está em acabamento: minutos com o Premiere
                                  em primeiro plano, exports novos (com prévia) e projeto salvo
  previa   --sessao SID --arquivo VIDEO   3 quadros (início, meio e fim) do vídeo gerado, para o card do ao vivo
  vincular [--id ID] [--jira K] [--demanda D-0012] [--titulo T] [--tipo reel|...]   corrige o vídeo aberto
  resumo   [--dias 30] [--json arquivo]  junta o registro do time (ou o local) e imprime o painel
  pulso    [--hook SessionStart]          sinal de vida da sessão (status.json para a central de controle);
           no SessionStart deixa um vigia em segundo plano mandando sinal a cada minuto
  configurar --nome "Ana Souza" [--pasta X]  primeira vez no computador: acha a pasta do time no Drive
           (pelo arquivo cw-registro-time.txt), grava nome e pasta no perfil e manda o primeiro sinal
  config   mostra pessoa e pastas em uso

Onde grava:
  ~/.cw-cortes/registro/<AAAA-MM>.jsonl                 (fonte da verdade, sempre)
  ~/.cw-cortes/registro/status.json                     (quem está conectado e no que está, agora)
  <Pasta do registro do time>/<pessoa>/...              (espelho dos dois, se o perfil tiver a pasta)
  Central CW Studios (Supabase)                         (status.json a cada sinal, se central.py entrar foi feito)
"""
import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HUB = Path(os.environ.get("CW_CORTES_HOME", Path.home() / ".cw-cortes"))
LOCAL = HUB / "registro"
ESTADO = LOCAL / ".sessoes.json"
PLUGIN = Path(__file__).resolve().parents[1]

# Uso real do plugin numa resposta (a lista de skills aparece em todo transcript e não conta)
USO_PLUGIN = re.compile(r'"(?:skill|subagent_type)":\s*"cw-cortes:|\.cw-cortes/venv/bin/python ')

INTERVALO_STOP_S = 600   # no Stop, só relê o transcript se a última leitura tem mais de 10 min
PAUSA_MAX_S = 300        # buraco maior que 5 min entre mensagens não conta como tempo ativo
PULSO_S = int(os.environ.get("CW_PULSO_S", "60"))            # sinal de vida da sessão aberta
SESSAO_MORTA_S = 600     # sessão sem sinal há 10 min sai do status (fechou sem SessionEnd)
STATUS = LOCAL / "status.json"

# US$ por milhão de tokens: entrada, saída, leitura de cache. Escrita de cache = 1,25x (5 min) ou 2x (1 h)
# da entrada. Tabela de 06/10/2026; é ESTIMATIVA (equivalente API). Modelo novo: acrescente aqui.
PRECOS = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}


def agora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def versao_plugin():
    try:
        return json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
    except Exception:
        return "?"


def ler_perfil():
    """Lê 'Nome no registro' e 'Pasta do registro do time' do perfil.md (variáveis de ambiente vencem)."""
    campos = {}
    try:
        for linha in (HUB / "perfil.md").read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*-\s*([^:]+):\s*(.+?)\s*$", linha)
            if m:
                campos[m.group(1).strip().lower()] = m.group(2).strip()
    except FileNotFoundError:
        pass
    pessoa = os.environ.get("CW_REGISTRO_PESSOA") or campos.get("nome no registro") or ""
    if not pessoa or pessoa.startswith("("):
        pessoa = os.environ.get("USER") or socket.gethostname().split(".")[0]
    pasta = os.environ.get("CW_REGISTRO_PASTA") or campos.get("pasta do registro do time") or ""
    if pasta.startswith("("):
        pasta = ""
    pasta = Path(os.path.expanduser(pasta)) if pasta else None
    return slug(pessoa), pasta


def slug(texto):
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9._-]+", "-", sem_acento.strip().lower()).strip("-") or "sem-nome"


def nome_exibicao():
    """Nome como a pessoa escreveu no perfil (com acento), para a central."""
    try:
        m = re.search(r"^- Nome no registro:\s*(.+?)\s*$", (HUB / "perfil.md").read_text(encoding="utf-8"), re.M)
        if m and not m.group(1).startswith("("):
            return m.group(1)
    except OSError:
        pass
    return os.environ.get("CW_REGISTRO_PESSOA") or ""


def arquivo_mes(base, ts=None):
    return base / "{}.jsonl".format((ts or datetime.now()).strftime("%Y-%m"))


def gravar(evento):
    """Acrescenta no registro local e espelha o arquivo do mês na pasta do time."""
    pessoa, pasta = ler_perfil()
    evento.setdefault("ts", agora())
    evento.setdefault("pessoa", pessoa)
    evento.setdefault("maquina", socket.gethostname().split(".")[0])
    evento.setdefault("plugin", versao_plugin())
    LOCAL.mkdir(parents=True, exist_ok=True)
    local = arquivo_mes(LOCAL)
    with open(local, "a", encoding="utf-8") as f:
        f.write(json.dumps(evento, ensure_ascii=False) + "\n")
    espelhar(local, pessoa, pasta)


def espelhar(local, pessoa, pasta):
    if not pasta:
        return
    try:
        destino = pasta / pessoa / local.name
        destino.parent.mkdir(parents=True, exist_ok=True)
        tmp = destino.with_suffix(".tmp")
        tmp.write_bytes(local.read_bytes())
        os.replace(tmp, destino)
    except OSError as e:  # Drive offline: o local continua certo e o próximo evento espelha de novo
        print("registro: não consegui espelhar em {} ({})".format(pasta, e), file=sys.stderr)


def eventos_locais():
    for arq in sorted(LOCAL.glob("*.jsonl")):
        for linha in open(arq, encoding="utf-8"):
            try:
                yield json.loads(linha)
            except ValueError:
                continue


# ---------------------------------------------------------------- trabalhos

def cmd_inicio(a):
    tid = a.id or "{}-{}-{}".format(datetime.now().strftime("%Y%m%d"), slug(a.titulo)[:40], uuid.uuid4().hex[:4])
    gravar({"ev": "inicio", "id": tid, "tipo": a.tipo, "titulo": a.titulo, "area": a.area or "",
            "entrada": a.entrada or "", "demanda": a.demanda or "", "jira": (a.jira or "").upper(),
            "sessao": os.environ.get("CLAUDE_CODE_SESSION_ID", "")})
    atualizar_status(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "sinal")
    enviar_em_segundo_plano()
    print(tid)


def cmd_retomar(a):
    if not any(e.get("id") == a.id and e.get("ev") == "inicio" for e in eventos_locais()):
        sys.exit("registro: trabalho {} não existe neste computador (veja 'resumo')".format(a.id))
    gravar({"ev": "retomar", "id": a.id, "sessao": os.environ.get("CLAUDE_CODE_SESSION_ID", "")})
    atualizar_status(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "sinal")
    enviar_em_segundo_plano()
    print(a.id)


def cmd_fim(a):
    if not a.id:
        t = trabalho_da_sessao(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        if not t:
            sys.exit("registro: diga qual vídeo fechar (--id); nenhum aberto nesta sessão")
        a.id = t["id"]
    gravar({"ev": "fim", "id": a.id, "status": a.status, "pecas": a.pecas, "saida": a.saida or "",
            "nota": a.nota or "", "sessao": os.environ.get("CLAUDE_CODE_SESSION_ID", "")})
    atualizar_status(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "sinal")
    enviar_em_segundo_plano()
    print(a.id)


# ---------------------------------------------------------------- sessões (hooks)

def preco(modelo):
    for chave in sorted(PRECOS, key=len, reverse=True):  # "claude-opus-5-5" antes de "claude-opus-5"
        if modelo.startswith(chave):
            return PRECOS[chave]
    return None


def mensagem_humana(o):
    """Mensagem que o editor escreveu (não é resultado de ferramenta nem mensagem interna)."""
    if o.get("type") != "user" or o.get("isMeta") or o.get("toolUseResult") is not None:
        return False
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        return bool(c.strip()) and not c.lstrip().startswith("<")
    return isinstance(c, list) and any(x.get("type") == "text" for x in c) and not any(x.get("type") == "tool_result" for x in c)


def nome_ferramenta(bloco):
    """'skill:x', 'agente:y' ou 'mcp:servidor' de um tool_use (o que compõe o fluxo de edição)."""
    nome, ent = bloco.get("name") or "", bloco.get("input") or {}
    if nome == "Skill" and ent.get("skill"):
        return "skill:" + ent["skill"]
    if nome in ("Agent", "Task") and ent.get("subagent_type"):
        return "agente:" + ent["subagent_type"]
    if nome.startswith("mcp__"):
        return "mcp:" + nome.split("__")[1]
    return None


def ler_transcript(caminho, por_modelo, vistos, momentos, agentes, nome_agente=None, atencao=None, fluxo=None):
    """Soma o usage por modelo (cada resposta aparece uma vez por bloco: deduplica pelo id).
    atencao: soma as mensagens do editor e o tempo que ele levou para responder (lendo e decidindo), até 5 min cada.
    fluxo: conta skills, agentes e conectores usados."""
    custo_oficial = None
    usa_plugin = False
    ultimo_claude = None
    with open(caminho, encoding="utf-8", errors="replace") as f:
        for linha in f:
            if '"cost-state"' in linha:
                try:
                    custo_oficial = json.loads(linha).get("totalCostUSD")
                except ValueError:
                    pass
                continue
            if '"type":"assistant"' not in linha and '"type":"user"' not in linha:
                continue
            if not usa_plugin and '"type":"assistant"' in linha and USO_PLUGIN.search(linha):
                usa_plugin = True
            try:
                o = json.loads(linha)
            except ValueError:
                continue
            ts = o.get("timestamp")
            if ts:
                momentos.append(ts)
            if atencao is not None and ts and mensagem_humana(o):
                atencao["interacoes"] += 1
                if ultimo_claude:
                    try:
                        gap = (datetime.fromisoformat(ts.replace("Z", "+00:00"))
                               - datetime.fromisoformat(ultimo_claude.replace("Z", "+00:00"))).total_seconds()
                        atencao["segundos"] += min(max(gap, 10), PAUSA_MAX_S)
                    except ValueError:
                        pass
            if o.get("type") != "assistant":
                continue
            ultimo_claude = ts or ultimo_claude
            msg = o.get("message") or {}
            if fluxo is not None and isinstance(msg.get("content"), list):
                for bloco in msg["content"]:
                    nome = bloco.get("type") == "tool_use" and nome_ferramenta(bloco)
                    if nome:
                        fluxo[nome] = fluxo.get(nome, 0) + 1
            mid = msg.get("id") or o.get("requestId")
            u = msg.get("usage")
            if not u or mid in vistos:
                continue
            vistos.add(mid)
            m = por_modelo.setdefault(msg.get("model") or "?", {"entrada": 0, "saida": 0, "cache_leitura": 0,
                                                                "cache_5m": 0, "cache_1h": 0, "chamadas": 0})
            m["entrada"] += u.get("input_tokens", 0)
            m["saida"] += u.get("output_tokens", 0)
            m["cache_leitura"] += u.get("cache_read_input_tokens", 0)
            cc = u.get("cache_creation") or {}
            h1 = cc.get("ephemeral_1h_input_tokens", 0)
            m["cache_1h"] += h1
            m["cache_5m"] += max(u.get("cache_creation_input_tokens", 0) - h1, 0)
            m["chamadas"] += 1
            if nome_agente:
                agentes[nome_agente] = agentes.get(nome_agente, 0) + 1
    return custo_oficial, usa_plugin


def segundos_ativos(momentos):
    pontos = sorted(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in momentos)
    total = 0.0
    for antes, depois in zip(pontos, pontos[1:]):
        total += min((depois - antes).total_seconds(), PAUSA_MAX_S)
    return int(total), (pontos[0].isoformat() if pontos else ""), (pontos[-1].isoformat() if pontos else "")


def custo_estimado(por_modelo):
    total, desconhecido = 0.0, False
    for modelo, m in por_modelo.items():
        p = preco(modelo)
        if not p:
            if m["chamadas"] and modelo != "<synthetic>":
                desconhecido = True
            continue
        ent, sai, leit = p
        total += (m["entrada"] * ent + m["saida"] * sai + m["cache_leitura"] * leit
                  + m["cache_5m"] * ent * 1.25 + m["cache_1h"] * ent * 2) / 1e6
    return round(total, 4), desconhecido


def cmd_sessao(a):
    try:
        dados = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    sid = dados.get("session_id") or ""
    transcript = dados.get("transcript_path") or ""
    if not sid or not os.path.isfile(transcript):
        return
    atualizar_status(sid, "fim" if a.evento == "SessionEnd" else "sinal")
    espelho_em_segundo_plano(sid, transcript, forcar=True, fim=a.evento == "SessionEnd")
    estado = {}
    try:
        estado = json.loads(ESTADO.read_text())
    except (OSError, ValueError):
        pass
    if a.evento == "Stop" and time.time() - estado.get(sid, 0) < INTERVALO_STOP_S:
        return

    trabalhos = sorted({e["id"] for e in eventos_locais()
                        if e.get("ev") in ("inicio", "retomar") and e.get("sessao") == sid})
    por_modelo, vistos, momentos, agentes = {}, set(), [], {}
    atencao, fluxo = {"interacoes": 0, "segundos": 0.0}, {}
    oficial, usa_plugin = ler_transcript(transcript, por_modelo, vistos, momentos, agentes, atencao=atencao, fluxo=fluxo)
    pasta_sub = Path(transcript[:-len(".jsonl")]) / "subagents"
    for sub in sorted(glob.glob(str(pasta_sub / "*.jsonl"))):
        nome = "subagente"
        try:
            nome = json.loads(Path(sub[:-len(".jsonl")] + ".meta.json").read_text()).get("agentType", nome)
        except (OSError, ValueError):
            pass
        ler_transcript(sub, por_modelo, vistos, momentos, agentes, nome)
    if not trabalhos and not usa_plugin:  # sessão que não é de edição: não entra no registro
        return

    ativos, inicio, fim = segundos_ativos(momentos)
    try:
        por_etapa = json.loads(arquivo_atividade(sid).read_text()).get("por_etapa", {})
    except (OSError, ValueError):
        por_etapa = {}
    estimado, desconhecido = custo_estimado(por_modelo)
    gravar({"ev": "sessao", "sessao": sid, "trabalhos": trabalhos, "gatilho": a.evento,
            "origem": os.environ.get("CLAUDE_CODE_ENTRYPOINT", ""), "inicio": inicio, "fim": fim,
            "segundos_ativos": ativos, "modelos": por_modelo, "subagentes": agentes,
            "tokens": sum(m["entrada"] + m["saida"] + m["cache_leitura"] + m["cache_5m"] + m["cache_1h"]
                          for m in por_modelo.values()),
            "custo_estimado_usd": estimado, "custo_oficial_usd": oficial, "modelo_sem_preco": desconhecido,
            "por_etapa": por_etapa, "interacoes": atencao["interacoes"],
            "segundos_humanos": min(round(atencao["segundos"]), ativos), "fluxo": fluxo})
    if trabalhos:
        enviar_em_segundo_plano()
    estado[sid] = time.time()
    LOCAL.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps(estado))


# ---------------------------------------------------------------- atividade ao vivo (hook PostToolUse)

# Etapas da edição, na ordem do pipeline (reel, cortes e CW Studio caem nas mesmas).
ETAPAS = ["preparo", "transcricao", "corte", "montagem", "motion", "render", "acabamento", "revisao", "entrega"]
NOME_ETAPA = {"preparo": "Preparo", "transcricao": "Transcrição", "corte": "Corte e seleção",
              "montagem": "Montagem", "motion": "Legenda e motion", "render": "Render", "acabamento": "Acabamento (Premiere)",
              "revisao": "Revisão", "entrega": "Entrega"}
# (padrão no comando, etapa, o que aparece no painel). Primeiro que casar vence.
SCRIPTS = [
    (r"\bjob\.py\s+entrega|entrega\.py|publica_youtube|seo_txt", "entrega", "entregando"),
    (r"\bqc\.py|\bqa\.py|revisa_timeu|confere_reel|libera\.py|output_qc|licao\.py", "revisao", "revisão / QC"),
    (r"master_v5|reelscw/render|reels01/render|timeu01/render|podcast02/render|cinema01/render|render_template"
     r"|render_ref|\bmonta\.py|orquestrador-cortes/esteira\.py|corte-viral/scripts/render\.py", "render", "renderizando"),
    (r"legenda|captions|subtitles", "motion", "legendando"),
    (r"build_motion|cw_motion|autocena|cenas|cutout|asset_bank|compose_template|foto_real|sfx|\bsom\.py"
     r"|night_photo|dinamica|voxlab|apoio", "motion", "motion, assets e som"),
    (r"qc_montagem|verifica_ouvido", "montagem", "conferindo a montagem"),
    (r"montagem\.py|sincroniza_tela|enquadra\.py|autoframe|framing|reframe_active|active_speaker|corte_tela",
     "montagem", "montando e enquadrando"),
    (r"limpa_takes|fala_camera|ritmo\.py|leitura\.py|aperta_pausas|olhar\.py", "corte", "limpando takes e ritmo"),
    (r"digest\.py|confere_plano", "corte", "selecionando momentos"),
    (r"transcreve|transcribe|transcrever|realinhar|normalize_transcript", "transcricao", "transcrevendo"),
    (r"prepara_reel|media_probe|preflight|recebe_episodio|identifica_video|classifica_fonte|extract_audio"
     r"|visual_index|\bjob\.py\s+init", "preparo", "preparando o material"),
]
SKILLS = {"corte-viral": ("corte", "selecionando cortes"), "editor-reels": ("corte", "editor de reels"),
          "motion-vox": ("motion", "motion"), "revisor-cortes": ("revisao", "revisando"),
          "orquestrador-cortes": ("preparo", "orquestrando os cortes"), "cw-studio": ("preparo", "equipe CW Studio"),
          "novo-estilo": (None, "criando estilo")}
AGENTES = {"context-mapper": ("corte", "mapeando o vídeo"), "clip-hunter": ("corte", "caçando momentos"),
           "clip-builder": ("corte", "montando candidatos"), "clip-deduplicator": ("corte", "tirando repetidos"),
           "feed-psychologist": ("corte", "avaliando ganchos"), "blind-reviewer": ("corte", "revisão cega dos cortes"),
           "aprovador-cortes": ("corte", "aprovando cortes"), "edit-director": ("montagem", "planejando a edição"),
           "diretor-motion": ("motion", "dirigindo o motion"), "quality-controller": ("revisao", "QC editorial"),
           "revisor-cortes": ("revisao", "revisor checando o master"),
           "editor-reels": ("corte", "editor de reels"), "editor-reels-cardapio-web": ("corte", "editor de reels CW"),
           "orquestrador-cortes": (None, "orquestrador"), "hub-cortes": (None, "hub")}
# Quando o comando não é de um script do cw-cortes: reconhece pelo que ele faz (qualquer editor, qualquer agente).
GENERICO = [
    (r"rclone\s+(copy|copyto|move|moveto|sync)\b|gdrive\s+(files\s+)?upload|youtube.*upload|upload_(video|youtube)|publica_youtube",
     "entrega", "entregando"),
    (r"whisper|transcri|speech.?to.?text|assemblyai|deepgram", "transcricao", "transcrevendo"),
    (r"(final|master|export|render|entrega|pronto)[^/\s\"']*\.(mp4|mov)\b", "render", "renderizando a versão final"),
    (r"\.(srt|ass|vtt)\b|subtitles=|legend|caption", "motion", "legendando"),
    (r"drawtext|overlay=|remotion|lottie|manim|motion|after.?effects|\.aep\b|moviepy", "motion", "motion e grafismos"),
    (r"loudnorm|afftdn|amix|sidechain|trilha|sfx", "motion", "som e trilha"),
    (r"crop=|reframe|autoframe|9:16|1080:1920", "montagem", "enquadrando"),
    (r"concat|-ss\s|-to\s|trim=|atrim|silencedetect|auto-editor", "corte", "cortando e emendando"),
]
# Passagem para o Premiere (o Claude gera XML/EDL/projeto, ou abre o Premiere): vira etapa de acabamento.
PREMIERE = [(r"\.(prproj|fcpxml|edl|aaf)\b|xmeml|premiere", "acabamento", "passando para o Premiere")]
PROJETO_PREMIERE = re.compile(r"\.(prproj|fcpxml|edl|aaf)$", re.I)
PEDIU_PREMIERE = re.compile(r"premiere|ajuste fino|acabamento|finalizar (?:no|na m[aã]o)", re.I)
PREMIERE_S = int(os.environ.get("CW_PREMIERE_S", "60"))

# ffmpeg/melt gravando o arquivo final (último argumento) é render, mesmo queimando legenda no caminho.
RENDER_FINAL = [(r"\b(ffmpeg|melt)\b.*\b[^\s\"']*(final|master|export|render|pronto|entrega)[^/\s\"']*\.(mp4|mov)[\"']?\s*$",
                 "render", "renderizando a versão final")]
# Nome e descrição de agentes e skills de qualquer editor.
NOMES = [
    (r"transcri|whisper", "transcricao"), (r"revis|review|qc\b|aprov|quality|checa|confer", "revisao"),
    (r"render|export|master|finaliz", "render"), (r"entreg|deliver|upload|public", "entrega"),
    (r"legend|caption|subt|motion|anim|grafic|visual|thumb|capa|\bsom\b|audio|áudio|trilha|sfx", "motion"),
    (r"enquadr|reframe|crop|mont|timeline|vertical", "montagem"),
    (r"corte|cortar|\bcut|clip|take|ritmo|selec|highlight|moment|decup", "corte"),
    (r"prepar|ingest|import|organiz|baix|download", "preparo"),
]
VIDEO = re.compile(r"(?:~|/)[^\s\"'<>|;,()\[\]\\$]*?\.(?:mp4|mov|mkv|m4v|webm|avi|mxf|mts)\b", re.I)
NAO_E_BRUTO = re.compile(r"/\.claude/|/plugins/|/assets?/|/fontes/|/venv/|broll|/biblioteca/|/tmp/|/T/|scratchpad|/cache/"
                         r"|/output/|/saida/|/final/|/prontos?/|/render", re.I)
JIRA = re.compile(r"\b(?!D-)([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
DEMANDA = re.compile(r"\bD-?(\d{1,5})\b")
ENTREGOU = re.compile(r"\b(entreguei|finalizei|terminei|acabei|pode fechar|fecha (?:o|esse) v[ií]deo|v[ií]deo (?:est[aá] )?pronto"
                      r"|finaliza(?:r)? (?:o|esse) v[ií]deo|subi (?:o|os) (?:master|v[ií]deo)|enviei (?:o|os) (?:master|v[ií]deo))\b", re.I)
PASTAS_GENERICAS = {"downloads", "desktop", "movies", "videos", "vídeos", "documents", "documentos", "bruto", "brutos", "raw",
                    "input", "entrada", "camera", "câmera", "tela", "footage", "media", "mídia", "gravacoes", "gravações", ""}


def projetos_jira():
    """Prefixos de projeto do Jira aceitos (SCC, CONT...), para não confundir UTF-8 ou H-264 com card."""
    try:
        import central
        return set(central.config().get("jira_projetos") or ["SCC", "CONT"])
    except Exception:
        return {"SCC", "CONT"}


def achar_jira(texto):
    aceitos = projetos_jira()
    return next((k for k in JIRA.findall(texto or "") if k.split("-")[0] in aceitos), None)


def titulo_do_video(caminho):
    p = Path(os.path.expanduser(caminho))
    pasta = p.parent.name
    base = pasta if pasta.lower() not in PASTAS_GENERICAS and not re.fullmatch(r"[\d_\- .]+", pasta) else p.stem
    return re.sub(r"[_\-]+", " ", base).strip()[:80] or p.name


PASTA_TRABALHO = re.compile(r"CW-Work/(reels|episodios|cwstudio)/([^/\s\"']+)")
ENVIO_MIN_S = 20  # entre um envio e outro à central disparado pela atividade (o vigia manda a cada minuto)


# Verificador: comando que só lista, conta ou consulta arquivos (relatório, varredura de pasta) não é edição,
# mesmo citando "final.mp4". Só processamento de verdade (ffmpeg, whisper, scripts de edição) abre e move etapas.
INVENTARIO = {"ls", "find", "du", "stat", "mdls", "mdfind", "wc", "cat", "head", "tail", "grep", "egrep", "rg", "sort",
              "uniq", "awk", "sed", "cut", "tr", "echo", "printf", "xargs", "tree", "file", "basename", "dirname", "date",
              "cd", "pwd", "test", "[", "true", "false", "column", "nl", "jq", "realpath", "readlink", "ffprobe",
              "mediainfo", "exiftool", "sleep", "export", "set", "which", "command", "type", "env", "printenv"}
RCLONE_CONSULTA = re.compile(r"\brclone\s+(ls|lsd|lsl|lsf|lsjson|size|about|tree|cat|check|listremotes)\b")
PROCESSA = re.compile(r"\b(ffmpeg|melt|handbrake(cli)?|auto-editor|whisper|faster_whisper|mlx_whisper|moviepy|remotion"
                      r"|sox|magick|vspipe|afconvert)\b", re.I)


def programas(cmd):
    """Programas chamados no comando (o primeiro nome de cada trecho entre ; && || | e quebras de linha)."""
    corpo = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\s*(?=\n|$)", "", cmd, flags=re.S)
    saida = []
    for trecho in re.split(r"&&|\|\||[;|\n()]|\$\(|`", corpo):
        toks = trecho.strip().split()
        while toks and (re.match(r"^[A-Za-z_]\w*=", toks[0]) or toks[0] in ("sudo", "time", "nice", "nohup", "!", "do",
                                                                           "then", "else", "if", "while", "for", "done", "fi")):
            toks = toks[1:] if toks[0] != "for" else []
        if toks:
            saida.append(os.path.basename(toks[0].strip("\"'")))
    return saida


SCRIPT_PY = re.compile(r"\bpython[0-9.]*\s+(?:-u\s+)?[^\s-][^\s]*\.py\b")


def processa(cmd):
    """Roda um programa de vídeo/áudio ou um script Python (arquivo .py): pode ser edição."""
    return bool(PROCESSA.search(cmd) or SCRIPT_PY.search(cmd))


def so_consulta(cmd):
    """True se o comando só lista/conta/consulta arquivos (não edita nada)."""
    if processa(cmd) or any(re.search(p, cmd, re.I) for p, _, _ in SCRIPTS):
        return False
    progs = programas(cmd)
    resto = [p for p in progs if p not in INVENTARIO and p != "rclone"]
    if "rclone" in progs and not RCLONE_CONSULTA.search(cmd):
        return False
    return not resto or all(p.startswith("python") for p in resto) and not re.search(r"\.(prproj|fcpxml|edl|aaf)\b|xmeml", cmd)


GENERICOS_AGENTES = {"", "general-purpose", "Explore", "Plan", "claude-code-guide", "statusline-setup", "output-style-setup"}


def classificar(ferramenta, entrada):
    """(etapa ou None, rótulo) do que o Claude acabou de fazer, ou None se não é passo de edição."""
    entrada = entrada or {}
    if ferramenta == "Bash":
        cmd = entrada.get("command") or ""
        if so_consulta(cmd):
            return None
        regras = PREMIERE + RENDER_FINAL + SCRIPTS + GENERICO if processa(cmd) else PREMIERE + SCRIPTS + GENERICO[:1]
        for padrao, etapa, rotulo in regras:
            if re.search(padrao, cmd, re.I):
                return etapa, rotulo
        if PROCESSA.search(cmd):
            return None, "processando vídeo"
        return None
    if ferramenta == "Write":
        caminho = entrada.get("file_path") or ""
        if PROJETO_PREMIERE.search(caminho) or (caminho.lower().endswith(".xml") and "xmeml" in (entrada.get("content") or "")[:2000]):
            return "acabamento", "projeto do Premiere gerado"
        return None
    if ferramenta == "Skill":
        nome = (entrada.get("skill") or "").split(":")[-1]
        return SKILLS.get(nome) or por_nome(nome, entrada.get("args") or "", "skill " + nome)
    if ferramenta in ("Agent", "Task"):
        nome = (entrada.get("subagent_type") or "").split(":")[-1]
        if nome in GENERICOS_AGENTES:   # pesquisa/consulta: aparece no histórico, mas não é etapa de edição
            return None, "{}: {}".format(nome or "agente", (entrada.get("description") or "")[:60]).rstrip(": ")
        return AGENTES.get(nome) or por_nome(nome, entrada.get("description") or "", nome or "agente")
    if "generate" in ferramenta or "upscale" in ferramenta:
        return "motion", "gerando imagem/vídeo (pago)"
    return None


MIDIA = re.compile(r"\.(mp4|mov|mkv|m4v|webm|avi|mxf|mts|wav|mp3|m4a|aac)$", re.I)
FILTROS = [(r"subtitles|\.ass\b|\.srt\b", "legenda"), (r"drawtext", "texto na tela"), (r"overlay", "sobreposição"),
           (r"crop|scale=", "enquadramento"), (r"concat", "emenda"), (r"x?fade", "transição"),
           (r"loudnorm|afftdn|amix|volume=", "áudio"), (r"setpts|atempo", "velocidade")]
SEGREDO = re.compile(r"(cw_|sbp_|ATATT|sk-|ghp_|eyJ)[A-Za-z0-9_\-=.]{8,}|\b[A-Za-z0-9_\-]{32,}\b")
PREVIA_MIN_S = 120


def _tokens(cmd):
    import shlex
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def detalhar(ferramenta, entrada):
    """Uma linha legível do que o passo fez, só com nomes de arquivo (nunca caminhos nem o comando inteiro)."""
    entrada = entrada or {}
    if ferramenta in ("Agent", "Task"):
        return (entrada.get("description") or "").strip()[:120] or None
    if ferramenta == "Skill":
        return (str(entrada.get("args") or "").strip().split("\n")[0])[:120] or None
    if ferramenta != "Bash":
        return None
    cmd = entrada.get("command") or ""
    tk = _tokens(cmd)
    base = lambda x: os.path.basename(x.rstrip("/"))
    if any(os.path.basename(t) in ("ffmpeg", "ffmpeg.exe") for t in tk):
        ins, saida, val = [], None, {}
        for i, t in enumerate(tk):
            if t == "-i" and i + 1 < len(tk):
                ins.append(base(tk[i + 1]))
            elif t in ("-ss", "-to", "-t") and i + 1 < len(tk):
                val.setdefault(t, tk[i + 1])
        cand = [t for t in tk if MIDIA.search(t) and base(t) not in ins and not t.startswith("-")]
        if cand:
            saida = base(cand[-1])
        tags = [nome for pad, nome in FILTROS if re.search(pad, cmd, re.I)]
        txt = (", ".join(dict.fromkeys(ins))[:60] or "vídeo") + (" → " + saida if saida else "")
        if "-ss" in val or "-to" in val or "-t" in val:
            txt += " · trecho {}{}".format(val.get("-ss", "0"), "–" + val["-to"] if "-to" in val else (" +" + val["-t"] if "-t" in val else ""))
        if tags:
            txt += " · " + ", ".join(dict.fromkeys(tags))
        return txt[:140]
    midias = [base(t) for t in tk if MIDIA.search(t)]
    script = next((base(t) for t in tk if re.search(r"\.(py|js|sh|ts)$", t)), None)
    if re.search(r"whisper|transcri", cmd, re.I):
        return ("transcrevendo " + midias[0]) if midias else "transcrevendo"
    if script:
        return (script + (" · " + ", ".join(dict.fromkeys(midias))[:80] if midias else ""))[:140]
    return None


def resumo_pedido(texto):
    t = " ".join((texto or "").split())
    if not t or t.startswith("<"):
        return None
    t = SEGREDO.sub("•••", t)
    return t[:140] + ("…" if len(t) > 140 else "")


def video_gerado(entrada):
    """Arquivo de vídeo que o comando acabou de gravar (o último argumento de mídia que existe e é recente)."""
    tk = _tokens((entrada or {}).get("command") or "")
    agora_s = time.time()
    for t in reversed(tk):
        if MIDIA.search(t) and not re.search(r"\.(wav|mp3|m4a|aac)$", t, re.I):
            caminho = os.path.expanduser(t)
            try:
                if os.path.getsize(caminho) > 50000 and agora_s - os.path.getmtime(caminho) < 180:
                    return os.path.abspath(caminho)
            except OSError:
                continue
    return None


def app_em_frente():
    """Nome do app em primeiro plano no macOS, sem pedir permissão (lsappinfo). '' fora do Mac."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=5).stdout.strip()
        info = subprocess.run(["lsappinfo", "info", "-only", "name", asn], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r'"([^"]+)"', info)     # saída: "Adobe Premiere Pro 2025" ASN:0x0-...: (in front)
        return m.group(1) if m else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def premiere_aberto():
    try:
        return subprocess.run(["pgrep", "-f", "Adobe Premiere Pro"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def arquivo_premiere(tid):
    return LOCAL / "premiere-{}.json".format(slug(tid))


def iniciar_vigia_premiere(sid, tid, pasta):
    """Sobe (uma vez por vídeo) o vigia que mede o acabamento no Premiere."""
    if not tid:
        return
    marca = LOCAL / ".premiere-{}.pid".format(slug(tid))
    try:
        if vivo(int(marca.read_text())):
            return
    except (OSError, ValueError):
        pass
    subprocess.Popen([sys.executable, __file__, "vigia-premiere", "--trabalho", tid, "--pasta", pasta or "", "--sessao", sid or ""],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def entrar_acabamento(sid, at, caminho=None):
    """Vídeo passou para o Premiere: etapa de acabamento e vigia ligado."""
    t = trabalho_da_sessao(sid)
    if not t:
        return
    pasta = os.path.dirname(os.path.expanduser(caminho)) if caminho else os.path.dirname(os.path.expanduser(t.get("entrada") or ""))
    iniciar_vigia_premiere(sid, t["id"], pasta)


def cmd_vigia_premiere(a):
    """Roda em segundo plano enquanto o vídeo está aberto (até 14 h). A cada minuto: Premiere na frente conta como
    acabamento ativo; vídeo novo exportado na pasta vira prévia e passo; projeto salvo vira passo."""
    LOCAL.mkdir(parents=True, exist_ok=True)
    marca = LOCAL / ".premiere-{}.pid".format(slug(a.trabalho))
    marca.write_text(str(os.getpid()))
    arq = arquivo_premiere(a.trabalho)
    try:
        st = json.loads(arq.read_text())
    except (OSError, ValueError):
        st = {"desde": agora(), "segundos": 0, "exports": [], "passos": []}
    inicio, ultimo_evento = time.time(), st.get("segundos", 0)
    pasta = a.pasta if a.pasta and os.path.isdir(a.pasta) else None
    def videos():
        out = {}
        if not pasta:
            return out
        for raiz, dirs, nomes in os.walk(pasta):
            if raiz[len(pasta):].count(os.sep) >= 2:
                dirs[:] = []
            for n in nomes:
                if MIDIA.search(n) and not re.search(r"\.(wav|mp3|m4a|aac)$", n, re.I):
                    c = os.path.join(raiz, n)
                    try:
                        out[c] = (os.path.getsize(c), os.path.getmtime(c))
                    except OSError:
                        pass
        return out
    vistos, tamanhos, projeto_mtime = videos(), {}, {}
    try:
        while time.time() - inicio < 14 * 3600:
            time.sleep(PREMIERE_S)
            if a.trabalho not in trabalhos_abertos():
                break
            frente = app_em_frente()
            st["rodando"] = premiere_aberto()
            if "premiere" in frente.lower():
                st["segundos"] = st.get("segundos", 0) + PREMIERE_S
                st["ultimo_ativo"] = agora()
            for c, (tam, mt) in videos().items():
                if c in vistos or mt < inicio - 5:
                    continue
                if tamanhos.get(c) == tam and tam > 50000:      # parou de crescer: export terminou
                    vistos[c] = (tam, mt)
                    st.setdefault("exports", []).append({"arquivo": os.path.basename(c), "ts": agora()})
                    st.setdefault("passos", []).append({"ts": agora(), "rotulo": "exportado do Premiere", "detalhe": os.path.basename(c)})
                    subprocess.Popen([sys.executable, __file__, "previa", "--sessao", a.sessao or "x", "--trabalho", a.trabalho, "--arquivo", c],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                else:
                    tamanhos[c] = tam
            if pasta:
                for raiz, _d, nomes in os.walk(pasta):
                    for n in nomes:
                        if n.lower().endswith(".prproj"):
                            c = os.path.join(raiz, n)
                            mt = os.path.getmtime(c)
                            if c in projeto_mtime and mt > projeto_mtime[c]:
                                st["projeto_salvo"] = agora()
                            projeto_mtime[c] = mt
                    break
            st["passos"] = st.get("passos", [])[-8:]
            tmp = arq.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False))
            os.replace(tmp, arq)
            if st["segundos"] - ultimo_evento >= 300:
                gravar({"ev": "premiere", "id": a.trabalho, "segundos": st["segundos"]})
                ultimo_evento = st["segundos"]
                enviar_em_segundo_plano()
            atualizar_status(a.sessao or "", "sinal")
    finally:
        if st.get("segundos", 0) != ultimo_evento:
            gravar({"ev": "premiere", "id": a.trabalho, "segundos": st.get("segundos", 0)})
            enviar_em_segundo_plano()
        marca.unlink(missing_ok=True)


def cmd_previa(a):
    """3 quadros pequenos (15%, 50%, 85%) lado a lado, enviados à central para o card do ao vivo."""
    import base64
    import tempfile
    import central
    if not shutil.which("ffmpeg") or not central.configurada():
        return
    t = {"id": a.trabalho} if getattr(a, "trabalho", None) else (trabalho_da_sessao(a.sessao) or {"id": "sessao-" + a.sessao[:8]})
    try:
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", a.arquivo],
                                   capture_output=True, text=True, timeout=30).stdout.strip() or 0)
    except (ValueError, subprocess.SubprocessError, OSError):
        return
    if dur <= 0:
        return
    with tempfile.TemporaryDirectory() as tmp:
        saida = os.path.join(tmp, "previa.jpg")
        args = ["ffmpeg", "-nostdin", "-v", "error"]
        for f in (0.15, 0.5, 0.85):
            args += ["-ss", "{:.2f}".format(dur * f), "-i", a.arquivo]
        args += ["-filter_complex", "[0:v]scale=-2:220[a];[1:v]scale=-2:220[b];[2:v]scale=-2:220[c];[a][b][c]hstack=3",
                 "-frames:v", "1", "-q:v", "7", "-y", saida]
        if subprocess.run(args, capture_output=True, timeout=60).returncode or not os.path.exists(saida):
            return
        dados = base64.b64encode(open(saida, "rb").read()).decode()
    try:
        central.rpc("cw_previa", timeout=15, p_trabalho=t["id"], p_arquivo=os.path.basename(a.arquivo),
                    p_imagem="data:image/jpeg;base64," + dados)
    except central.ErroCentral:
        pass


def por_nome(nome, descricao, rotulo):
    """Agente ou skill fora do cw-cortes: a etapa sai do nome e da descrição da tarefa."""
    texto = "{} {}".format(nome, descricao)
    for padrao, etapa in NOMES:
        if re.search(padrao, texto, re.I):
            desc = (descricao or "").strip().split("\n")[0][:60]
            return etapa, "{}: {}".format(rotulo, desc) if desc else rotulo
    return (None, rotulo) if nome else None


def trabalho_da_sessao(sid):
    """Vídeo aberto ligado a esta sessão (o mais recente), ou None."""
    abertos = trabalhos_abertos()
    ligados = [t for t in abertos.values() if sid and sid in t["_sessoes"]]
    return max(ligados, key=lambda t: t["desde"] or "") if ligados else None


def abrir_video(sid, caminho, at):
    """Primeira vez que o Claude mexe num vídeo nesta sessão: retoma o vídeo aberto da mesma pasta ou abre um novo."""
    if trabalho_da_sessao(sid):
        return
    caminho = os.path.expanduser(caminho)
    pasta = str(Path(caminho).parent)
    for t in trabalhos_abertos().values():
        entrada = os.path.expanduser(t.get("entrada") or "")
        if entrada and (entrada == caminho or str(Path(entrada).parent) == pasta):
            gravar({"ev": "retomar", "id": t["id"], "sessao": sid, "auto": True})
            return
    tid = "{}-{}-{}".format(datetime.now().strftime("%Y%m%d"), slug(titulo_do_video(caminho))[:40], uuid.uuid4().hex[:4])
    gravar({"ev": "inicio", "id": tid, "tipo": "outro", "titulo": titulo_do_video(caminho), "area": "", "entrada": caminho,
            "demanda": at.pop("demanda_pendente", ""), "jira": at.pop("jira_pendente", ""), "sessao": sid, "auto": True})


def arquivo_atividade(sid):
    return LOCAL / "atividade-{}.json".format(sid)


def cmd_atividade(_a):
    try:
        dados = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    sid = dados.get("session_id") or ""
    achado = classificar(dados.get("tool_name") or "", dados.get("tool_input"))
    if not sid:
        return
    espelho_em_segundo_plano(sid, dados.get("transcript_path") or "")
    LOCAL.mkdir(parents=True, exist_ok=True)
    arq = arquivo_atividade(sid)
    try:
        at = json.loads(arq.read_text())
    except (OSError, ValueError):
        at = {"historico": [], "enviado": 0}
    texto_entrada = json.dumps(dados.get("tool_input") or {}, ensure_ascii=False)
    videos = list(dict.fromkeys(v for v in VIDEO.findall(texto_entrada) if not NAO_E_BRUTO.search(v)))
    abriu = False
    # só abre edição com prova de trabalho no vídeo: um passo de edição de verdade, citando poucos vídeos
    # (uma varredura que cita dezenas de arquivos é consulta, não edição)
    prova = bool(achado and (achado[0] or dados.get("tool_name") == "Bash")) and len(videos) <= 3
    if videos and prova and not at.get("video_aberto"):
        try:
            abrir_video(sid, videos[0], at)
            at["video_aberto"] = True
            abriu = True
        except Exception as e:  # nunca trava a edição
            print("registro: não consegui abrir o vídeo ({})".format(e), file=sys.stderr)
    if not achado:
        if abriu:
            arq.write_text(json.dumps(at, ensure_ascii=False))
            enviar_status_em_segundo_plano(sid)
        return
    etapa, rotulo = achado
    try:
        registrar_peca(sid, at, dados.get("tool_name") or "", dados.get("tool_input") or {}, rotulo)
    except Exception as e:  # nunca trava a edição
        print("registro: peça não registrada ({})".format(e), file=sys.stderr)
    m = PASTA_TRABALHO.search(texto_entrada)
    if m:
        at["pasta"] = m.group(2)
    mudou_etapa = registrar_passo(at, etapa, rotulo, detalhar(dados.get("tool_name") or "", dados.get("tool_input")))
    if etapa == "acabamento":
        ent = dados.get("tool_input") or {}
        caminho = ent.get("file_path") or next((t for t in reversed(_tokens(ent.get("command") or "")) if PROJETO_PREMIERE.search(t) or t.lower().endswith(".xml")), None)
        entrar_acabamento(sid, at, caminho)
    if dados.get("tool_name") == "Bash" and time.time() - at.get("previa_ts", 0) > PREVIA_MIN_S:
        gerado = video_gerado(dados.get("tool_input"))
        if gerado:
            at["previa_ts"] = time.time()
            subprocess.Popen([sys.executable, __file__, "previa", "--sessao", sid, "--arquivo", gerado], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    enviar = mudou_etapa or abriu or time.time() - at.get("enviado", 0) > ENVIO_MIN_S
    if enviar:
        at["enviado"] = time.time()
    tmp = arq.with_suffix(".tmp")
    tmp.write_text(json.dumps(at, ensure_ascii=False))
    os.replace(tmp, arq)
    if enviar:  # em segundo plano: o hook volta na hora e o painel recebe em segundos
        enviar_status_em_segundo_plano(sid)


# Versão final: render final, cópia para uma pasta PRONTOS do Drive, envio para a publicação.
PRONTOS = re.compile(r"\b(cp|mv|rsync|ditto)\b.*PRONTOS", re.I)
ENVIO_PUBLICACAO = re.compile(r"publicar\.py\s+enviar.*--arquivo\s+(\S+|\"[^\"]+\"|'[^']+')")


def registrar_peca(sid, at, ferramenta, entrada, rotulo):
    if ferramenta != "Bash":
        return
    cmd = entrada.get("command") or ""
    arquivo = None
    m = ENVIO_PUBLICACAO.search(cmd)
    if m:
        arquivo = m.group(1).strip("\"'")
    elif rotulo == "renderizando a versão final" or PRONTOS.search(cmd):
        arquivo = next((t for t in reversed(_tokens(cmd)) if MIDIA.search(t) and not re.search(r"\.(wav|mp3|m4a|aac)$", t, re.I)), None)
    if not arquivo:
        return
    nome = os.path.basename(arquivo)
    feitas = at.setdefault("pecas", [])
    t = trabalho_da_sessao(sid)
    if not t or nome in feitas:
        return
    feitas.append(nome)
    gravar({"ev": "peca", "id": t["id"], "arquivo": nome, "sessao": sid})


def registrar_passo(at, etapa, rotulo, detalhe=None):
    """Anota um passo na atividade da sessão; devolve True se a etapa mudou.
    O tempo desde o último passo vai para a etapa que estava em andamento (pausa longa não conta)."""
    if at.get("etapa") and at.get("ultimo_ts"):
        delta = min(max(time.time() - at["ultimo_ts"], 0), PAUSA_MAX_S)
        por = at.setdefault("por_etapa", {})
        por[at["etapa"]] = round(por.get(at["etapa"], 0) + delta)
    at["ultimo_ts"] = time.time()
    mudou = bool(etapa) and etapa != at.get("etapa")
    if etapa:
        at["etapa"] = etapa
        at["etapa_max"] = max([etapa, at.get("etapa_max") or etapa], key=ETAPAS.index)
    at["agora"] = {"ts": agora(), "rotulo": rotulo, "etapa": etapa, "detalhe": detalhe}
    hist = at.setdefault("historico", [])
    if not hist or hist[-1].get("rotulo") != rotulo or hist[-1].get("detalhe") != detalhe:
        hist.append({"ts": agora(), "rotulo": rotulo, "etapa": etapa, "detalhe": detalhe})
    at["historico"] = hist[-12:]
    return mudou


def marcar_etapa(sid, etapa, rotulo):
    """Passo vindo de um comando do plugin (preparar), não de um hook."""
    at = ler_atividade(sid)
    registrar_passo(at, etapa, rotulo)
    at["video_aberto"] = True
    at["enviado"] = time.time()
    salvar_atividade(sid, at)
    enviar_status_em_segundo_plano(sid)


def enviar_status_em_segundo_plano(sid):
    subprocess.Popen([sys.executable, __file__, "pulso", "--sessao", sid], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def ler_atividade(sid):
    try:
        return json.loads(arquivo_atividade(sid).read_text())
    except (OSError, ValueError):
        return {"historico": [], "enviado": 0}


def salvar_atividade(sid, at):
    LOCAL.mkdir(parents=True, exist_ok=True)
    tmp = arquivo_atividade(sid).with_suffix(".tmp")
    tmp.write_text(json.dumps(at, ensure_ascii=False))
    os.replace(tmp, arquivo_atividade(sid))


def cmd_prompt(_a):
    """UserPromptSubmit: card citado vira vínculo; "entreguei" vira lembrete para o Claude fechar o vídeo."""
    try:
        dados = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    sid, texto = dados.get("session_id") or "", dados.get("prompt") or ""
    if not sid:
        return
    espelho_em_segundo_plano(sid, dados.get("transcript_path") or "", forcar=True)
    at, contexto, mudou = ler_atividade(sid), [], False
    pedido = resumo_pedido(texto)
    if pedido:
        at["pedido"], at["pedido_ts"] = pedido, agora()
    t = trabalho_da_sessao(sid)
    jira = achar_jira(texto)
    m = DEMANDA.search(texto)
    demanda = "D-{:04d}".format(int(m.group(1))) if m and re.search(r"\bD-?\d", texto) else ""
    if jira or demanda:
        if t and (jira and t.get("jira") != jira or demanda and t.get("demanda") != demanda):
            gravar({"ev": "vincular", "id": t["id"], "jira": jira, "demanda": demanda, "sessao": sid})
            mudou = True
        elif not t:
            if jira:
                at["jira_pendente"] = jira
            if demanda:
                at["demanda_pendente"] = demanda
    for v in VIDEO.findall(texto):  # caminho de vídeo colado na mensagem também abre o vídeo
        if not NAO_E_BRUTO.search(v) and not at.get("video_aberto"):
            abrir_video(sid, v, at)
            at["video_aberto"], mudou = True, True
            t = trabalho_da_sessao(sid)
            break
    raiz = Path(__file__).resolve().parent
    chave = jira or demanda
    if chave and not t and not at.get("sugeriu_" + chave):
        try:
            import preparar
            pasta = preparar.achar_pasta(preparar.pasta_entrada(), chave)
        except Exception:
            pasta = None
        if pasta:
            at["sugeriu_" + chave] = True
            contexto.append("Central CW Studios: o material de {} está na pasta de entrada ({}). Antes de editar, prepare: "
                            "`python3 \"{}\" {}` (copia o material para a pasta de trabalho, transcreve se der, escreve o "
                            "BRIEF.md e abre o vídeo na central). Depois leia o BRIEF.md e siga o fluxo de edição."
                            .format(chave, pasta, raiz / "preparar.py", chave))
    if t and PEDIU_PREMIERE.search(texto) and not ENTREGOU.search(texto):
        registrar_passo(at, "acabamento", "acabamento no Premiere")
        at["enviado"] = time.time()
        mudou = True
        iniciar_vigia_premiere(sid, t["id"], os.path.dirname(os.path.expanduser(t.get("entrada") or "")))
    if t and ENTREGOU.search(texto):
        contexto.append(
            "Central CW Studios: o vídeo \"{}\" (registro {}) está aberto nesta sessão. Se o editor terminou, confirme "
            "quantas peças foram entregues e rode `python3 \"{}\" fim --id {} --status entregue --pecas N` (ajuste pedido: "
            "--status refazer; abandonado: --status cancelado). Se veio da central (D-0012), rode também "
            "`python3 \"{}\" entregar --id <codigo> --link <link> --pecas N`. Se ele não terminou, ignore."
            .format(t.get("titulo"), t["id"], raiz / "registro.py", t["id"], raiz / "central.py"))
    try:
        import central
        sem_token = central.config()["url"] and not central.configurada()
    except Exception:
        sem_token = False
    if sem_token and not at.get("avisou_conexao") and (t or VIDEO.search(texto) or ENTREGOU.search(texto)):
        at["avisou_conexao"] = True
        contexto.append("Central CW Studios: este Claude ainda não está conectado à central, então a edição não aparece "
                        "no painel do gestor. Avise o editor em uma linha: peça o token ao gestor e diga "
                        "\"me conecta na central com o token cw_...\".")
    salvar_atividade(sid, at)
    if mudou:
        enviar_status_em_segundo_plano(sid)
        enviar_em_segundo_plano()
    if contexto:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "\n".join(contexto)}}, ensure_ascii=False))



# ---------------------------------------------------------------- espelho do Claude
# Sessão rodada direto no Claude do editor aparece para a gestão na central (o editor é avisado no começo da sessão).
# Manda pedidos, respostas e passos; segredos mascarados; a estação (CW_ESC) já aparece por conta própria.
ESPELHO_S = 15            # no máximo um envio a cada 15 s por sessão (Stop e fim sempre mandam)
SEGREDOS = [
    (re.compile(r"\bcw_[A-Za-z0-9_]{12,}"), "cw_[oculto]"),
    (re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_\-]{16,}"), "[chave oculta]"),
    (re.compile(r"\b(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{16,}"), "[token oculto]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[chave oculta]"),
    (re.compile(r"\beyJ[\w\-]{10,}\.[\w\-]{10,}\.[\w\-]{5,}"), "[token oculto]"),
    (re.compile(r"\b(EAA|IGQ|IGAA)[A-Za-z0-9]{30,}"), "[token oculto]"),
    (re.compile(r"(?i)\b(bearer|key)\s+[A-Za-z0-9_\-.:]{20,}"), r"\1 [oculto]"),
    (re.compile(r"(?i)\b(senha|password|passwd|token|secret|segredo|api[_-]?key|client[_-]?secret)(\"?\s*[:=]\s*\"?)[^\s\"',}]{4,}"), r"\1\2[oculto]"),
]


def mascarar(texto):
    for pad, troca in SEGREDOS:
        texto = pad.sub(troca, texto)
    return texto


def _texto_usuario(c):
    if isinstance(c, str):
        return c
    return "\n".join(x.get("text") or "" for x in c if isinstance(x, dict) and x.get("type") == "text")


def _passo(bloco):
    nome, ent = bloco.get("name") or "", bloco.get("input") or {}
    if nome == "Bash":
        cmd = (ent.get("description") or "").strip() or (ent.get("command") or "").strip().split("\n")[0]
        return "Terminal: " + cmd[:300]
    if nome in ("Read", "Write", "Edit", "NotebookEdit"):
        return {"Read": "Leu", "Write": "Escreveu", "Edit": "Editou", "NotebookEdit": "Editou"}[nome] + " " + os.path.basename(str(ent.get("file_path") or ent.get("notebook_path") or ""))
    if nome == "Skill":
        return "Skill " + str(ent.get("skill") or "")
    if nome in ("Agent", "Task"):
        return "Agente {}: {}".format(ent.get("subagent_type") or "geral", (ent.get("description") or "")[:160])
    if nome.startswith("mcp__"):
        partes = nome.split("__")
        return "Conector {}: {}".format(partes[1], partes[-1])
    alvo = ent.get("pattern") or ent.get("query") or ent.get("url") or ""
    return (nome + (" " + str(alvo)[:160] if alvo else "")).strip()


def espelho_em_segundo_plano(sid, transcript, forcar=False, fim=False):
    if os.environ.get("CW_ESC") or not sid or not transcript:
        return
    marca = LOCAL / "espelho-{}.json".format(re.sub(r"[^\w\-]", "", sid))
    try:
        if not (forcar or fim) and time.time() - json.loads(marca.read_text()).get("ultimo", 0) < ESPELHO_S:
            return
    except (OSError, ValueError):
        pass
    args = [sys.executable, __file__, "espelho", "--sessao", sid, "--transcript", transcript] + (["--fim"] if fim else [])
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_espelho(a):
    try:
        import central
        if not central.configurada():
            return
    except Exception:
        return
    LOCAL.mkdir(parents=True, exist_ok=True)
    marca = LOCAL / "espelho-{}.json".format(re.sub(r"[^\w\-]", "", a.sessao))
    trava = marca.with_suffix(".trava")
    try:
        if trava.exists() and time.time() - trava.stat().st_mtime < 120:
            return
        trava.write_text(str(os.getpid()))
    except OSError:
        return
    try:
        try:
            est = json.loads(marca.read_text())
        except (OSError, ValueError):
            est = {"pos": 0, "seq": 0}
        msgs, pos = [], est.get("pos", 0)
        with open(a.transcript, "rb") as f:
            f.seek(pos)
            for linha in f:
                if not linha.endswith(b"\n"):
                    break
                pos += len(linha)
                try:
                    o = json.loads(linha)
                except ValueError:
                    continue
                if o.get("isSidechain"):
                    continue
                ts, c = o.get("timestamp"), (o.get("message") or {}).get("content")
                if mensagem_humana(o):
                    msgs.append(("editor", _texto_usuario(c), ts))
                elif o.get("type") == "user" and isinstance(c, str) and "<command-name>" in c:
                    m = re.search(r"<command-name>([^<]+)</command-name>", c)
                    if m:
                        msgs.append(("editor", m.group(1).strip(), ts))
                elif o.get("type") == "assistant" and isinstance(c, list):
                    for b in c:
                        if b.get("type") == "text" and (b.get("text") or "").strip():
                            msgs.append(("ia", b["text"], ts))
                        elif b.get("type") == "tool_use":
                            msgs.append(("ferramenta", _passo(b), ts))
                if len(msgs) >= 280:
                    break
        seq = est.get("seq", 0)
        corpo = []
        for papel, texto, ts in msgs:
            seq += 1
            corpo.append({"seq": seq, "papel": papel, "texto": mascarar(texto.strip())[:4000], "ts": ts})
        t = trabalho_da_sessao(a.sessao) or {}
        primeiro = next((m["texto"] for m in corpo if m["papel"] == "editor"), None)
        info = {"maquina": socket.gethostname().split(".")[0], "ativa": not a.fim,
                "titulo": (t.get("titulo") or (primeiro or "").split("\n")[0][:120]) or None,
                "trabalho": t.get("jira") or t.get("demanda") or None, "pasta": os.getcwd()}
        if not corpo and not a.fim and est.get("pos"):
            est["ultimo"] = time.time()
        else:
            central.rpc("cw_espelho_enviar", p_sessao=a.sessao, p_info=info, p_msgs=corpo)
            est = {"pos": pos, "seq": seq, "ultimo": time.time()}
        marca.write_text(json.dumps(est))
    except Exception:
        pass
    finally:
        try:
            trava.unlink()
        except OSError:
            pass


def regras_do_time():
    """Regras ativas da central (cópia local para quando estiver sem rede)."""
    cache = LOCAL / "regras.json"
    try:
        import central
        if central.configurada():
            regras = central.rpc("cw_regras", timeout=3) or []
            LOCAL.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(regras, ensure_ascii=False))
            return regras
    except Exception:
        pass
    try:
        return json.loads(cache.read_text())
    except (OSError, ValueError):
        return []


def dicionario_do_time():
    """Dicionário do time (marcas, nomes e termos: como escrever), com cópia local para quando estiver sem rede."""
    cache = LOCAL / "glossario.json"
    try:
        import central
        if central.configurada():
            itens = central.rpc("cw_glossario", timeout=3) or []
            LOCAL.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(itens, ensure_ascii=False))
            return itens
    except Exception:
        pass
    try:
        return json.loads(cache.read_text())
    except (OSError, ValueError):
        return []


def texto_dicionario(itens):
    return ("Dicionário do time (em legenda, título, post e texto, escreva sempre assim): "
            + "; ".join(i["certo"] + (" (não " + ", ".join(i["errados"][:3]) + ")" if i.get("errados") else "") for i in itens[:80]))


def licoes_aprendidas():
    """Lições que a Lia aprendeu com as correções do time e estão ativas para o kit deste editor (cópia local sem rede)."""
    cache = LOCAL / "licoes.json"
    try:
        import central
        if central.configurada():
            licoes = central.rpc("cw_conhecimento_ativo", timeout=3) or []
            LOCAL.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(licoes, ensure_ascii=False))
            return licoes
    except Exception:
        pass
    try:
        return json.loads(cache.read_text())
    except (OSError, ValueError):
        return []


def roteamento():
    """Etapas da edição que o time manda para modelos baratos (OpenRouter), se valer nesta máquina. Cópia local sem rede."""
    cache = LOCAL / "roteamento.json"
    try:
        import central
        if central.configurada():
            r = central.rpc("cw_roteamento", timeout=3) or {}
            LOCAL.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(r, ensure_ascii=False))
        else:
            r = json.loads(cache.read_text())
    except Exception:
        try:
            r = json.loads(cache.read_text())
        except (OSError, ValueError):
            return []
    onde = r.get("onde") or "servidor"
    vale = onde == "todos" or (onde == "servidor" and os.environ.get("CW_SERVIDOR") == "1")
    return (r.get("etapas") or []) if vale else []


def texto_roteamento(etapas):
    linhas = ["- {} (etapa `{}`, modelo `{}`): {}".format(e["nome"], e["etapa"], e["modelo"], e["quando"]) for e in etapas]
    return ("Economia do time (roteamento definido pela gestão): estas etapas da edição NÃO são feitas por você. Mande para um "
            "modelo barato com a ferramenta cw-studios › texto, passando o CAMINHO do arquivo (transcrição, legenda), a `etapa` "
            "e o `modelo` indicados, e `salvar_em` quando a resposta for longa; depois trabalhe em cima do resultado:\n"
            + "\n".join(linhas) +
            "\nContinuam com você (Claude), sempre: a decisão final (quais cortes, o que aprovar ou refazer), conferir o que o "
            "modelo barato devolveu antes de usar, escrever ou mudar código e scripts, ffmpeg/Remotion/render, olhar quadros e "
            "vídeo, a revisão de qualidade final, a conversa com o editor e tudo que mexe em arquivos do projeto. Se a "
            "ferramenta falhar ou o resultado vier ruim, faça você mesmo e diga em uma linha. Ao chamar um agente ou subagente "
            "para uma dessas etapas, peça que ele use a ferramenta cw-studios › texto do mesmo jeito.")


TIPO_NOME = {"sistema": "Todo trabalho", "reel": "Reel", "cortes": "Cortes", "tutorial": "Tutorial", "youtube": "YouTube", "anuncio": "Anúncio",
             "story": "Story", "outro": "Outro"}


def cmd_abertura(_a):
    """SessionStart: sinal de vida (com vigia) e as regras do time como contexto do Claude."""
    try:
        dados = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        dados = {}
    sid = dados.get("session_id") or ""
    if os.environ.get("CW_ESTEIRA"):   # Titeu (esteira): abertura enxuta, só as regras e as lições do time (economia de contexto)
        partes = []
        regras = regras_do_time()[:40]
        sistema = [r for r in regras if r.get("tipo") == "sistema"]
        video = [r for r in regras if r.get("tipo") != "sistema"] if os.environ.get("CW_PAPEL", "video") == "video" else []
        if sistema:
            partes.append("Regras do sistema CW Studios (valem em todo trabalho):\n" + "\n".join("- " + r["texto"] for r in sistema))
        if video:
            partes.append("Regras do time CW Studios para edição de vídeo:\n" + "\n".join("- " + r["texto"] for r in video))
        dic = dicionario_do_time()
        if dic:
            partes.append(texto_dicionario(dic))
        licoes = licoes_aprendidas()[:40]
        if licoes:
            partes.append("Lições aprendidas com as correções do time (siga sempre):\n" + "\n".join("- " + l["texto"] for l in licoes))
        if partes:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(partes)}}, ensure_ascii=False))
        return
    if sid:
        pid = pid_do_claude()
        enviar_status_em_segundo_plano(sid)
        if pid:
            subprocess.Popen([sys.executable, __file__, "pulso", "--vigiar", "--sessao", sid, "--pid", str(pid)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    contexto = []
    try:   # kit do time: instala/atualiza os agentes e skills oficiais (cw-kit-*)
        import kit
        instalados = kit.sincronizar()
        bib = kit.sincronizar_biblioteca()
        if bib:
            contexto.append("Itens da biblioteca da central que este editor escolheu (já instalados): " + ", ".join(n.split(":", 1)[1] for n in bib))
        if instalados:
            contexto.append("Kit do time CW Studios instalado neste Claude (prefira estes fluxos ao editar; item que acabou de "
                            "chegar fica disponível na próxima sessão): " + ", ".join(n.split(":", 1)[1] for n in instalados))
    except Exception:
        pass
    try:   # ferramentas e biblioteca do time: o Claude recomenda quando o pedido combinar
        import kit
        cat = kit.texto_catalogo(kit.catalogo())
        if cat:
            contexto.append(cat)
    except Exception:
        pass
    try:   # escritório virtual: mantém a estação (se ligada) na mesma versão do plugin
        import estacao
        estacao.atualizar_se_ligada()
        if not os.environ.get("CW_ESC"):   # a pasta onde o editor usa o Claude vira o projeto da estação (agentes, CLAUDE.md, memória)
            adotado = estacao.adotar_projeto(dados.get("cwd") or os.getcwd())
            if adotado:
                contexto.append("A estação do escritório deste editor passou a abrir o Claude nesta pasta ({}), para usar os mesmos "
                                "agentes, regras e memória. Avise em uma linha; conversas antigas da estação seguem como estavam "
                                "(ele deve começar uma Nova conversa na central).".format(adotado))
    except Exception:
        pass
    if not os.environ.get("CW_ESC"):   # o que este Claude enxerga (skills, agentes, conectores), para comparar com a estação
        try:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "capacidades.py"), "enviar",
                              "--cwd", dados.get("cwd") or os.getcwd()], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass
    regras = regras_do_time()[:40]
    sistema = [r for r in regras if r.get("tipo") == "sistema"]
    regras = [r for r in regras if r.get("tipo") != "sistema"]
    if sistema:
        contexto.append("Regras do sistema CW Studios (definidas pela gestão; valem em todo trabalho, de vídeo ou não):\n"
                        + "\n".join("- " + r["texto"] for r in sistema))
    if regras:
        linhas = ["- {}{}".format("[{}] ".format(TIPO_NOME.get(r.get("tipo"), r.get("tipo"))) if r.get("tipo") else "", r["texto"])
                  for r in regras]
        contexto.append("Regras do time CW Studios para edição de vídeo (definidas pela gestão; siga em toda edição desta sessão, "
                        "as marcadas com tipo só valem para aquele tipo de vídeo):\n" + "\n".join(linhas))
    dic = dicionario_do_time()
    if dic:
        contexto.append(texto_dicionario(dic))
    rotas = roteamento()
    if rotas:
        contexto.append(texto_roteamento(rotas))
    licoes = licoes_aprendidas()[:40]
    if licoes:
        linhas = ["- {}{}".format("[{}] ".format(TIPO_NOME.get(l.get("tipo"), l.get("tipo"))) if l.get("tipo") else "", l["texto"]) for l in licoes]
        contexto.append("Lições aprendidas com as correções do time (aba Conhecimento da central; o time já corrigiu isso antes, "
                        "então siga sempre. Ao chamar um agente/subagente, copie no pedido dele as lições que valem para a tarefa, porque ele não "
                        "recebe esta lista sozinho):\n" + "\n".join(linhas))
    saida = {}
    try:
        import central
        espelhada = central.configurada() and not os.environ.get("CW_ESC")
    except Exception:
        espelhada = False
    if espelhada:
        saida["systemMessage"] = ("Central CW Studios: esta sessão aparece para a gestão na central (pedidos, respostas e passos; "
                                  "senhas e tokens ficam ocultos; apagado em 15 dias).")
        contexto.append("Esta sessão é espelhada na Central CW Studios para a gestão (o editor foi avisado). Se o editor perguntar, "
                        "confirme: a gestão vê pedidos, respostas e passos desta sessão; senhas e tokens são mascarados.")
    if contexto:
        saida["hookSpecificOutput"] = {"hookEventName": "SessionStart", "additionalContext": "\n\n".join(contexto)}
    if saida:
        print(json.dumps(saida, ensure_ascii=False))


def cmd_vincular(a):
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    tid = a.id or (trabalho_da_sessao(sid) or {}).get("id")
    if not tid:
        abertos = sorted(trabalhos_abertos().values(), key=lambda t: t["desde"] or "")
        tid = abertos[-1]["id"] if abertos else None
    if not tid:
        sys.exit("registro: nenhum vídeo aberto para vincular")
    gravar({"ev": "vincular", "id": tid, "jira": (a.jira or "").upper(), "demanda": a.demanda or "",
            "titulo": a.titulo or "", "tipo": a.tipo or "", "sessao": sid})
    atualizar_status(sid, "sinal")
    enviar_em_segundo_plano()
    print(tid)


def atividades(sessoes_vivas):
    """Atividade de cada sessão viva; apaga a de sessões mortas há mais de 2 dias."""
    saida = {}
    for arq in LOCAL.glob("atividade-*.json"):
        sid = arq.stem[len("atividade-"):]
        if sid in sessoes_vivas:
            try:
                saida[sid] = json.loads(arq.read_text())
            except (OSError, ValueError):
                pass
        elif time.time() - arq.stat().st_mtime > 2 * 86400:
            arq.unlink(missing_ok=True)
    return saida


def juntar_atividade(t, ats):
    """Põe no trabalho a etapa, o 'agora' e o histórico das sessões ligadas a ele."""
    if not ats:
        return
    ultima = max(ats, key=lambda x: (x.get("agora") or {}).get("ts", ""))
    t["etapa"] = ultima.get("etapa")
    t["etapa_max"] = max([x.get("etapa_max") for x in ats if x.get("etapa_max")] or [None],
                         key=lambda e: ETAPAS.index(e) if e else -1)
    t["agora_faz"] = ultima.get("agora")
    t["historico"] = sorted((h for x in ats for h in x.get("historico", [])), key=lambda h: h["ts"])[-12:]
    t["pasta"] = t.get("pasta") or ultima.get("pasta")
    com_pedido = [x for x in ats if x.get("pedido")]
    if com_pedido:
        p = max(com_pedido, key=lambda x: x.get("pedido_ts", ""))
        t["pedido"], t["pedido_ts"] = p["pedido"], p.get("pedido_ts")


# ---------------------------------------------------------------- status ao vivo (central de controle)

def trabalhos_abertos():
    """Trabalhos desta pessoa ainda não entregues nem cancelados, com as sessões ligadas a cada um."""
    abertos = {}
    for e in eventos_locais():
        tid = e.get("id")
        if e.get("ev") == "inicio":
            abertos[tid] = {"id": tid, "titulo": e.get("titulo"), "tipo": e.get("tipo"), "area": e.get("area"),
                            "demanda": e.get("demanda") or "", "jira": e.get("jira") or "",
                            "desde": e.get("ts"), "status": "em edição",
                            "_sessoes": {e.get("sessao")}}
        elif tid in abertos and e.get("ev") == "retomar":
            abertos[tid]["_sessoes"].add(e.get("sessao"))
        elif tid in abertos and e.get("ev") == "vincular":
            abertos[tid].update({k: e[k] for k in ("jira", "demanda", "titulo", "tipo") if e.get(k)})
        elif tid in abertos and e.get("ev") == "fim":
            if e.get("status") == "refazer":
                abertos[tid]["status"] = "refazer"
            else:
                abertos.pop(tid)
    return abertos


def hoje_feito():
    hoje = datetime.now().astimezone().date().isoformat()
    feito = {"entregues": 0, "pecas": 0, "itens": [], "minutos_ativos": 0, "tokens": 0, "custo_estimado_usd": 0.0}
    titulos = {}
    sessoes_hoje = {}  # cada Stop regrava a sessão inteira (acumulado): vale a última medição de cada uma
    for e in eventos_locais():
        if e.get("ev") == "inicio":
            titulos[e.get("id")] = (e.get("titulo"), e.get("tipo"), e.get("area"))
        quando = e.get("ts", "")
        try:
            quando = datetime.fromisoformat(quando).astimezone().date().isoformat()
        except ValueError:
            continue
        if e.get("ev") == "sessao" and quando == hoje:
            sessoes_hoje[e.get("sessao")] = e
        if e.get("ev") == "fim" and e.get("status") == "entregue" and quando == hoje:
            feito["entregues"] += 1
            feito["pecas"] += e.get("pecas") or 0
            titulo, tipo, area = titulos.get(e.get("id"), (e.get("id"), "", ""))
            feito["itens"].append({"id": e.get("id"), "titulo": titulo, "tipo": tipo, "area": area,
                                   "pecas": e.get("pecas") or 0, "ts": e.get("ts")})
    feito["itens"] = feito["itens"][-15:]
    for e in sessoes_hoje.values():
        feito["minutos_ativos"] += round((e.get("segundos_ativos") or 0) / 60)
        feito["tokens"] += e.get("tokens") or 0
        feito["custo_estimado_usd"] += e.get("custo_estimado_usd") or 0
    feito["custo_estimado_usd"] = round(feito["custo_estimado_usd"], 2)
    return feito


def disco_livre_gb():
    try:
        return round(shutil.disk_usage(Path.home() / "CW-Work" if (Path.home() / "CW-Work").exists()
                                       else Path.home()).free / 1e9, 1)
    except OSError:
        return None


INATIVO_S = int(os.environ.get("CW_FECHA_INATIVO_S", str(2 * 3600)))
ETAPAS_FINAIS = {"motion", "render", "acabamento", "revisao", "entrega"}


def _epoch(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


def fechar_inativos(forcar=False):
    """Fecha sozinho o vídeo que ficou INATIVO_S sem nenhum passo (nem Premiere em uso): finalizado se chegou a uma
    versão final (peça, render, acabamento, motion...), senão "sem entrega". O editor não precisa avisar o fim."""
    marca = LOCAL / ".fechar.ts"
    try:
        if not forcar and time.time() - marca.stat().st_mtime < 600:
            return []
    except OSError:
        pass
    LOCAL.mkdir(parents=True, exist_ok=True)
    marca.touch()
    abertos = {k: v for k, v in trabalhos_abertos().items() if v.get("status") != "refazer"}
    if not abertos:
        return []
    ats = {}
    for arq in LOCAL.glob("atividade-*.json"):
        try:
            ats[arq.stem[len("atividade-"):]] = (json.loads(arq.read_text()), arq.stat().st_mtime)
        except (OSError, ValueError):
            pass
    fim_sessao, etapas_sessao, pecas = {}, {}, {}
    for e in eventos_locais():
        if e.get("ev") == "sessao":
            fim_sessao[e.get("sessao")] = e.get("fim") or e.get("ts")
            etapas_sessao[e.get("sessao")] = set((e.get("por_etapa") or {}).keys())
        elif e.get("ev") == "peca":
            pecas[e.get("id")] = pecas.get(e.get("id"), 0) + 1
    fechados = []
    for tid, t in abertos.items():
        ultimo, etapas = _epoch(t.get("desde")), set()
        for sid in t["_sessoes"]:
            if sid in ats:
                at, mtime = ats[sid]
                ultimo = max(ultimo, mtime)
                etapas |= {h.get("etapa") for h in at.get("historico", []) if h.get("etapa")}
                etapas |= {x for x in (at.get("etapa"), at.get("etapa_max")) if x}
            if sid in fim_sessao:
                ultimo = max(ultimo, _epoch(fim_sessao[sid]))
                etapas |= etapas_sessao.get(sid, set())
        try:
            pr = json.loads(arquivo_premiere(tid).read_text())
            ultimo = max(ultimo, _epoch(pr.get("ultimo_ativo")))
            n_exports = len(pr.get("exports") or [])
        except (OSError, ValueError):
            n_exports = 0
        if time.time() - ultimo < INATIVO_S:
            continue
        n = pecas.get(tid, 0) + n_exports
        final = n > 0 or bool(etapas & ETAPAS_FINAIS)
        gravar({"ev": "fim", "id": tid, "status": "entregue" if final else "sem_entrega", "pecas": max(n, 1) if final else 0,
                "saida": "", "auto": True, "fechado_ultima_atividade": datetime.fromtimestamp(ultimo, timezone.utc).isoformat(),
                "nota": "fechado sozinho após {} h sem atividade".format(INATIVO_S // 3600), "sessao": ""})
        fechados.append(tid)
    if fechados:
        enviar_em_segundo_plano()
    return fechados


def atualizar_status(sid, acao):
    """acao: 'sinal' (sessão viva) ou 'fim' (sessão fechou). Regrava status.json e espelha no time."""
    LOCAL.mkdir(parents=True, exist_ok=True)
    try:
        fechar_inativos()
    except Exception as e:  # nunca trava a edição
        print("registro: fechamento automático falhou ({})".format(e), file=sys.stderr)
    with open(LOCAL / ".status.lock", "w") as trava:
        try:
            import fcntl
            fcntl.flock(trava, fcntl.LOCK_EX)
        except ImportError:  # Windows sem WSL: segue sem trava (uma sessão por vez na prática)
            pass
        try:
            st = json.loads(STATUS.read_text())
        except (OSError, ValueError):
            st = {}
        sessoes = st.get("sessoes", {})
        if acao == "fim":
            sessoes.pop(sid, None)
        elif sid:
            s = sessoes.setdefault(sid, {"desde": agora()})
            s["ultimo_sinal"] = agora()
            s["origem"] = os.environ.get("CLAUDE_CODE_ENTRYPOINT", s.get("origem", ""))
        limite = (datetime.now(timezone.utc) - timedelta(seconds=SESSAO_MORTA_S)).isoformat()
        sessoes = {k: v for k, v in sessoes.items() if v.get("ultimo_sinal", "") >= limite}

        abertos = trabalhos_abertos()
        ats = atividades(set(sessoes))
        usadas = set()
        for t in abertos.values():
            ligadas = t.pop("_sessoes") & set(sessoes)
            t["agora"] = bool(ligadas)
            try:
                pr = json.loads(arquivo_premiere(t["id"]).read_text())
                t["premiere"] = {k: pr.get(k) for k in ("desde", "segundos", "ultimo_ativo", "rodando", "projeto_salvo")}
                t["premiere"]["ultimo_export"] = (pr.get("exports") or [None])[-1]
                if pr.get("ultimo_ativo") and pr["ultimo_ativo"] >= (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat():
                    t["agora"] = True     # editando no Premiere, mesmo com o Claude fechado
            except (OSError, ValueError):
                pass
            juntar_atividade(t, [ats[s] for s in ligadas if s in ats])
            if t.get("premiere"):
                t["etapa_max"] = max([t.get("etapa_max") or "acabamento", "acabamento"], key=lambda e: ETAPAS.index(e) if e in ETAPAS else -1)
                if t["agora"] and not ligadas:
                    t["etapa"] = "acabamento"
            usadas |= ligadas
        for sid, at in ats.items():  # editando com o plugin sem ter aberto trabalho no registro
            # só vira "edição sem registro" se houve passo de edição de verdade (etapa); consulta não aparece
            if sid not in usadas and at.get("agora") and not at.get("descartada") \
                    and any(x.get("etapa") for x in at.get("historico") or []):
                t = {"id": "sessao-" + sid[:8], "titulo": at.get("pasta") or "edição sem registro",
                     "tipo": "", "area": "", "demanda": "", "jira": "", "desde": at["historico"][0]["ts"],
                     "status": "sem registro", "agora": True}
                juntar_atividade(t, [at])
                abertos[t["id"]] = t
        estado = "editando" if any(t["agora"] for t in abertos.values()) else (
            "conectado" if sessoes else "desconectado")
        pessoa, pasta = ler_perfil()
        st = {"pessoa": pessoa, "nome": nome_exibicao(), "maquina": socket.gethostname().split(".")[0], "estado": estado,
              "atualizado": agora(), "ultimo_sinal": max([v["ultimo_sinal"] for v in sessoes.values()]
                                                         or [st.get("ultimo_sinal", "")]),
              "sessoes": sessoes, "trabalhos": sorted(abertos.values(), key=lambda t: t["desde"] or ""),
              "hoje": hoje_feito(), "disco_livre_gb": disco_livre_gb(), "plugin": versao_plugin(),
              "etapas": [[e, NOME_ETAPA[e]] for e in ETAPAS]}
        tmp = STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
        os.replace(tmp, STATUS)
        espelhar(STATUS, pessoa, pasta)
    try:  # central CW Studios (Supabase): só se este editor entrou com token; nunca trava o registro
        import central
        central.enviar_status(st)
    except Exception:
        pass
    return st


def eh_claude(args):
    """O processo é o Claude Code? Binário nativo 'claude' ou o pacote npm rodando no node."""
    partes = args.split()
    if not partes:
        return False
    exe = os.path.basename(partes[0]).lower()
    if exe in ("claude", "claude.exe"):
        return True
    return exe.startswith("node") and any("claude-code" in x or os.path.basename(x) == "claude" for x in partes[1:3])


def pid_do_claude():
    """Sobe a árvore de processos até achar o Claude Code (o vigia morre junto com ele)."""
    if os.name != "posix":
        return None
    pid = os.getppid()
    for _ in range(8):
        try:
            saida = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True,
                                   text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        if not saida:
            return None
        pai, _, args = saida.partition(" ")
        if eh_claude(args.strip()):
            return pid
        pid = int(pai)
        if pid <= 1:
            return None
    return None


def vivo(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def cmd_pulso(a):
    if a.hook:  # SessionStart: sinal agora e vigia em segundo plano enquanto o Claude estiver aberto
        try:
            sid = json.loads(sys.stdin.read() or "{}").get("session_id") or ""
        except ValueError:
            return
        if not sid:
            return
        atualizar_status(sid, "sinal")
        pid = pid_do_claude()
        if pid:
            subprocess.Popen([sys.executable, __file__, "pulso", "--vigiar", "--sessao", sid, "--pid", str(pid)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        return
    if a.vigiar:
        LOCAL.mkdir(parents=True, exist_ok=True)
        marca = LOCAL / ".pulso-{}.pid".format(a.sessao)
        try:
            if vivo(int(marca.read_text())):
                return  # já tem vigia nesta sessão (ex.: /clear dispara SessionStart de novo)
        except (OSError, ValueError):
            pass
        marca.write_text(str(os.getpid()))
        try:
            while vivo(a.pid):
                time.sleep(PULSO_S)
                if not vivo(a.pid):
                    break
                atualizar_status(a.sessao, "sinal")
            atualizar_status(a.sessao, "fim")
        finally:
            marca.unlink(missing_ok=True)
        return
    print(json.dumps(atualizar_status(a.sessao or os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "sinal"),
                     ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- resumo

def todos_eventos(pasta):
    if pasta and pasta.is_dir():
        for arq in sorted(pasta.glob("*/*.jsonl")):
            for linha in open(arq, encoding="utf-8"):
                try:
                    yield json.loads(linha)
                except ValueError:
                    continue
    else:
        yield from eventos_locais()


def montar_resumo(eventos, dias):
    corte = datetime.now(timezone.utc) - timedelta(days=dias)
    trabalhos, sessoes = {}, {}
    for e in eventos:
        if e.get("ev") == "sessao":
            sessoes[e["sessao"]] = e  # a última leitura da sessão é a que vale (é cumulativa)
            continue
        t = trabalhos.setdefault(e.get("id"), {"id": e.get("id"), "voltas": 0})
        if e["ev"] == "inicio":
            t.update({k: e.get(k) for k in ("tipo", "titulo", "area", "pessoa", "entrada", "jira", "demanda")})
            t["aberto_em"] = e["ts"]
        elif e["ev"] == "vincular":
            t.update({k: e[k] for k in ("jira", "demanda", "titulo", "tipo") if e.get(k)})
        elif e["ev"] == "premiere":
            t["segundos_premiere"] = e.get("segundos") or 0
        elif e["ev"] == "peca":
            t["pecas_vistas"] = t.get("pecas_vistas", 0) + 1
        elif e["ev"] == "fim":
            if e.get("status") == "refazer":
                t["voltas"] += 1
            t.update({"status": e.get("status"), "pecas": e.get("pecas") or t.get("pecas_vistas") or 0,
                      "saida": e.get("saida"), "fechado_em": e.get("fechado_ultima_atividade") or e["ts"], "auto": bool(e.get("auto"))})

    for t in trabalhos.values():
        t.update({"segundos": 0, "tokens": 0, "custo_usd": 0.0, "etapas": {}, "segundos_humanos": 0, "interacoes": 0,
                  "fluxo": {}})
    sem_trabalho = {"sessoes": 0, "segundos": 0, "custo_usd": 0.0}
    for s in sessoes.values():
        custo = max(s.get("custo_oficial_usd") or 0, s.get("custo_estimado_usd") or 0)
        alvos = [trabalhos[i] for i in s.get("trabalhos", []) if i in trabalhos]
        if not alvos:
            if (s.get("fim") or "") >= corte.isoformat():
                sem_trabalho["sessoes"] += 1
                sem_trabalho["segundos"] += s.get("segundos_ativos", 0)
                sem_trabalho["custo_usd"] += custo
            continue
        for t in alvos:  # sessão com mais de um trabalho: divide igual
            t["segundos"] += s.get("segundos_ativos", 0) / len(alvos)
            t["tokens"] += s.get("tokens", 0) / len(alvos)
            t["custo_usd"] += custo / len(alvos)
            for etapa, seg in (s.get("por_etapa") or {}).items():
                t["etapas"][etapa] = t["etapas"].get(etapa, 0) + seg / len(alvos)
            t["segundos_humanos"] += (s.get("segundos_humanos") or 0) / len(alvos)
            t["interacoes"] += (s.get("interacoes") or 0) / len(alvos)
            for nome, n in (s.get("fluxo") or {}).items():
                t["fluxo"][nome] = t["fluxo"].get(nome, 0) + n

    recentes = [t for t in trabalhos.values() if (t.get("aberto_em") or "") >= corte.isoformat()]
    por_pessoa = {}
    for t in recentes:
        p = por_pessoa.setdefault(t.get("pessoa") or "?", {"trabalhos": 0, "entregues": 0, "pecas": 0,
                                                            "voltas": 0, "horas": 0.0, "tokens": 0,
                                                            "custo_usd": 0.0})
        p["trabalhos"] += 1
        p["entregues"] += t.get("status") == "entregue"
        if t.get("status") == "entregue":
            p["pecas"] += t.get("pecas") or 0
        p["voltas"] += t["voltas"]
        p["horas"] += t["segundos"] / 3600
        p["tokens"] += t["tokens"]
        p["custo_usd"] += t["custo_usd"]
    abertos = [t for t in recentes if t.get("status") not in ("entregue", "cancelado", "nao_edicao", "sem_entrega")]
    return {"gerado_em": agora(), "dias": dias, "por_pessoa": por_pessoa, "abertos": abertos,
            "trabalhos": sorted(recentes, key=lambda t: t.get("aberto_em") or ""),
            "sessoes_sem_trabalho": sem_trabalho}


DIAS_ENVIO = 180  # janela do histórico que vai para a central a cada envio (upsert: reenviar é inofensivo)


def enviar_edicoes():
    """Manda à central uma linha por vídeo (trabalho) com status, peças e tempo ativo. Devolve quantas."""
    import central
    r = montar_resumo(eventos_locais(), DIAS_ENVIO)
    lista = [{"id": t["id"], "titulo": t.get("titulo"), "tipo": t.get("tipo"), "area": t.get("area"),
              "jira": t.get("jira"), "demanda": t.get("demanda"), "status": t.get("status"),
              "aberto_em": t.get("aberto_em"), "fechado_em": t.get("fechado_em"), "pecas": t.get("pecas"),
              "voltas": t.get("voltas", 0), "segundos": round(t.get("segundos", 0)),
              "tokens": round(t.get("tokens", 0)), "custo_usd": round(t.get("custo_usd", 0.0), 2),
              "etapas": {k: round(v + (t.get("segundos_premiere") or 0 if k == "acabamento" else 0)) for k, v in
                         dict(t.get("etapas", {}), **({"acabamento": t.get("etapas", {}).get("acabamento", 0)} if t.get("segundos_premiere") else {})).items()},
              "segundos_premiere": round(t.get("segundos_premiere") or 0),
              "segundos_humanos": round(t.get("segundos_humanos", 0)), "interacoes": round(t.get("interacoes", 0)),
              "fluxo": t.get("fluxo", {})}
             for t in r["trabalhos"] if t.get("id") and t.get("aberto_em")]
    return central.rpc("cw_edicoes", timeout=15, p_lista=lista) if lista else 0


def enviar_em_segundo_plano():
    try:
        import central
        if not central.configurada():
            return
    except Exception:
        return
    subprocess.Popen([sys.executable, __file__, "enviar"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_enviar(_a):
    print("{} vídeo(s) enviados à central".format(enviar_edicoes()))


def cmd_resumo(a):
    _, pasta = ler_perfil()
    r = montar_resumo(todos_eventos(pasta), a.dias)
    if a.json:
        Path(a.json).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    origem = "time ({})".format(pasta) if pasta and pasta.is_dir() else "só este computador"
    print("Produção dos últimos {} dias — {}".format(a.dias, origem))
    print("{:<18}{:>9}{:>10}{:>7}{:>8}{:>8}{:>10}{:>10}{:>11}".format(
        "pessoa", "trabalhos", "entregues", "peças", "voltas", "horas", "tokens", "US$", "US$/peça"))
    for nome, p in sorted(r["por_pessoa"].items(), key=lambda kv: -kv[1]["pecas"]):
        por_peca = p["custo_usd"] / p["pecas"] if p["pecas"] else 0
        print("{:<18}{:>9}{:>10}{:>7}{:>8}{:>8.1f}{:>9.1f}M{:>10.2f}{:>11.2f}".format(
            nome[:17], p["trabalhos"], p["entregues"], p["pecas"], p["voltas"], p["horas"],
            p["tokens"] / 1e6, p["custo_usd"], por_peca))
    if r["abertos"]:
        print("\nEm andamento:")
        for t in r["abertos"]:
            print("  {}  {} · {} · {} ({})".format(t["id"], t.get("pessoa"), t.get("tipo"), t.get("titulo"),
                                                  t.get("status") or "aberto"))
    s = r["sessoes_sem_trabalho"]
    if s["sessoes"]:
        print("\nSessões com o plugin sem trabalho registrado: {} ({:.1f} h, US$ {:.2f})".format(
            s["sessoes"], s["segundos"] / 3600, s["custo_usd"]))
    print("\nCusto = equivalente API (estimativa); em plano de assinatura não é cobrança real.")


# ---------------------------------------------------------------- configurar (primeira vez no computador)

MARCADOR = "cw-registro-time.txt"   # arquivo que o gestor põe na pasta do registro do time
NOMES_DRIVES = ("drives compartilhados", "shared drives", "unidades compartilhadas", "meu drive", "my drive")


def raizes_drive():
    """Onde o Google Drive para computador costuma aparecer em cada sistema."""
    home = Path.home()
    raizes = sorted((home / "Library" / "CloudStorage").glob("GoogleDrive-*"))            # macOS
    raizes += [p for p in home.glob("*") if "google" in p.name.lower() and p.is_dir()]    # Linux
    letras = [Path("/mnt") / l for l in "defghijklmnopqrstuvwxyz"] if os.name == "posix" else \
        [Path("{}:\\".format(l.upper())) for l in "defghijklmnopqrstuvwxyz"]             # WSL / Windows
    for r in letras:
        try:
            if r.is_dir() and any(f.name.lower() in NOMES_DRIVES for f in os.scandir(r)):
                raizes.append(r)
        except OSError:
            continue
    return raizes


def achar_pasta_time(limite_s=40, profundidade=7):
    """Procura o marcador nos drives compartilhados primeiro (o Meu Drive pode ser enorme)."""
    fim = time.time() + limite_s
    fila = [(r, 0) for r in raizes_drive()]
    while fila and time.time() < fim:
        pasta, nivel = fila.pop(0)
        try:
            entradas = sorted(os.scandir(pasta), key=lambda f: f.name.lower() not in NOMES_DRIVES[:3])
        except OSError:
            continue
        if any(f.name == MARCADOR for f in entradas):
            return Path(pasta)
        if nivel < profundidade:
            fila += [(Path(f.path), nivel + 1) for f in entradas
                     if not f.name.startswith(".") and f.is_dir(follow_symlinks=False)]
    return None


def gravar_perfil(nome, pasta):
    perfil = HUB / "perfil.md"
    HUB.mkdir(parents=True, exist_ok=True)
    if not perfil.exists():
        modelo = PLUGIN / "conhecimento" / "perfil-modelo.md"
        perfil.write_text(modelo.read_text(encoding="utf-8") if modelo.exists() else
                          "# Perfil local\n\n- Nome no registro: (seu nome)\n- Pasta do registro do time: (opcional)\n",
                          encoding="utf-8")
    texto = perfil.read_text(encoding="utf-8")
    linhas = {"Nome no registro": nome, "Pasta do registro do time": str(pasta)}
    for chave, valor in linhas.items():
        rx = re.compile(r"^- {}:.*$".format(re.escape(chave)), re.M)
        if rx.search(texto):
            texto = rx.sub(lambda _m: "- {}: {}".format(chave, valor), texto, count=1)
        else:
            if "## Registro do time" not in texto:
                texto = texto.rstrip("\n") + "\n\n## Registro do time\n"
            texto = texto.replace("## Registro do time\n", "## Registro do time\n- {}: {}\n".format(chave, valor), 1)
    perfil.write_text(texto, encoding="utf-8")


def cmd_configurar(a):
    pasta = Path(os.path.expanduser(a.pasta)) if a.pasta else achar_pasta_time()
    if not pasta or not pasta.is_dir():
        sys.exit("registro: não achei a pasta do registro do time ({} não está em nenhum Google Drive "
                 "sincronizado). Confira se o Google Drive para computador está aberto e com o drive "
                 "compartilhado do time; ou passe o caminho com --pasta.".format(MARCADOR))
    gravar_perfil(a.nome.strip(), pasta)
    st = atualizar_status(os.environ.get("CLAUDE_CODE_SESSION_ID", ""), "sinal")
    print("ok: {} registrado em {}\nestado agora: {} (aparece na central em até 1 minuto)".format(
        a.nome.strip(), pasta / st["pessoa"], st["estado"]))


def cmd_config(_a):
    pessoa, pasta = ler_perfil()
    print("pessoa: {}\nregistro local: {}\npasta do time: {}\nplugin: {}".format(
        pessoa, LOCAL, pasta or "(não configurada — só local)", versao_plugin()))


def main():
    if os.environ.get("CW_ESTEIRA") and len(sys.argv) > 1 and sys.argv[1] in ("prompt", "atividade", "sessao"):
        return   # Titeu (esteira): sem registro de atividade nem espelho a cada passo
    ap = argparse.ArgumentParser(description="Registro de produção do hub de cortes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inicio")
    p.add_argument("--tipo", required=True,
                   choices=["reel", "cortes", "tutorial", "youtube", "anuncio", "story", "outro"])
    p.add_argument("--titulo", required=True)
    p.add_argument("--area")
    p.add_argument("--entrada")
    p.add_argument("--demanda", help="código da demanda na central (D-0012), se veio de lá")
    p.add_argument("--jira", help="chave do card no Jira (SCC-1762), se veio de lá")
    p.add_argument("--id")
    p = sub.add_parser("retomar")
    p.add_argument("--id", required=True)
    p = sub.add_parser("fim")
    p.add_argument("--id")
    p.add_argument("--status", required=True, choices=["entregue", "refazer", "cancelado", "nao_edicao", "sem_entrega"])
    p.add_argument("--pecas", type=int, default=0)
    p.add_argument("--saida")
    p.add_argument("--nota")
    p = sub.add_parser("sessao")
    p.add_argument("--evento", default="SessionEnd")
    sub.add_parser("atividade")
    sub.add_parser("prompt")
    sub.add_parser("abertura")
    p = sub.add_parser("espelho")
    p.add_argument("--sessao", required=True)
    p.add_argument("--transcript", required=True)
    p.add_argument("--fim", action="store_true")
    p = sub.add_parser("previa")
    p.add_argument("--sessao", required=True)
    p.add_argument("--arquivo", required=True)
    p.add_argument("--trabalho")
    p = sub.add_parser("vigia-premiere")
    p.add_argument("--trabalho", required=True)
    p.add_argument("--pasta", default="")
    p.add_argument("--sessao", default="")
    p = sub.add_parser("vincular")
    p.add_argument("--id")
    p.add_argument("--jira")
    p.add_argument("--demanda")
    p.add_argument("--titulo")
    p.add_argument("--tipo", choices=["reel", "cortes", "tutorial", "youtube", "anuncio", "story", "outro"])
    sub.add_parser("enviar")
    p = sub.add_parser("resumo")
    p.add_argument("--dias", type=int, default=30)
    p.add_argument("--json")
    p = sub.add_parser("pulso")
    p.add_argument("--hook")
    p.add_argument("--vigiar", action="store_true")
    p.add_argument("--sessao", default="")
    p.add_argument("--pid", type=int)
    p = sub.add_parser("configurar")
    p.add_argument("--nome", required=True)
    p.add_argument("--pasta")
    sub.add_parser("config")
    a = ap.parse_args()
    {"atividade": cmd_atividade, "prompt": cmd_prompt, "abertura": cmd_abertura, "previa": cmd_previa, "vigia-premiere": cmd_vigia_premiere, "vincular": cmd_vincular, "enviar": cmd_enviar, "inicio": cmd_inicio, "retomar": cmd_retomar, "fim": cmd_fim, "sessao": cmd_sessao,
     "resumo": cmd_resumo, "pulso": cmd_pulso, "configurar": cmd_configurar, "config": cmd_config, "espelho": cmd_espelho}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # o registro nunca pode travar a edição
        print("registro: erro {}: {}".format(type(e).__name__, e), file=sys.stderr)
        sys.exit(0)
