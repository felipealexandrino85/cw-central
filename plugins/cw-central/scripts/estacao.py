#!/usr/bin/env python3
"""Estação do Escritório virtual: o editor escreve na página da central e o Claude da máquina dele executa.

Só biblioteca padrão. Regras (as mesmas do banco e da página):
  - só o próprio editor manda prompt e aprova pedidos de OK (a gestão só acompanha);
  - o Claude trabalha sozinho só dentro da pasta do trabalho (ler, gravar, ffmpeg, Python, scripts do kit);
    qualquer outra coisa para e pede OK ao editor na página (a "cerca", hook PreToolUse que vale mais que as
    permissões do Claude do editor). É uma cerca de boa-fé para o dia a dia, não uma caixa de areia: um script
    Python rodado na pasta ainda pode tocar o resto do computador.

  ligar        instala e liga a estação (serviço do macOS que sobe sozinho ao entrar no computador)
  desligar     desliga e remove o serviço
  status       mostra se está ligada e o fim do registro
  rodar        roda em primeiro plano (é o que o serviço chama)
  porteiro     hook PreToolUse da cerca (chamado pelo Claude, não à mão)
"""
import argparse
import fcntl
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import central  # noqa: E402

VERSAO = "1"
HOME = Path.home()
HUB = Path(os.environ.get("CW_CORTES_HOME", HOME / ".cw-cortes"))
CASA = HUB / "estacao"                       # cópia estável dos scripts (o caminho do plugin muda a cada versão)
CONFIG = CASA / "config.json"
LOG = CASA / "estacao.log"
CERCA = CASA / "cerca.json"
ROTULO = "com.cwstudios.estacao"
PLIST = HOME / "Library" / "LaunchAgents" / (ROTULO + ".plist")
ESPERA_OK_S = 30 * 60
TRABALHO = Path(os.path.expanduser(os.environ.get("CW_TRABALHO") or "~/CW-Work"))


def log(*partes):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *partes, flush=True)


# ----------------------------------------------------------------------------------------------- cerca (porteiro)
def _temp():
    t = [Path("/tmp"), Path("/private/tmp"), Path("/var/folders"), Path("/private/var/folders")]
    if os.environ.get("TMPDIR"):
        t.append(Path(os.environ["TMPDIR"]))
    return t


def zonas_leitura():
    return [HOME / ".claude", TRABALHO, HOME / "Downloads", HOME / "Movies", HOME / "Library" / "CloudStorage", CASA]


SENSIVEL = re.compile(r"(^|/)(\.ssh|\.aws|\.gnupg|\.config/gh|Keychains|\.netrc|\.env[^/]*|supabase-token|"
                      r"[^/]*credential[^/]*|central\.json)(/|$)", re.I)


def _resolver(caminho, base):
    try:
        p = Path(os.path.expanduser(str(caminho)))
        return (p if p.is_absolute() else Path(base) / p).resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _dentro(p, raizes):
    if p is None:
        return False
    for r in raizes:
        try:
            r = r.resolve()
        except OSError:
            continue
        if p == r or r in p.parents:
            return True
    return False


def pode_gravar(p, pasta):
    return p is not None and (str(p) == "/dev/null" or _dentro(p, [Path(pasta)] + _temp()))


def pode_ler(p, pasta):
    if p is None:
        return False
    if SENSIVEL.search(str(p)) and not _dentro(p, [Path(pasta)]):
        return False
    return pode_gravar(p, pasta) or _dentro(p, zonas_leitura())


LIVRES = {"Skill", "Agent", "Task", "TodoWrite", "TodoRead", "ToolSearch", "WebSearch", "WebFetch", "BashOutput",
          "KillShell", "KillBash", "TaskOutput", "TaskStop", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
          "ExitPlanMode", "EnterPlanMode", "AskUserQuestion", "ListMcpResourcesTool", "ReadMcpResourceTool"}
