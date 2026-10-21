#!/usr/bin/env python3
"""Estação do Escritório virtual: o editor escreve na página da central e o Claude da máquina dele executa.

Só biblioteca padrão. Regras (as mesmas do banco e da página):
  - só o próprio editor manda prompt e aprova pedidos de OK (a gestão só acompanha);
  - o Claude trabalha sozinho só dentro da pasta do trabalho (ler, gravar, ffmpeg, Python, scripts do kit);
    qualquer outra coisa para e pede OK ao editor na página (a "cerca", hook PreToolUse que vale mais que as
    permissões do Claude do editor). Funciona no macOS (serviço do launchd) e no Windows (Inicialização). É uma cerca de boa-fé para o dia a dia, não uma caixa de areia: um script
    Python rodado na pasta ainda pode tocar o resto do computador.

  ligar        instala e liga a estação (serviço do macOS que sobe sozinho ao entrar no computador)
  desligar     desliga e remove o serviço
  status       mostra se está ligada e o fim do registro
  rodar        roda em primeiro plano (é o que o serviço chama)
  porteiro     hook PreToolUse da cerca (chamado pelo Claude, não à mão)
"""
import argparse
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
import tempfile
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
WINDOWS = os.name == "nt"
INICIAR_WIN = Path(os.environ.get("APPDATA", HOME)) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "cw-estacao.vbs"
NOMES_DRIVE = ("Shared drives", "Drives compartilhados", "My Drive", "Meu Drive")
SERVIDOR = os.environ.get("CW_SERVIDOR") == "1"   # estação no servidor da nuvem (um usuário Linux por editor)
TOKEN_CLAUDE = CASA / "claude-token"            # só no servidor: o token da conta Claude do editor (fica só aqui, 600)
TRABALHO = Path(os.path.expanduser(os.environ.get("CW_TRABALHO") or "~/CW-Work"))


def log(*partes):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *partes, flush=True)


# ----------------------------------------------------------------------------------------------- cerca (porteiro)
def _temp():
    t = [Path("/tmp"), Path("/private/tmp"), Path("/var/folders"), Path("/private/var/folders"), Path(tempfile.gettempdir())]
    for v in ("TMPDIR", "TEMP", "TMP"):
        if os.environ.get(v):
            t.append(Path(os.environ[v]))
    return t


def raizes_drive():
    """Onde o Google Drive para computador aparece: CloudStorage no Mac, uma letra (G:, H:...) no Windows."""
    if not WINDOWS:
        return [HOME / "Library" / "CloudStorage"]
    achadas = []
    for letra in "DEFGHIJKLMNOPQRSTUVWXYZ":
        raiz = Path(letra + ":/")
        try:
            if any((raiz / n).is_dir() for n in NOMES_DRIVE):
                achadas.append(raiz)
        except OSError:
            continue
    return achadas


def zonas_leitura():
    extras = [Path(x) for x in _cfg().get("drive_raizes") or []] or (raizes_drive() if not WINDOWS else [])
    liberadas = [Path(x) for x in _cfg().get("liberadas") or []]   # pastas que o editor liberou pela central
    if projeto_valido(_cfg().get("projeto")):
        liberadas.append(Path(_cfg()["projeto"]))                   # projeto do editor (agentes, skills e regras dele)
    return [HOME / ".claude", TRABALHO, HOME / "Downloads", HOME / "Movies", HOME / "Videos", CASA] + extras + liberadas


ALVO = {"pasta": None, "conector": None, "duro": False}
EDITORES_LOCAIS = re.compile(r"premiere|after.?effects|aftereffects|\bppro\b|adobe", re.I)
PERIGO_EDITOR = re.compile(r"export|render|encode|queue|save_as|close|quit|exit|delete_(project|item|bin|file|sequence)|"
                           r"remove_(item|bin|project|sequence)|relink|consolidate|trash|purge|script|eval|execute", re.I)   # última pasta de fora que pediu OK (para o botão "liberar esta pasta")


def _fora(parte, p, motivo):
    ALVO["pasta"] = p
    return motivo


def projeto_valido(caminho):
    """Pasta do projeto do editor: dentro da casa dele, existente, sem pasta oculta nem dado sensível."""
    if not caminho:
        return None
    try:
        p = Path(os.path.expanduser(str(caminho))).resolve()
        rel = p.relative_to(HOME)
    except (OSError, ValueError):
        return None
    if not p.is_dir() or not rel.parts or rel.parts[0] in ("Library", "AppData") or \
            any(x.startswith(".") for x in rel.parts) or SENSIVEL.search(str(p)):
        return None
    return p


def _cod(p):   # como o Claude nomeia a pasta de sessões de cada diretório (~/.claude/projects/<isto>)
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def pasta_de_abertura(pasta, sessao):
    """Abre o Claude no projeto do editor (para ele ter os agentes, skills e CLAUDE.md de sempre). Conversa que já
    começou continua no diretório em que nasceu (a sessão do Claude fica guardada por diretório)."""
    projeto = projeto_valido(_cfg().get("projeto"))
    if not projeto:
        return Path(pasta)
    if sessao:
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / "projects"
        for d in (projeto, Path(pasta)):
            if (base / _cod(d) / (sessao + ".jsonl")).exists():
                return d
    return projeto


