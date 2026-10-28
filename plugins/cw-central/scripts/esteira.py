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
    if not (W / "tools").exists():   # pasta preparada em outro servidor (ou perdida): monta de novo a partir da skill do estilo
        skills = HOME / ".claude" / "skills"
        montar = next((p for p in (skills / ("cw-estilo-" + ((estilo or {}).get("slug") or "")) / "scripts" / "montar_pasta.py",
                                   skills / "cw-estilo-cw-cast-timeu" / "scripts" / "montar_pasta.py") if p.exists()), None)
        if montar:
            W.mkdir(parents=True, exist_ok=True)
            r = subprocess.run([sys.executable, "-I", str(montar), str(W), "--sem-hooks"], capture_output=True, text=True, timeout=300)
            log("pasta do estilo montada de novo:", W, (r.stderr or r.stdout)[-200:])
    if W.exists():
        aplicar_overrides(W, (estilo or {}).get("overrides") or {})
    return W


EDITAVEIS = re.compile(r"\.(md|json|txt|ya?ml|ass|css|py|sh)$", re.I)   # textos e scripts do pacote (a versão só vale depois do teste aprovado)


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
        # conta por lote (CW_EST_HIGGS_ARQ); a pasta do episódio é reaproveitada entre lotes e não serve de contador
        orc, cont = int(os.environ.get("CW_EST_HIGGS") or 0), Path(os.environ.get("CW_EST_HIGGS_ARQ") or Path(pasta or "/tmp") / ".higgs_usados")
        cont.parent.mkdir(parents=True, exist_ok=True)
        usados = int(cont.read_text()) if cont.exists() else 0
        try:
            sem_credito = time.time() - float((cont.parent / SEM_CREDITO.name).read_text()) < 1800   # no gancho, BASE não vale (CW_ESTEIRA=1)
        except (OSError, ValueError):
            sem_credito = False
        if sem_credito:
            motivo = "a conta do Higgsfield está sem créditos; siga sem gerar e diga nas notas que faltou crédito"
        elif usados < orc:
            cont.write_text(str(usados + 1))
            decisao, duro = "allow", False
        else:   # a mensagem diz a verdade: quem acabou foi o orçamento do lote, não a conta
            motivo = "o orçamento de Higgsfield deste lote acabou ({}/{} gerações); siga sem gerar e diga isso nas notas".format(usados, orc)
    if decisao != "allow":
        # sem ninguém para aprovar: ler e rodar programas do fluxo (venv, vch, node) passa; gravar/apagar/mover fora da pasta
        # e tudo que é "duro" (mandar para fora, instalar, administrador, senha) é recusado
        mexe = re.match(r"^(gravar|mexer|apagar|mover|rm |rm$)", str(motivo))
        decisao = "deny" if duro or mexe else "allow"
    razao = "na esteira não pode: {}".format(motivo) if decisao == "deny" else "liberado na esteira"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decisao,
                                             "permissionDecisionReason": razao}}, ensure_ascii=False))


SEM_CREDITO = BASE / ".higgs" / "sem_credito"   # a conta do Higgsfield ficou sem crédito: pausa as gerações por 30 min


def matar_grupo(proc):
    """Mata o processo e todo o grupo dele (start_new_session): sem render órfão gastando CPU."""
    import signal
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def devolver_higgs(arq):
    """Desfaz a contagem de uma geração que a API recusou (o porteiro conta antes de saber se deu certo)."""
    try:
        p = Path(arq)
        p.write_text(str(max(0, int(p.read_text()) - 1)))
        log("geração do Higgsfield recusada pela API: não contou no orçamento do lote")
    except (OSError, ValueError):
        pass