LEITURA = {"Read", "NotebookRead", "LS"}
BUSCA = {"Glob", "Grep"}
ESCRITA = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# comandos de terminal liberados dentro da pasta (programas de vídeo, Python e utilitários de arquivo)
CMD_LER = {"ls", "cat", "head", "tail", "wc", "grep", "egrep", "find", "sort", "uniq", "cut", "tr", "awk", "jq", "du",
           "stat", "file", "basename", "dirname", "realpath", "date", "sleep", "cd", "pwd", "test", "[", "true",
           "false", "echo", "printf", "diff", "cmp", "seq", "nl", "column", "md5", "shasum", "bc", "expr", "which",
           "type", "mediainfo", "ffprobe", "exiftool", "python3", "python", "whisper", "open"}
CMD_GRAVA = {"mkdir", "cp", "mv", "rm", "touch", "tee", "sed", "ffmpeg", "magick", "convert", "sox", "unzip", "zip",
             "tar", "sips", "afconvert", "ln", "rmdir"}
SEPARA = {";", "&&", "||", "|", "&", "(", ")", "\n", "{", "}", "$(", "`", ";;", "|&"}
REDIRECIONA = {">", ">>", "&>", ">|", "2>", "2>>", "&>>"}


def _sem_heredoc(cmd):
    """Tira o corpo dos heredocs (python3 - <<'EOF' ... EOF): o corpo é o programa, não comandos de terminal."""
    return re.sub(r"<<-?\s*['\"]?(\w+)['\"]?([^\n]*)\n.*?\n\s*\1\s*(?=\n|$)", r"\2", cmd, flags=re.S)


def avaliar_bash(cmd, pasta):
    """Devolve None se liberado, ou o motivo para pedir OK."""
    texto = _sem_heredoc(cmd).replace("$(", " $( ").replace("`", " ` ")
    try:
        lex = shlex.shlex(texto, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:
        return "comando que não consegui ler com segurança"
    base = Path(pasta)
    inicio, programa, ler_proximo, gravar_proximo, args = True, None, False, False, []
    pular = False   # lista do "for x in ..." / "case x in": não é comando

    def fechar():
        if programa is None:
            return None
        nome = os.path.basename(programa)
        if nome not in CMD_LER and nome not in CMD_GRAVA:
            return "rodar '{}' (fora dos programas liberados)".format(nome)
        if nome == "sed" and not any(a.startswith("-i") for a in args):
            gravar = []
        elif nome == "ffmpeg":
            gravar = [a for i, a in enumerate(args) if not (i and args[i - 1] == "-i")]
        elif nome == "cp":
            gravar = [a for a in args if not a.startswith("-")][-1:]
        elif nome in CMD_GRAVA:
            gravar = args
        else:
            gravar = []
        if nome == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
            gravar = args
        for a in args:
            if "$" in a and (a in gravar or "/" in a):
                return "{} com caminho em variável ({}), que não dá para conferir".format(nome, a)
            for parte in ([a.split("=", 1)[1]] if a.startswith("-") and "=" in a else [a]):
                if not (parte.startswith(("/", "~", "./", "../")) or "/" in parte):
                    continue
                if re.match(r"^[a-z]+://", parte):
                    continue
                p = _resolver(parte, base)
                if a in gravar and not pode_gravar(p, pasta):
                    return "mexer em {} (fora da pasta do trabalho)".format(parte)
                if not pode_ler(p, pasta):
                    return "usar {} (fora da pasta do trabalho)".format(parte)
        if nome == "cd" and args:
            p = _resolver(args[0], base)
            if not pode_ler(p, pasta):
                return "ir para {} (fora da pasta do trabalho)".format(args[0])
        return None

    for t in toks:
        if gravar_proximo:
            gravar_proximo = False
            p = _resolver(t, base)
            if "$" in t or (not t.startswith("&") and not pode_gravar(p, pasta)):
                return "gravar em {} (fora da pasta do trabalho)".format(t)
            continue
        if t in REDIRECIONA or (t.endswith(">") and set(t) <= set("&>0123456789|")):
            gravar_proximo = True
            continue
        if t in ("<", "<<", "<<<"):
            ler_proximo = t == "<"
            continue
        if t in SEPARA or t == "$" or set(t) <= set(";&|()"):
            if pular:
                pular = False
                inicio, programa, args = True, None, []
                continue
            m = fechar()
            if m:
                return m
            inicio, programa, args = True, None, []
            continue
        if pular:
            if not pode_ler(_resolver(t, base), pasta) and ("/" in t or t.startswith("~")):
                return "usar {} (fora da pasta do trabalho)".format(t)
            continue
        if ler_proximo:
            ler_proximo = False
            if not pode_ler(_resolver(t, base), pasta):
                return "ler {} (fora da pasta do trabalho)".format(t)
            continue
        if inicio:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):       # VAR=valor antes do comando
                continue
            if t in ("sudo", "doas", "su"):
                return "usar {} (permissão de administrador)".format(t)
            if t in ("time", "nice", "nohup", "command", "do", "then", "else", "elif", "fi", "done", "esac", "!"):
                continue
            if t in ("for", "case", "select"):
                pular = True
                continue
            if t in ("while", "until", "if"):
                continue
            programa, inicio = t, False
            continue
        args.append(t)
    return fechar()