def pasta_liberavel(p):
    """Pasta que o editor pode liberar de vez: dentro da casa dele, sem pastas ocultas nem dados sensíveis."""
    if p is None:
        return None
    alvo = p if p.is_dir() else p.parent
    try:
        rel = alvo.relative_to(HOME)
    except ValueError:
        return None
    if not rel.parts or rel.parts[0] in ("Library", "AppData") or any(x.startswith(".") for x in rel.parts) or SENSIVEL.search(str(alvo)):
        return None
    # libera a pasta do projeto (~/estudio-video), não cada subpasta; em pastas amplas (Documentos, Mesa) vai um nível abaixo
    amplas = {"Documents", "Documentos", "Desktop", "Mesa", "Pictures", "Music", "Public", "Applications", "OneDrive", "Dropbox"}
    n = 2 if rel.parts[0] in amplas else 1
    if len(rel.parts) < n:
        return None
    return HOME.joinpath(*rel.parts[:n])


SENSIVEL = re.compile(r"(^|[/\\])(\.ssh|\.aws|\.gnupg|\.config[/\\]gh|Keychains|\.netrc|\.env[^/\\]*|supabase-token|"
                      r"[^/\\]*credential[^/\\]*|central\.json)([/\\]|$)", re.I)


def _windows(caminho):
    """No Windows o terminal do Claude (Git Bash) escreve /c/Users/...: vira C:/Users/..."""
    m = re.match(r"^/([a-zA-Z])(/|$)(.*)$", caminho) if WINDOWS else None
    return "{}:/{}".format(m.group(1).upper(), m.group(3)) if m else caminho


def _resolver(caminho, base):
    try:
        p = Path(os.path.expanduser(_windows(str(caminho))))
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
    projeto = projeto_valido(_cfg().get("projeto"))   # as skills do editor gravam na estrutura do projeto dele (work/, lotes/...)
    return p is not None and (str(p) == "/dev/null" or _dentro(p, [Path(pasta)] + _temp() + ([projeto] if projeto else [])))


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
           "type", "mediainfo", "ffprobe", "exiftool", "python3", "python", "whisper", "open", "node"}
CMD_GRAVA = {"mkdir", "cp", "mv", "rm", "touch", "tee", "sed", "ffmpeg", "magick", "convert", "sox", "unzip", "zip",
             "tar", "sips", "afconvert", "ln", "rmdir"}
SEPARA = {";", "&&", "||", "|", "&", "(", ")", "\n", "{", "}", "$(", "`", ";;", "|&"}
# mesmo com "Aprovar tudo" ligado, estes continuam pedindo OK: mandam coisa para fora, instalam ou mexem no sistema
DUROS = {"curl", "wget", "ssh", "scp", "sftp", "rsync", "nc", "ncat", "telnet", "ftp", "osascript", "security", "launchctl",
         "crontab", "dd", "diskutil", "shutdown", "reboot", "halt", "killall", "pkill", "git", "gh", "npm", "npx", "pnpm",
         "yarn", "pip", "pip3", "brew", "port", "winget", "choco", "scoop", "powershell", "pwsh", "reg", "chmod", "chown",
         "eval", "exec", "sh", "bash", "zsh", "fish", "defaults", "networksetup", "systemsetup", "csrutil", "spctl", "xattr"}
APAGA = {"rm", "rmdir", "mv", "ln", "unlink", "shred", "truncate"}
CODIGO = {"node": ("-e", "--eval", "-p", "--print"), "python3": ("-c",), "python": ("-c",)}
REDIRECIONA = {">", ">>", "&>", ">|", "2>", "2>>", "&>>"}


def _sem_heredoc(cmd):
    """Tira o corpo dos heredocs (python3 - <<'EOF' ... EOF): o corpo é o programa, não comandos de terminal."""
    return re.sub(r"<<-?\s*['\"]?(\w+)['\"]?([^\n]*)\n.*?\n\s*\1\s*(?=\n|$)", r"\2", cmd, flags=re.S)


def duro(motivo):
    """Motivo que pede OK mesmo com "Aprovar tudo" ligado."""
    ALVO["duro"] = True
    return motivo