def claude(prompt, pasta, modelo, ao_passo=None, limite=1800, extra_dirs=(), higgs=0, papel="video", trabalho="", higgs_lote=None):
    """Roda um Claude Code da esteira em 'pasta'. Devolve o texto final.
    modelo: "sonnet"/"opus"/"haiku" (ou "claude:sonnet"…) = conta Claude do CW Studios; "or:<id>" = modelo do OpenRouter pela
    ponte "llm" da central (a chave do OpenRouter fica só na central; o uso fica registrado por trabalho)."""
    modelo = (modelo or "sonnet").replace("claude:", "")
    openrouter = modelo.startswith("or:")
    if not openrouter and not TOKEN_CLAUDE.exists():
        raise RuntimeError("a conta Claude do CW Studios ainda não foi ligada na esteira (aba Esteira › ☁️)")
    cmd = [str(HOME / ".local/bin/claude"), "-p", prompt, "--output-format", "stream-json", "--verbose", "--model", modelo[3:] if openrouter else modelo,
           "--permission-mode", "default", "--settings", str(CERCA), "--add-dir", str(pasta)]
    for d in extra_dirs:
        cmd += ["--add-dir", str(d)]
    env = dict(os.environ, CW_EST_PASTA=str(pasta),
               CW_ESC_PASTA=str(pasta), CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1", CW_SERVIDOR="1",
               CW_ESTEIRA="1", CW_EST_HIGGS=str(higgs), CW_PAPEL=papel or "video")   # CW_ESTEIRA: abertura enxuta (só regras e lições do time)
    if higgs_lote:
        env["CW_EST_HIGGS_ARQ"] = str(BASE / ".higgs" / "lote-{}".format(higgs_lote))
    if openrouter:
        cfg, mid = central.config(), modelo[3:]
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env.update(ANTHROPIC_BASE_URL=cfg["url"] + "/functions/v1/llm", ANTHROPIC_AUTH_TOKEN=cfg["token"], ANTHROPIC_API_KEY="",
                   ANTHROPIC_DEFAULT_OPUS_MODEL=mid, ANTHROPIC_DEFAULT_SONNET_MODEL=mid, ANTHROPIC_DEFAULT_HAIKU_MODEL=mid,
                   ANTHROPIC_SMALL_FAST_MODEL=mid, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   ANTHROPIC_CUSTOM_HEADERS="x-cw-trabalho: " + re.sub(r"[^\w .:/-]", "", trabalho or "esteira")[:80])
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = TOKEN_CLAUDE.read_text().strip()
    proc = subprocess.Popen(cmd, cwd=str(pasta), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
                            encoding="utf-8", errors="replace", start_new_session=True)
    estourou = threading.Event()

    def relogio():   # o limite vale mesmo se o Claude ficar parado sem escrever nada (antes só era conferido a cada linha)
        if proc.poll() is None:
            estourou.set()
            matar_grupo(proc)
    timer = threading.Timer(limite, relogio)
    timer.daemon = True
    timer.start()
    fim, resultado, gerar_ids = time.time() + limite, "", set()
    try:
        for linha in proc.stdout:
            if time.time() > fim:
                matar_grupo(proc)
                raise RuntimeError("passou do tempo ({} min)".format(limite // 60))
            try:
                ev = json.loads(linha)
            except ValueError:
                continue
            if ev.get("type") == "user" and higgs_lote and gerar_ids:   # geração recusada pela API não gasta o orçamento do lote
                for c in (ev.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("tool_use_id") in gerar_ids:
                        gerar_ids.discard(c.get("tool_use_id"))
                        txt = json.dumps(c.get("content"), ensure_ascii=False)
                        if c.get("is_error") or re.search(r"not_enough_credits|erro_envio|\"erro\"", txt):
                            devolver_higgs(env["CW_EST_HIGGS_ARQ"])
                        if "not_enough_credits" in txt:
                            SEM_CREDITO.parent.mkdir(parents=True, exist_ok=True)
                            SEM_CREDITO.write_text(str(time.time()))
                            log("Higgsfield SEM CRÉDITOS: gerações pausadas por 30 min")
            elif ev.get("type") == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") != "tool_use":
                        continue
                    if "cw-studios__gerar" in (c.get("name") or ""):
                        gerar_ids.add(c.get("id"))
                    if ao_passo:
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
        if estourou.is_set():
            raise RuntimeError("passou do tempo ({} min)".format(limite // 60))
    finally:
        timer.cancel()
        matar_grupo(proc)   # leva junto o que o Claude deixou rodando (ffmpeg, render)
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
        if formato_canal2(estilo):   # segundo canal: momentos que o CW Cast ainda não usou ("para não parecer que é nosso")
            usados = srv("trechos_usados", episodio=ep["id"], lote=lote["id"]) or []
            if usados:
                prompt += ("\n\nESTES TRECHOS JÁ FORAM USADOS em cortes do canal oficial: escolha momentos DIFERENTES, sem sobrepor nenhum deles "
                           "(segundos do episódio): " + json.dumps(usados))
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
- highlights (amarelos): leia só REGRAS_EDITORIAIS §3 e CRITERIOS C-10 em {ref}. {enfase}
- gancho: confira hook.duration e headline (no máximo 2 linhas de ~32 letras, quebra com |);
- foto do gancho (hook.media): {foto}
- {broll}
- P&B, punch, sfx e trilha conforme §6–§10 se fizer sentido.
{instrucoes}{ajuste}Valem as regras e lições do time. NÃO rode render, gates nem ferramentas de tempo (isso é automático). Seja direto: poucas leituras.
Responda APENAS com JSON: {{"ok": true, "notas": "uma linha do que decidiu"}}"""

CORRIGE_TIMEU = """O corte {cid} (CW Cast, estilo do Timeu) foi renderizado e os gates técnicos reprovaram. Pasta {W}:
work/{cid}/plan.json, work/{cid}/gates.json (o que falhou), transcript.json e a prévia saida/{cid}_rascunho.mp4.
Corrija SÓ a causa de cada FAIL (editando plan.json ou com o script indicado em RECEITAS_OPERACIONAIS §3 em {ref}),
sem refazer o corte. NÃO renderize nem rode os gates (é automático). Valem as regras do time.
Responda APENAS com JSON: {{"ok": true, "corrigiu": "uma linha"}}"""


class Falhou(Exception):
    pass


# ------------------------------------------------------------------------------------------- ajuste cirúrgico
# Felipe, 28/10/2026: "sempre altera mais do que é pedido". O ajuste é dividido em itens, cada item ganha as ÁREAS que toca
# (e o trecho, se o editor disse o momento); o resto do plano fica TRAVADO: o código desfaz o que o Claude mexer fora disso.
AREAS = {   # área -> chaves do plan.json que ela pode mudar
    "broll": {"broll"},
    "gancho": {"headline", "hook"},
    "legenda": {"highlights", "captions", "caption_style", "legenda"},
    "enfase": {"enfase", "highlights", "pb", "punch"},
    "audio": {"fixes", "music", "audio", "sfx", "loudness"},
    "trilha": {"music"},
    "enquadramento": {"shots", "_base_shots", "punch", "zoom"},
    "fonte": {"font", "fonte", "caption_style"},
    "motion": set(),
}
LIVRE = {"corte", "outro"}   # mexe no tempo do corte ou não deu para entender: sem trava (como antes)
SEMPRE = {"fixes"}            # correções técnicas dos gates
ROTULOS = {"b-roll": "broll", "broll": "broll", "imagens": "motion", "motion": "motion", "legenda": "legenda", "headline": "gancho",
           "gancho": "gancho", "capa": "gancho", "áudio": "audio", "audio": "audio", "trilha": "trilha", "enquadramento": "enquadramento",
           "câmera": "enquadramento", "corte": "corte", "início/fim": "corte", "fonte": "fonte", "ênfase": "enfase", "outro": "outro"}
PALAVRAS_AREA = [
    ("broll", r"b-?\s?rolls?|v[íi]deo de apoio|inserts?"),
    ("motion", r"motion|anima[çc]|frases? na tela|texto na tela|tela cheia"),
    ("broll", r"\bfotos?\b|imagens?"), ("motion", r"\bfotos?\b|imagens?"),
    ("gancho", r"gancho|headline|t[íi]tulo|\bcapa\b"),
    ("legenda", r"legenda|palavra|escrit|ortograf|amarel|destaque"),
    ("enfase", r"[êe]nfase|preto e branco|p&b|\bpb\b"),
    ("audio", r"[áa]udio|\bsom\b|volume|estour|chiad|ru[íi]do|\bvoz\b|efeito sonoro|sfx"),
    ("trilha", r"trilha|m[úu]sica"),
    ("enquadramento", r"enquadr|c[âa]mera|\bzoom|centraliz|rosto|cabe[çc]a|cortad[oa] na tela|aparece"),
    ("fonte", r"\bfonte|montserrat|tipografia|\bletra|@|arroba|\bperfil\b"),
    ("corte", r"come[çc]a|termina|\bfinal do|tir[ae]r? (a|essa|esta) fala|encurt|mais curto|dura[çc][ãa]o|corte seco|emenda"),
]


def _seg(m, s):
    return int(m or 0) * 60 + int(s)


def itens_ajuste(texto):
    """[{pedido, areas, t0, t1}] a partir do texto do ajuste. Aceita a etiqueta da página "[área · 0:12] pedido"
    e, sem etiqueta, acha as áreas pelas palavras (sem nenhuma: "outro", sem trava)."""
    itens = []
    for linha in re.split(r"\n+|;\s*|(?:^|\s)\d+\)\s", texto or ""):
        linha = linha.strip(" -•\t")
        if not linha:
            continue
        areas, t0, t1 = set(), None, None
        m = re.match(r"\[([^\]]+)\]\s*(.*)", linha)
        corpo = linha
        if m:
            corpo = m.group(2) or linha
            for parte in re.split(r"[·,/]", m.group(1)):
                a = ROTULOS.get(parte.strip().lower())
                if a:
                    areas.add(a)
        tempos = ([_seg(a, b) for a, b in re.findall(r"\b(\d{1,2}):(\d{2})\b", linha)]
                  + [int(x) for x in re.findall(r"\b(\d{1,3})\s?(?:s|seg|segundos?)\b", linha, re.I)])
        if tempos:
            t0, t1 = min(tempos), max(tempos)
        if not areas:
            areas = {a for a, rx in PALAVRAS_AREA if re.search(rx, corpo, re.I)} or {"outro"}
        itens.append({"pedido": corpo, "areas": areas, "t0": t0, "t1": t1})
    return itens or [{"pedido": texto or "", "areas": {"outro"}, "t0": None, "t1": None}]


def areas_do_ajuste(itens):
    return set().union(*[i["areas"] for i in itens]) if itens else {"outro"}


def chaves_livres(areas):
    if areas & LIVRE:
        return None   # sem trava
    return set(SEMPRE).union(*[AREAS.get(a, set()) for a in areas])


class Trava:
    """Fotografa plan.json / transcript.json / cenas_fluido.json antes do ajuste e desfaz o que mudar fora das áreas pedidas."""
    def __init__(self, work, itens):
        self.work, self.itens, self.areas = work, itens, areas_do_ajuste(itens)
        self.livres = chaves_livres(self.areas)
        self.antes = {n: self._ler(n) for n in ("plan.json", "transcript.json", "cenas_fluido.json")}

    def _ler(self, nome):
        try:
            return json.loads((self.work / nome).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _janelas_broll(self):
        js = [(i["t0"], i["t1"]) for i in self.itens if "broll" in i["areas"] and i["t0"] is not None]
        sem_tempo = any("broll" in i["areas"] and i["t0"] is None for i in self.itens)
        return None if sem_tempo or not js else js

    def aplicar(self):
        """Desfaz o que saiu da área. Devolve a lista do que foi desfeito (para o log)."""
        desfeito = []
        if self.livres is None:
            return desfeito
        antes, depois = self.antes["plan.json"], self._ler("plan.json")
        if isinstance(antes, dict) and isinstance(depois, dict):
            for k in set(antes) | set(depois):
                if k in self.livres or antes.get(k) == depois.get(k):
                    continue
                if k in antes:
                    depois[k] = antes[k]
                else:
                    depois.pop(k, None)
                desfeito.append(k)
            js = self._janelas_broll()
            if "broll" in self.livres and js and depois.get("broll") != antes.get("broll"):   # b-roll pedido num momento: só ali muda
                dentro = lambda b: any(a - 1.5 <= float(b.get("start") or 0) <= z + 1.5 for a, z in js)
                novo = [b for b in depois.get("broll") or [] if dentro(b)] + [b for b in antes.get("broll") or [] if not dentro(b)]
                novo.sort(key=lambda b: float(b.get("start") or 0))
                if novo != depois.get("broll"):
                    depois["broll"] = novo
                    desfeito.append("broll fora do momento pedido")
            (self.work / "plan.json").write_text(json.dumps(depois, ensure_ascii=False, indent=1), encoding="utf-8")
        ta, td = self.antes["transcript.json"], self._ler("transcript.json")
        if isinstance(ta, dict) and isinstance(td, dict) and ta != td:
            if "legenda" not in self.areas:
                (self.work / "transcript.json").write_text(json.dumps(ta, ensure_ascii=False), encoding="utf-8")
                desfeito.append("transcript")
            elif ta.get("ranges") != td.get("ranges"):
                td["ranges"] = ta.get("ranges")
                (self.work / "transcript.json").write_text(json.dumps(td, ensure_ascii=False), encoding="utf-8")
                desfeito.append("trechos do corte")
        return desfeito

    def reaproveitar_motion(self):
        """Motion (fotos/frases) fica como estava, a não ser que o pedido seja sobre ele ou mude o tempo do corte."""
        return self.livres is not None and "motion" not in self.areas and bool((self.antes["cenas_fluido.json"] or {}).get("cenas"))

    def resumo(self):
        """O que mudou de verdade (comparando com antes), em português curto, para o time conferir."""
        partes = []
        antes, depois = self.antes["plan.json"] or {}, self._ler("plan.json") or {}
        if antes.get("headline") != depois.get("headline"):
            partes.append("headline: \"{}\" → \"{}\"".format((antes.get("headline") or "").replace("|", " "), (depois.get("headline") or "").replace("|", " ")))
        if (antes.get("hook") or {}).get("media") != (depois.get("hook") or {}).get("media"):
            partes.append("foto do gancho trocada")
        ba = {(b.get("file"), b.get("start")) for b in antes.get("broll") or []}
        bd = {(b.get("file"), b.get("start")) for b in depois.get("broll") or []}
        if ba != bd:
            novos = sorted(float(s or 0) for f, s in bd - ba)
            partes.append("b-roll: {} novo(s){}{}".format(len(novos), " em " + ", ".join(_mm(t) for t in novos[:6]) if novos else "",
                                                         ", {} saiu(ram)".format(len(ba - bd) - len(novos)) if len(ba - bd) > len(novos) else ""))
        for k, nome in (("highlights", "destaques da legenda"), ("enfase", "ênfase"), ("music", "trilha"), ("shots", "enquadramento")):
            if antes.get(k) != depois.get(k):
                partes.append(nome + " alterado")
        ta, td = self.antes["transcript.json"] or {}, self._ler("transcript.json") or {}
        if ta.get("ranges") != td.get("ranges"):
            partes.append("trechos do corte (início/fim/falas) alterados")
        elif ta != td:
            partes.append("texto da legenda corrigido")
        ca, cd = (self.antes["cenas_fluido.json"] or {}).get("cenas"), (self._ler("cenas_fluido.json") or {}).get("cenas")
        if ca is not None or cd is not None:
            partes.append("imagens/frases do motion mantidas" if ca == cd else "imagens/frases do motion refeitas")
        return "; ".join(partes) or "nada mudou no plano (mudança só no render ou no estilo)"


def _mm(t):
    return "{}:{:02d}".format(int(t) // 60, int(round(t)) % 60)


def texto_itens(itens):
    rot = {"broll": "b-roll", "gancho": "gancho/headline", "legenda": "legenda", "enfase": "ênfase", "audio": "áudio", "trilha": "trilha",
           "enquadramento": "enquadramento", "fonte": "fonte", "motion": "imagens/frases do motion", "corte": "início/fim/falas", "outro": "geral"}
    return "\n".join("{}. [{}{}] {}".format(n + 1, ", ".join(rot.get(a, a) for a in sorted(i["areas"])),
                                           (" · " + _mm(i["t0"]) + ("–" + _mm(i["t1"]) if i["t1"] != i["t0"] else "")) if i["t0"] is not None else "", i["pedido"])
                     for n, i in enumerate(itens))


AJUSTE_TIMEU = """Você é o editor do corte {cid} (CW Cast), sem ninguém olhando. O corte JÁ ESTÁ PRONTO e aprovado pelo time em tudo,
menos nos itens abaixo. Faça SÓ o que cada item pede, no trecho dito, e nada mais:
{itens}
Pasta {W}: work/{cid}/plan.json, work/{cid}/transcript.json/.txt; referências do estilo em {ref}.
TRAVADO: tudo o que não é das áreas acima (headline, gancho, destaques, ênfase, b-roll, trilha, enquadramento, trechos do corte) fica
exatamente como está; o código desfaz qualquer mudança fora das áreas pedidas, então não perca tempo com elas.
{extra}{prints}
NÃO renderize nem rode gates (é automático). Responda APENAS com JSON: {{"ok": true, "notas": "uma linha do que mudou em cada item"}}"""


def guardar_versao(final, cid):
    """Cópia do vídeo final e do plano desta versão (o time pode voltar para ela). Ficam só as 3 últimas do corte."""
    try:
        W = final.parent.parent
        pasta_v = final.parent / "versoes"
        pasta_v.mkdir(exist_ok=True)
        nome = "{}_{}".format(cid, time.strftime("%Y%m%d-%H%M%S"))
        copia = pasta_v / (nome + final.suffix)
        shutil.copyfile(final, copia)
        foto = pasta_v / nome
        foto.mkdir(exist_ok=True)
        for n in ("plan.json", "transcript.json", "cenas_fluido.json", "captions.ass", "captions_final.ass"):
            if (W / "work" / cid / n).exists():
                shutil.copyfile(W / "work" / cid / n, foto / n)
        velhas = sorted(pasta_v.glob(cid + "_*" + final.suffix))[:-3]
        for v in velhas:
            v.unlink(missing_ok=True)
            shutil.rmtree(pasta_v / v.stem, ignore_errors=True)
        return copia
    except Exception as e:
        log("versão do", cid, "não foi guardada:", e)
        return None


def partir_da_versao(c, work):
    """Ajuste começa do vídeo que o time está vendo (inclusive depois de "voltar à versão anterior")."""
    arq = Path(c.get("arquivo") or "")
    foto = arq.parent / arq.stem
    if arq.parent.name == "versoes" and foto.is_dir():
        for n in foto.iterdir():
            shutil.copyfile(n, work / n.name)
        return True
    return False


# ------------------------------------------------------------------------------------------- b-roll: vídeo e sem repetir
REGRAS_BROLL = ("REGRAS DO TIME PARA B-ROLL (obrigatórias): (1) b-roll é sempre VÍDEO com movimento de verdade (pessoas, mãos, vapor, "
    "câmera andando); nunca foto parada, nunca foto com zoom, nunca imagem convertida em mp4. (2) Nenhum b-roll se repete: cada insert "
    "é um arquivo e uma cena diferentes, e é proibido usar arquivo que já entrou em outro corte. (3) A cena mostra exatamente o que está "
    "sendo dito naquele trecho, num restaurante/delivery brasileiro real. (4) Sem tela de celular ou notebook com texto, número ou gráfico "
    "(a IA escreve letras sem sentido): se precisar de celular, mostre a pessoa e a reação, com a tela de costas; sem logo, marca ou pessoa real. "
    "Gere no Higgsfield (cw-studios › gerar; SEM limite de gerações): uma imagem realista 9:16 sem texto em /higgsfield-ai/soul/v2/standard "
    "e anime com /kling-video/v2.6/pro/image-to-video (\"duration\": 5, número inteiro: só aceita 5 ou 10) com um prompt de AÇÃO (o que se "
    "move na cena); ou direto em /kling-video/v2.5-turbo/pro/text-to-video. Acompanhe com status e baixe o mp4 em broll/ com um nome que diga "
    "a cena. Se uma geração falhar, tente de novo (até 3 vezes). Só use arquivo pronto de broll/ ou de {bib} se for vídeo com movimento, "
    "combinar com a fala e não estiver na lista de proibidos. Proibidos (já usados em outros cortes): {proibidos}.")

TROCA_BROLL = """No corte {cid} (pasta {W}), estes b-rolls de work/{cid}/plan.json não podem ficar:
{lista}
Troque cada um por um VÍDEO NOVO, diferente de todos os outros, ligado ao que é falado naquele trecho (veja work/{cid}/transcript.txt),
mantendo start e dur. Se a lista citar a FOTO DO GANCHO, gere outra foto (Higgsfield, realista, sem texto, combinando com a headline),
salve com nome novo em work/{cid}/ e troque hook.media. {regras}
Mexa só nesses itens de "broll". NÃO renderize nem rode gates. Responda APENAS com JSON: {{"ok": true, "trocou": "uma linha"}}"""

BROLL_REGISTRO = BASE / "broll_usados.json"   # sha256 do arquivo -> corte que usou (vale para todos os episódios)
IMAGEM = re.compile(r"\.(jpe?g|png|webp|gif|bmp|heic|tiff?)$", re.I)


_SHA = {}


def _sha_arq(p):
    """sha256 do arquivo, guardado por (caminho, tamanho, data) para não reler vídeo a cada conferência."""
    st = os.stat(p)
    chave = (str(p), st.st_size, st.st_mtime_ns)
    if chave not in _SHA:
        _SHA[chave] = _sha_calc(p)
    return _SHA[chave]


def _sha_calc(p):
    import hashlib
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _broll_do_plano(arq, W):
    try:
        itens = json.loads(Path(arq).read_text(encoding="utf-8")).get("broll") or []
    except (OSError, ValueError, AttributeError):
        return []
    saida = []
    for b in itens:
        f = str((b or {}).get("file") or "")
        if f:
            saida.append((b, Path(f) if os.path.isabs(f) else W / f))
    return saida


def _imagens_do_motion(arq, W):
    """(cena, caminho) de cada imagem do motion (cenas_fluido.json): foto e cada item de grade."""
    try:
        cenas = json.loads(Path(arq).read_text(encoding="utf-8")).get("cenas") or []
    except (OSError, ValueError, AttributeError):
        return []
    saida = []
    for c in cenas:
        for f in [c] + list(c.get("imagens") or []):
            nome = str((f or {}).get("imagem") or "")
            if nome:
                p = Path(nome) if os.path.isabs(nome) else next((x for x in (Path(arq).parent / nome, W / nome) if x.is_file()), W / nome)
                saida.append((c, f, p))
    return saida


def _foto_gancho(plan, W):
    try:
        m = str((json.loads(Path(plan).read_text(encoding="utf-8")).get("hook") or {}).get("media") or "")
    except (OSError, ValueError):
        return None
    return (Path(m) if os.path.isabs(m) else W / m) if m else None


def _arquivos_do_corte(pasta_corte, W):
    g = _foto_gancho(pasta_corte / "plan.json", W)
    return ([p for _, p in _broll_do_plano(pasta_corte / "plan.json", W)] + [p for _, _, p in _imagens_do_motion(pasta_corte / "cenas_fluido.json", W)]
            + ([g] if g else []))


def broll_usados_fora(W, cid):
    """{sha: corte} dos b-rolls que já entraram em OUTROS cortes: o registro geral + os planos dos outros cortes desta pasta."""
    try:
        usados = json.loads(BROLL_REGISTRO.read_text())
    except (OSError, ValueError):
        usados = {}
    usados = {h: v for h, v in usados.items() if v.get("corte") != cid}
    for outro in (W / "work").glob("l*c*/plan.json"):
        nome = outro.parent.name
        if nome == cid or "_antigo_" in nome:
            continue
        for p in _arquivos_do_corte(outro.parent, W):
            if p.is_file():
                usados.setdefault(_sha_arq(p), {"corte": nome, "arquivo": p.name})
    return usados


def nomes_broll_proibidos(W, cid):
    try:
        return sorted({v.get("arquivo") or "?" for v in broll_usados_fora(W, cid).values()})[:60]
    except Exception:
        return []


def broll_parado(p):
    """True quando o 'vídeo' é foto: imagem, ou um mp4 em que quase nada se mexe (freezedetect)."""
    if IMAGEM.search(p.name):
        return True
    dur = ffprobe_dur(p) or 0
    if dur <= 0:
        return True
    r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", str(p), "-an", "-vf", "scale=320:-2,freezedetect=n=0.002:d=0.6",
                        "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    inicios = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", r.stderr)]
    fins = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", r.stderr)]
    parado = sum((fins[i] if i < len(fins) else dur) - t for i, t in enumerate(inicios))   # parado até o fim não tem freeze_end
    return parado >= 0.8 * dur


def problemas_broll(W, cid, plan):
    fora, vistos, probs = broll_usados_fora(W, cid), {}, []
    for b, p in _broll_do_plano(plan, W):
        rot = "{} (em {:.1f} s)".format(b.get("file"), float(b.get("start") or 0))
        if not p.is_file():
            probs.append((b, rot + ": o arquivo não existe"))
            continue
        h = _sha_arq(p)
        if h in vistos:
            probs.append((b, rot + ": repete o b-roll " + vistos[h]))
        elif h in fora:
            probs.append((b, rot + ": já foi usado no corte " + str(fora[h].get("corte"))))
        elif broll_parado(p):
            probs.append((b, rot + ": é imagem parada, não vídeo"))
        vistos.setdefault(h, str(b.get("file")))
    g = _foto_gancho(plan, W)   # a foto do gancho também nunca repete a de outro corte (o time reclamou 3 vezes)
    if g and g.is_file() and _sha_arq(g) in fora:
        probs.append((None, "FOTO DO GANCHO ({}): já foi usada no corte {}".format(g.name, fora[_sha_arq(g)].get("corte"))))
    return probs


def sem_imagem_repetida(cenas_arq, W, cid):
    """Cenas do motion sem imagem repetida no corte nem já usada em outro corte (grade fica com as que sobram, mínimo 2).
    Cena sem imagem (frase) passa como está."""
    fora, vistos, tirou = broll_usados_fora(W, cid), set(), []
    doc = json.loads(Path(cenas_arq).read_text(encoding="utf-8"))

    def boa(f):
        nome = str((f or {}).get("imagem") or "")
        p = Path(nome) if os.path.isabs(nome) else next((x for x in (Path(cenas_arq).parent / nome, W / nome) if x.is_file()), None)
        if not nome or not p:
            return True
        h = _sha_arq(p)
        if h in vistos or h in fora:
            tirou.append(p.name)
            return False
        vistos.add(h)
        return True

    saida = []
    for c in doc.get("cenas") or []:
        if c.get("imagens"):
            c["imagens"] = [f for f in c["imagens"] if boa(f)]
            if len(c["imagens"]) < 2:
                continue
        elif c.get("imagem") and not boa(c):
            continue
        saida.append(c)
    if tirou:
        log("motion do", cid, "sem imagem repetida:", ", ".join(tirou)[:400])
    return saida


def garantir_broll(W, cid, plan, passo, higgs, lote_id):
    """Troca (com o Claude) o b-roll repetido ou parado; o que ainda sobrar sai do plano: sem b-roll é melhor que repetido ou foto."""
    for _ in range(2 if higgs else 0):
        probs = problemas_broll(W, cid, plan)
        if not probs:
            return
        passo("Trocando b-roll repetido ou parado")
        log("b-roll do", cid + ":", "; ".join(x for _, x in probs)[:600])
        try:
            claude(TROCA_BROLL.format(cid=cid, W=W, lista="\n".join("- " + x for _, x in probs),
                                      regras=REGRAS_BROLL.format(bib=BIBLIOTECA_CW, proibidos=", ".join(nomes_broll_proibidos(W, cid)) or "nenhum")),
                   W, "sonnet", ao_passo=passo, limite=1800, higgs=higgs, higgs_lote=lote_id)
        except Exception as e:
            log("troca de b-roll do", cid, "falhou:", e)
    probs = problemas_broll(W, cid, plan)
    if probs:
        if any(b is None for b, _ in probs):
            log("foto do gancho do", cid, "continua repetida depois da troca")
        ruins = {(str(b.get("file")), float(b.get("start") or 0)) for b, _ in probs if b is not None}
        pl = json.loads(plan.read_text(encoding="utf-8"))
        pl["broll"] = [b for b in pl.get("broll") or [] if (str(b.get("file")), float(b.get("start") or 0)) not in ruins]
        plan.write_text(json.dumps(pl, ensure_ascii=False, indent=1), encoding="utf-8")
        log("b-roll do", cid, "tirado do plano:", "; ".join(x for _, x in probs)[:600])


def registrar_broll(W, cid, plan):
    """Marca os b-rolls do corte entregue como usados (outro corte não pode repetir)."""
    import fcntl
    try:
        BROLL_REGISTRO.parent.mkdir(parents=True, exist_ok=True)
        with open(str(BROLL_REGISTRO) + ".trava", "w") as trava:
            fcntl.flock(trava, fcntl.LOCK_EX)
            try:
                usados = json.loads(BROLL_REGISTRO.read_text())
            except (OSError, ValueError):
                usados = {}
            for p in _arquivos_do_corte(Path(plan).parent, W):
                if p.is_file():
                    usados[_sha_arq(p)] = {"corte": cid, "arquivo": p.name}
            BROLL_REGISTRO.write_text(json.dumps(usados, ensure_ascii=False))
    except Exception as e:
        log("registro de b-roll do", cid, "falhou:", e)


# ------------------------------------------------------------------------------------------- ajuste garantido
CONFERE = """Conferência de um AJUSTE pedido pelo editor no corte "{titulo}".
Pedido do editor: {pedido}
{prints}Quadros do corte já refeito (com o tempo no nome do arquivo; os primeiros são o começo do vídeo, onde ficam gancho, headline e foto de capa): {quadros}
Abra os prints (se houver) e os quadros e responda com honestidade se o pedido foi ATENDIDO no corte novo.
Julgue cada parte do pedido no quadro em que ela aparece (o que é do gancho/headline/capa, julgue nos quadros do começo).
Se o pedido era mostrar quem fala e essa pessoa não aparece em nenhuma câmera do vídeo original naquele momento, cobrir o trecho
com B-roll ou foto (ou tirar a fala) conta como atendido; diga isso no motivo.
SEMPRE (atendido ou não) escreva a LIÇÃO que o pedido ensina: regra curta e geral, no imperativo, que vale para os próximos cortes
deste estilo, sem citar este corte (ex.: "Headline do gancho em Montserrat Bold, fundo branco e texto preto, com sombra fraca").
Deixe vazio só se o pedido for algo que vale apenas para este corte (ex.: trocar uma palavra errada da fala).
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


HIGGS_LIVRE = 100000   # 28/10/2026: o Felipe tirou o limite de Higgsfield da esteira (o orçamento do lote não trava mais)


def higgs_do_lote(lote):
    """Gerações de Higgsfield liberadas para o lote: sem limite desde 28/10/2026 (o campo do lote fica só como registro)."""
    return HIGGS_LIVRE


def higgs_restante(lote):
    """Gerações de Higgsfield que ainda cabem no orçamento do lote (o mesmo contador do gancho de permissão)."""
    arq = BASE / ".higgs" / "lote-{}".format(lote["id"])
    try:
        usados = int(arq.read_text())
    except (OSError, ValueError):
        usados = 0
    return higgs_do_lote(lote) - usados, usados


def quadros_do_ajuste(video, pedido, pasta, transcript=None, extra=()):
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
    tempos = sorted(set([0.4, 1.5, 3.0] + [round(t, 1) for t in list(tempos) + [x for x in extra if 0 <= x <= dur]]))   # o começo (gancho, headline, foto de capa) sempre entra
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


def conferir_ajuste(c, video, pasta, prints, passo, transcript=None, extra=()):
    """O Claude olha os quadros do trecho e diz se o pedido foi atendido. Devolve o dict da resposta."""
    passo("Conferindo se o ajuste foi atendido")
    quadros = quadros_do_ajuste(video, c.get("ajuste"), pasta, transcript, extra)
    try:
        r = json_final(claude(CONFERE.format(titulo=c["titulo"], pedido=c.get("ajuste"), prints=texto_prints(prints),
                                             quadros=", ".join(str(q) for q in quadros)), pasta, "sonnet", ao_passo=passo, limite=600))
    except Exception as e:
        r = {"atendido": False, "motivo": "não consegui conferir ({})".format(e)}
    return r


def registrar_conferencia(c, r, cid):
    ok = bool(r.get("atendido"))
    srv("corte", id=c["id"], ajuste_conferido="sim" if ok else "nao", ajuste_nota=(r.get("motivo") or "")[:500])
    if len(r.get("licao") or "") >= 5:   # todo pedido do editor vira regra: do time (Conhecimento) e do estilo (## APRENDIZADOS da receita)
        srv("licao", texto=r["licao"][:500], categoria=r.get("categoria") or "", kit="cw-cast", ref="corte " + cid, pedido=c.get("ajuste") or "",
            corte_id=c["id"])   # o banco acha o estilo pelo lote (lote de teste de estilo não mexe na receita)


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


# ------------------------------------------------------------------------------------------- motion design do Timeu-CW-01
# Camada de motion (pílulas, notificação, telas cheias na cor da marca, CTA) por cima do corte pronto do Titeu, com o motor
# timeu01 da skill motion-vox (cw-cortes). Se qualquer passo falhar, o corte segue sem a camada (nunca quebra a entrega).
def _motor_timeu():
    for r in sorted(HOME.glob(".claude/plugins/marketplaces/*/plugins/cw-cortes/skills/motion-vox/lib/timeu01/render.py")):
        return r
    return None


PLANO_MOTION = """Você é o diretor de motion do corte {cid} (CW Cast, estilo Timeu-CW-01, motion da skill motion-vox), sem ninguém olhando.
O corte já está PRONTO (câmera, legenda queimada, b-roll, trilha): {final} ({dur:.1f} s). Você só acrescenta MOTION DESIGN por cima.
A fala com os tempos do vídeo final está em {fala} (uma linha por legenda: início-fim | texto).
Leia o catálogo de peças e telas em {telas} e um plano aprovado de exemplo em {exemplo} (copie o jeito, nunca os textos/tempos).

Preencha {saida} (JSON; ele já existe com as chaves de configuração: mantenha-as) com "motion": [...] e "telas": [...].
O corte tem que ficar BEM animado (Felipe: "poderia ter mais motion, animação estilo do motion vox"):
- Uma peça a cada 5–7 s no corte INTEIRO (nenhuma nos 3 primeiros segundos; ~{n_pecas} peças neste corte), cada uma de 1,5 a 3 s,
  começando no instante em que a fala diz aquilo. VARIE os tipos, não só pílula:
  pilula (conceito em 1–4 palavras MAIÚSCULAS, icone e cor da paleta), notificacao (pedido/cliente/venda), lista (quando enumerar,
  itens entrando um a um), contador (quando falar número), etiqueta (preço, com risca_em/troca_em), balao no fim se pedir comentário.
- 1 a 2 "telas" cheias de motion (2,5–4 s) por corte: use as do catálogo que encaixem na fala (destaque para uma ideia forte sem número,
  comparacao para errado × certo, checklist, numero, barras, ranking, chat…). Nunca no momento emocional nem nos 3 primeiros segundos.
- POSIÇÃO (Felipe: "as tags estão bem no rosto"): o rosto ocupa de 0.15 a 0.62 da altura e a legenda queimada fica perto de 0.66.
  Toda peça fica ABAIXO da legenda: x 0.5, y entre 0.78 e 0.84 (faixa do peito). Peça grande (lista, notificação) use y 0.80–0.86
  e size menor. Nada acima de 0.72.
- Textos fiéis à fala, sem inventar dado. Nada de logo/cartão final.
Depois confira: rode
  python3 {motor} {saida} {final} {quadros} --quadros <tempos separados por vírgula: o meio de cada peça e de cada tela>
e abra {quadros}/folha.jpg. Se alguma peça tapar o rosto, encostar na legenda ou sair cortada, ajuste y/size e confira de novo
(no máximo 2 voltas). NENHUMA IMAGEM SE REPETE (regra do time): cada insert com uma imagem diferente, e é proibido usar arquivo que já entrou em outro corte
(proibidos: {proibidos}); imagem repetida é tirada automaticamente.
NÃO renderize o vídeo (é automático). Responda APENAS com JSON: {{"ok": true, "pecas": n, "notas": "uma linha"}}"""


def _ass_t(x):
    h, m, s = x.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def fala_do_ass(ass):
    """Legendas do corte pronto (tempo do vídeo final): [(t0, t1, texto)]."""
    out, linhas = set(), [l.split(",", 9) for l in ass.read_text(encoding="utf-8", errors="ignore").splitlines() if l.startswith("Dialogue:")]
    linhas = [p for p in linhas if len(p) >= 10 and "\\p1" not in p[9]]   # desenhos (caixa do título) não são fala
    if any(p[3].strip() == "Cap" for p in linhas):   # só a legenda (a headline do gancho tem estilo próprio)
        linhas = [p for p in linhas if p[3].strip() == "Cap"]
    for p in linhas:
        txt = re.sub(r"\{[^}]*\}", "", p[9]).replace("\\N", " ").replace("\\n", " ").strip()
        try:
            t0, t1 = _ass_t(p[1]), _ass_t(p[2])
        except ValueError:
            continue
        if txt and t1 > t0:
            out.add((t0, t1, txt))   # cada frase vem em duas camadas (sombra e texto): uma só
    return sorted(out)


def motion_timeu(cid, W, work, final, previa, pasta, passo):
    motor = _motor_timeu()
    ass = work / "captions.ass"
    if not motor or not final.exists() or not ass.exists():
        return
    try:
        fala = fala_do_ass(ass)
        if not fala:
            return
        pal = []
        for t0, t1, txt in fala:
            ws = txt.split()
            d = (t1 - t0) / len(ws)
            pal += [{"w": w, "t": round(t0 + i * d, 3), "e": round(t0 + (i + 1) * d, 3)} for i, w in enumerate(ws)]
        (work / "palavras_final.json").write_text(json.dumps(pal, ensure_ascii=False), encoding="utf-8")
        (work / "fala_final.txt").write_text("\n".join("{:.2f}-{:.2f} | {}".format(*x) for x in fala), encoding="utf-8")
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                                   capture_output=True, text=True).stdout.strip() or 0)
        cenas, quadros = work / "cenas_motion.json", work / "quadros_motion"
        passo("Motion design (planejando as peças)")
        doc0 = {"so_motion": True, "sfx": True, "limpa_audio": False, "palavras": "palavras_final.json", "motion": [], "telas": [], "destaques": []}
        cenas.write_text(json.dumps(doc0, ensure_ascii=False), encoding="utf-8")   # a conferência com --quadros já sai sem legenda duplicada
        claude(PLANO_MOTION.format(cid=cid, final=final, dur=dur, fala=work / "fala_final.txt", telas=motor.parent / "TELAS.md",
                                   exemplo=motor.parent / "cenas_exemplo.json", saida=cenas, motor=motor, quadros=quadros,
                                   n_pecas=max(4, int((dur - 3) / 6)), proibidos=", ".join(nomes_broll_proibidos(W, cid)) or "nenhum"),
               W, "sonnet", ao_passo=passo, limite=1200)
        doc = json.loads(cenas.read_text(encoding="utf-8"))
        for m in doc.get("motion") or []:   # nunca no rosto: tudo na faixa do peito, abaixo da legenda queimada
            if m.get("tipo") != "balao" and float(m.get("y", 0.74)) < 0.76:
                m["y"] = 0.80
        doc = {"motion": doc.get("motion") or [], "telas": doc.get("telas") or [], "destaques": [],
               "so_motion": True, "sfx": True, "limpa_audio": False, "palavras": "palavras_final.json"}
        if not doc["motion"] and not doc["telas"]:
            return
        cenas.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        passo("Motion design (render)")
        saida = work / "{}_motion.mov".format(cid)
        env = dict(os.environ, TIMEU_SFX_BANCO=str(motor.parents[2] / "assets" / "sfx_banco"))
        r = subprocess.run([sys.executable, str(motor), str(cenas), str(final), str(saida)], cwd=str(work), env=env,
                           capture_output=True, text=True, timeout=3600)
        (pasta / "motor.log").open("a").write("\n$ motion timeu01\n{}\n{}\n".format(r.stdout[-2000:], r.stderr[-2000:]))
        if r.returncode or not saida.exists() or saida.stat().st_size < 100000:
            raise RuntimeError((r.stderr or r.stdout)[-300:])
        shutil.move(str(final), str(final.with_name(final.stem + "_sem_motion" + final.suffix)))
        shutil.move(str(saida), str(final))
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(final), "-vf", "scale=720:-2", "-c:v", "libx264", "-crf", "26",
                        "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(previa)], timeout=1200)
        log("motion do Timeu no", cid + ":", len(doc["motion"]), "peça(s),", len(doc["telas"]), "tela(s)")
    except Exception as e:
        log("motion do Timeu no", cid, "ficou de fora:", str(e)[-300:])


# ------------------------------------------------------------------------------------------- motion CW-Fluido (23/10/2026)
# Referências do Felipe: câmera em cima e cena ilustrada embaixo + tipografia cinética fluida, na marca da CW.
# O corte é renderizado também SEM legenda; o motor (motion-vox/lib/cwfluido) alterna câmera / tela dividida / motion em
# tela cheia, desenha a legenda do Titeu em todos os momentos e anima as palavras no instante em que são ditas.
def _motor_fluido():
    for r in sorted(HOME.glob(".claude/plugins/marketplaces/*/plugins/cw-cortes/skills/motion-vox/lib/cwfluido/render.py")):
        return r
    return None


PLANO_FLUIDO = """Você é o editor de inserts do corte {cid} (CW Cast), sem ninguém olhando. O corte está PRONTO; você só acrescenta
INSERTS DE CONTEXTO no formato dos cortes do ROI Hunters (referência do Felipe, 24/10/2026): o corpo fica em câmera limpa e,
quando a fala cita uma empresa, marca, pessoa, lugar ou produto ESPECÍFICO, a tela divide (rosto em cima) e embaixo entra uma
FOTO REAL daquilo, com zoom lento. Comparações rápidas entre 2 a 4 empresas viram uma grade com as fotos/logos entrando uma a uma.
Arquivos: {pal} (cada palavra com o instante, em segundos), {fala} (as legendas), vídeo {video} ({dur:.1f} s).
Preencha {saida} (JSON; ele já existe com as chaves de configuração: mantenha-as) com:
- "cenas": 2 a 4 inserts depois de {evitar:.1f} s, cada um de 2 a 4 s, começando no instante em que o nome é dito; entre um e
  outro, pelo menos 4 s de câmera. Só para coisa ESPECÍFICA citada (iFood, Domino's, Chanel, Nubank, um bairro, uma pessoa).
  · foto:  {{"t0", "t1", "layout": "split", "tipo": "foto", "imagem": "broll/fotos_reais/<arquivo>", "credito": "...", "foco": 0.5}}
  · grade: {{"t0", "t1", "layout": "split", "tipo": "grade", "imagens": [{{"imagem", "t", "credito"}}, ...]}}  (2 a 4)
  A foto vem SÓ da Wikipedia/Wikimedia com licença livre (foto de Google, banco de imagem ou Instagram dá strike):
      python3 {foto_real} "<nome>" --saida broll/fotos_reais [--pessoa]
  Ele devolve JSON com "arquivo", "credito" e "foco" (copie os três). Se não houver foto livre, NÃO faça o insert
  (nunca gere pessoa ou marca real com IA). Antes, veja se já existe em broll/fotos_reais/ (ls).
  A FOTO TEM QUE REPRESENTAR O QUE A FALA QUER DIZER (Felipe: "falou do Mercado Livre e botou o escritório do Mercado Livre").
  Abra cada foto (Read) antes de usar. Prédio, sede ou escritório só se a fala for sobre o lugar; para marca, procure o que
  a pessoa reconhece (o logo, a loja, o produto, o app): tente nomes mais específicos ("Mercado Livre logo", "iFood",
  "Domino's Pizza loja", "pizza", "aeroporto de Guarulhos"). Se nenhuma servir, sem insert.
- Nada de motion, ilustração, cartela, tag ou balão: o formato é jornalístico e limpo.
Depois confira: rode
  python3 {motor} {saida} {video} {quadros} --quadros <o meio de cada insert>
e abra {quadros}/folha.jpg (foto certa? enquadramento bom? nada cortado?). Ajuste e confira de novo (no máximo 2 voltas).
NENHUMA IMAGEM SE REPETE (regra do time): cada insert com uma imagem diferente, e é proibido usar arquivo que já entrou em outro corte
(proibidos: {proibidos}); imagem repetida é tirada automaticamente.
NÃO renderize o vídeo (é automático). Responda APENAS com JSON: {{"ok": true, "inserts": n, "notas": "uma linha"}}"""


PLANO_CANAL2 = """Você é o editor de motion do corte {cid} do SEGUNDO CANAL do CW Cast, sem ninguém olhando. O corte está PRONTO
(enquadramento, cortes, b-roll); o motor desenha a legenda pequena, o gancho em caixas brancas e o que você planejar. É uma edição
BÁSICA e diferente da oficial (referência do Felipe: Signal55): frases animadas por cima da câmera e imagens em tela cheia.
Arquivos: {pal} (cada palavra com o instante, em segundos), {fala} (a fala por blocos), vídeo {video} ({dur:.1f} s).
Preencha {saida} (JSON; ele já existe com as chaves de configuração: mantenha-as) com "cenas": [...] depois de {evitar:.1f} s:
- 3 a 5 FRASES: {{"t0", "t1", "tipo": "frase", "texto": "<as palavras EXATAMENTE como ditas, 3 a 7>", "destaque": [1 a 2 palavras do texto],
  "pos": "esq" | "dir" | "centro", "pb": false}}. Cada uma de 2 a 4 s, no começo da frase mais forte de cada trecho.
  Uma delas (a de maior impacto) pode ter "pb": true (o momento fica em preto e branco). Entre frases, pelo menos 4 s.
- 2 a 4 IMAGENS em tela cheia, de 1,5 a 3 s, quando a fala cita algo que dá para mostrar:
  {{"t0", "t1", "layout": "full", "tipo": "foto", "imagem": "<caminho>", "credito": "<crédito>"}}
  · coisa/marca/lugar/pessoa ESPECÍFICA citada: python3 {imagem_web} "<busca curta e específica>" --saida broll/web
    (traz da internet, com o crédito; ABRA a imagem com Read antes de usar: se vier errada ou genérica, tente --origem wikipedia
    ou --origem web, ou troque a busca. Logo, loja, produto ou app reconhecível > prédio/escritório.)
  · cena genérica (cozinha cheia, motoboy, cliente no celular): python3 {objeto3d} "<cena em inglês>" --foto --slug <slug> --banco broll/ia
    (Higgsfield; nunca marca ou pessoa real).
  Copie "arquivo" e "credito" do JSON que o comando devolve. Imagem e frase nunca ao mesmo tempo.
Depois confira: rode
  python3 {motor} {saida} {video} {quadros} --quadros <o meio de cada cena>
e abra {quadros}/folha.jpg (frase legível, sem tapar o rosto? imagem certa?). Ajuste e confira de novo (no máximo 2 voltas).
NENHUMA IMAGEM SE REPETE (regra do time): cada insert com uma imagem diferente, e é proibido usar arquivo que já entrou em outro corte
(proibidos: {proibidos}); imagem repetida é tirada automaticamente.
NÃO renderize o vídeo (é automático). Responda APENAS com JSON: {{"ok": true, "cenas": n, "notas": "uma linha"}}"""


def formato_canal2(estilo):
    """Estilo do segundo canal (marca na receita: "FORMATO: CANAL 2")."""
    return "formato: canal 2" in ((estilo or {}).get("receita") or "").lower()


def formato_02(estilo):
    """O estilo escolhido no pedido é o "Padrão #02"? (marca na receita: "FORMATO: PADRÃO #02")."""
    return "formato: padrão #02" in ((estilo or {}).get("receita") or "").lower()


def cortar_receita(rec, n):
    """Encurta a receita sem perder os APRENDIZADOS do time (ficam no fim e eram os primeiros a sumir no corte)."""
    if len(rec) <= n:
        return rec
    i = rec.find("## APRENDIZADOS")
    if i < 0:
        return rec[:n]
    apr = rec[i:i + 3500]
    return rec[:max(0, n - len(apr) - 2)] + "\n\n" + apr


def arroba_do_estilo(estilo):
    """@ do canal, da linha "ARROBA: @perfil" na receita do estilo (o motor desenha embaixo da legenda)."""
    m = re.search(r"(?im)^\s*ARROBA:\s*(@[\w.]+)", (estilo or {}).get("receita") or "")
    return m.group(1) if m else ""


def motion_fluido(cid, W, work, final, previa, pasta, passo, lote, plan_hook=0.0, tema="roi", arroba="", reusar=False):
    motor = _motor_fluido()
    if not motor or not final.exists() or not (work / "captions.ass").exists():
        return
    lib = motor.parent
    try:
        passo("Motion (versão sem legenda)")
        r = subprocess.run([sys.executable, str(lib / "sem_legenda.py"), cid], cwd=str(W), capture_output=True, text=True, timeout=2400,
                           env=dict(os.environ, CW_SEM_TEXTO="1" if tema == "canal2" else "0"))
        (pasta / "motor.log").open("a").write("\n$ sem legenda\n{}\n{}\n".format(r.stdout[-1500:], r.stderr[-1500:]))
        sem = W / "saida" / "{}_sem_legenda.mov".format(cid)
        if r.returncode or not sem.exists():
            raise RuntimeError("render sem legenda: " + (r.stderr or r.stdout)[-300:])
        passo("Motion (tempo de cada palavra)")
        r = subprocess.run([sys.executable, str(lib / "palavras.py"), str(sem), str(work / "palavras_final.json")],
                           cwd=str(work), capture_output=True, text=True, timeout=900)
        if r.returncode:
            raise RuntimeError("palavras: " + (r.stderr or r.stdout)[-300:])
        fala = fala_do_ass(work / "captions_final.ass")
        (work / "fala_final.txt").write_text("\n".join("{:.2f}-{:.2f} | {}".format(*x) for x in fala), encoding="utf-8")
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(sem)],
                                   capture_output=True, text=True).stdout.strip() or 0)
        evitar = max(4.0, float(plan_hook or 0) + 0.5)
        cenas, quadros = work / "cenas_fluido.json", work / "quadros_fluido"
        try:   # ajuste que não é sobre o motion: as mesmas imagens e frases de antes (o time já aprovou)
            antigas = json.loads(cenas.read_text(encoding="utf-8")).get("cenas") if reusar else None
        except (OSError, ValueError):
            antigas = None
        cfg = {"palavras": "palavras_final.json", "legendas_ass": "captions_final.ass", "evitar_ate": evitar, "cenas": [], "cartelas": []}
        if tema == "canal2":   # o motor desenha a legenda e o gancho do Canal 2 (o vídeo veio sem nenhum texto)
            plano = json.loads((work / "plan.json").read_text(encoding="utf-8"))
            cfg.update(tema="canal2", headline=(plano.get("headline") or ""), gancho_ate=max(3.0, float(plan_hook or 4.0)), legendas_ass="",
                       arroba=arroba)
            if not arroba:
                log("motion do", cid, "sem @: falta a linha \"ARROBA: @perfil\" na receita do estilo")
        cenas.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        (W / "broll" / "fotos_reais").mkdir(parents=True, exist_ok=True)
        higgs = higgs_do_lote(lote)
        (W / "broll" / "web").mkdir(parents=True, exist_ok=True); (W / "broll" / "ia").mkdir(parents=True, exist_ok=True)
        modelo_prompt = PLANO_CANAL2 if tema == "canal2" else PLANO_FLUIDO
        if antigas:
            passo("Motion (mesmas cenas de antes)")
            cenas.write_text(json.dumps(dict(cfg, cenas=antigas), ensure_ascii=False), encoding="utf-8")
        else:
            passo("Motion (planejando as cenas)")
        if not antigas: claude(modelo_prompt.format(cid=cid, pal=work / "palavras_final.json", fala=work / "fala_final.txt", video=sem, dur=dur,
                                   saida=cenas, evitar=evitar, objeto3d=lib / "objeto3d.py", motor=motor, quadros=quadros,
                                   catalogo=lib / "infografico.py", foto_real=lib.parent.parent / "scripts" / "foto_real.py",
                                   imagem_web=lib / "imagem_web.py", proibidos=", ".join(nomes_broll_proibidos(W, cid)) or "nenhum"),
               W, "sonnet", ao_passo=passo, limite=1500, higgs=higgs, higgs_lote=lote["id"])
        doc = json.loads(cenas.read_text(encoding="utf-8"))
        doc.update({k: v for k, v in cfg.items() if k != "cenas"})   # cartelas ficam vazias (Felipe, 24/10: sem a tag de tema)
        doc.pop("tag", None)   # Felipe: a headline no meio, não a tag em cima
        doc["cenas"] = [c for c in doc.get("cenas") or [] if float(c.get("t0", 0)) >= evitar]
        cenas.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        if not antigas:
            doc["cenas"] = sem_imagem_repetida(cenas, W, cid)   # regra do time: imagem nenhuma se repete (no corte nem entre cortes)
        # sem insert o corte passa pelo motor do mesmo jeito: transições, SFX e sem a tela de ênfase (formato padrão)
        creditos = sorted({f.get("credito") for c in doc["cenas"] for f in ([c] + list(c.get("imagens") or []))
                           if f.get("credito") and not re.search(r"CC0|dom[ií]nio p[uú]blico|public domain", f["credito"], re.I)})   # CC0 não exige crédito
        creditos = [re.sub(r"^Foto:\s*", "", x) for x in creditos]
        creditos = [re.sub(r"^Imagem:\s*", "", x) for x in creditos]
        (work / "creditos.txt").write_text(("Imagens: " + "; ".join(creditos)) if creditos else "", encoding="utf-8")
        cenas.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        passo("Motion (render)")
        saida = work / "{}_fluido.mov".format(cid)
        r = subprocess.run([sys.executable, str(motor), str(cenas), str(sem), str(saida)], cwd=str(work), capture_output=True, text=True, timeout=3600)
        (pasta / "motor.log").open("a").write("\n$ motion cw-fluido\n{}\n{}\n".format(r.stdout[-2000:], r.stderr[-2000:]))
        if r.returncode or not saida.exists() or saida.stat().st_size < 100000:
            raise RuntimeError((r.stderr or r.stdout)[-300:])
        shutil.move(str(final), str(final.with_name(final.stem + "_sem_motion" + final.suffix)))
        shutil.move(str(saida), str(final))
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(final), "-vf", "scale=720:-2", "-c:v", "libx264", "-crf", "26",
                        "-preset", "veryfast", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(previa)], timeout=1200)
        log("motion CW-Fluido no", cid + ":", len(doc["cenas"]), "cena(s)")
    except Exception as e:
        log("motion CW-Fluido no", cid, "ficou de fora:", str(e)[-300:])


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
    trava = None
    if c.get("ajuste") and plan.exists():
        itens = itens_ajuste(c["ajuste"])
        partir_da_versao(c, work)
        trava = Trava(work, itens)   # o que não foi pedido fica como está
        log("ajuste do", cid, "áreas:", ",".join(sorted(trava.areas)))
    if trava and trava.livres is not None and "enquadramento" not in trava.areas:
        rodar("Pontas e emendas", ["scripts/alinhar_frames.py", cid], ok=(0, 1))
    elif c.get("ajuste") and plan.exists():
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
        # revisor de legendas (padrão em todo corte): dicionário do time + IA barata conferem marcas, nomes e termos
        rodar("Revisão das legendas", [AQUI / "legendas.py", "revisar", "work/{}/transcript.json".format(cid),
                                       "--contexto", "{} · corte \"{}\"".format(ep["nome"], c["titulo"])], ok=(0, 1), tempo=300)
    # a única chamada do Claude: as decisões editoriais
    foto = ("se ainda não for arquivo, gere UMA imagem no Higgsfield (cw-studios › gerar; foto realista, horizontal, sem texto) a partir da descrição e salve em work/{}/gancho.jpg; use esse caminho.".format(cid)
            if higgs else "use uma foto de assets/ganchos que combine com a descrição; se nenhuma servir, deixe a descrição e diga nas notas.")
    prints = baixar_prints(c, pasta) if c.get("ajuste") else []
    broll = ("B-roll ao longo do corte (não só no gancho): planeje 2 a 4 inserts de 1,5–2,5 s espalhados, nas falas que citam algo visual. "
             + REGRAS_BROLL.format(bib=BIBLIOTECA_CW, proibidos=", ".join(nomes_broll_proibidos(W, cid)) or "nenhum")
             + " Ponha em plan.json \"broll\": [{\"file\": \"broll/arquivo.mp4\", \"start\": s, \"dur\": s, \"mode\": \"full\"}] "
             "(\"file\" é sempre o caminho em texto). Nunca tape o rosto num momento de emoção."
             ) if higgs else "b-roll só se houver VÍDEO bom em broll/ que nunca foi usado em outro corte (sem b-roll é melhor que genérico ou repetido)."
    if formato_canal2(estilo):   # Canal 2: as imagens entram depois, no motion do Zeca (b-roll do Titeu aqui sai cortado no reenquadramento)
        broll = 'SEM b-roll neste formato: deixe "broll": [] (as imagens entram depois, no motion do Zeca).'
    rec =receita(estilo, "CORTE") if (estilo or {}).get("receita") else ""
    instrucoes = ("INSTRUÇÕES DA VERSÃO DO ESTILO (valem acima das regras gerais acima):\n" + cortar_receita(rec, 6000) + "\n") if rec else ""
    if trava:
        passo("Ajuste: " + ", ".join(sorted(trava.areas)))
        extra = ""
        if "broll" in trava.areas:
            extra += "B-ROLL: " + broll + "\n"
        if "gancho" in trava.areas:
            extra += "FOTO DO GANCHO (hook.media): " + foto + "\n"
        if "fonte" in trava.areas:
            extra += ("FONTE e @: são desenhadas pelo motor do estilo (Montserrat e o @ do canal já vêm do estilo); se o pedido for só isso, "
                      "não há o que mudar no plan.json: responda ok e diga isso nas notas.\n")
        claude(AJUSTE_TIMEU.format(cid=cid, W=W, ref=ref, itens=texto_itens(trava.itens), extra=extra + instrucoes, prints=texto_prints(prints)),
               W, "sonnet", ao_passo=passo, limite=2100 if higgs else 900, higgs=higgs, higgs_lote=lote["id"])
        desfeito = trava.aplicar()
        if desfeito:
            log("ajuste do", cid, "desfeito fora do pedido:", ", ".join(desfeito))
    else:
        passo("Plano do corte (gancho, legenda, B-roll)")
        claude(DECISAO_TIMEU.format(cid=cid, W=W, ref=ref, foto=foto, broll=broll, instrucoes=instrucoes,
                                enfase=('NÃO use "enfase" (a tela branca com a frase): este formato não tem; deixe "enfase": [].'
                                        if formato_02(estilo) or formato_canal2(estilo) else "Ênfase (frase de impacto) conforme as regras do estilo, se fizer sentido."),
                                ajuste=("AJUSTE PEDIDO PELO EDITOR (o corte já existe): " + c["ajuste"] + ". Mude só isso; se for câmera/enquadramento, "
                                        "ajuste cx/cy/stack dos shots daquele trecho conferindo o episodio.json (câmeras) e quem fala "
                                        "(os planos já foram refeitos pelo mapa de câmeras). Se quem fala NÃO aparece em nenhuma câmera do bruto "
                                        "naquele momento, reenquadrar não resolve: cubra o trecho com B-roll ou com a foto do gancho, ou tire a fala "
                                        "se o corte continuar fazendo sentido, e diga isso nas notas.\n" + texto_prints(prints))
                                if c.get("ajuste") else ""),
           W, "sonnet", ao_passo=passo, limite=2100 if higgs else 900, higgs=higgs, higgs_lote=lote["id"])
    def broll_ok():   # regra do time: b-roll é vídeo com movimento e nunca se repete (no corte nem entre cortes)
        if trava:
            trava.aplicar()
        if not formato_canal2(estilo) and (not trava or trava.livres is None or "broll" in trava.areas):
            garantir_broll(W, cid, plan, passo, higgs, lote["id"])
    broll_ok()
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
        broll_ok()
    rodar("Render final", ["scripts/render.py", "work/{}/plan.json".format(cid)], tempo=2400)
    rodar("Revisão técnica", ["tools/final_probe.py", "saida/{}.mov".format(cid), "--perfil", "final"], ok=(0, 1))
    final, previa = W / "saida" / "{}.mov".format(cid), W / "saida" / "{}_preview.mp4".format(cid)
    try:
        hook_dur = float((json.loads(plan.read_text(encoding="utf-8")).get("hook") or {}).get("duration") or 0)
    except (OSError, ValueError):
        hook_dur = 0.0
    def motion():
        if formato_02(estilo):   # Padrão #02: fotos reais, transições com desfoque e SFX (formato ROI Hunters, 24/10/2026)
            motion_fluido(cid, W, work, final, previa, pasta, passo, lote, hook_dur, reusar=bool(trava and trava.reaproveitar_motion()))
        elif formato_canal2(estilo):   # segundo canal: edição básica com frases animadas e imagens da internet/Higgsfield
            motion_fluido(cid, W, work, final, previa, pasta, passo, lote, hook_dur, tema="canal2", arroba=arroba_do_estilo(estilo),
                          reusar=bool(trava and trava.reaproveitar_motion()))
    motion()
    def meios_broll():   # a conferência também olha o meio de cada b-roll (inserts de 2 s somem entre quadros espalhados)
        try:
            return [float(b["start"]) + float(b.get("dur") or 0) / 2 for b in json.loads(plan.read_text(encoding="utf-8")).get("broll") or []][:6]
        except (OSError, ValueError, KeyError, TypeError):
            return []
    if c.get("ajuste") and final.exists():   # ajuste garantido: confere, e se não atendeu corrige uma vez e confere de novo
        r = conferir_ajuste(c, final, pasta, prints, passo, work / "transcript.json", meios_broll())
        if not r.get("atendido"):
            passo("O ajuste não ficou bom: corrigindo de novo")
            claude(CORRIGE_TIMEU.format(cid=cid, W=W, ref=ref) + "\nO PEDIDO DO EDITOR AINDA NÃO FOI ATENDIDO (mexa só nestes itens): "
                   + (texto_itens(trava.itens) if trava else c["ajuste"])
                   + "\nO que a conferência viu: " + (r.get("motivo") or "") + "\n" + texto_prints(prints)
                   + ("\n" + REGRAS_BROLL.format(bib=BIBLIOTECA_CW, proibidos=", ".join(nomes_broll_proibidos(W, cid)) or "nenhum") if higgs else ""),
                   W, "sonnet", ao_passo=passo, limite=2100 if higgs else 900, higgs=higgs, higgs_lote=lote["id"])
            broll_ok()
            rodar("Enquadramento", ["scripts/movimento.py", "work/{}/plan.json".format(cid)], ok=(0, 1))
            rodar("Render final", ["scripts/render.py", "work/{}/plan.json".format(cid), "--preview"], tempo=1800)
            rodar("Render final", ["scripts/render.py", "work/{}/plan.json".format(cid)], tempo=2400)
            motion()   # o render novo sai sem o motion do formato: aplica de novo antes de conferir
            r = conferir_ajuste(c, final, pasta, prints, passo, work / "transcript.json", meios_broll())
        resta, usados = higgs_restante(lote)
        if not r.get("atendido") and higgs and resta <= 0:   # o time vê a causa real na Central
            r["motivo"] = "Orçamento de Higgsfield do lote acabou ({}/{}), então não deu para gerar b-roll novo. {}".format(usados, higgs, r.get("motivo") or "")
        registrar_conferencia(c, r, cid)
    if trava:   # o time vê na Central o que mudou de verdade
        srv("corte", id=c["id"], ajuste_mudancas=trava.resumo()[:900])
    if not final.exists():
        raise Falhou("o render final não gerou saida/{}.mov".format(cid))
    registrar_broll(W, cid, plan)
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