def decidir(ferramenta, entrada, pasta):
    """('allow', None) ou ('ask', motivo)."""
    entrada = entrada or {}
    if ferramenta in LIVRES:
        return "allow", None
    if ferramenta in LEITURA:
        alvo = entrada.get("file_path") or entrada.get("notebook_path") or entrada.get("path") or pasta
        return ("allow", None) if pode_ler(_resolver(alvo, pasta), pasta) else ("ask", "ler {}".format(alvo))
    if ferramenta in BUSCA:
        alvo = entrada.get("path") or pasta
        return ("allow", None) if pode_ler(_resolver(alvo, pasta), pasta) else ("ask", "procurar em {}".format(alvo))
    if ferramenta in ESCRITA:
        alvo = entrada.get("file_path") or entrada.get("notebook_path") or ""
        return ("allow", None) if pode_gravar(_resolver(alvo, pasta), pasta) else ("ask", "gravar {}".format(alvo))
    if ferramenta == "Bash":
        motivo = avaliar_bash(entrada.get("command") or "", pasta)
        return ("allow", None) if motivo is None else ("ask", motivo)
    if ferramenta.startswith("mcp__") and "cw-studios__" in ferramenta:   # ferramentas da própria CW Studios
        if ferramenta.endswith(("__listar", "__status", "__baixar")):
            return "allow", None
        return "ask", "gerar com a conta da empresa ({}; conta no limite do mês)".format((entrada or {}).get("ferramenta") or "API")
    if ferramenta.startswith("mcp__"):
        acao = ferramenta.split("__")[-1].lower()
        if re.search(r"(^|_)(get|list|search|read|fetch|show|query|describe|balance|status|find|lookup|explore|wait|"
                     r"transactions|info)(_|$)", acao):
            return "allow", None
        return "ask", "usar {} (conector externo; pode ter custo)".format(nome_conector(ferramenta))
    return "ask", "usar a ferramenta {}".format(ferramenta)


def nome_conector(ferramenta):
    return re.sub(r"^(claude_ai_|plugin_[^_]+_)", "", ferramenta.split("__", 1)[1]).replace("__", " › ").replace("_", " ", 0)