def avaliar_bash(cmd, pasta):
    """Devolve None se liberado, ou o motivo para pedir OK."""
    oficina = _cfg().get("oficina")   # só na Oficina (estação da gestão): consultar é livre, publicar sempre pede OK
    if oficina and not re.search(r"[;&|`<>]|\$\(", cmd):
        simples = cmd.strip()
        for padrao in oficina.get("publicar") or []:
            if re.match(padrao, simples):
                return duro("publicar no sistema: " + simples[:200])
        for padrao in oficina.get("livre") or []:
            if re.match(padrao, simples):
                return None
    texto = _sem_heredoc(cmd).replace("$(", " $( ").replace("`", " ` ")
    try:
        lex = shlex.shlex(texto, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        toks = list(lex)
    except ValueError:
        return duro("comando que não consegui ler com segurança")
    base = Path(pasta)
    pilha = []      # subshell "( cd x && ... )": o cd vale só até fechar o parêntese
    inicio, programa, ler_proximo, gravar_proximo, args = True, None, False, False, []
    pular = False   # lista do "for x in ..." / "case x in": não é comando
    laco = {"HOME": [str(HOME)]}   # variáveis com valor conhecido: for x in 2.9 3.8, F=~/fontes/x.otf, $HOME
    lendo_laco = None

    def expandir(a):
        nomes = re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", a)
        if not nomes or "$(" in a or any(n not in laco for n in nomes):
            return None
        saida = [a]
        for n in dict.fromkeys(nomes):
            saida = [x.replace("${" + n + "}", v).replace("$" + n, v) for x in saida for v in laco[n]][:50]
        return saida

    def fechar():
        nonlocal base
        if programa is None:
            return None
        nome = os.path.basename(programa)
        if nome.lower().removesuffix(".exe") in DUROS:
            return duro("rodar '{}' (manda coisa para fora, instala ou mexe no sistema)".format(nome))
        if nome not in CMD_LER and nome not in CMD_GRAVA:
            return "rodar '{}' (fora dos programas liberados)".format(nome)
        apaga = nome in APAGA or (nome == "find" and "-delete" in args)
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
        for i0, a0 in enumerate(args):
          if nome in CODIGO and i0 and args[i0 - 1] in CODIGO[nome]:
              continue   # node -e '...' / python3 -c '...': é o programa, não caminho
          for a in (expandir(a0) or [a0]) if "$" in a0 else [a0]:
            if "$" in a and (a0 in gravar or "/" in a):
                msg = "{} com caminho em variável ({}), que não dá para conferir".format(nome, a)
                return duro(msg) if apaga else msg
            filtro = nome == "ffmpeg" and i0 and args[i0 - 1] in ("-vf", "-af", "-filter_complex", "-lavfi", "-filter:v", "-filter:a")
            so_ler = bool(filtro or (nome == "ffmpeg" and i0 and args[i0 - 1] == "-i" and ":" in a and "=" in a and not a.startswith(("/", "~", "."))))
            if so_ler:   # arquivos citados dentro do filtro (fonte, legenda) só são lidos
                partes = [v for k, v in re.findall(r"(?:^|[:,;=])\s*(fontfile|textfile|filename|subtitles|ass|file|f)\s*=\s*'?([^:,;']+)", a)]
                partes += [x for x in re.findall(r"(?:subtitles|ass)=([^:,;]+)", a)]
            else:
                partes = [a.split("=", 1)[1]] if a.startswith("-") and "=" in a else [a]
            for parte in partes:
                if not (parte.startswith(("/", "~", "./", "../")) or "/" in parte):
                    continue
                if re.match(r"^[a-z]+://", parte):
                    continue
                p = _resolver(parte, base)
                if p is not None and SENSIVEL.search(str(p)) and not _dentro(p, [Path(pasta)]):
                    return duro("mexer em {} (senha, chave ou credencial)".format(parte))
                if a0 in gravar and not so_ler and not pode_gravar(p, pasta):
                    msg = "{} {} (fora da pasta do trabalho)".format("apagar/mover" if apaga else "mexer em", parte)
                    return duro(msg) if apaga else msg
                if not pode_ler(p, pasta):
                    return _fora(parte, p, "usar {} (fora da pasta do trabalho)".format(parte))
        if nome == "cd":
            p = _resolver(args[0], base) if args else HOME
            if not pode_ler(p, pasta):
                return _fora(args[0] if args else "~", p, "ir para {} (fora da pasta do trabalho)".format(args[0] if args else "~"))
            base = p   # caminhos relativos seguintes partem daqui
        return None

    for t in toks:
        if gravar_proximo:
            gravar_proximo = False
            p = _resolver(t, base)
            if not t.startswith("&") and p is not None and SENSIVEL.search(str(p)):
                return duro("gravar em {} (senha, chave ou credencial)".format(t))
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
            lendo_laco = None
            fecha_sub = ")" in t
            if pular:
                pular = False
                inicio, programa, args = True, None, []
                continue
            m = fechar()
            if m:
                return m
            inicio, programa, args = True, None, []
            if t in ("(", "$("):
                pilha.append(base)
            elif fecha_sub and pilha:
                base = pilha.pop()
            continue
        if pular:
            if lendo_laco is True:            # nome da variável do for
                lendo_laco = t if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", t) else None
                if lendo_laco:
                    laco[lendo_laco] = []
                continue
            if lendo_laco and t != "in":
                if "$" in t or "`" in t or "*" in t or "?" in t:
                    laco.pop(lendo_laco, None)
                    lendo_laco = None
                elif lendo_laco in laco:
                    laco[lendo_laco].append(t)
            if not pode_ler(_resolver(t, base), pasta) and ("/" in t or t.startswith("~")):
                return "usar {} (fora da pasta do trabalho)".format(t)
            continue
        if ler_proximo:
            ler_proximo = False
            if not pode_ler(_resolver(t, base), pasta):
                return _fora(t, _resolver(t, base), "ler {} (fora da pasta do trabalho)".format(t))
            continue
        if inicio:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):       # VAR=valor: guarda o valor se der para conferir
                nome_v, valor = t.split("=", 1)
                if "$(" in valor or "`" in valor:
                    laco.pop(nome_v, None)
                else:
                    vals = expandir(valor) if "$" in valor else [valor]
                    if vals is None:
                        laco.pop(nome_v, None)
                    else:
                        laco[nome_v] = vals
                continue
            if t in ("sudo", "doas", "su", "runas"):
                return duro("usar {} (permissão de administrador)".format(t))
            if t in ("time", "nice", "nohup", "command", "do", "then", "else", "elif", "fi", "done", "esac", "!"):
                continue
            if t in ("for", "case", "select"):
                pular = True
                lendo_laco = True if t == "for" else None
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
    ALVO["pasta"], ALVO["conector"], ALVO["duro"] = None, None, False
    if ferramenta in LIVRES:
        return "allow", None
    if ferramenta in LEITURA:
        alvo = entrada.get("file_path") or entrada.get("notebook_path") or entrada.get("path") or pasta
        if pode_ler(_resolver(alvo, pasta), pasta):
            return "allow", None
        if SENSIVEL.search(str(alvo)):
            return "ask", duro("ler {} (senha, chave ou credencial)".format(alvo))
        return "ask", _fora(alvo, _resolver(alvo, pasta), "ler {}".format(alvo))
    if ferramenta in BUSCA:
        alvo = entrada.get("path") or pasta
        return ("allow", None) if pode_ler(_resolver(alvo, pasta), pasta) else ("ask", _fora(alvo, _resolver(alvo, pasta), "procurar em {}".format(alvo)))
    if ferramenta in ESCRITA:
        alvo = entrada.get("file_path") or entrada.get("notebook_path") or ""
        if pode_gravar(_resolver(alvo, pasta), pasta):
            return "allow", None
        return "ask", duro("gravar {} (senha, chave ou credencial)".format(alvo)) if SENSIVEL.search(str(alvo)) else "gravar {}".format(alvo)
    if ferramenta == "Bash":
        motivo = avaliar_bash(entrada.get("command") or "", pasta)
        return ("allow", None) if motivo is None else ("ask", motivo)
    if ferramenta.startswith("mcp__") and "cw-studios__" in ferramenta:   # ferramentas da própria CW Studios
        if ferramenta.endswith(("__listar", "__status", "__baixar", "__texto")):   # texto: modelo barato, centavos, com limite na central
            return "allow", None
        return "ask", duro("gerar com a conta da empresa ({}; conta no limite do mês)".format((entrada or {}).get("ferramenta") or "API"))
    if ferramenta.startswith("mcp__"):
        servidor, acao = ferramenta.split("__")[1], ferramenta.split("__")[-1].lower()
        if servidor in (_cfg().get("conectores_liberados") or []):
            return "allow", None
        if EDITORES_LOCAIS.search(servidor):   # Premiere/After Effects na máquina: editar a timeline é o trabalho
            if PERIGO_EDITOR.search(acao):
                return "ask", "{} no {} (exportar ou apagar arquivo do projeto)".format(acao.replace("_", " "), servidor)
            return "allow", None
        if re.search(r"(^|_)(get|list|search|read|fetch|show|query|describe|balance|status|find|lookup|explore|wait|"
                     r"transactions|info)(_|$)", acao):
            return "allow", None
        ALVO["conector"] = servidor
        local = not servidor.startswith(("claude_ai_", "plugin_"))
        motivo = "usar {}{}".format(nome_conector(ferramenta), "" if local else " (conector externo; pode ter custo)")
        return "ask", motivo if local else duro(motivo)
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
    dados = {"ferramenta": ferramenta, "detalhe": str(detalhe)[:1500], "livre": not ALVO["duro"]}
    liberavel = pasta_liberavel(ALVO["pasta"])
    if liberavel:
        dados["liberar"] = str(liberavel)
    elif ALVO["conector"]:
        dados["liberar_conector"] = ALVO["conector"]
    try:
        mid = central.rpc("cw_esc_pedir_ok", timeout=10, p_conversa=conversa, p_texto="Posso " + motivo + "?", p_dados=dados)
    except central.ErroCentral as e:
        return "deny", "Não consegui pedir OK ao editor na central ({}). Não faça isso; explique o que precisava.".format(e)
    limite, primeira = time.time() + ESPERA_OK_S, True
    while True:
        if not primeira:
            time.sleep(2)
        primeira = False
        try:
            estado = central.rpc("cw_esc_ok", timeout=10, p_msg=mid, p_expirar=time.time() > limite)
        except central.ErroCentral:
            if time.time() > limite:
                return "deny", "Sem resposta do editor."
            continue
        if estado == "aprovado":
            try:
                liberada = central.rpc("cw_esc_ok_sempre", timeout=10, p_msg=mid)
            except central.ErroCentral:
                liberada = None
            if liberada and liberada.startswith("conector:"):
                cfg = _cfg()
                cfg["conectores_liberados"] = sorted(set(cfg.get("conectores_liberados") or []) | {liberada[9:]})
                CONFIG.write_text(json.dumps(cfg, indent=1))
                return "allow", "O editor aprovou e liberou o conector {} para as próximas vezes.".format(liberada[9:])
            if liberada and pasta_liberavel(Path(liberada)) == Path(liberada):
                cfg = _cfg()
                cfg["liberadas"] = sorted(set(cfg.get("liberadas") or []) | {liberada})
                CONFIG.write_text(json.dumps(cfg, indent=1))
                return "allow", "O editor aprovou e liberou {} para as próximas vezes (só leitura e rodar; gravar fora da pasta continua pedindo OK).".format(liberada)
            return "allow", "O editor aprovou pela central."
        if estado in ("negado", "expirado"):
            return "deny", ("O editor NÃO aprovou: " if estado == "negado" else "O editor não respondeu em 30 min: ") + \
                motivo + ". Não tente outro caminho para fazer a mesma coisa; explique e pergunte o que ele prefere."


