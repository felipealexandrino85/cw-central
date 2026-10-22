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
ENTRADA = DRIVE / "CW Cast - Cortes" / "1. ENVIE AQUI" / "Podcast (episódio inteiro)"
ENTRADA_REELS = DRIVE / "CW Cast - Cortes" / "1. ENVIE AQUI" / "Reels (câmera + tela)"     # FabI.Ano: uma pasta por reel
PRONTOS_REELS = DRIVE / "CW Cast - Cortes" / "_sistema (não mexer)" / "Prontos (antes da reorganização)" / "4. PRONTOS - Reels"   # só para não refazer os antigos
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


def vigiar_reels():
    """Cada pasta nova em "1. ENVIE AQUI/Reels" vira um reel do FabI.Ano (depois que os arquivos pararem de mudar).
    Pastas que já tinham pronto nos PRONTOS antigos são registradas como arquivadas (não reprocessa o que já foi feito)."""
    if not ENTRADA_REELS.is_dir():
        return
    for pasta in sorted(ENTRADA_REELS.iterdir()):
        if not pasta.is_dir():
            continue
        try:
            arqs = [f for f in pasta.rglob("*") if f.is_file() and VIDEO.search(f.name)]
            tam = sum(f.stat().st_size for f in arqs)
        except OSError:
            continue
        if not arqs:
            continue
        chave = "reels:" + pasta.name
        if _TAMANHOS.get(chave) == tam:
            srv("registrar", arquivo=str(pasta.relative_to(DRIVE)), nome=pasta.name, tamanho=tam, linha="reels",
                arquivado=(PRONTOS_REELS / pasta.name).exists())
        _TAMANHOS[chave] = tam


def preparar_reel(ep):
    """Copia a pasta do reel (câmera + tela), transcreve a fala (a gravação mais longa com áudio) e manda para a produção."""
    eid = ep["id"]
    pasta = BASE / str(eid)
    bruto = pasta / "bruto"
    try:
        srv("episodio", id=eid, estado="preparando", etapa="copiando a pasta do reel", progresso=10)
        for tentativa in range(4):   # o Drive montado às vezes falha no meio de um arquivo grande: tenta de novo
            if bruto.exists():
                shutil.rmtree(bruto)
            try:
                shutil.copytree(DRIVE / ep["arquivo"], bruto, copy_function=shutil.copyfile)   # o Drive montado não aceita copiar atributos (dá erro 5)
                break
            except (OSError, shutil.Error) as e:
                if tentativa == 3:
                    raise
                log("cópia do reel", eid, "falhou, tentando de novo:", str(e)[:200])
                time.sleep(30 * (tentativa + 1))
        videos = sorted([f for f in bruto.rglob("*") if f.is_file() and VIDEO.search(f.name)], key=lambda f: ffprobe_dur(f) or 0, reverse=True)
        com_audio = [f for f in videos if subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(f)],
                                                         capture_output=True, text=True).stdout.strip()]
        principal = (com_audio or videos)[0]
        dur = ffprobe_dur(principal)
        srv("episodio", id=eid, etapa="transcrevendo a fala", progresso=40, duracao_s=round(dur or 0, 1))
        audio = pasta / "audio.wav"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(principal), "-vn", "-ac", "1", "-ar", "16000", str(audio)], check=True)
        segs = transcrever_groq(audio, pasta, dur, lambda pct: srv("episodio", id=eid, progresso=pct))
        (pasta / "transcricao.json").write_text(json.dumps(segs, ensure_ascii=False))
        (pasta / "transcricao.txt").write_text("\n".join("[{}] {}".format(hms(s["inicio"]), s["texto"]) for s in segs), encoding="utf-8")
        (pasta / "material.txt").write_text("Fala principal (com áudio): {}\nTodos os vídeos da pasta:\n{}".format(
            principal, "\n".join("- {} ({:.0f} s)".format(f, ffprobe_dur(f) or 0) for f in videos)), encoding="utf-8")
        srv("episodio", id=eid, estado="pronto", etapa="na produção do FabI.Ano", progresso=100)
        srv("reel_pronto", id=eid)
        log("reel", eid, "pronto para editar")
    except Exception as e:
        log("preparo do reel", eid, "falhou:", traceback.format_exc()[-600:])
        srv("episodio", id=eid, estado="erro", erro=str(e)[:500])


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
    apr = re.search(r"^## APRENDIZADOS[^\n]*\n(.*?)(?=^## |\Z)", t, re.S | re.M)   # aprendizados do editor valem sempre
    return (cab + ("\n" + sec.group(1) if sec else "") + ("\n\nAPRENDIZADOS DO EDITOR (siga sempre):\n" + apr.group(1) if apr else "")).strip()


def pasta_estilo(ep, estilo):
    """Pasta de trabalho do episódio num estilo. Versão em teste usa uma pasta própria (estilo-<slug>-v<N>)."""
    e = estilo or {}
    return BASE / str(ep["id"]) / ("estilo-" + (e.get("pasta") or e.get("slug") or "generico"))


def _ignorar_trabalho(raiz):
    """Ao copiar a pasta de outro estilo/versão: leva os scripts e o mapa de câmeras (work/eps), não os cortes e os finais."""
    raiz = str(raiz)

    def ignora(d, nomes):
        if d == raiz:
            return [n for n in nomes if n in ("saida", ".originais")]
        if d == os.path.join(raiz, "work"):
            return [n for n in nomes if n != "eps"]
        return []
    return ignora


def garantir_pasta_estilo(ep, estilo):
    """Estilo novo ou versão em teste ainda sem pasta neste episódio: copia a do estilo de partida e aplica os arquivos da versão."""
    W = pasta_estilo(ep, estilo)
    base = (estilo or {}).get("base_pasta")
    if not W.exists() and base:
        origem = BASE / str(ep["id"]) / ("estilo-" + base)
        if origem.is_dir():
            shutil.copytree(origem, W, ignore=_ignorar_trabalho(origem), symlinks=True)
    if W.exists():
        aplicar_overrides(W, (estilo or {}).get("overrides") or {})
    return W


EDITAVEIS = re.compile(r"\.(md|json|txt|ya?ml|ass|css)$", re.I)