def resumo_ferramenta(nome, ent):
    ent = ent or {}
    if nome == "Bash":
        return ent.get("description") or (ent.get("command") or "")[:200]
    for k in ("file_path", "notebook_path", "path"):
        if ent.get(k):
            return "{} {}".format({"Read": "lendo", "Write": "gravando", "Edit": "editando", "MultiEdit": "editando"}
                                  .get(nome, nome), Path(str(ent[k])).name or ent[k])
    if nome == "Skill":
        return "skill " + str(ent.get("skill") or ent.get("command") or "")
    if nome in ("Agent", "Task"):
        return "agente {}: {}".format(ent.get("subagent_type") or "", ent.get("description") or "").strip(": ")
    if nome in ("Glob", "Grep"):
        return "procurando " + str(ent.get("pattern") or "")
    if nome == "TodoWrite":
        return "atualizando a lista de tarefas"
    return (nome_conector(nome) if nome.startswith("mcp__") else nome) + " " + \
        json.dumps(ent, ensure_ascii=False)[:140]


def cmd_porteiro(_a):
    """Hook PreToolUse. Sem CW_ESC_CONVERSA (sessão normal do editor) não opina."""
    conversa, pasta = os.environ.get("CW_ESC_CONVERSA"), os.environ.get("CW_ESC_PASTA")
    if not conversa or not pasta:
        return
    try:
        dado = json.load(sys.stdin)
    except ValueError:
        dado = {}
    ferramenta, entrada = dado.get("tool_name") or "", dado.get("tool_input") or {}
    try:
        decisao, motivo = decidir(ferramenta, entrada, pasta)
    except Exception as e:  # na dúvida, pergunta
        decisao, motivo = "ask", "ação que a cerca não soube avaliar ({})".format(e)
    razao = "dentro da pasta do trabalho"
    if decisao == "ask":
        decisao, razao = pedir_ok(int(conversa), ferramenta, entrada, motivo)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decisao,
                                             "permissionDecisionReason": razao}}, ensure_ascii=False))


def pedir_ok(conversa, ferramenta, entrada, motivo):
    detalhe = entrada.get("command") or entrada.get("file_path") or json.dumps(entrada, ensure_ascii=False)
    try:
        mid = central.rpc("cw_esc_pedir_ok", timeout=10, p_conversa=conversa, p_texto="Posso " + motivo + "?",
                          p_dados={"ferramenta": ferramenta, "detalhe": str(detalhe)[:1500]})
    except central.ErroCentral as e:
        return "deny", "Não consegui pedir OK ao editor na central ({}). Não faça isso; explique o que precisava.".format(e)
    limite = time.time() + ESPERA_OK_S
    while True:
        time.sleep(2)
        try:
            estado = central.rpc("cw_esc_ok", timeout=10, p_msg=mid, p_expirar=time.time() > limite)
        except central.ErroCentral:
            if time.time() > limite:
                return "deny", "Sem resposta do editor."
            continue
        if estado == "aprovado":
            return "allow", "O editor aprovou pela central."
        if estado in ("negado", "expirado"):
            return "deny", ("O editor NÃO aprovou: " if estado == "negado" else "O editor não respondeu em 30 min: ") + \
                motivo + ". Não tente outro caminho para fazer a mesma coisa; explique e pergunte o que ele prefere."


# ----------------------------------------------------------------------------------------------- execução
SISTEMA = """Você está rodando na estação do editor {nome}, pelo Escritório da Central CW Studios. Quem escreve é o próprio editor, pela página da central (ele não vê o terminal).
Pasta do trabalho: {pasta}{trabalho}
Dentro dessa pasta você age sozinho: ler e gravar arquivos, rodar ffmpeg, Python e os scripts do kit e dos plugins. Qualquer outra coisa (instalar programa, apagar ou gravar fora da pasta, geração paga, conectores externos) passa por um pedido de OK ao editor na página, que pode demorar. Se o OK for negado, não procure outro caminho para fazer a mesma coisa: explique e pergunte.
Material bruto costuma estar em ~/Downloads, na pasta do Google Drive ou em ~/CW-Work/preparo/<CHAVE>; copie para a pasta do trabalho o que for editar.
Responda em português, curto, dizendo o que fez, onde ficou o arquivo e o que falta."""


