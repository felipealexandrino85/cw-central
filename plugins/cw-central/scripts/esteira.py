#!/usr/bin/env python3
"""Esteira de cortes do CW Studios (roda no servidor, usuário cw-esteira, conta Claude do CW Studios).

① vigia a pasta de entrada do podcast no Drive e prepara cada episódio novo UMA vez: cópia local, áudio, transcrição
  com tempo por palavra e o mapa do episódio (modelo barato, OpenRouter);
② quando alguém pede N cortes na página, um Claude escolhe os momentos e a lista vai para o editor conferir;
③ os cortes liberados são produzidos em paralelo (vários operários), cada um numa pasta própria;
④ cada corte aprovado no QC vai na hora para a Publicação (Drive "1. Para aprovar" + card).

Sem ninguém olhando: a cerca RECUSA (não pergunta) o que sair da pasta do trabalho.

  python3 esteira.py rodar            serviço (systemd cw-esteira)
  python3 esteira.py porteiro         hook PreToolUse dos Claudes da esteira
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import central  # noqa: E402

HOME = Path.home()
DRIVE = Path(os.environ.get("CW_DRIVE", "/srv/drive"))
ENTRADA = DRIVE / "CW Cast - Cortes" / "1. ENVIE AQUI - Podcast (episódio inteiro)"
BASE = Path(os.environ.get("CW_ESTEIRA", HOME / "esteira"))
OPERARIOS = int(os.environ.get("CW_OPERARIOS", "3"))
TOKEN_CLAUDE = HOME / ".cw-cortes" / "estacao" / "claude-token"
CERCA = BASE / "cerca.json"
VIDEO = re.compile(r"\.(mp4|mov|mkv|m4v)$", re.I)
SAIDA_LOG = BASE / "esteira.log"


def log(*a):
    linha = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(linha, flush=True)


def srv(acao, **dados):
    return central.rpc("cw_esteira_srv", timeout=30, p_acao=acao, p_dados=dados)


def ffprobe_dur(arq):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(arq)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def tem_gpu():
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


# ------------------------------------------------------------------------------------------- ① vigia e preparo
_TAMANHOS = {}


def vigiar():
    """Registra brutos novos depois que o tamanho parou de mudar (upload terminado)."""
    if not ENTRADA.is_dir():
        return
    for arq in sorted(ENTRADA.iterdir()):
        if not arq.is_file() or not VIDEO.search(arq.name):
            continue
        try:
            tam = arq.stat().st_size
        except OSError:
            continue
        if tam < 50_000_000:
            continue
        if _TAMANHOS.get(arq.name) == tam:
            srv("registrar", arquivo=str(arq.relative_to(DRIVE)), nome=arq.stem, tamanho=tam)
        _TAMANHOS[arq.name] = tam


MAPA = """Você recebe a transcrição de um episódio do CW Cast (podcast da Cardápio Web sobre gestão, marketing e finanças para
donos de restaurante e delivery), com o tempo [hh:mm:ss] no começo de cada linha. Devolva em markdown:
1. Quem fala e o tema geral (3 linhas).
2. Assuntos por bloco de tempo: [início–fim] assunto, em uma linha cada.
3. Até 40 frases fortes com o tempo exato (citação literal curta): números, histórias, opiniões polêmicas, conselhos práticos,
   viradas. Não invente nada que não esteja na transcrição."""


def hms(s):
    s = int(s)
    return "{:02d}:{:02d}:{:02d}".format(s // 3600, s % 3600 // 60, s % 60)


def transcrever_groq(audio, pasta, dur, aviso):
    """whisper-large-v3 na Groq (via função Ferramentas da central): pedaços de 10 min, 3 de cada vez. Devolve a lista de segmentos."""
    import base64
    from concurrent.futures import ThreadPoolExecutor
    import ferramentas_mcp
    pedaco, partes = 600, []
    total = int((dur or 0) // pedaco) + 1
    tmp = pasta / "pedacos"
    tmp.mkdir(exist_ok=True)
    feitos = [0]

    def um(k):
        arq = tmp / "p{:03d}.mp3".format(k)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(k * pedaco), "-t", str(pedaco), "-i", str(audio),
                        "-ac", "1", "-ar", "16000", "-b:a", "32k", str(arq)], check=True)
        if arq.stat().st_size < 2000:
            return k, {"segmentos": [], "palavras": []}
        for tentativa in range(3):
            try:
                r = ferramentas_mcp.chamar({"acao": "transcrever", "ferramenta": "groq", "modelo": "whisper-large-v3", "nome": arq.name,
                                            "audio_b64": base64.b64encode(arq.read_bytes()).decode(), "trabalho": "esteira"}, timeout=300)
                if r.get("erro"):
                    raise RuntimeError(r["erro"])
                feitos[0] += 1
                aviso(20 + int(65 * feitos[0] / total))
                return k, r
            except Exception:
                if tentativa == 2:
                    raise
                time.sleep(5 * (tentativa + 1))

    with ThreadPoolExecutor(3) as ex:
        resultados = dict(ex.map(um, range(total)))
    segs = []
    for k in range(total):
        r, off = resultados[k], k * pedaco
        palavras = [{"p": (w.get("p") or "").strip(), "i": round(w["i"] + off, 2), "f": round(w["f"] + off, 2)} for w in r.get("palavras") or []]
        for sg in r.get("segmentos") or []:
            ini, fim = sg["inicio"] + off, sg["fim"] + off
            segs.append({"inicio": round(ini, 2), "fim": round(fim, 2), "texto": (sg.get("texto") or "").strip(),
                         "palavras": [w for w in palavras if ini - 0.05 <= w["i"] < fim + 0.05]})
    shutil.rmtree(tmp, ignore_errors=True)
    return segs


def preparar(ep):
    eid = ep["id"]
    pasta = BASE / str(eid)
    pasta.mkdir(parents=True, exist_ok=True)
    origem = DRIVE / ep["arquivo"]
    bruto = pasta / ("bruto" + origem.suffix.lower())
    try:
        srv("episodio", id=eid, estado="preparando", etapa="copiando o bruto do Drive", progresso=5)
        if not bruto.exists() or bruto.stat().st_size != origem.stat().st_size:
            tmp = bruto.with_suffix(".parcial")
            shutil.copyfile(origem, tmp)
            tmp.replace(bruto)
        dur = ffprobe_dur(bruto)
        srv("episodio", id=eid, etapa="separando o áudio", progresso=15, duracao_s=round(dur or 0, 1))
        audio = pasta / "audio.wav"
        if not audio.exists():
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(bruto), "-vn", "-ac", "1", "-ar", "16000", str(audio)], check=True)
        transc = pasta / "transcricao.json"
        if not transc.exists() and os.environ.get("CW_TRANSCRICAO", "groq") == "groq":
            srv("episodio", id=eid, etapa="transcrevendo (whisper-large-v3 na Groq)", progresso=20)
            try:
                segs = transcrever_groq(audio, pasta, dur, lambda pct: srv("episodio", id=eid, progresso=pct))
                tmp = transc.with_suffix(".parcial")
                tmp.write_text(json.dumps(segs, ensure_ascii=False))
                tmp.replace(transc)
            except Exception as e:   # sem Groq (chave, limite, rede): transcreve aqui mesmo
                log("Groq falhou, transcrevendo no servidor:", e)
        if not transc.exists():
            from faster_whisper import WhisperModel
            gpu = tem_gpu()
            modelo = os.environ.get("CW_WHISPER", "large-v3" if gpu else "medium")
            srv("episodio", id=eid, etapa="transcrevendo ({}, {})".format(modelo, "placa de vídeo" if gpu else "processador"), progresso=20)
            m = WhisperModel(modelo, device="cuda" if gpu else "cpu", compute_type="float16" if gpu else "int8")
            segs, _ = m.transcribe(str(audio), language="pt", word_timestamps=True, vad_filter=True, condition_on_previous_text=False)
            saida, ultimo = [], 0
            for s in segs:
                saida.append({"inicio": round(s.start, 2), "fim": round(s.end, 2), "texto": s.text.strip(),
                              "palavras": [{"p": w.word.strip(), "i": round(w.start, 2), "f": round(w.end, 2)} for w in (s.words or [])]})
                if dur and s.end - ultimo > 120:   # progresso a cada ~2 min de áudio
                    ultimo = s.end
                    srv("episodio", id=eid, progresso=20 + int(65 * s.end / dur))
            tmp = transc.with_suffix(".parcial")
            tmp.write_text(json.dumps(saida, ensure_ascii=False))
            tmp.replace(transc)
        segs = json.loads(transc.read_text())
        txt = pasta / "transcricao.txt"
        txt.write_text("\n".join("[{}] {}".format(hms(s["inicio"]), s["texto"]) for s in segs), encoding="utf-8")
        mapa = pasta / "mapa.md"
        if not mapa.exists():
            srv("episodio", id=eid, etapa="montando o mapa do episódio", progresso=90)
            try:
                import ferramentas_mcp
                r = ferramentas_mcp.chamar({"acao": "texto", "ferramenta": "openrouter", "modelo": "google/gemini-3.7-flash",
                                            "instrucao": MAPA, "texto": txt.read_text(encoding="utf-8"), "etapa": "mapa_bruto",
                                            "trabalho": "esteira-{}".format(eid)}, timeout=600)
                mapa.write_text(r.get("texto") or "", encoding="utf-8")
            except Exception as e:   # sem mapa a seleção ainda funciona (lê a transcrição)
                log("mapa falhou:", e)
        srv("episodio", id=eid, estado="pronto", etapa="pronto para cortar", progresso=100)
        log("episódio", eid, "pronto")
    except Exception as e:
        log("preparo do episódio", eid, "falhou:", traceback.format_exc()[-800:])
        srv("episodio", id=eid, estado="erro", erro=str(e)[:500])


# ------------------------------------------------------------------------------------------- estilos de edição
_ESTILOS = {"ts": 0}


def sincronizar_estilos():
    """Instala os estilos ativos como skills cw-estilo-<slug> (com fontes, sons, scripts...). A cada 10 min no máximo."""
    if time.time() - _ESTILOS["ts"] < 600:
        return
    _ESTILOS["ts"] = time.time()
    try:
        import kit
        estilos = srv("estilos") or []
        itens = [e["item"] for e in estilos if e.get("item")]
        manifesto = HOME / ".cw-cortes" / "estilos.json"
        try:
            antes = json.loads(manifesto.read_text())
        except (OSError, ValueError):
            antes = {}
        kit._instalar(itens, "cw-", manifesto)
        for it in itens:
            pasta = kit.CLAUDE / "skills" / ("cw-" + re.sub(r"[^a-z0-9-]+", "-", it["nome"].lower()).strip("-"))
            faltando = [a for a in it.get("anexos") or [] if not pasta.joinpath(a["caminho"]).exists()]
            if faltando:   # confere todos os arquivos do estilo, não só alguns
                log("baixando os arquivos do estilo", pasta.name, len(faltando), "faltando")
                kit._baixar_anexos(pasta, faltando)
                ainda = [a["caminho"] for a in faltando if not pasta.joinpath(a["caminho"]).exists()]
                if ainda:
                    log("estilo", pasta.name, "ainda sem", len(ainda), "arquivos:", ", ".join(ainda[:8]))
    except Exception as e:
        log("estilos:", e)


def receita(estilo, parte):
    """Trecho da receita do estilo: o cabeçalho (vale sempre) + a seção '## EPISÓDIO' ou '## CORTE'."""
    t = (estilo or {}).get("receita") or ""
    cab = t.split("\n## ")[0]
    sec = re.search(r"^## " + parte + r"[^\n]*\n(.*?)(?=^## |\Z)", t, re.S | re.M)
    return (cab + ("\n" + sec.group(1) if sec else "")).strip()


def pasta_estilo(ep, estilo):
    return BASE / str(ep["id"]) / ("estilo-" + ((estilo or {}).get("slug") or "generico"))


# ------------------------------------------------------------------------------------------- Claude sem ninguém olhando
def escrever_cerca():
    BASE.mkdir(parents=True, exist_ok=True)
    CERCA.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "{} {} porteiro".format(sys.executable, Path(__file__).resolve())}]}]}}))


def cmd_porteiro():
    """Hook: usa a mesma avaliação da estação, mas o que pediria OK é recusado (não há ninguém para aprovar)."""
    pasta = os.environ.get("CW_EST_PASTA")
    try:
        dado = json.load(sys.stdin)
    except ValueError:
        dado = {}
    ferramenta, entrada = dado.get("tool_name") or "", dado.get("tool_input") or {}
    duro = False
    try:
        import estacao
        decisao, motivo = estacao.decidir(ferramenta, entrada, pasta) if pasta else ("ask", "sem pasta")
        duro = bool(estacao.ALVO.get("duro"))
    except Exception as e:
        decisao, motivo, duro = "ask", "não consegui avaliar ({})".format(e), True
    if decisao != "allow" and "cw-studios__gerar" in ferramenta:   # Higgsfield: liberado até o orçamento do lote
        orc, cont = int(os.environ.get("CW_EST_HIGGS") or 0), Path(pasta or "/tmp") / ".higgs_usados"
        usados = int(cont.read_text()) if cont.exists() else 0
        if usados < orc:
            cont.write_text(str(usados + 1))
            decisao, duro = "allow", False
    if decisao != "allow":
        # sem ninguém para aprovar: ler e rodar programas do fluxo (venv, vch, node) passa; gravar/apagar/mover fora da pasta
        # e tudo que é "duro" (mandar para fora, instalar, administrador, senha) é recusado
        mexe = re.match(r"^(gravar|mexer|apagar|mover|rm |rm$)", str(motivo))
        decisao = "deny" if duro or mexe else "allow"
    razao = "na esteira não pode: {}".format(motivo) if decisao == "deny" else "liberado na esteira"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decisao,
                                             "permissionDecisionReason": razao}}, ensure_ascii=False))


def claude(prompt, pasta, modelo, ao_passo=None, limite=1800, extra_dirs=(), higgs=0):
    """Roda um Claude da esteira em 'pasta'. Devolve o texto final."""
    if not TOKEN_CLAUDE.exists():
        raise RuntimeError("a conta Claude do CW Studios ainda não foi ligada na esteira (aba Esteira › ☁️)")
    cmd = [str(HOME / ".local/bin/claude"), "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", modelo,
           "--permission-mode", "default", "--settings", str(CERCA), "--add-dir", str(pasta)]
    for d in extra_dirs:
        cmd += ["--add-dir", str(d)]
    env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=TOKEN_CLAUDE.read_text().strip(), CW_EST_PASTA=str(pasta),
               CW_ESC_PASTA=str(pasta), CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1", CW_SERVIDOR="1",
               CW_ESTEIRA="1", CW_EST_HIGGS=str(higgs))   # CW_ESTEIRA: abertura enxuta (só regras e lições do time)
    proc = subprocess.Popen(cmd, cwd=str(pasta), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
                            encoding="utf-8", errors="replace", start_new_session=True)
    fim, resultado = time.time() + limite, ""
    try:
        for linha in proc.stdout:
            if time.time() > fim:
                proc.kill()
                raise RuntimeError("passou do tempo ({} min)".format(limite // 60))
            try:
                ev = json.loads(linha)
            except ValueError:
                continue
            if ev.get("type") == "assistant" and ao_passo:
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "tool_use":
                        try:
                            import estacao
                            ao_passo(estacao.resumo_ferramenta(c.get("name", ""), c.get("input") or {})[:120])
                        except Exception:
                            pass
            elif ev.get("type") == "result":
                resultado = ev.get("result") or ""
                if ev.get("is_error"):
                    raise RuntimeError("o Claude parou com erro: " + resultado[:300])
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    return resultado


def json_final(texto):
    m = re.search(r"\{.*\}", texto or "", re.S)
    if not m:
        raise RuntimeError("o Claude não devolveu o JSON combinado: " + (texto or "")[-300:])
    return json.loads(m.group(0))


# ------------------------------------------------------------------------------------------- ② seleção
SELECAO = """Você é o selecionador de cortes da esteira do CW Studios (CW Cast, podcast da Cardápio Web para donos de restaurante e delivery).
Nesta pasta estão: transcricao.txt (linhas com [hh:mm:ss]), transcricao.json (tempo por palavra) e mapa.md (resumo do episódio).
Escolha os {n} MELHORES momentos para cortes verticais (Reels/Shorts/TikTok), seguindo a skill de cortes do kit do time se ela estiver
instalada (cw-kit-cortes-cwcast) e as lições do time que você recebeu.
Cada corte: entre 25 e 90 segundos, começa e termina em frase completa, faz sentido sozinho, tem gancho nos 3 primeiros segundos e
entrega a promessa. Sem dois cortes sobre a mesma ideia. Confira os tempos exatos em transcricao.json.
Pedido de quem pediu o lote: {instrucoes}
Responda APENAS com JSON: {{"resumo": "2-3 linhas sobre o episódio e o critério", "cortes": [{{"titulo": "...", "inicio": segundos,
"fim": segundos, "gancho": "primeira frase do corte", "motivo": "por que funciona"}}]}}"""


def selecionar(lote, ep, estilo=None):
    pasta = BASE / str(ep["id"])
    try:
        sincronizar_estilos()
        prompt = SELECAO.format(n=lote["quantidade"], instrucoes=lote.get("instrucoes") or "(nenhum)")
        extra = []
        if estilo and "## EPISÓDIO" in (estilo.get("receita") or ""):
            W = pasta_estilo(ep, estilo)
            W.mkdir(parents=True, exist_ok=True)
            bruto = next(pasta.glob("bruto.*"))
            prompt += ("\n\nANTES de responder, prepare o episódio para o estilo escolhido (pasta de trabalho W = {}; bruto = {}; "
                       "transcrição = {}). Os cortes que você escolher vão ser produzidos nesse estilo:\n{}").format(
                W, bruto, pasta / "transcricao.txt", receita(estilo, "EPISÓDIO"))
            extra = [W]
        texto = claude(prompt, pasta, "opus", limite=2400, extra_dirs=extra)
        r = json_final(texto)
        cortes = [c for c in r.get("cortes") or [] if float(c.get("fim", 0)) > float(c.get("inicio", 0))]
        if not cortes:
            raise RuntimeError("nenhum corte válido na seleção")
        srv("lista", lote=lote["id"], cortes=cortes, resumo=r.get("resumo") or "")
        log("lote", lote["id"], "com", len(cortes), "cortes para conferir")
    except Exception as e:
        log("seleção do lote", lote["id"], "falhou:", e)
        srv("lote", id=lote["id"], estado="erro", erro=str(e)[:500])


# ------------------------------------------------------------------------------------------- ③ produção e ④ entrega
PRODUCAO = """Você produz UM corte vertical da esteira do CW Studios (CW Cast), sem ninguém olhando: não pergunte nada, decida e
siga. Valem as regras e lições do time que você recebeu e o pedido abaixo.
Corte {cid}: do segundo {inicio:.2f} ao {fim:.2f} do episódio (bruto: {bruto}; transcrição com tempo por palavra: {transc}).
Título: {titulo}
Gancho sugerido: {gancho}
{ajuste}{higgs}
Pasta de trabalho do corte: {pasta}{w}