NÃO altere os arquivos do estilo (scripts, receita): use como estão. Se algo do estilo estiver errado, diga no "motivo";
quem muda o estilo é o time, pela Oficina (✨ Melhorar).
PARA SER RÁPIDO (vale por cima da receita):
- Rode os scripts do estilo com "python3 script.py ..." (sem -I): a transcrição deles vai para a Groq, como no podcast.
- Revisão das legendas (obrigatória): assim que a transcrição palavra por palavra do estilo estiver pronta (ex.: palavras.json depois
  do "analisar") e ANTES do plano/montagem, rode: python3 {legendas} revisar <arquivo de palavras> --contexto "{titulo}"
  (corrige marcas, nomes e termos pelo dicionário do time; o tempo das palavras não muda).
- Rascunho primeiro: enquanto ajusta (plano, montagem, conferência e correções), monte na resolução menor (ex.: --res 1080x1920).
  Só quando a conferência aprovar, renderize UMA vez na qualidade final da receita (ex.: 2160x3840) e entregue esse arquivo.
  Nunca monte em 4K mais de uma vez.

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
    def passo(t):   # andamento na página; falha momentânea da central não derruba o corte
        try:
            srv("corte", id=c["id"], etapa=t)
        except Exception as e:
            log("andamento do corte", c["id"], "não foi:", e)
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
        higgs = higgs_do_lote(lote)
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
                                       ajuste=("AJUSTE PEDIDO PELO EDITOR (este corte já foi produzido e aprovado em todo o resto):\n"
                                               + texto_itens(itens_ajuste(c["ajuste"])) + "\n"
                                               "Não refaça do zero: parta do plan.json e dos arquivos que já existem para este corte e mude SÓ esses itens, "
                                               "no trecho dito. Headline, gancho, imagens, b-roll, enquadramento e trechos que não estão na lista ficam IGUAIS. "
                                               "Re-renderize e rode os gates de novo.\n" + texto_prints(prints)) if c.get("ajuste") else "",
                                       higgs="Higgsfield liberado: até {} geração(ões) neste lote (ferramenta cw-studios › gerar).".format(higgs) if higgs else "Higgsfield: não use.",
                                       pasta=pasta, w=w, receita=receita(estilo, "CORTE") if estilo else "Vertical 1080x1920 com legenda e gancho."),
                       pasta, "sonnet", ao_passo=passo, limite=2700, extra_dirs=extra, higgs=higgs, higgs_lote=lote["id"])
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
    higgs = higgs_do_lote(lote)
    prints = baixar_prints(c, pasta) if c.get("ajuste") else []
    base = BASE / str(ep["id"])
    # reel: a receita inteira vale (EPISÓDIO = análise do bruto + CORTE = o vídeo), com a pasta da skill do estilo
    skill_est = HOME / ".claude" / "skills" / ("cw-estilo-" + (estilo or {}).get("slug", ""))
    if skill_est.is_dir() and (estilo or {}).get("overrides"):   # versão do estilo com arquivos trocados: cópia própria da skill
        copia = BASE / "estilos" / "skill-{}-v{}".format(estilo["slug"], estilo.get("versao") or 0)
        if copia.exists():
            subprocess.run(["chmod", "-R", "u+w", str(copia)])
        else:
            shutil.copytree(skill_est, copia, symlinks=True)
        aplicar_overrides(copia, estilo["overrides"])
        subprocess.run(["chmod", "-R", "a-w", str(copia)])   # quem produz usa o estilo como está: mudar o estilo é pela Oficina
        skill_est = copia
    rec = ((estilo or {}).get("receita") or "").strip()
    if rec and skill_est.is_dir():
        rec = "A pasta desta skill (\"a pasta desta skill\" na receita) é {}. Use {} como TRAB.\n\n{}".format(skill_est, pasta, rec)
    texto = claude(PRODUCAO_REEL.format(cid=cid, titulo=c["titulo"], bruto=base / "bruto", material=base / "material.txt",
                                        transc=base / "transcricao.json", transc_txt=base / "transcricao.txt",
                                        ajuste=("AJUSTE PEDIDO PELO EDITOR (o reel já foi feito e aprovado em todo o resto; parta do que existe na pasta de "
                                                "trabalho e mude SÓ estes itens, no trecho dito; o que não está na lista fica IGUAL):\n"
                                                + texto_itens(itens_ajuste(c["ajuste"])) + "\n" + texto_prints(prints)) if c.get("ajuste") else "",
                                        higgs="Higgsfield liberado: até {} geração(ões) (ferramenta cw-studios › gerar).".format(higgs) if higgs else "Higgsfield: não use.",
                                        pasta=pasta, biblioteca=BIBLIOTECA_CW, legendas=AQUI / "legendas.py",
                                        receita=rec or "Reel vertical 1080x1920 no padrão Reels-CW-01 da Cardápio Web."),
                   pasta, "sonnet", ao_passo=passo, limite=5400, extra_dirs=[base / "bruto", BIBLIOTECA_CW] + ([skill_est] if skill_est.is_dir() else []), higgs=higgs, higgs_lote=lote["id"])
    r = json_final(texto)
    final = Path(r.get("final") or "")
    if final and not final.is_absolute():
        final = pasta / final
    if not final.is_file():
        srv("corte", id=c["id"], estado="reprovado", erro=(r.get("motivo") or "sem vídeo final")[:500])
        return
    previa = Path(r.get("previa") or final)
    if not previa.is_absolute():
        previa = pasta / previa
    if c.get("ajuste"):
        registrar_conferencia(c, conferir_ajuste(c, final, pasta, prints, passo), cid)
    # o vídeo existe: vai para a prévia mesmo com avisos da conferência automática (quem decide é uma pessoa)
    entregar_previa(c, final, previa if previa.is_file() else final, cid, r.get("legenda_post"), pasta,
                    aviso=None if r.get("aprovado") else (r.get("motivo") or "a conferência automática apontou problemas"))