# ----------------------------------------------------------------------------------------------- execução
SISTEMA = """Você está rodando na estação do editor {nome}: é o mesmo Claude dele, só que comandado pela página da Central CW Studios (ele escreve pela página e não vê o terminal).
Trabalhe exatamente como ele trabalha no Claude normal: siga as skills, os agentes, o CLAUDE.md e as regras do time, com as pastas, os scripts e o formato de resposta que eles definem. Não simplifique nem pule etapas por estar na estação.
Você grava sem pedir OK na pasta do trabalho ({pasta}){projeto}, nas pastas que o editor liberou e nas temporárias.{trabalho} Fora disso (instalar programa, apagar fora dessas pastas, geração paga, conectores externos) o pedido de OK aparece para o editor na página e pode demorar; se for negado, não procure outro caminho para fazer a mesma coisa: explique e pergunte.
IMPORTANTE nesta estação: quando você termina a resposta, o processo fecha e qualquer agente ou comando em segundo plano morre sem gravar nada. Então rode agentes (Agent/Task) e comandos sempre em primeiro plano (run_in_background: false), espere TODOS terminarem e só depois responda. Se a skill pede agentes em paralelo, chame-os na mesma mensagem em primeiro plano e espere os resultados. Nunca encerre dizendo "estão rodando, quando terminarem eu sigo"."""


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