ESTILO DE EDIÇÃO:
{receita}

Responda APENAS com JSON: {{"aprovado": true|false, "motivo": "uma linha", "legenda_post": "legenda curta para o post",
"final": "caminho do vídeo final", "previa": "caminho de uma prévia leve (pode ser o próprio final)"}}"""


def subir_previa(arq, corte):
    """Prévia leve (540x960) no bucket "previas", para o player da página. Devolve o caminho no bucket."""
    import urllib.request
    leve = arq.with_name(arq.stem + "_web.mp4")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(arq), "-vf", "scale=540:-2", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "28", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(leve)], check=True)
    caminho = "{}/{}-{}.mp4".format(corte["lote_id"], corte["id"], os.urandom(4).hex())
    import kit
    r = kit._funcao({"acao": "previa", "caminho": caminho})
    req = urllib.request.Request(r["url"], data=leve.read_bytes(), method="PUT", headers={"Content-Type": "video/mp4", "x-upsert": "true"})
    with urllib.request.urlopen(req, timeout=300):
        pass
    return caminho


PREVIAS_DRIVE = DRIVE / "CW Cast - Cortes" / "11. PUBLICAÇÃO" / "0. Prévias do Titeu"


def produzir(item):
    c, lote, ep, estilo = item["corte"], item["lote"], item["episodio"], item.get("estilo")
    pasta = BASE / str(ep["id"]) / "lote-{}".format(lote["id"]) / "corte-{:02d}".format(c["ordem"])
    pasta.mkdir(parents=True, exist_ok=True)
    passo = lambda t: srv("corte", id=c["id"], etapa=t)
    try:
        sincronizar_estilos()
        bruto = next((BASE / str(ep["id"])).glob("bruto.*"))
        ini, fim = max(0.0, float(c["inicio"]) - 1), float(c["fim"]) + 1
        segs = json.loads((BASE / str(ep["id"]) / "transcricao.json").read_text())
        palavras = [{"p": w["p"], "i": round(w["i"] - ini, 2), "f": round(w["f"] - ini, 2)}
                    for s in segs for w in s.get("palavras") or [] if ini <= w["i"] <= fim]
        (pasta / "palavras.json").write_text(json.dumps(palavras, ensure_ascii=False))
        extra, w = [], ""
        if estilo and "## EPISÓDIO" in (estilo.get("receita") or ""):
            W = pasta_estilo(ep, estilo)
            extra, w = [W], "\nPasta de trabalho do episódio neste estilo (W): {} (já preparada na seleção)".format(W)
        else:   # estilos simples trabalham num trecho já cortado
            passo("cortando o trecho do 4K")
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(ini), "-i", str(bruto), "-t", str(fim - ini),
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", str(pasta / "trecho.mp4")], check=True)
            w = "\nNa pasta do corte: trecho.mp4 (com 1 s de folga) e palavras.json (tempos a partir do começo do trecho)."
        cid = "l{}c{:02d}".format(lote["id"], c["ordem"])
        higgs = int(lote.get("higgs_orcamento") or 0)
        texto = claude(PRODUCAO.format(cid=cid, inicio=float(c["inicio"]), fim=float(c["fim"]), bruto=bruto,
                                       transc=BASE / str(ep["id"]) / "transcricao.json", titulo=c["titulo"], gancho=c.get("gancho") or "",
                                       ajuste="Pedido de melhoria do editor (aplique na versão anterior, se existir): " + c["ajuste"] + "\n" if c.get("ajuste") else "",
                                       higgs="Higgsfield liberado: até {} geração(ões) neste lote (ferramenta cw-studios › gerar).".format(higgs) if higgs else "Higgsfield: não use.",
                                       pasta=pasta, w=w, receita=receita(estilo, "CORTE") if estilo else "Vertical 1080x1920 com legenda e gancho."),
                       pasta, "sonnet", ao_passo=passo, limite=2700, extra_dirs=extra, higgs=higgs)
        r = json_final(texto)
        final = Path(r.get("final") or "")
        if final and not final.is_absolute():
            final = (extra[0] if extra else pasta) / final
        if not r.get("aprovado") or not final.is_file():
            srv("corte", id=c["id"], estado="reprovado", erro=(r.get("motivo") or "sem vídeo final")[:500])
            return
        passo("subindo a prévia")
        previa = Path(r.get("previa") or final)
        if not previa.is_absolute():
            previa = (extra[0] if extra else pasta) / previa
        caminho = subir_previa(previa if previa.is_file() else final, c)
        PREVIAS_DRIVE.mkdir(parents=True, exist_ok=True)
        nome = "{}_{}{}".format(cid, re.sub(r"[^A-Za-z0-9]+", "-", c["titulo"])[:60].strip("-"), final.suffix)
        shutil.copyfile(final, PREVIAS_DRIVE / nome)
        (pasta / "legenda_post.txt").write_text(r.get("legenda_post") or c["titulo"], encoding="utf-8")
        srv("corte", id=c["id"], estado="previa", etapa="esperando aprovação", arquivo=str(final), previa=caminho,
            final_drive=str((PREVIAS_DRIVE / nome).relative_to(DRIVE)))
        log("corte", c["id"], "em prévia")
    except Exception as e:
        log("corte", c["id"], "falhou:", traceback.format_exc()[-600:])
        srv("corte", id=c["id"], estado="erro", erro=str(e)[:500])


def entregar(item):
    """Corte aprovado na prévia → Publicação (Drive "1. Para aprovar" + card)."""
    c, ep = item["corte"], item["episodio"]
    try:
        final = Path(c.get("arquivo") or "")
        legenda = Path(BASE / str(ep["id"])).glob("lote-{}/corte-{:02d}/legenda_post.txt".format(c["lote_id"], c["ordem"]))
        leg = next(legenda, None)
        saida = subprocess.run([sys.executable, str(AQUI / "publicar.py"), "enviar", "--arquivo", str(final), "--titulo", c["titulo"][:120],
                                "--legenda", leg.read_text(encoding="utf-8") if leg else c["titulo"], "--episodio", ep["nome"][:120]],
                               capture_output=True, text=True, timeout=900)
        m = re.search(r"P-(\d+)", saida.stdout)
        if not m:
            raise RuntimeError("não consegui entregar: " + (saida.stderr or saida.stdout)[-300:])
        srv("corte", id=c["id"], estado="entregue", etapa="na Publicação", publicacao_id=int(m.group(1)))
        log("corte", c["id"], "entregue P-" + m.group(1))
    except Exception as e:
        log("entrega do corte", c["id"], "falhou:", e)
        srv("corte", id=c["id"], estado="erro", erro=str(e)[:500])


def operario(n):
    while True:
        try:
            item = srv("pegar_corte")
        except central.ErroCentral as e:
            log("operário", n, "central:", e)
            time.sleep(20)
            continue
        if item:
            produzir(item)
            continue
        try:
            aprovado = srv("pegar_aprovado")
        except central.ErroCentral:
            aprovado = None
        if aprovado:
            entregar(aprovado)
        else:
            time.sleep(10)


def cmd_rodar():
    BASE.mkdir(parents=True, exist_ok=True)
    escrever_cerca()
    for n in range(OPERARIOS):
        threading.Thread(target=operario, args=(n + 1,), daemon=True).start()
    log("esteira ligada:", OPERARIOS, "operários; GPU:", tem_gpu())
    threading.Thread(target=sincronizar_estilos, daemon=True).start()
    ultimo_vigia = 0
    while True:
        try:
            if time.time() - ultimo_vigia > 120:
                ultimo_vigia = time.time()
                vigiar()
            t = srv("trabalho")
            if t and t.get("tipo") == "preparar":
                preparar(t["episodio"])
                continue
            if t and t.get("tipo") == "selecionar":
                selecionar(t["lote"], t["episodio"], t.get("estilo"))
                continue
        except central.ErroCentral as e:
            log("central:", e)
        except Exception:
            log("erro:", traceback.format_exc()[-600:])
        time.sleep(15)


if __name__ == "__main__":
    {"rodar": cmd_rodar, "porteiro": cmd_porteiro}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