def entregar_previa(c, final, previa, cid, legenda, pasta, aviso=None):
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
    cred = next(Path(final).parent.parent.glob("work/{}/creditos.txt".format(cid)), None)   # crédito das fotos livres (CC BY) vai no post
    cred = cred.read_text(encoding="utf-8").strip() if cred else ""
    (pasta / "legenda_post.txt").write_text((legenda or c["titulo"]) + ("\n\n" + cred if cred and cred not in (legenda or "") else ""), encoding="utf-8")
    versao = guardar_versao(Path(final), cid) if Path(final).parent.name == "saida" else None   # o time pode voltar para esta versão
    anterior = {"previa_anterior": c.get("previa"), "arquivo_anterior": c.get("arquivo")} if c.get("ajuste") and c.get("previa") else {}
    srv("corte", id=c["id"], estado="previa", etapa=("esperando aprovação · ⚠ conferência: " + aviso)[:480] if aviso else "esperando aprovação",
        arquivo=str(versao or final), previa=caminho,
        final_drive=str((PREVIAS_DRIVE / nome).relative_to(DRIVE)), **anterior)
    log("corte", c["id"], "em prévia")
    avisar_video_pronto(c, final, cid)


def avisar_video_pronto(c, final, cid):
    """Agentes com gatilho "quando um vídeo fica pronto" (ex.: a Capitu, das capas) recebem o vídeo, o título e a headline."""
    headline, bruto, trechos = "", "", []
    try:   # do plano do corte: a headline e o vídeo bruto (horizontal, sem legenda nem logo gravados) com os trechos usados
        plano = json.loads((Path(final).parent.parent / "work" / cid / "plan.json").read_text(encoding="utf-8"))
        headline = (plano.get("headline") or "").replace("|", " ").strip()
        bruto = plano.get("source") or ""
        tr = json.loads((Path(final).parent.parent / "work" / cid / "transcript.json").read_text(encoding="utf-8"))
        trechos = [[round(float(a), 2), round(float(b), 2)] for a, b in tr.get("ranges") or []]
    except (OSError, ValueError, TypeError):
        pass
    try:
        srv("tarefas_video", chave="{}:{}".format(c["id"], Path(final).name + str(int(Path(final).stat().st_mtime))), entrada={
            "texto": "Vídeo pronto: \"{}\"{}.".format(c["titulo"], " (headline do vídeo: \"{}\")".format(headline) if headline else ""),
            "local": str(final), "titulo": c["titulo"], "headline": headline, "corte_id": c["id"],
            "bruto": bruto, "trechos_do_bruto": trechos})
    except Exception as e:
        log("aviso de vídeo pronto:", e)


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
            try:
                produzir(item)
            except Exception:   # nunca deixa o operário morrer calado (o corte voltaria só no próximo reinício)
                log("operário", n, "pegou erro fora do corte:", traceback.format_exc()[-600:])
                time.sleep(10)
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
            try:
                entregar(aprovado)
            except Exception:
                log("entregador pegou erro:", traceback.format_exc()[-600:])
                time.sleep(15)
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
   posições). Mude só o necessário para atender o pedido. Mantenha as seções e o jeito de produzir (comandos e scripts).
   Se o pedido é sobre COMO o vídeo é cortado ou montado (pausas, repetições, ritmo), ajuste os scripts que já existem em skill/
   (ex.: limites de pausa, detecção de repetição), mantendo os mesmos comandos e saídas; confira com "python3 -m py_compile" e,
   se der, rode o script num material de teste. Não crie scripts novos nem dependa de programa que não está instalado.
   Use só recursos que o pacote já tem (fontes, sons, presets); se a referência pede algo que não existe aqui, use o mais
   próximo e diga nas notas.
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
                srv("presos")   # corte parado há 2 h e meia volta para a fila (3 vezes = erro)
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