ROBO = """Você é {nome}, um agente do time CW Studios (Cardápio Web). Conversa pela página da Central e roda no servidor da empresa.
Missão: {missao}
Instruções de quem te criou:
{instrucoes}
Suas skills (já instaladas; use-as sempre que o pedido combinar, seguindo o que elas mandam): {skills}.
Ferramentas pagas da empresa que você pode usar (conector cw-studios; cada geração paga pede OK): {ferramentas}.
Você grava sem pedir OK na pasta desta conversa ({pasta}); o Drive compartilhado fica em ~/Drive (ler é livre, gravar pede OK).
Rode agentes e comandos sempre em primeiro plano. Responda em português do Brasil, direto e curto, como um colega de time."""


def _cfg():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def achar_claude():
    import glob as _glob
    candidatos = [HOME / ".local/bin/claude.exe" if WINDOWS else None, shutil.which("claude"), HOME / ".local/bin/claude",
                  HOME / ".claude/local/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude", HOME / ".npm-global/bin/claude",
                  HOME / ".bun/bin/claude", HOME / ".volta/bin/claude"]
    candidatos += sorted(_glob.glob(str(HOME / ".nvm/versions/node/*/bin/claude")), reverse=True)
    candidatos += sorted(_glob.glob(str(HOME / "Library/Application Support/Claude/claude-code/*/claude")), reverse=True)   # o do app Claude (Mac)
    if WINDOWS:
        candidatos += [Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"]
        candidatos += sorted(_glob.glob(os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "claude.exe")), reverse=True)
    for c in candidatos:
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
                if WINDOWS:   # encerra o Claude e tudo que ele abriu
                    subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
                else:
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
        claude = _cfg().get("claude")
        if not claude or not Path(claude).exists():   # o Claude foi reinstalado em outro lugar: procura de novo e guarda
            claude = achar_claude()
            if claude:
                cfg = _cfg()
                cfg["claude"] = claude
                CONFIG.write_text(json.dumps(cfg, indent=1))
        if not claude:
            self.registrar([{"papel": "sistema", "texto": "Não achei o Claude Code nesta máquina."}], "erro")
            return
        texto = p["texto"]
        if not p.get("sessao") and p.get("trabalho"):
            texto = "[{}] {}".format(p["trabalho"], texto)        # o plugin liga a edição ao card/demanda citado
        abrir = pasta_de_abertura(pasta, p.get("sessao"))
        sistema = SISTEMA.format(nome=self.nome, pasta=pasta, trabalho=" Trabalho: " + p["trabalho"] + "." if p.get("trabalho") else "",
                                 projeto=", na pasta do projeto ({})".format(abrir) if abrir != Path(pasta) else "")
        if p.get("agente"):   # conversa com um robô da aba Agentes: ele veste a persona e usa as skills escolhidas
            ag = p["agente"]
            sistema = ROBO.format(nome=ag.get("nome") or "Agente", missao=ag.get("missao") or "ajudar o time",
                                  instrucoes=(ag.get("instrucoes") or "").strip() or "(sem instruções extras)",
                                  skills=", ".join(x for x in ag.get("skills") or [] if x) or "nenhuma específica",
                                  ferramentas=", ".join(ag.get("ferramentas") or []) or "nenhuma", pasta=pasta)
        cmd = [claude, "-p", texto, "--output-format", "stream-json", "--verbose", "--permission-mode", "default",
               "--settings", str(CERCA), "--add-dir", str(pasta), "--append-system-prompt", sistema]
        try:   # skills criadas no app Claude (sincronizadas com a conta) não carregam sozinhas no Claude da estação
            import capacidades
            app = capacidades.pasta_skills_app()
            if app:
                cmd += ["--add-dir", str(app)]
        except Exception:
            capacidades = None
        if p.get("sessao"):
            cmd += ["--resume", p["sessao"]]
        if p.get("modelo") in ("sonnet", "haiku", "opus"):   # escolhido na conversa (economia: sonnet/haiku gastam bem menos do limite)
            cmd += ["--model", p["modelo"]]
        env = dict(os.environ, CW_ESC_CONVERSA=str(self.conversa), CW_ESC_PASTA=str(pasta), CW_ESC="1",
                   CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1")   # sem tarefas em segundo plano: o processo fecha ao responder
        if SERVIDOR:   # no servidor o Claude roda com a conta do próprio editor
            if not TOKEN_CLAUDE.exists():
                self.registrar([{"papel": "sistema", "texto": "Os robôs ainda não têm conta Claude no servidor: a gestão liga em Agentes › "
                                 "☁️ Ligar a conta Claude dos robôs. Depois mande de novo." if p.get("agente") else
                                 "Sua conta Claude ainda não está ligada no servidor. Na página, abra a estação "
                                 "e clique em ☁️ Ligar minha conta Claude no servidor; depois mande o pedido de novo."}], "erro")
                return
            env["CLAUDE_CODE_OAUTH_TOKEN"] = TOKEN_CLAUDE.read_text().strip()
        self.registrar([], "rodando", pasta=str(pasta))
        log("conversa", self.conversa, "em", pasta)
        resultado, erro_txt = None, []
        try:
            grupo = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} if WINDOWS   # sem janela
                     else {"start_new_session": True})
            self.proc = subprocess.Popen(cmd, cwd=str(abrir), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         encoding="utf-8", errors="replace", env=env, bufsize=1, **grupo)
            threading.Thread(target=lambda: erro_txt.extend(self.proc.stderr.read().splitlines()[-20:]), daemon=True).start()
            for linha in self.proc.stdout:
                try:
                    ev = json.loads(linha)
                except ValueError:
                    continue
                tipo = ev.get("type")
                if tipo == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
                    info = "Claude {} · aberto em {}".format(ev.get("model") or "?", Path(ev.get("cwd") or str(abrir)).name)
                    if capacidades:
                        cap = capacidades.do_init(ev)
                        info += " · {} skills, {} agentes".format(len(cap["skills"]), len(cap["agentes"]))
                        ruins = [m["nome"] for m in cap["mcp"] if m["status"] in ("failed", "needs-auth")]
                        if ruins:
                            info += " · conector sem acesso: " + ", ".join(ruins[:4])
                        threading.Thread(target=capacidades.enviar, args=("estacao", cap), daemon=True).start()
                    self.registrar([{"papel": "sistema", "texto": info}], None, sessao=ev["session_id"])
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
            if re.search(r"not logged in|please run /login|invalid api key|oauth|failed to authenticate", str(detalhe), re.I):
                detalhe = ("o Claude de linha de comando deste computador ({}) não está logado (ou o login venceu / trocou de conta). O login do app é separado. "
                           "Para resolver: abra o {} neste computador, digite claude, depois "
                           "/login, entre com a conta que tem uso disponível e feche. Depois mande o pedido de novo aqui."
                           .format(claude, "PowerShell" if WINDOWS else "Terminal"))
            self.registrar([{"papel": "sistema", "texto": "Deu erro na estação: " + str(detalhe)[:1500]}], "erro",
                           sessao=(resultado or {}).get("session_id"))
        log("conversa", self.conversa, "terminou", self.proc.returncode)


