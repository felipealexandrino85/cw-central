#!/usr/bin/env python3
"""O que o Claude de cada editor enxerga (skills, agentes, conectores), para a estação não ficar "sem o conhecimento".

- No Claude normal (hook SessionStart), lê das pastas o que aquela sessão carrega e manda para a central (origem "normal").
- Na estação, o próprio Claude conta no início o que carregou (evento init) e a estação manda (origem "estacao").
- A página compara os dois e mostra o que falta na estação.
- As skills criadas no app Claude (sincronizadas com a conta em ~/.claude/skills/synced) não carregam no Claude da
  estação: pasta_skills_app() monta uma pasta com links para elas, que a estação passa com --add-dir.

  python3 capacidades.py enviar --cwd PASTA     (o hook chama em segundo plano)
  python3 capacidades.py ver --cwd PASTA        (mostra a lista na tela)
"""
import argparse
import json
import os
import re
import shutil
import socket
import sys
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
SYNCED = CLAUDE / "skills" / "synced"
APP = HOME / ".cw-cortes" / "estacao" / "app"
WINDOWS = os.name == "nt"
SO_NO_APP = {"built-in-browser", "chrome-browser", "computer-use"}   # dependem do navegador/tela do app: não servem na estação


def _nome_frontmatter(arq, padrao):
    try:
        cab = arq.read_text(encoding="utf-8", errors="replace")[:1500]
    except OSError:
        return padrao
    m = re.search(r"^name:\s*['\"]?([^'\"\n]+)", cab, re.M) if cab.startswith("---") else None
    return m.group(1).strip() if m else padrao