def aplicar_overrides(W, overrides):
    """Arquivos de texto do estilo trocados por uma versão ({caminho: conteúdo}). Guarda o original em .originais/ e
    devolve o original quando a versão deixa de trocar aquele arquivo (voltar versão funciona)."""
    W = Path(W)
    manifesto = W / ".originais" / "trocados.json"
    try:
        antes = set(json.loads(manifesto.read_text()))
    except (OSError, ValueError):
        antes = set()
    agora = set()
    for cam, conteudo in (overrides or {}).items():
        if not isinstance(conteudo, str) or ".." in Path(cam).parts or Path(cam).is_absolute() or not EDITAVEIS.search(cam):
            continue
        alvo, orig = W / cam, W / ".originais" / cam
        if alvo.exists() and not orig.exists():
            orig.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(alvo, orig)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        if not alvo.exists() or alvo.read_text(encoding="utf-8", errors="replace") != conteudo:
            alvo.write_text(conteudo, encoding="utf-8")
        agora.add(cam)
    for cam in antes - agora:   # a versão nova não troca mais este arquivo: volta o original
        orig = W / ".originais" / cam
        if orig.exists():
            shutil.copy2(orig, W / cam)
    if agora or antes:
        manifesto.parent.mkdir(parents=True, exist_ok=True)
        manifesto.write_text(json.dumps(sorted(agora)))


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


def claude(prompt, pasta, modelo, ao_passo=None, limite=1800, extra_dirs=(), higgs=0, papel="video"):
    """Roda um Claude da esteira em 'pasta'. Devolve o texto final."""
    if not TOKEN_CLAUDE.exists():
        raise RuntimeError("a conta Claude do CW Studios ainda não foi ligada na esteira (aba Esteira › ☁️)")
    cmd = [str(HOME / ".local/bin/claude"), "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", modelo,
           "--permission-mode", "default", "--settings", str(CERCA), "--add-dir", str(pasta)]
    for d in extra_dirs:
        cmd += ["--add-dir", str(d)]
    env = dict(os.environ, CLAUDE_CODE_OAUTH_TOKEN=TOKEN_CLAUDE.read_text().strip(), CW_EST_PASTA=str(pasta),
               CW_ESC_PASTA=str(pasta), CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1", CW_SERVIDOR="1",
               CW_ESTEIRA="1", CW_EST_HIGGS=str(higgs), CW_PAPEL=papel or "video")   # CW_ESTEIRA: abertura enxuta (só regras e lições do time)
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


# ------------------------------------------------------------------------------------------- motor automático (estilo do Timeu)
# A parte mecânica do roteiro do Timeu roda como programa; o Claude entra uma vez (decisões editoriais) e só volta se um gate reprovar.
DECISAO_TIMEU = """Você é o editor do corte {cid} (CW Cast, estilo do Timeu), sem ninguém olhando. A parte mecânica já foi feita: na pasta
{W} existem work/{cid}/plan.json (rascunho com fonte, headline, gancho, câmeras, trilha), work/{cid}/transcript.txt,
work/{cid}/transcript.json e work/{cid}/sugestoes.json (candidatos e a lista "revisar").
Sua tarefa é SÓ a parte editorial, editando work/{cid}/plan.json:
- highlights (amarelos) e enfase: leia só REGRAS_EDITORIAIS §3 e CRITERIOS C-10 em {ref};
- gancho: confira hook.duration e headline (no máximo 2 linhas de ~32 letras, quebra com |);
- foto do gancho (hook.media): {foto}
- b-roll só se houver arquivo bom em broll/ (sem b-roll é melhor que genérico); P&B, punch, sfx e trilha conforme §6–§10 se fizer sentido.
{ajuste}Valem as regras e lições do time. NÃO rode render, gates nem ferramentas de tempo (isso é automático). Seja direto: poucas leituras.
Responda APENAS com JSON: {{"ok": true, "notas": "uma linha do que decidiu"}}"""

CORRIGE_TIMEU = """O corte {cid} (CW Cast, estilo do Timeu) foi renderizado e os gates técnicos reprovaram. Pasta {W}:
work/{cid}/plan.json, work/{cid}/gates.json (o que falhou), transcript.json e a prévia saida/{cid}_rascunho.mp4.
Corrija SÓ a causa de cada FAIL (editando plan.json ou com o script indicado em RECEITAS_OPERACIONAIS §3 em {ref}),
sem refazer o corte. NÃO renderize nem rode os gates (é automático). Valem as regras do time.
Responda APENAS com JSON: {{"ok": true, "corrigiu": "uma linha"}}"""


class Falhou(Exception):
    pass


# ------------------------------------------------------------------------------------------- ajuste garantido
CONFERE = """Conferência de um AJUSTE pedido pelo editor no corte "{titulo}" (CW Cast).
Pedido do editor: {pedido}
{prints}Quadros do corte já refeito (com o tempo no nome do arquivo): {quadros}
Abra os prints (se houver) e os quadros e responda com honestidade se o pedido foi ATENDIDO no corte novo.
Se o pedido era mostrar quem fala e essa pessoa não aparece em nenhuma câmera do vídeo original naquele momento, cobrir o trecho
com B-roll ou foto (ou tirar a fala) conta como atendido; diga isso no motivo.
Se foi, escreva também uma LIÇÃO curta e geral (imperativo, vale para os próximos cortes, sem citar este corte) para o erro não se repetir.
Responda APENAS com JSON: {{"atendido": true|false, "motivo": "uma linha do que você viu", "licao": "regra geral ou vazio",
"categoria": "corte|legenda|visual|audio|conteudo|marca|outro"}}"""


def baixar_prints(c, pasta):
    """Prints anexados ao pedido de ajuste → arquivos na pasta do corte."""
    caminhos = c.get("ajuste_imagens") or []
    if not caminhos:
        return []
    import urllib.request
    import kit
    destino = pasta / "prints_ajuste"
    destino.mkdir(exist_ok=True)
    saida = []
    try:
        links = kit._funcao({"acao": "ajuste", "caminhos": caminhos}).get("links") or {}
    except Exception as e:
        log("prints do ajuste:", e)
        return []
    for i, (cam, url) in enumerate(links.items()):
        arq = destino / "print{}{}".format(i + 1, Path(cam).suffix or ".png")
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                arq.write_bytes(r.read())
            saida.append(arq)
        except OSError:
            pass
    return saida


def texto_prints(prints):
    return ("Prints que o editor mandou mostrando o problema (abra e veja): {}\n".format(", ".join(str(x) for x in prints))) if prints else ""