def cmd_rodar(_a):
    CASA.mkdir(parents=True, exist_ok=True)
    trava = open(CASA / "rodar.lock", "w")
    try:
        if WINDOWS:
            import msvcrt
            msvcrt.locking(trava.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("a estação já está rodando nesta máquina")
    (CASA / "rodar.pid").write_text(str(os.getpid()))
    copiar_scripts()
    escrever_cerca()
    maquina = "servidor" if SERVIDOR else socket.gethostname().split(".")[0]
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
            if WINDOWS:   # o laço do rodar.cmd sobe a versão nova em seguida
                sys.exit(0)
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
        if SERVIDOR:
            conta_claude_no_servidor()
        time.sleep(2 if atual else pausa)


# ----------------------------------------------------------------------------------------------- conta Claude no servidor
_PAREAR = {"proc": None, "fd": None, "desde": 0, "visto": 0}


def _ler_pty(fd, segundos, ate=None):
    import select
    saida, fim = "", time.time() + segundos
    while time.time() < fim:
        pronto, _, _ = select.select([fd], [], [], 0.5)
        if pronto:
            try:
                saida += os.read(fd, 65536).decode("utf-8", "replace")
            except OSError:
                break
            if ate and re.search(ate, re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\s", "", saida)):
                break
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)", "", saida)


def _parear_fechar():
    if _PAREAR["proc"]:
        try:
            _PAREAR["proc"].kill()
        except OSError:
            pass
    if _PAREAR["fd"] is not None:
        try:
            os.close(_PAREAR["fd"])
        except OSError:
            pass
    _PAREAR.update(proc=None, fd=None)


def conta_claude_no_servidor():
    """Liga a conta Claude do editor: o editor pede na página, o servidor abre o login do Claude (setup-token), publica o
    link, recebe o código que o editor colou e guarda o token só aqui. A cada ~5 s; não trava a estação."""
    if time.time() - _PAREAR["visto"] < 5:
        return
    _PAREAR["visto"] = time.time()
    if _PAREAR["proc"] and time.time() - _PAREAR["desde"] > 900:   # link esquecido: fecha
        _parear_fechar()
    try:
        r = central.rpc("cw_srv_conta_puxar", timeout=10)
    except central.ErroCentral:
        return
    if not r:
        return
    status = lambda estado, link=None, msg=None: central.rpc("cw_srv_conta_status", timeout=10, p_estado=estado, p_link=link, p_mensagem=msg)
    if r.get("estado") == "pedido":
        import pty, fcntl, termios, struct
        _parear_fechar()
        mestre, escravo = pty.openpty()
        fcntl.ioctl(escravo, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 2000, 0, 0))   # largo: o link e o token não quebram linha
        claude = _cfg().get("claude") or achar_claude() or "claude"
        _PAREAR["proc"] = subprocess.Popen([claude, "setup-token"], stdin=escravo, stdout=escravo, stderr=escravo, close_fds=True,
                                           env=dict(os.environ, BROWSER="/bin/false", COLUMNS="2000"), start_new_session=True)
        os.close(escravo)
        _PAREAR.update(fd=mestre, desde=time.time())
        texto = _ler_pty(mestre, 40, ate=r"Paste|code")
        links = re.findall(r"https://claude\.(?:com|ai)/[^\s\x07\x1b]*oauth/authorize\?[^\s\x07\x1b]+", texto)
        if not links:
            _parear_fechar()
            status("erro", msg="O login do Claude não abriu no servidor. Tente de novo em instantes.")
            return
        status("link", link=max(links, key=len), msg="Abra o link, entre com a sua conta Claude e cole aqui o código que aparecer.")
    elif r.get("estado") == "codigo" and r.get("codigo"):
        if _PAREAR["fd"] is None:
            status("erro", msg="O link venceu. Peça um link novo.")
            return
        os.write(_PAREAR["fd"], (r["codigo"].strip() + "\r").encode())
        texto = _ler_pty(_PAREAR["fd"], 60, ate=r"sk-ant-oat[0-9A-Za-z_-]{40,}|[Ee]rror|[Ii]nvalid")
        _parear_fechar()
        token = re.search(r"sk-ant-oat[0-9A-Za-z_-]{40,}", re.sub(r"\s", "", texto))
        if not token:
            status("erro", msg="O Claude não aceitou esse código. Peça um link novo e cole o código assim que aparecer.")
            return
        CASA.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(TOKEN_CLAUDE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token.group(0))
        status("ok", msg="Conta Claude ligada no servidor. Os pedidos rodam com o seu plano.")
        log("conta Claude ligada no servidor")