def _json(arq):
    try:
        return json.loads(Path(arq).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _skills_em(pasta, prefixo=""):
    if not pasta.is_dir():
        return []
    return sorted({prefixo + _nome_frontmatter(s, s.parent.name) for s in pasta.glob("*/SKILL.md")})


def _agentes_em(pasta, prefixo=""):
    if not pasta.is_dir():
        return []
    return sorted({prefixo + _nome_frontmatter(a, a.stem) for a in pasta.rglob("*.md")})


def skills_do_app():
    """{nome: pasta} das skills do app (a cópia mais recente de cada uma)."""
    achadas = {}
    if not SYNCED.is_dir():
        return achadas
    for s in SYNCED.glob("*/*/SKILL.md"):
        if s.parent.parent.name.startswith("."):
            continue
        nome = s.parent.name
        if nome in SO_NO_APP:
            continue
        if nome not in achadas or s.stat().st_mtime > (achadas[nome] / "SKILL.md").stat().st_mtime:
            achadas[nome] = s.parent
    return achadas


def pasta_skills_app():
    """Pasta com as skills do app (links; cópia no Windows) para a estação abrir com --add-dir. None se não houver."""
    app = skills_do_app()
    destino = APP / ".claude" / "skills"
    try:
        destino.mkdir(parents=True, exist_ok=True)
        for velho in destino.iterdir():
            if velho.name not in app:
                (velho.unlink() if velho.is_symlink() or velho.is_file() else shutil.rmtree(velho, ignore_errors=True))
        for nome, origem in app.items():
            alvo = destino / nome
            if WINDOWS:
                if not alvo.exists() or (origem / "SKILL.md").stat().st_mtime > (alvo / "SKILL.md").stat().st_mtime:
                    shutil.rmtree(alvo, ignore_errors=True)
                    shutil.copytree(origem, alvo)
            elif not (alvo.is_symlink() and os.readlink(alvo) == str(origem)):
                if alvo.is_symlink() or alvo.is_file():
                    alvo.unlink()
                elif alvo.exists():
                    shutil.rmtree(alvo, ignore_errors=True)
                alvo.symlink_to(origem, target_is_directory=True)
    except OSError:
        return None
    return APP if app else None


def _plugins():
    ativos = {}
    for cfg in (CLAUDE / "settings.json", CLAUDE / "settings.local.json"):
        ativos.update(_json(cfg).get("enabledPlugins") or {})
    inst = _json(CLAUDE / "plugins" / "installed_plugins.json").get("plugins") or {}
    saida = []
    for chave, ligado in ativos.items():
        if not ligado or not inst.get(chave):
            continue
        caminho = Path(inst[chave][-1].get("installPath") or "")
        saida.append((chave.split("@")[0], caminho))
    return saida


def _pastas_do_projeto(cwd):
    """A pasta aberta e as de cima (até a home), como o Claude faz com .claude/ e .mcp.json."""
    p, saida = Path(cwd).resolve(), []
    while True:
        saida.append(p)
        if p == HOME or p.parent == p:
            break
        p = p.parent
    return saida


def do_disco(cwd):
    """O que uma sessão normal do Claude aberta em cwd carrega (lido das pastas)."""
    skills, agentes, mcp = set(), set(), {}
    skills.update(_skills_em(CLAUDE / "skills"))
    app = sorted(skills_do_app())
    agentes.update(_agentes_em(CLAUDE / "agents"))
    for nome in (_json(HOME / ".claude.json").get("mcpServers") or {}):
        mcp[nome] = "config"
    proj = (_json(HOME / ".claude.json").get("projects") or {})
    for pasta in _pastas_do_projeto(cwd):
        skills.update(_skills_em(pasta / ".claude" / "skills"))
        agentes.update(_agentes_em(pasta / ".claude" / "agents"))
        for nome in (_json(pasta / ".mcp.json").get("mcpServers") or {}):
            mcp[nome] = "projeto"
        for nome in ((proj.get(str(pasta)) or {}).get("mcpServers") or {}):
            mcp[nome] = "projeto"
    plugins = []
    for nome, caminho in _plugins():
        plugins.append(nome)
        skills.update(_skills_em(caminho / "skills", nome + ":"))
        agentes.update(_agentes_em(caminho / "agents", nome + ":"))
        for srv in (_json(caminho / ".mcp.json").get("mcpServers") or _json(caminho / ".mcp.json")):
            if isinstance(srv, str) and srv != "mcpServers":
                mcp["plugin:{}:{}".format(nome, srv)] = "plugin"
    desk = HOME / ("AppData/Roaming/Claude" if WINDOWS else "Library/Application Support/Claude") / "claude_desktop_config.json"
    for nome in (_json(desk).get("mcpServers") or {}):
        mcp.setdefault(nome, "app")
    return {"cwd": str(Path(cwd).resolve()), "skills": sorted(skills), "skills_app": app, "agentes": sorted(agentes),
            "mcp": [{"nome": k, "status": v} for k, v in sorted(mcp.items())], "plugins": sorted(plugins)}


def do_init(ev):
    """O que o Claude da estação contou que carregou (evento init do stream-json)."""
    return {"cwd": ev.get("cwd"), "modelo": ev.get("model"), "versao": ev.get("claude_code_version"),
            "skills": sorted(ev.get("skills") or []), "agentes": sorted(ev.get("agents") or []),
            "mcp": [{"nome": m.get("name"), "status": m.get("status")} for m in ev.get("mcp_servers") or []],
            "plugins": sorted(p.get("name") for p in ev.get("plugins") or [] if p.get("name"))}


def _base(n):
    return str(n).split(":")[-1].strip().lower()


def faltando(normal, estacao):
    """Itens que o Claude normal tem e o da estação não (comparando pelo nome, sem o prefixo do plugin)."""
    tem = {k: {_base(x) for x in estacao.get(k) or []} for k in ("skills", "agentes")}
    tem["mcp"] = {_base(m["nome"]) for m in estacao.get("mcp") or []}
    return {"skills": [s for s in normal.get("skills", []) + normal.get("skills_app", []) if _base(s) not in tem["skills"]],
            "agentes": [a for a in normal.get("agentes", []) if _base(a) not in tem["agentes"]],
            "mcp": [m["nome"] for m in normal.get("mcp", []) if _base(m["nome"]) not in tem["mcp"]]}


def enviar(origem, dados):
    try:
        import central
        central.rpc("cw_capacidades", timeout=15, p_maquina=socket.gethostname().split(".")[0], p_origem=origem, p_dados=dados)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("enviar", "ver"):
        s = sub.add_parser(c)
        s.add_argument("--cwd", default=os.getcwd())
    a = ap.parse_args()
    dados = do_disco(a.cwd)
    if a.cmd == "enviar":
        enviar("normal", dados)
    else:
        print(json.dumps(dados, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