def _tempo_da_frase(pedido, transcript):
    """Segundo do corte em que começa a frase citada entre aspas no pedido (ou None)."""
    try:
        words = json.loads(Path(transcript).read_text(encoding="utf-8"))["words"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    limpa = lambda x: re.sub(r"[^\w]", "", x.lower())
    for m in re.finditer(r"[\"'“‘]([^\"'”’]{6,})[\"'”’]", pedido or ""):
        alvo = [limpa(x) for x in m.group(1).split() if limpa(x)]
        if not alvo:
            continue
        ws = [limpa(w["w"]) for w in words]
        n = min(len(alvo), 4)
        for i in range(len(ws) - n + 1):
            if sum(ws[i + k] == alvo[k] for k in range(n)) >= n - 1:
                return float(words[i]["start"])
    return None


def quadros_do_ajuste(video, pedido, pasta, transcript=None):
    """Quadros do trecho citado no pedido (ex.: "0:18 até 0:29" ou uma frase entre aspas); sem nada citado, 5 quadros espalhados."""
    dur = ffprobe_dur(video) or 60
    tempos = []
    t_frase = _tempo_da_frase(pedido, transcript) if transcript else None
    if t_frase is not None:
        tempos = [max(0, t_frase - 0.5), t_frase + 0.3, t_frase + 1.0, min(dur - 0.1, t_frase + 2.0)]
    for m in re.finditer(r"(\d{1,2}):(\d{2})|(?:segundo|seg|s)\s*(\d{1,3})(?![:\d])", pedido or "", re.I):
        t = int(m.group(1)) * 60 + int(m.group(2)) if m.group(1) else int(m.group(3))
        if 0 <= t <= dur:
            tempos.append(t)
    if t_frase is not None:
        pass
    elif len(tempos) >= 2:
        a, b = min(tempos), max(tempos)
        tempos = [a + (b - a) * k / 4 for k in range(5)]
    elif tempos:
        tempos = [max(0, tempos[0] - 1), tempos[0], min(dur - 0.1, tempos[0] + 1.5)]
    else:
        tempos = [dur * k / 6 for k in range(1, 6)]
    destino = pasta / "conferencia"
    shutil.rmtree(destino, ignore_errors=True)
    destino.mkdir()
    saida = []
    for t in tempos:
        arq = destino / "quadro_{:05.1f}s.jpg".format(t)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "{:.2f}".format(t), "-i", str(video), "-frames:v", "1", "-vf", "scale=540:-2", str(arq)],
                       env=dict(os.environ, PATH="/usr/local/bin:" + os.environ.get("PATH", "")))
        if arq.exists():
            saida.append(arq)
    return saida


def conferir_ajuste(c, video, pasta, prints, passo, transcript=None):
    """O Claude olha os quadros do trecho e diz se o pedido foi atendido. Devolve o dict da resposta."""
    passo("Conferindo se o ajuste foi atendido")
    quadros = quadros_do_ajuste(video, c.get("ajuste"), pasta, transcript)
    try:
        r = json_final(claude(CONFERE.format(titulo=c["titulo"], pedido=c.get("ajuste"), prints=texto_prints(prints),
                                             quadros=", ".join(str(q) for q in quadros)), pasta, "sonnet", ao_passo=passo, limite=600))
    except Exception as e:
        r = {"atendido": False, "motivo": "não consegui conferir ({})".format(e)}
    return r


def registrar_conferencia(c, r, cid):
    ok = bool(r.get("atendido"))
    srv("corte", id=c["id"], ajuste_conferido="sim" if ok else "nao", ajuste_nota=(r.get("motivo") or "")[:500])
    if ok and len(r.get("licao") or "") >= 5:
        srv("licao", texto=r["licao"][:500], categoria=r.get("categoria") or "", kit="cw-cast", ref="corte " + cid, pedido=c.get("ajuste") or "")


CAMERAS = """Mapa de câmeras do episódio (estilo do Timeu), na pasta {W}. O script já separou as câmeras do episódio e montou a folha
work/eps/ep/cameras.jpg (um quadro por câmera, com grade). Abra SÓ essa imagem e, para cada câmera (C1, C2, …), grave o centro do
enquadramento vertical pelo CORPO de quem aparece (cabeça e ombros inteiros, sem cortar ninguém; RE §5 em {ref}):
  python -I tools/episodio_preparar.py --out work/eps/ep --definir-camera C1 <cx> <cy> --descricao "quem aparece"
(cx/cy de 0 a 1; câmera aberta com duas pessoas: --dz 2.2 e cx de quem mais fala). Não faça mais nada.
Responda APENAS com JSON: {{"ok": true, "cameras": "C1=..., C2=..."}}"""
_PREP = threading.Lock()


def preparar_episodio_estilo(ep, W, janelas, passo):
    """Uma vez por lote: mapa de câmeras do episódio nos trechos do lote + cx/cy de cada câmera (o Claude olha a folha)."""
    epj = W / "work" / "eps" / "ep" / "episodio.json"
    marca = W / "work" / "eps" / "ep" / ".janelas"
    with _PREP:
        try:
            e = json.loads(epj.read_text())
        except (OSError, ValueError):
            e = {}
        ja = marca.read_text().strip() if marca.exists() else ""
        definidas = lambda d: bool(d.get("cameras")) and all(c.get("cx") is not None for c in d["cameras"])
        if e.get("src") and definidas(e) and (not janelas or ja == janelas):
            return
        passo("Mapa de câmeras do episódio")
        bruto = next((BASE / str(ep["id"])).glob("bruto.*"))
        env = dict(os.environ, PATH="/usr/local/bin:" + os.environ.get("PATH", ""))
        args = [sys.executable, "-I", "tools/episodio_preparar.py", "--src", str(bruto), "--transcricao", str(BASE / str(ep["id"]) / "transcricao.txt"),
                "--out", "work/eps/ep", "--refazer"] + (["--janelas", janelas] if janelas else [])
        r = subprocess.run(args, cwd=str(W), env=env, capture_output=True, text=True, timeout=3600)
        if r.returncode != 0 or not (W / "work" / "eps" / "ep" / "cameras.jpg").exists():
            raise Falhou("mapa de câmeras: " + (r.stderr or r.stdout)[-300:])
        claude(CAMERAS.format(W=W, ref=HOME / ".claude/skills/cw-estilo-cw-cast-timeu/references"), W, "sonnet", ao_passo=passo, limite=900)
        e = json.loads(epj.read_text())
        if not definidas(e):
            raise Falhou("as câmeras não foram definidas")
        marca.write_text(janelas or "")