def resolver_pasta(pedido):
    if pedido.get("pasta"):
        alvo = _resolver(pedido["pasta"], HOME)
        if alvo and HOME in alvo.parents and len(alvo.relative_to(HOME).parts) >= 2 and \
                not any(p.startswith(".") for p in alvo.relative_to(HOME).parts):
            alvo.mkdir(parents=True, exist_ok=True)
            return alvo
    chave = re.sub(r"[^A-Za-z0-9_-]+", "-", pedido.get("trabalho") or "conversa-{}".format(pedido["conversa"])).strip("-")
    preparo = TRABALHO / "preparo" / chave
    alvo = preparo if preparo.is_dir() else TRABALHO / "escritorio" / chave
    alvo.mkdir(parents=True, exist_ok=True)
    return alvo


def _cfg():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def achar_claude():
    for c in [shutil.which("claude"), HOME / ".local/bin/claude", HOME / ".claude/local/claude", "/opt/homebrew/bin/claude",
              "/usr/local/bin/claude"]:
        if c and Path(c).exists():
            return str(c)
    return None


class Execucao:
    def __init__(self, pedido, nome):
        self.pedido, self.nome, self.conversa = pedido, nome, pedido["conversa"]
        self.proc, self.parado = None, False
        self.thread = threading.Thread(target=self._rodar, daemon=True)
        self.thread.start()

    def registrar(self, msgs=None, estado=None, sessao=None, pasta=None):
        try:
            central.rpc("cw_esc_registrar", timeout=15, p_conversa=self.conversa, p_msgs=msgs or [], p_estado=estado,
                        p_sessao=sessao, p_pasta=pasta)
        except central.ErroCentral as e:
            log("falha ao registrar:", e)

    def parar(self):
        self.parado = True
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass

    def _rodar(self):
        p = self.pedido
        try:
            pasta = resolver_pasta(p)
        except OSError as e:
            self.registrar([{"papel": "sistema", "texto": "Não consegui criar a pasta do trabalho: {}".format(e)}], "erro")
            return
        claude = _cfg().get("claude") or achar_claude()
        if not claude:
            self.registrar([{"papel": "sistema", "texto": "Não achei o Claude Code nesta máquina."}], "erro")
            return
        texto = p["texto"]
        if not p.get("sessao") and p.get("trabalho"):
            texto = "[{}] {}".format(p["trabalho"], texto)        # o plugin liga a edição ao card/demanda citado
        cmd = [claude, "-p", texto, "--output-format", "stream-json", "--verbose", "--permission-mode", "default",
               "--settings", str(CERCA), "--append-system-prompt",
               SISTEMA.format(nome=self.nome, pasta=pasta, trabalho=" · trabalho " + p["trabalho"] if p.get("trabalho") else "")]
        if p.get("sessao"):
            cmd += ["--resume", p["sessao"]]
        env = dict(os.environ, CW_ESC_CONVERSA=str(self.conversa), CW_ESC_PASTA=str(pasta), CW_ESC="1")
        self.registrar([], "rodando", pasta=str(pasta))
        log("conversa", self.conversa, "em", pasta)
        resultado, erro_txt = None, []
        try:
            self.proc = subprocess.Popen(cmd, cwd=str(pasta), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         env=env, start_new_session=True, bufsize=1)
            threading.Thread(target=lambda: erro_txt.extend(self.proc.stderr.read().splitlines()[-20:]), daemon=True).start()
            for linha in self.proc.stdout:
                try:
                    ev = json.loads(linha)
                except ValueError:
                    continue
                tipo = ev.get("type")
                if tipo == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
                    self.registrar([], None, sessao=ev["session_id"])
                elif tipo == "assistant":
                    msgs = []
                    for c in (ev.get("message") or {}).get("content") or []:
                        if c.get("type") == "text" and c.get("text", "").strip():
                            msgs.append({"papel": "ia", "texto": c["text"].strip()})
                        elif c.get("type") == "tool_use":
                            ent = c.get("input") or {}
                            msgs.append({"papel": "ferramenta", "texto": resumo_ferramenta(c.get("name", ""), ent),
                                         "dados": {"ferramenta": c.get("name"), "comando": (ent.get("command") or "")[:600] or None}})
                    if msgs:
                        self.registrar(msgs)
                elif tipo == "result":
                    resultado = ev
            self.proc.wait()
        except OSError as e:
            self.registrar([{"papel": "sistema", "texto": "Não consegui abrir o Claude: {}".format(e)}], "erro")
            return
        if self.parado:
            self.registrar([{"papel": "sistema", "texto": "Parado pelo editor."}], "parada")
        elif resultado and not resultado.get("is_error"):
            seg = round((resultado.get("duration_ms") or 0) / 1000)
            self.registrar([{"papel": "sistema", "texto": "Pronto em {}".format("{} min {} s".format(seg // 60, seg % 60) if seg >= 60 else "{} s".format(seg)),
                             "dados": {"segundos": seg, "custo_usd": resultado.get("total_cost_usd"), "turnos": resultado.get("num_turns")}}],
                           "parada", sessao=resultado.get("session_id"))
        else:
            detalhe = (resultado or {}).get("result") or "\n".join(erro_txt[-6:]) or "código {}".format(self.proc.returncode)
            self.registrar([{"papel": "sistema", "texto": "Deu erro na estação: " + str(detalhe)[:1500]}], "erro",
                           sessao=(resultado or {}).get("session_id"))
        log("conversa", self.conversa, "terminou", self.proc.returncode)


def cmd_rodar(_a):
    CASA.mkdir(parents=True, exist_ok=True)
    trava = open(CASA / "rodar.lock", "w")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("a estação já está rodando nesta máquina")
    copiar_scripts()
    escrever_cerca()
    maquina = socket.gethostname().split(".")[0]
    try:
        nome = central.rpc("cw_quem_sou", timeout=10).get("nome") or "editor"
    except Exception:
        nome = "editor"
    log("estação ligada:", nome, "em", maquina)
    meu = Path(__file__).resolve()
    marca = meu.stat().st_mtime
    atual, pausa = None, 3
    while True:
        if atual and not atual.thread.is_alive():
            atual = None
        if atual is None and meu.stat().st_mtime != marca:   # a abertura do plugin copiou uma versão nova
            log("versão nova da estação; reiniciando")
            os.execv(sys.executable, [sys.executable, str(meu), "rodar"])
        try:
            r = central.rpc("cw_esc_puxar", timeout=10, p_maquina=maquina, p_versao=VERSAO,
                            p_rodando=atual.conversa if atual else None) or {}
            pausa = 3
        except central.ErroCentral as e:
            log("central:", e)
            pausa = min(60, pausa * 2)
            time.sleep(pausa)
            continue
        for cid in r.get("parar") or []:
            if atual and atual.conversa == cid:
                atual.parar()
            else:
                try:
                    central.rpc("cw_esc_registrar", timeout=10, p_conversa=cid, p_msgs=[{"papel": "sistema", "texto": "Parado."}],
                                p_estado="parada", p_sessao=None, p_pasta=None)
                except central.ErroCentral:
                    pass
        if r.get("pedido") and atual is None:
            atual = Execucao(r["pedido"], nome)
        time.sleep(2 if atual else pausa)


# ----------------------------------------------------------------------------------------------- instalação
def escrever_cerca():
    CASA.mkdir(parents=True, exist_ok=True)
    cmd = "{} {} porteiro".format(shlex.quote(sys.executable), shlex.quote(str(CASA / "scripts" / "estacao.py")))
    CERCA.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": cmd, "timeout": ESPERA_OK_S + 120}]}]}}, indent=1))


def copiar_scripts():
    """Copia estacao.py, central.py e central.json do plugin para ~/.cw-cortes/estacao. Devolve True se mudou algo."""
    mudou = False
    (CASA / "scripts").mkdir(parents=True, exist_ok=True)
    for origem, destino in [(AQUI / "estacao.py", CASA / "scripts" / "estacao.py"),
                            (AQUI / "central.py", CASA / "scripts" / "central.py"),
                            (AQUI.parent / "central.json", CASA / "central.json")]:
        if origem.resolve() == destino.resolve() or not origem.exists():
            continue
        if not destino.exists() or destino.read_bytes() != origem.read_bytes():
            shutil.copy2(origem, destino)
            mudou = True
    return mudou


def ligada():
    return PLIST.exists()


def atualizar_se_ligada():
    """Chamado na abertura de cada sessão: mantém a cópia da estação na versão do plugin."""
    if ligada():
        copiar_scripts()


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def cmd_ligar(_a):
    if platform.system() != "Darwin":
        sys.exit("por enquanto a estação liga sozinha só no macOS; em outro sistema rode 'estacao.py rodar' num terminal aberto")
    if not central.configurada():
        sys.exit("conecte primeiro: central.py entrar --token cw_...")
    claude = achar_claude()
    if not claude:
        sys.exit("não achei o comando claude nesta máquina")
    copiar_scripts()
    escrever_cerca()
    CONFIG.write_text(json.dumps({"claude": claude}, indent=1))
    caminho = os.pathsep.join(dict.fromkeys([str(Path(claude).parent), "/opt/homebrew/bin", "/usr/local/bin",
                                             *os.environ.get("PATH", "").split(os.pathsep), "/usr/bin", "/bin"]))
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{rotulo}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{script}</string><string>rodar</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string><key>HOME</key><string>{home}</string>{extra}</dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
""".format(extra="".join("<key>{}</key><string>{}</string>".format(k, escape(os.environ[k]))
                          for k in ("CW_CORTES_HOME", "CW_TRABALHO") if os.environ.get(k)),
           rotulo=ROTULO, py=sys.executable, script=CASA / "scripts" / "estacao.py", path=escape(caminho), home=escape(str(HOME)), log=escape(str(LOG))))
    dominio = "gui/{}".format(os.getuid())
    _launchctl("bootout", dominio + "/" + ROTULO)
    r = _launchctl("bootstrap", dominio, str(PLIST))
    if r.returncode != 0:
        sys.exit("não consegui ligar o serviço: " + (r.stderr or r.stdout).strip())
    print("Estação ligada nesta máquina. Ela sobe sozinha quando você entra no computador.\n"
          "Agora é só abrir a central › Escritório e escrever o pedido. Registro: {}".format(LOG))


def cmd_desligar(_a):
    _launchctl("bootout", "gui/{}/{}".format(os.getuid(), ROTULO))
    PLIST.unlink(missing_ok=True)
    print("Estação desligada (o Escritório da central não manda mais nada para esta máquina).")


def cmd_status(_a):
    r = _launchctl("print", "gui/{}/{}".format(os.getuid(), ROTULO))
    print("Estação:", "ligada" if r.returncode == 0 else ("instalada mas parada" if ligada() else "desligada"))
    if LOG.exists():
        print("".join(LOG.read_text(errors="replace").splitlines(True)[-8:]))


def main():
    ap = argparse.ArgumentParser(description="Estação do Escritório virtual CW Studios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("ligar", "desligar", "status", "rodar", "porteiro"):
        sub.add_parser(c)
    a = ap.parse_args()
    if a.cmd == "rodar" and LOG.exists() and LOG.stat().st_size > 2_000_000:
        LOG.write_text("")
    {"ligar": cmd_ligar, "desligar": cmd_desligar, "status": cmd_status, "rodar": cmd_rodar, "porteiro": cmd_porteiro}[a.cmd](a)


if __name__ == "__main__":
    main()