# ----------------------------------------------------------------------------------------------- instalação
def python_console():
    """O hook precisa de saída no terminal: no Windows, python.exe (não pythonw.exe)."""
    py = Path(sys.executable)
    if WINDOWS and py.name.lower() == "pythonw.exe" and (py.parent / "python.exe").exists():
        py = py.parent / "python.exe"
    return py


def escrever_cerca():
    CASA.mkdir(parents=True, exist_ok=True)
    cmd = '"{}" "{}" porteiro'.format(python_console().as_posix(), (CASA / "scripts" / "estacao.py").as_posix())
    CERCA.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": cmd, "timeout": ESPERA_OK_S + 120}]}]}}, indent=1))


def copiar_scripts():
    """Copia estacao.py, central.py e central.json do plugin para ~/.cw-cortes/estacao. Devolve True se mudou algo."""
    mudou = False
    (CASA / "scripts").mkdir(parents=True, exist_ok=True)
    for origem, destino in [(AQUI / "estacao.py", CASA / "scripts" / "estacao.py"),
                            (AQUI / "central.py", CASA / "scripts" / "central.py"),
                            (AQUI / "capacidades.py", CASA / "scripts" / "capacidades.py"),
                            (AQUI.parent / "central.json", CASA / "central.json")]:
        if origem.resolve() == destino.resolve() or not origem.exists():
            continue
        if not destino.exists() or destino.read_bytes() != origem.read_bytes():
            shutil.copy2(origem, destino)
            mudou = True
    return mudou


def ligada():
    return (CASA / "ligada").exists() if WINDOWS or platform.system() == "Linux" else PLIST.exists()   # Linux: servidor


def atualizar_se_ligada():
    """Chamado na abertura de cada sessão: mantém a cópia da estação na versão do plugin."""
    if ligada():
        copiar_scripts()


def _launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _pid_vivo(pid):
    if not pid:
        return False
    if WINDOWS:
        r = subprocess.run(["tasklist", "/FI", "PID eq {}".format(pid), "/NH"], capture_output=True, text=True)
        return str(pid) in r.stdout
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def ligar_windows(claude):
    """Inicialização do Windows: um .vbs abre, sem janela, um laço que mantém a estação rodando (e a reinicia se cair)."""
    py = python_console()
    extra = "".join('set "{}={}"\n'.format(k, os.environ[k]) for k in ("CW_CORTES_HOME", "CW_TRABALHO") if os.environ.get(k))
    laco = CASA / "rodar.cmd"
    laco.write_text("@echo off\nchcp 65001 >nul\nset PYTHONIOENCODING=utf-8\n" + extra +
                    ':loop\nif not exist "{flag}" exit /b\n"{py}" "{script}" rodar >> "{log}" 2>&1\n'
                    "timeout /t 30 /nobreak >nul\ngoto loop\n".format(flag=CASA / "ligada", py=py, script=CASA / "scripts" / "estacao.py", log=LOG),
                    encoding="utf-8")
    (CASA / "ligada").write_text("1")
    INICIAR_WIN.parent.mkdir(parents=True, exist_ok=True)
    INICIAR_WIN.write_text('CreateObject("WScript.Shell").Run """{}""", 0, False\n'.format(laco), encoding="utf-8")
    if not _pid_vivo((CASA / "rodar.pid").read_text().strip() if (CASA / "rodar.pid").exists() else None):
        subprocess.Popen(["wscript", str(INICIAR_WIN)], creationflags=0x08000000)
    print("Estação ligada neste PC. Ela sobe sozinha (sem janela) quando você entra no Windows.\n"
          "Agora é só abrir a central › Sala e clicar no seu computador. Registro: {}".format(LOG))