def mmss_t(seg):
    seg = max(0.0, float(seg))
    h, r = divmod(seg, 3600)
    return "{}:{:02d}:{:05.2f}".format(int(h), int(r // 60), r % 60) if h else "{}:{:05.2f}".format(int(r // 60), r % 60)


def produzir_timeu(c, lote, ep, W, pasta, passo, higgs, janelas="", estilo=None):
    """Roteiro do Timeu como programa. Devolve (final, previa). Levanta Falhou se um passo mecânico quebrar."""
    cid = "l{}c{:02d}".format(lote["id"], c["ordem"])
    bruto = next((BASE / str(ep["id"])).glob("bruto.*"))
    # referências do estilo: as da pasta de trabalho (com os arquivos trocados pela versão) ou as da skill instalada
    ref = next((r for r in (W / "references", HOME / ".claude" / "skills" / ("cw-estilo-" + ((estilo or {}).get("slug") or "")) / "references")
                if r.is_dir()), HOME / ".claude" / "skills" / "cw-estilo-cw-cast-timeu" / "references")
    env = dict(os.environ, PATH="/usr/local/bin:" + os.environ.get("PATH", ""))
    epj = W / "work" / "eps" / "ep" / "episodio.json"
    preparar_episodio_estilo(ep, W, janelas, passo)

    def rodar(nome, args, ok=(0,), tempo=900):
        passo(nome)
        r = subprocess.run([sys.executable, "-I"] + [str(a) for a in args], cwd=str(W), env=env, capture_output=True, text=True, timeout=tempo)
        (pasta / "motor.log").open("a").write("\n$ {}\n{}\n{}\n".format(" ".join(str(a) for a in args), r.stdout[-3000:], r.stderr[-2000:]))
        if r.returncode not in ok:
            raise Falhou("{}: {}".format(nome, (r.stderr or r.stdout)[-400:]))
        return r.stdout

    work = W / "work" / cid
    plan = work / "plan.json"
    ini, fim = float(c["inicio"]), float(c["fim"])
    if c.get("ajuste") and plan.exists():
        # ajuste: parte do plano que já existe, mas refaz os planos de câmera pelo mapa ATUAL do episódio
        # (cortes feitos antes do mapa ficaram com uma câmera só e o recorte errado nas trocas)
        passo("Enquadramento pelo mapa de câmeras")
        sys.path.insert(0, str(W / "tools"))
        try:
            import plano_rascunho
            pl = json.loads(plan.read_text(encoding="utf-8"))
            tr = json.loads((work / "transcript.json").read_text(encoding="utf-8"))
            shots = plano_rascunho.montar_shots([(float(x), float(y)) for x, y in tr["ranges"]], json.loads(epj.read_text(encoding="utf-8")), [])
            pl["_base_shots"], pl["shots"] = [dict(x) for x in shots], [dict(x) for x in shots]
            plan.write_text(json.dumps(pl, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            (pasta / "motor.log").open("a").write("\nenquadramento do ajuste: {}\n".format(e))
        finally:
            sys.path.remove(str(W / "tools"))
        rodar("Pontas e emendas", ["scripts/alinhar_frames.py", cid], ok=(0, 1))
    else:
        if work.exists():   # tentativa anterior: guarda e começa limpo
            shutil.move(str(work), str(W / "work" / "{}_antigo_{}".format(cid, int(time.time()))))
        work.mkdir(parents=True)
        ficha = W / "work" / "eps" / "ep" / "ficha_{}".format(cid)
        if not ficha.with_suffix(".json").exists():
            (work / "propostas.json").write_text(json.dumps([{"id": cid, "ranges": "{}-{}".format(mmss_t(ini), mmss_t(fim)),
                "headline": c["titulo"][:64], "foto": c.get("gancho") or c["titulo"], "motivo": c.get("motivo") or ""}], ensure_ascii=False))
            rodar("Ficha do corte", ["tools/ficha_corte.py", "--episodio", epj, "--propostas", work / "propostas.json", "--out", ficha])
            rodar("Ficha do corte", ["tools/ficha_corte.py", "--aprovar", "humano:conferencia-na-central", "--ficha", ficha.with_suffix(".json")])
        ranges = "{}-{}".format(mmss_t(ini), mmss_t(fim))
        saida = rodar("Pontas no vale do som", ["tools/pontas.py", "--src", bruto, "--ranges", ranges], ok=(0, 1))
        linha = next((l for l in saida.splitlines() if l.startswith("RANGES:")), "")
        sug = linha.split(":", 1)[1].strip() if linha else ""
        medido = bool(sug) and not re.search(r"^FAIL", saida, re.M)   # alguma ponta sem vale: deixa o transcribe ajustar sozinho
        rodar("Transcrição do trecho", ["scripts/transcribe.py", "--src", bruto, "--ranges", sug if medido else ranges, "--out", "work/" + cid,
                                        "--cut-silence", "0.8", "--cut-gaps", "0", "--keep-pause", "0.24"] + (["--no-snap-edges"] if medido else []), tempo=1200)
        base = ["tools/plano_rascunho.py", "--ficha", ficha.with_suffix(".json"), "--corte", cid, "--episodio", epj, "--transcript", "work/{}/transcript.json".format(cid), "--root", "."]
        rodar("Plano do corte", base)
        rodar("Pontas e emendas", ["scripts/refinar_inicios.py", "work/{}/plan.json".format(cid)], ok=(0, 1))
        rodar("Pontas e emendas", ["scripts/encaixar_voz.py", "work/{}/plan.json".format(cid)], ok=(0, 1))
        rodar("Pontas e emendas", ["scripts/alinhar_frames.py", cid], ok=(0, 1))
        rodar("Enquadramento", base + ["--atualizar-shots"], ok=(0, 1))
    # a única chamada do Claude: as decisões editoriais
    foto = ("se ainda não for arquivo, gere UMA imagem no Higgsfield (cw-studios › gerar; foto realista, horizontal, sem texto) a partir da descrição e salve em work/{}/gancho.jpg; use esse caminho.".format(cid)
            if higgs else "use uma foto de assets/ganchos que combine com a descrição; se nenhuma servir, deixe a descrição e diga nas notas.")
    prints = baixar_prints(c, pasta) if c.get("ajuste") else []
    passo("Plano do corte (gancho, legenda, B-roll)")
    claude(DECISAO_TIMEU.format(cid=cid, W=W, ref=ref, foto=foto,
                                ajuste=("AJUSTE PEDIDO PELO EDITOR (o corte já existe): " + c["ajuste"] + ". Mude só isso; se for câmera/enquadramento, "
                                        "ajuste cx/cy/stack dos shots daquele trecho conferindo o episodio.json (câmeras) e quem fala "
                                        "(os planos já foram refeitos pelo mapa de câmeras). Se quem fala NÃO aparece em nenhuma câmera do bruto "
                                        "naquele momento, reenquadrar não resolve: cubra o trecho com B-roll ou com a foto do gancho, ou tire a fala "
                                        "se o corte continuar fazendo sentido, e diga isso nas notas.\n" + texto_prints(prints))
                                if c.get("ajuste") else ""),
           W, "sonnet", ao_passo=passo, limite=900, higgs=higgs)
    for ciclo in range(3):
        rodar("Enquadramento", ["scripts/movimento.py", "work/{}/plan.json".format(cid)], ok=(0, 1))
        rodar("Revisão técnica", ["tools/plan_validate.py", "work/{}/plan.json".format(cid), "--root", ".", "--saida-prefixo", "saida/"], ok=(0, 1))
        rodar("Render da prévia", ["scripts/render.py", "work/{}/plan.json".format(cid), "--preview"], tempo=1800)
        rodar("Revisão técnica", ["tools/gates.py", "work/{}/plan.json".format(cid), "--root", ".", "--video", "saida/{}_rascunho.mp4".format(cid),
                                  "--json", "work/{}/gates.json".format(cid), "--replay"], ok=(0, 1, 2))
        try:
            g = json.loads((work / "gates.json").read_text())
        except (OSError, ValueError):
            g = {}
        falhas = [x for x in g.get("gates") or [] if x.get("status") == "FAIL"]
        if g.get("geral") != "FAIL" or not falhas or ciclo == 2:
            break
        passo("Corrigindo o que os gates reprovaram")
        claude(CORRIGE_TIMEU.format(cid=cid, W=W, ref=ref), W, "sonnet", ao_passo=passo, limite=900)
    rodar("Render final", ["scripts/render.py", "work/{}/plan.json".format(cid)], tempo=2400)
    rodar("Revisão técnica", ["tools/final_probe.py", "saida/{}.mov".format(cid), "--perfil", "final"], ok=(0, 1))
    final, previa = W / "saida" / "{}.mov".format(cid), W / "saida" / "{}_preview.mp4".format(cid)
    if c.get("ajuste") and final.exists():   # ajuste garantido: confere, e se não atendeu corrige uma vez e confere de novo
        r = conferir_ajuste(c, final, pasta, prints, passo, work / "transcript.json")
        if not r.get("atendido"):
            passo("O ajuste não ficou bom: corrigindo de novo")
            claude(CORRIGE_TIMEU.format(cid=cid, W=W, ref=ref) + "\nO PEDIDO DO EDITOR AINDA NÃO FOI ATENDIDO: " + c["ajuste"]
                   + "\nO que a conferência viu: " + (r.get("motivo") or "") + "\n" + texto_prints(prints), W, "sonnet", ao_passo=passo, limite=900)
            rodar("Enquadramento", ["scripts/movimento.py", "work/{}/plan.json".format(cid)], ok=(0, 1))
            rodar("Render final", ["scripts/render.py", "work/{}/plan.json".format(cid), "--preview"], tempo=1800)
            rodar("Render final", ["scripts/render.py", "work/{}/plan.json".format(cid)], tempo=2400)
            r = conferir_ajuste(c, final, pasta, prints, passo, work / "transcript.json")
        registrar_conferencia(c, r, cid)
    if not final.exists():
        raise Falhou("o render final não gerou saida/{}.mov".format(cid))
    return final, (previa if previa.exists() else final)


PRODUCAO_REEL = """Você é o FabI.Ano, o editor de reels da Cardápio Web na esteira do CW Studios, sem ninguém olhando: não pergunte nada,
decida e siga. Valem as regras e lições do time e o pedido abaixo.
Reel {cid} "{titulo}": o material gravado está em {bruto} (a câmera e, se houver, a gravação de tela do OBS); veja {material}.
Transcrição da fala com tempo por palavra: {transc} (texto em {transc_txt}).
{ajuste}{higgs}
Pasta de trabalho: {pasta}. Arquivos da marca (telas do sistema, logos e vinhetas, trilhas, sons, fundos, mascote) em
{biblioteca} (só leitura).

ESTILO DE EDIÇÃO:
{receita}

PARA SER RÁPIDO (vale por cima da receita):
- Rode os scripts do estilo com "python3 script.py ..." (sem -I): a transcrição deles vai para a Groq, como no podcast.
- Rascunho primeiro: enquanto ajusta (plano, montagem, conferência e correções), monte na resolução menor (ex.: --res 1080x1920).
  Só quando a conferência aprovar, renderize UMA vez na qualidade final da receita (ex.: 2160x3840) e entregue esse arquivo.

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


BIBLIOTECA_CW = DRIVE / "CW Cast - Cortes" / "3. BIBLIOTECA" / "Cardápio Web"
PREVIAS_DRIVE = DRIVE / "CW Cast - Cortes" / "2. PARA APROVAR" / "Prévias da IA (Titeu e FabI.Ano)"
# scripts dos estilos (ex.: reel.py do Fabiano) acham o Drive e a biblioteca por estas variáveis
os.environ.setdefault("CW_DRIVE", str(DRIVE))
os.environ.setdefault("CW_BIBLIOTECA", str(BIBLIOTECA_CW))
# transcrição dos scripts dos estilos (faster_whisper) vai para a Groq, como no podcast; vale para "python3 script.py" (não para "python -I")
os.environ["CW_SCRIPTS"] = str(AQUI)
os.environ["PYTHONPATH"] = os.pathsep.join([str(AQUI / "groq_whisper")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p])


def produzir(item):
    c, lote, ep, estilo = item["corte"], item["lote"], item["episodio"], item.get("estilo")
    pasta = BASE / str(ep["id"]) / "lote-{}".format(lote["id"]) / "corte-{:02d}".format(c["ordem"])
    pasta.mkdir(parents=True, exist_ok=True)
    passo = lambda t: srv("corte", id=c["id"], etapa=t)
    try:
        sincronizar_estilos()
        if ep.get("linha") == "reels":
            produzir_reel(item, pasta, passo)
            return
        bruto = next((BASE / str(ep["id"])).glob("bruto.*"))
        ini, fim = max(0.0, float(c["inicio"]) - 1), float(c["fim"]) + 1
        segs = json.loads((BASE / str(ep["id"]) / "transcricao.json").read_text())
        palavras = [{"p": w["p"], "i": round(w["i"] - ini, 2), "f": round(w["f"] - ini, 2)}
                    for s in segs for w in s.get("palavras") or [] if ini <= w["i"] <= fim]
        (pasta / "palavras.json").write_text(json.dumps(palavras, ensure_ascii=False))
        extra, w = [], ""
        if estilo and "## EPISÓDIO" in (estilo.get("receita") or ""):
            W = garantir_pasta_estilo(ep, estilo)
            extra, w = [W], "\nPasta de trabalho do episódio neste estilo (W): {} (já preparada na seleção)".format(W)
        else:   # estilos simples trabalham num trecho já cortado
            passo("cortando o trecho do 4K")
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(ini), "-i", str(bruto), "-t", str(fim - ini),
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac", "-b:a", "192k", str(pasta / "trecho.mp4")], check=True)
            w = "\nNa pasta do corte: trecho.mp4 (com 1 s de folga) e palavras.json (tempos a partir do começo do trecho)."
        cid = "l{}c{:02d}".format(lote["id"], c["ordem"])
        higgs = int(lote.get("higgs_orcamento") or 0)
        prints = baixar_prints(c, pasta) if c.get("ajuste") else []
        if estilo and estilo.get("motor") == "timeu" and extra:
            try:
                janelas = "" if estilo.get("teste") else (item.get("janelas") or "")   # teste de estilo: usa o mapa de câmeras que já existe
                final, previa = produzir_timeu(c, lote, ep, extra[0], pasta, passo, higgs, janelas, estilo)
                entregar_previa(c, final, previa, cid, "", pasta)
                return
            except Falhou as e:   # passo mecânico quebrou: este corte vai pelo modo antigo (o Claude faz tudo)
                log("motor automático do corte", c["id"], "caiu para o modo Claude:", e)
                passo("modo Claude (o automático parou em: {})".format(str(e)[:80]))
        texto = claude(PRODUCAO.format(cid=cid, inicio=float(c["inicio"]), fim=float(c["fim"]), bruto=bruto,
                                       transc=BASE / str(ep["id"]) / "transcricao.json", titulo=c["titulo"], gancho=c.get("gancho") or "",
                                       ajuste=("AJUSTE PEDIDO PELO EDITOR (este corte já foi produzido e revisado): " + c["ajuste"] + "\n"
                                               "Não refaça do zero: parta do plan.json e dos arquivos que já existem para este corte, mude SÓ o que foi pedido, "
                                               "re-renderize e rode os gates de novo.\n" + texto_prints(prints)) if c.get("ajuste") else "",
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
        previa = Path(r.get("previa") or final)
        if not previa.is_absolute():
            previa = (extra[0] if extra else pasta) / previa
        if c.get("ajuste"):
            registrar_conferencia(c, conferir_ajuste(c, final, pasta, prints, passo), cid)
        entregar_previa(c, final, previa if previa.is_file() else final, cid, r.get("legenda_post"), pasta)
    except Exception as e:
        log("corte", c["id"], "falhou:", traceback.format_exc()[-600:])
        srv("corte", id=c["id"], estado="erro", erro=str(e)[:500])


def produzir_reel(item, pasta, passo):
    c, lote, ep, estilo = item["corte"], item["lote"], item["episodio"], item.get("estilo")
    cid = "r{}".format(ep["id"])
    higgs = int(lote.get("higgs_orcamento") or 0)
    prints = baixar_prints(c, pasta) if c.get("ajuste") else []
    base = BASE / str(ep["id"])
    # reel: a receita inteira vale (EPISÓDIO = análise do bruto + CORTE = o vídeo), com a pasta da skill do estilo
    skill_est = HOME / ".claude" / "skills" / ("cw-estilo-" + (estilo or {}).get("slug", ""))
    rec = ((estilo or {}).get("receita") or "").strip()
    if rec and skill_est.is_dir():
        rec = "A pasta desta skill (\"a pasta desta skill\" na receita) é {}. Use {} como TRAB.\n\n{}".format(skill_est, pasta, rec)
    texto = claude(PRODUCAO_REEL.format(cid=cid, titulo=c["titulo"], bruto=base / "bruto", material=base / "material.txt",
                                        transc=base / "transcricao.json", transc_txt=base / "transcricao.txt",
                                        ajuste=("AJUSTE PEDIDO PELO EDITOR (o reel já foi feito; parta do que existe na pasta de trabalho e mude SÓ isto): "
                                                + c["ajuste"] + "\n" + texto_prints(prints)) if c.get("ajuste") else "",
                                        higgs="Higgsfield liberado: até {} geração(ões) (ferramenta cw-studios › gerar).".format(higgs) if higgs else "Higgsfield: não use.",
                                        pasta=pasta, biblioteca=BIBLIOTECA_CW,
                                        receita=rec or "Reel vertical 1080x1920 no padrão Reels-CW-01 da Cardápio Web."),
                   pasta, "sonnet", ao_passo=passo, limite=3600, extra_dirs=[base / "bruto", BIBLIOTECA_CW] + ([skill_est] if skill_est.is_dir() else []), higgs=higgs)
    r = json_final(texto)
    final = Path(r.get("final") or "")
    if final and not final.is_absolute():
        final = pasta / final
    if not r.get("aprovado") or not final.is_file():
        srv("corte", id=c["id"], estado="reprovado", erro=(r.get("motivo") or "sem vídeo final")[:500])
        return
    previa = Path(r.get("previa") or final)
    if not previa.is_absolute():
        previa = pasta / previa
    if c.get("ajuste"):
        registrar_conferencia(c, conferir_ajuste(c, final, pasta, prints, passo), cid)
    entregar_previa(c, final, previa if previa.is_file() else final, cid, r.get("legenda_post"), pasta)


def entregar_previa(c, final, previa, cid, legenda, pasta):
    """Prévia leve no bucket (player da página) + final no Drive "2. PARA APROVAR/Prévias da IA"; o corte vai para 'previa'."""
    srv("corte", id=c["id"], etapa="Prévia")
    caminho = subir_previa(previa, c)
    PREVIAS_DRIVE.mkdir(parents=True, exist_ok=True)
    nome = "{}_{}{}".format(cid, re.sub(r"[^A-Za-z0-9]+", "-", c["titulo"])[:60].strip("-"), final.suffix)
    shutil.copyfile(final, PREVIAS_DRIVE / nome)
    if not legenda:   # legenda do post: modelo barato (OpenRouter), a partir da transcrição do corte
        try:
            import ferramentas_mcp
            t = next(Path(final).parent.parent.glob("work/{}/transcript.txt".format(cid)), None)
            r = ferramentas_mcp.chamar({"acao": "texto", "ferramenta": "openrouter", "modelo": "anthropic/claude-haiku-5.5", "etapa": "textos_post",
                                        "instrucao": "Escreva a legenda de um Reels do CW Cast (podcast da Cardápio Web para donos de restaurante e delivery) "
                                                     "para este corte: 2 a 3 linhas, gancho na primeira, chamada para ouvir o episódio, no máximo 3 hashtags. Só a legenda.",
                                        "texto": "Título: {}\n\n{}".format(c["titulo"], t.read_text(encoding="utf-8")[:6000] if t else ""), "trabalho": "esteira"}, timeout=120)
            legenda = (r.get("texto") or "").strip()
        except Exception:
            legenda = ""
    (pasta / "legenda_post.txt").write_text(legenda or c["titulo"], encoding="utf-8")
    srv("corte", id=c["id"], estado="previa", etapa="esperando aprovação", arquivo=str(final), previa=caminho,
        final_drive=str((PREVIAS_DRIVE / nome).relative_to(DRIVE)))
    log("corte", c["id"], "em prévia")


def entregar(item):
    """Corte ou reel aprovado na prévia → Publicação (Drive "2. PARA APROVAR" + card)."""
    c, ep = item["corte"], item["episodio"]
    reel = ep.get("linha") == "reels"   # FabI.Ano: o reel também vai para a Publicação, na pasta da sprint da Cardápio Web
    try:
        final = Path(c.get("arquivo") or "")
        legenda = Path(BASE / str(ep["id"])).glob("lote-{}/corte-{:02d}/legenda_post.txt".format(c["lote_id"], c["ordem"]))
        leg = next(legenda, None)
        saida = subprocess.run([sys.executable, str(AQUI / "publicar.py"), "enviar", "--arquivo", str(final), "--titulo", c["titulo"][:120],
                                "--legenda", leg.read_text(encoding="utf-8") if leg else c["titulo"], "--episodio", ep["nome"][:120]]
                               + (["--destino", "Instagram Cardapio Web"] if reel else []),
                               capture_output=True, text=True, timeout=900)
        m = re.search(r"P-(\d+)", saida.stdout)
        if not m:
            raise RuntimeError("não consegui entregar: " + (saida.stderr or saida.stdout)[-300:])
        srv("corte", id=c["id"], estado="entregue", etapa="na Publicação", publicacao_id=int(m.group(1)))
        log("corte", c["id"], "entregue P-" + m.group(1))
    except Exception as e:
        log("entrega do corte", c["id"], "falhou:", e)
        srv("corte", id=c["id"], estado="erro", erro=str(e)[:500])


def operario(n, so_ajuste=False):
    """Pega o próximo corte (ajustes primeiro). O operário reservado só pega ajuste, para ele voltar na hora."""
    while True:
        try:
            item = srv("pegar_corte", so_ajuste=so_ajuste)
        except central.ErroCentral as e:
            log("operário", n, "central:", e)
            time.sleep(20)
            continue
        if item:
            produzir(item)
        else:
            time.sleep(10)


def entregador():
    """Manda para a Publicação cada corte aprovado, na hora (não espera operário livre)."""
    while True:
        try:
            aprovado = srv("pegar_aprovado")
        except central.ErroCentral:
            aprovado = None
        if aprovado:
            entregar(aprovado)
        else:
            time.sleep(15)


# ------------------------------------------------------------------------------------------- ⑤ estilos: versões a partir de referências
ESTILISTA = """Você é o Titeu cuidando dos ESTILOS de edição do CW Studios ({linha_txt}), sem ninguém olhando: não pergunte nada, decida.
Pedido de {quem}: {pedido}
Estilo: "{nome}" (versão atual {versao}){base}.
Referências enviadas: {refs}
Na pasta {pasta}:
- RECEITA.md: a receita atual do estilo (como produzir UM vídeo nele: seções ## EPISÓDIO, ## CORTE e ## APRENDIZADOS);
- skill/: cópia EDITÁVEL dos arquivos de texto do pacote do estilo (regras editoriais, critérios, presets de legenda, configurações).
{skill_txt}
Faça a versão nova do estilo:
1. Abra cada referência (são imagens: fotos, prints ou folhas de quadros dos vídeos, com o tempo no nome) e anote o que caracteriza
   a edição: legenda (fonte, tamanho, cor, posição, palavras por vez, destaque), ritmo dos cortes, zoom/punch, gancho, B-roll,
   transições, cores, trilha e sons, o que aparece na tela e quando.
2. Mude RECEITA.md e os arquivos de skill/ para que o estilo produza isso, com valores concretos (tamanhos, cores em hex, tempos,
   posições). Mude só o necessário para atender o pedido. Mantenha as seções e o jeito de produzir (comandos e scripts): não crie
   script nem arquivo de código e use só recursos que o pacote já tem (fontes, sons, presets). Se a referência pede algo que não
   existe aqui (ex.: uma fonte que não está no pacote), use o mais próximo e diga nas notas.
3. As regras de processo do time não mudam (aprovação, entrega, onde salvar).
4. Escreva NOTAS.md para o time, em até 10 linhas: o que mudou e o que não deu para fazer igual à referência.
Não renderize nada: o corte de teste é automático.
Responda APENAS com JSON: {{"ok": true, "notas": "o que mudou, em 2 linhas"}}"""


def _baixar_referencias(v, destino):
    """Referências do pedido → arquivos em destino/ (enviadas pela página e links de vídeo públicos)."""
    import urllib.request
    import kit
    destino.mkdir(parents=True, exist_ok=True)
    saida = []
    if v.get("referencias"):
        try:
            links = kit._funcao({"acao": "referencia", "caminhos": v["referencias"]}).get("links") or {}
        except Exception as e:
            log("referências:", e)
            links = {}
        for i, (cam, url) in enumerate(links.items()):
            arq = destino / "ref{}{}".format(i + 1, Path(cam).suffix.lower() or ".bin")
            try:
                with urllib.request.urlopen(url, timeout=300) as r, open(arq, "wb") as f:
                    shutil.copyfileobj(r, f)
                saida.append(arq)
            except OSError as e:
                log("referência", cam, e)
    for i, link in enumerate(v.get("links") or []):
        if not re.match(r"^https?://", link or ""):
            continue
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-playlist", "--max-filesize", "200M", "-f", "mp4[height<=1080]/best[height<=1080]/best",
                            "-o", str(destino / "link{}.%(ext)s".format(i + 1)), link], capture_output=True, text=True, timeout=600)
        achados = sorted(destino.glob("link{}.*".format(i + 1)))
        if achados:
            saida.append(achados[0])
        else:
            log("link de referência", link, (r.stderr or r.stdout)[-200:])
    return saida


def _folhas(video, pasta):
    """Vídeo de referência → 2 folhas de quadros: o começo (gancho, 2 por segundo) e o vídeo inteiro (20 quadros)."""
    dur = ffprobe_dur(video) or 30
    env = dict(os.environ, PATH="/usr/local/bin:" + os.environ.get("PATH", ""))
    folhas = []
    for nome, filtro in (("inicio", "trim=0:5,fps=2,scale=360:-2,tile=5x2"),
                         ("inteiro", "fps={:.4f},scale=300:-2,tile=5x4".format(20.0 / max(dur, 1)))):
        arq = pasta / "{}_{}_{:.0f}s.jpg".format(video.stem, nome, dur)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video), "-vf", filtro, "-frames:v", "1", str(arq)], env=env)
        if arq.exists():
            folhas.append(arq)
    return folhas


def _textos(raiz):
    out = {}
    for f in Path(raiz).rglob("*"):
        rel = f.relative_to(raiz).as_posix()
        if f.is_file() and EDITAVEIS.search(f.name) and f.stat().st_size <= 150_000 and not re.search(r"(^|/)(node_modules|\.git|work|saida|\.originais)/", rel):
            out[rel] = f.read_text(encoding="utf-8", errors="replace")
    return out


def criar_versao(t):
    v, e = t["versao"], t["estilo"]
    pasta = BASE / "estilos" / "v{}".format(v["id"])
    shutil.rmtree(pasta, ignore_errors=True)
    pasta.mkdir(parents=True)
    passo = lambda x: srv("estilo_versao", id=v["id"], etapa=x)
    try:
        sincronizar_estilos()
        passo("baixando as referências")
        refs = _baixar_referencias(v, pasta / "refs")
        imagens = []
        for r in refs:
            if VIDEO.search(r.name) or r.suffix.lower() in (".webm",):
                imagens += _folhas(r, pasta / "refs")
            elif r.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                imagens.append(r)
        # cópia editável dos textos da skill do estilo (ou do estilo de partida), já com os arquivos da versão atual
        skills = HOME / ".claude" / "skills"
        origem = next((d for d in (skills / ("cw-estilo-" + e["slug"]), skills / ("cw-estilo-" + ((t.get("base") or {}).get("slug") or "")))
                       if e.get("tem_pacote") and d.is_dir()), None)
        intocado = _textos(origem) if origem else {}
        (pasta / "skill").mkdir()
        for cam, txt in dict(intocado, **(e.get("overrides") or {})).items():
            (pasta / "skill" / cam).parent.mkdir(parents=True, exist_ok=True)
            (pasta / "skill" / cam).write_text(txt, encoding="utf-8")
        (pasta / "RECEITA.md").write_text(e.get("receita") or "", encoding="utf-8")
        passo("estudando as referências e escrevendo a versão {}".format(v["versao"]))
        texto = claude(ESTILISTA.format(
            linha_txt="reels da Cardápio Web" if e.get("linha") == "reels" else "cortes do CW Cast",
            quem=(v.get("pedido_por") or "o time").split("@")[0], pedido=v.get("pedido") or "(sem texto: siga as referências)",
            nome=e["nome"], versao=e.get("versao_atual") or 0,
            base=(" — estilo novo, criado a partir de \"{}\"".format(t["base"]["nome"]) if t.get("base") and not e.get("versao_atual") else ""),
            refs=", ".join(str(x) for x in imagens) or "nenhuma (só o pedido em texto)", pasta=pasta,
            skill_txt=("A skill completa do estilo (com scripts e arquivos, só para consulta) está em {}.".format(origem) if origem
                       else "Este estilo não tem pacote próprio: a mudança é só na receita.")),
            pasta, "sonnet", ao_passo=passo, limite=1500, extra_dirs=[origem] if origem else [])
        try:
            r = json_final(texto)
        except Exception:
            r = {}   # o que vale é o que ficou escrito em RECEITA.md e skill/
        nova = _textos(pasta / "skill")
        overrides = {k: txt for k, txt in nova.items() if intocado.get(k) != txt}
        if len(json.dumps(overrides)) > 900_000:
            raise RuntimeError("a versão mudou arquivos demais (mais de 900 KB de texto)")
        notas = (pasta / "NOTAS.md").read_text(encoding="utf-8") if (pasta / "NOTAS.md").exists() else (r.get("notas") or "")
        srv("estilo_versao", id=v["id"], receita=(pasta / "RECEITA.md").read_text(encoding="utf-8"), overrides=overrides,
            notas=notas.strip()[:3000], etapa="preparando o corte de teste")
        srv("estilo_teste", id=v["id"])
        log("estilo", e["slug"], "versão", v["versao"], "escrita;", len(overrides), "arquivo(s) trocado(s); corte de teste na fila")
    except Exception as ex:
        log("versão de estilo", v["id"], "falhou:", traceback.format_exc()[-600:])
        srv("estilo_versao", id=v["id"], estado="erro", erro=str(ex)[:900])


def estilista():
    """Pedidos de estilo novo/melhoria, um por vez (não atrasa os cortes)."""
    while True:
        try:
            t = srv("estilo_pegar")
            if t:
                criar_versao(t)
                continue
        except Exception as e:
            log("estilista:", e)
        time.sleep(30)


def cmd_rodar():
    BASE.mkdir(parents=True, exist_ok=True)
    escrever_cerca()
    try:
        srv("orfaos")   # o que estava em produção quando o Titeu desligou volta para a fila
    except Exception as e:
        log("órfãos:", e)
    for n in range(OPERARIOS):
        threading.Thread(target=operario, args=(n + 1,), daemon=True).start()
    threading.Thread(target=operario, args=(OPERARIOS + 1, True), daemon=True).start()   # vaga reservada para ajustes
    threading.Thread(target=entregador, daemon=True).start()
    threading.Thread(target=estilista, daemon=True).start()
    try:   # Oficina: skills do sistema (criar/melhorar com teste) e agentes que trabalham sozinhos
        import oficina
        oficina.ligar(sys.modules[__name__])
    except Exception as e:
        log("Oficina não ligou:", e)
    log("esteira ligada:", OPERARIOS, "operários; GPU:", tem_gpu())
    threading.Thread(target=sincronizar_estilos, daemon=True).start()
    ultimo_vigia = 0
    while True:
        try:
            if time.time() - ultimo_vigia > 120:
                ultimo_vigia = time.time()
                vigiar()
                vigiar_reels()
            t = srv("trabalho")
            if t and t.get("tipo") == "preparar":
                (preparar_reel if (t["episodio"].get("linha") == "reels") else preparar)(t["episodio"])
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