def projeto_atual():
    """A pasta onde o editor pediu para ligar a estação vira o projeto, se tiver CLAUDE.md ou .claude/ (agentes, skills)."""
    cwd = Path.cwd()
    if cwd != HOME and projeto_valido(cwd) and ((cwd / "CLAUDE.md").exists() or (cwd / ".claude").is_dir()):
        return str(cwd)
    return None


def adotar_projeto(cwd):
    """Sessão normal do editor numa pasta de projeto (CLAUDE.md ou .claude/) vira o projeto da estação, se ainda não houver um."""
    if not CONFIG.exists() or _cfg().get("projeto"):
        return None
    p = projeto_valido(cwd)
    if not p or p == HOME or not ((p / "CLAUDE.md").exists() or (p / ".claude").is_dir()):
        return None
    cfg = _cfg()
    cfg["projeto"] = str(p)
    CONFIG.write_text(json.dumps(cfg, indent=1))
    return p


def cmd_projeto(a):
    """Define a pasta do projeto do editor: a estação abre o Claude ali (agentes, skills, regras) e grava na pasta do vídeo."""
    cfg = _cfg()
    if a.limpar:
        cfg.pop("projeto", None)
        CONFIG.write_text(json.dumps(cfg, indent=1))
        print("Pronto: a estação volta a abrir o Claude na pasta de cada vídeo.")
        return
    p = projeto_valido(a.pasta or os.getcwd())
    if not p:
        sys.exit("essa pasta não serve como projeto (precisa existir, ficar dentro da sua pasta pessoal e não ser oculta)")
    cfg["projeto"] = str(p)
    CONFIG.write_text(json.dumps(cfg, indent=1))
    tem = [n for n in ("CLAUDE.md", ".claude/agents", ".claude/skills", ".claude/commands") if (p / n).exists()]
    print("Pronto: as próximas conversas da estação abrem em {} ({}). Conversas antigas continuam como estavam; "
          "comece uma 'Nova conversa' na central.".format(p, ", ".join(tem) if tem else "sem CLAUDE.md nem .claude/ — confira a pasta"))


def cmd_ligar(_a):
    if platform.system() not in ("Darwin", "Windows"):
        sys.exit("a estação liga sozinha no macOS e no Windows; neste sistema rode 'estacao.py rodar' num terminal aberto")
    if not central.configurada():
        sys.exit("conecte primeiro: central.py entrar --token cw_...")
    claude = achar_claude()
    if not claude:
        sys.exit("não achei o comando claude nesta máquina")
    copiar_scripts()
    escrever_cerca()
    CONFIG.write_text(json.dumps({"claude": claude, "drive_raizes": [str(r) for r in raizes_drive()],
                                  "liberadas": _cfg().get("liberadas") or [],
                                  "conectores_liberados": _cfg().get("conectores_liberados") or [],
                                  "projeto": _cfg().get("projeto") or projeto_atual()}, indent=1))
    if WINDOWS:
        return ligar_windows(claude)
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
          "Agora é só abrir a central › Sala e clicar no seu computador. Registro: {}".format(LOG))


def cmd_desligar(_a):
    if WINDOWS:
        (CASA / "ligada").unlink(missing_ok=True)
        INICIAR_WIN.unlink(missing_ok=True)
        pid = (CASA / "rodar.pid").read_text().strip() if (CASA / "rodar.pid").exists() else None
        if _pid_vivo(pid):
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
    else:
        _launchctl("bootout", "gui/{}/{}".format(os.getuid(), ROTULO))
        PLIST.unlink(missing_ok=True)
    print("Estação desligada (o Escritório da central não manda mais nada para esta máquina).")


def cmd_status(_a):
    if WINDOWS:
        vivo = _pid_vivo((CASA / "rodar.pid").read_text().strip() if (CASA / "rodar.pid").exists() else None)
        print("Estação:", "ligada" if ligada() and vivo else ("instalada mas parada (reinicie o PC ou rode estacao.py ligar)" if ligada() else "desligada"))
    else:
        r = _launchctl("print", "gui/{}/{}".format(os.getuid(), ROTULO))
        print("Estação:", "ligada" if r.returncode == 0 else ("instalada mas parada" if ligada() else "desligada"))
    if LOG.exists():
        print("".join(LOG.read_text(errors="replace").splitlines(True)[-8:]))


def main():
    ap = argparse.ArgumentParser(description="Estação do Escritório virtual CW Studios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("ligar", "desligar", "status", "rodar", "porteiro"):
        sub.add_parser(c)
    pp = sub.add_parser("projeto")
    pp.add_argument("pasta", nargs="?")
    pp.add_argument("--limpar", action="store_true")
    a = ap.parse_args()
    if WINDOWS:   # o console do Windows não é UTF-8 por padrão
        for f in (sys.stdout, sys.stderr):
            try:
                f.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    if a.cmd == "rodar" and LOG.exists() and LOG.stat().st_size > 2_000_000:
        LOG.write_text("")
    {"ligar": cmd_ligar, "desligar": cmd_desligar, "status": cmd_status, "rodar": cmd_rodar, "porteiro": cmd_porteiro, "projeto": cmd_projeto}[a.cmd](a)


if __name__ == "__main__":
    main()
