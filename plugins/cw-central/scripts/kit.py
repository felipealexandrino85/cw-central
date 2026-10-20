#!/usr/bin/env python3
"""Kit do time: compartilha os agentes e skills do editor com a Central e instala o kit oficial no Claude dele.

Só biblioteca padrão. O que vem do kit é gravado com o prefixo cw-kit- (em ~/.claude/agents e ~/.claude/skills) para
não colidir com os agentes do próprio editor; o que não está mais no kit é removido.

  listar                         mostra os agentes e skills deste editor que podem ser compartilhados
  compartilhar [--nomes a,b]     envia (todos, ou só os nomes indicados) para a gestão avaliar na aba Kit do time
  sincronizar                    instala/atualiza o kit oficial e o que você escolheu na biblioteca (roda sozinho ao abrir a sessão)
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import central  # noqa: E402

CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
HUB = Path(os.environ.get("CW_CORTES_HOME", Path.home() / ".cw-cortes"))
MANIFESTO = HUB / "kit.json"
MANIFESTO_BIB = HUB / "biblioteca.json"
PREFIXO = "cw-kit-"
PREFIXO_BIB = "cw-bib-"   # itens que o editor escolheu na biblioteca da central
TEXTO = re.compile(r"\.(md|txt|py|sh|js|ts|json|ya?ml|toml|csv)$", re.I)
SEGREDO = re.compile(r"(cw_|sbp_|ATATT|sk-|ghp_|eyJ)[A-Za-z0-9_\-=.]{8,}")
LIMITE_ARQ, LIMITE_TOTAL = 50_000, 300_000


def frontmatter(texto):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", texto, re.S)
    campos = {}
    if m:
        for linha in m.group(1).splitlines():
            k, _, v = linha.partition(":")
            if v:
                campos[k.strip()] = v.strip().strip('"').strip("'")
    return campos


def renomear(texto, novo):
    """Troca o name: do frontmatter (ou cria o frontmatter) para o nome do kit."""
    if re.match(r"^---\s*\n", texto):
        if re.search(r"^name\s*:", texto, re.M):
            return re.sub(r"^name\s*:.*$", "name: " + novo, texto, count=1, flags=re.M)
        return re.sub(r"^---\s*\n", "---\nname: " + novo + "\n", texto, count=1)
    return "---\nname: {}\n---\n\n{}".format(novo, texto)


def limpar(texto):
    return SEGREDO.sub("•••", texto)


def locais():
    """Agentes e skills do editor (usuário e projeto atual), sem os que vieram do kit."""
    itens = []
    raizes = [CLAUDE, Path.cwd() / ".claude"]
    for raiz in raizes:
        for arq in sorted((raiz / "agents").glob("*.md")) if (raiz / "agents").is_dir() else []:
            if arq.stem.startswith((PREFIXO, PREFIXO_BIB)):
                continue
            txt = arq.read_text(encoding="utf-8", errors="replace")
            fm = frontmatter(txt)
            itens.append({"tipo": "agente", "nome": fm.get("name") or arq.stem, "descricao": fm.get("description", "")[:600],
                          "conteudo": limpar(txt), "arquivos": {}, "origem": str(arq)})
        for pasta in sorted((raiz / "skills").iterdir()) if (raiz / "skills").is_dir() else []:
            principal = pasta / "SKILL.md"
            if not principal.is_file() or pasta.name.startswith((PREFIXO, PREFIXO_BIB)):
                continue
            txt = principal.read_text(encoding="utf-8", errors="replace")
            fm, extras, total = frontmatter(txt), {}, 0
            for f in sorted(pasta.rglob("*")):
                if f == principal or not f.is_file() or not TEXTO.search(f.name) or f.stat().st_size > LIMITE_ARQ:
                    continue
                if total + f.stat().st_size > LIMITE_TOTAL:
                    break
                extras[str(f.relative_to(pasta))] = limpar(f.read_text(encoding="utf-8", errors="replace"))
                total += f.stat().st_size
            itens.append({"tipo": "skill", "nome": fm.get("name") or pasta.name, "descricao": fm.get("description", "")[:600],
                          "conteudo": limpar(txt), "arquivos": extras, "origem": str(pasta)})
    vistos, unicos = set(), []
    for i in itens:  # o do projeto não duplica o do usuário com o mesmo nome
        if (i["tipo"], i["nome"]) not in vistos:
            vistos.add((i["tipo"], i["nome"]))
            unicos.append(i)
    return unicos


def cmd_listar(a):
    itens = locais()
    if a.json:
        print(json.dumps([{k: i[k] for k in ("tipo", "nome", "descricao", "origem")} for i in itens], ensure_ascii=False, indent=1))
        return
    if not itens:
        print("Nenhum agente ou skill próprio encontrado em {} (nem no projeto atual).".format(CLAUDE))
        return
    for i in itens:
        print("- [{}] {}{}{}".format(i["tipo"], i["nome"], " · " + i["descricao"][:90] if i["descricao"] else "",
                                    " (+{} arquivos)".format(len(i["arquivos"])) if i["arquivos"] else ""))


def cmd_compartilhar(a):
    itens = locais()
    if a.nomes:
        quer = {n.strip() for n in a.nomes.split(",") if n.strip()}
        itens = [i for i in itens if i["nome"] in quer]
    if not itens:
        print("Nada para compartilhar.")
        return
    for i in itens:
        i["hash"] = hashlib.sha256((i["conteudo"] + json.dumps(i["arquivos"], sort_keys=True)).encode()).hexdigest()[:16]
        i.pop("origem", None)
    n = central.rpc("cw_kit_enviar", timeout=30, p_itens=itens)
    print("Enviado para a gestão: {} item(ns) novo(s) ou alterado(s) de {} ({}).".format(
        n, len(itens), ", ".join(i["nome"] for i in itens)))


def sincronizar(silencioso=True):
    """Instala o kit oficial; devolve a lista de nomes instalados (ou None se não deu para falar com a central)."""
    if not central.configurada():
        return None
    try:
        kit = central.rpc("cw_kit", timeout=5) or []
    except central.ErroCentral:
        return None
    try:
        antes = json.loads(MANIFESTO.read_text())
    except (OSError, ValueError):
        antes = {}
    agora = {}
    for item in kit:
        nome = PREFIXO + item["nome"]
        chave = item["tipo"] + ":" + nome
        agora[chave] = item.get("versao", 0)
        if antes.get(chave) == agora[chave] and ((CLAUDE / "agents" / (nome + ".md")).exists() or (CLAUDE / "skills" / nome).exists()):
            continue
        if item["tipo"] == "agente":
            (CLAUDE / "agents").mkdir(parents=True, exist_ok=True)
            (CLAUDE / "agents" / (nome + ".md")).write_text(renomear(item["conteudo"], nome), encoding="utf-8")
        else:
            pasta = CLAUDE / "skills" / nome
            if pasta.exists():
                shutil.rmtree(pasta)
            pasta.mkdir(parents=True)
            (pasta / "SKILL.md").write_text(renomear(item["conteudo"], nome), encoding="utf-8")
            for rel, txt in (item.get("arquivos") or {}).items():
                destino = (pasta / rel).resolve()
                if pasta.resolve() not in destino.parents:   # nunca grava fora da pasta da skill
                    continue
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_text(txt, encoding="utf-8")
    for chave in set(antes) - set(agora):   # saiu do kit: remove
        tipo, nome = chave.split(":", 1)
        if not nome.startswith(PREFIXO):
            continue
        if tipo == "agente":
            (CLAUDE / "agents" / (nome + ".md")).unlink(missing_ok=True)
        else:
            shutil.rmtree(CLAUDE / "skills" / nome, ignore_errors=True)
    HUB.mkdir(parents=True, exist_ok=True)
    MANIFESTO.write_text(json.dumps(agora))
    if not silencioso:
        print("Kit do time instalado: {}".format(", ".join(sorted(agora)) or "(vazio)"))
    return sorted(agora)


def _instalar(itens, prefixo, manifesto):
    """Instala agentes/skills/comandos com o prefixo e remove os que saíram da lista. Devolve os nomes instalados."""
    try:
        antes = json.loads(manifesto.read_text())
    except (OSError, ValueError):
        antes = {}
    agora = {}
    for item in itens:
        nome = prefixo + re.sub(r"[^a-z0-9-]+", "-", item["nome"].lower()).strip("-")
        chave = item["tipo"] + ":" + nome
        agora[chave] = item.get("versao") or 0
        destino = {"agente": CLAUDE / "agents" / (nome + ".md"), "comando": CLAUDE / "commands" / (nome + ".md"),
                   "skill": CLAUDE / "skills" / nome}.get(item["tipo"])
        if destino is None or (antes.get(chave) == agora[chave] and destino.exists()):
            continue
        if item["tipo"] in ("agente", "comando"):
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(renomear(item["conteudo"], nome) if item["tipo"] == "agente" else item["conteudo"], encoding="utf-8")
        else:
            if destino.exists():
                shutil.rmtree(destino)
            destino.mkdir(parents=True)
            (destino / "SKILL.md").write_text(renomear(item["conteudo"], nome), encoding="utf-8")
            for rel, txt in (item.get("arquivos") or {}).items():
                alvo = (destino / rel).resolve()
                if destino.resolve() not in alvo.parents:   # nunca grava fora da pasta da skill
                    continue
                alvo.parent.mkdir(parents=True, exist_ok=True)
                alvo.write_text(txt, encoding="utf-8")
    for chave in set(antes) - set(agora):   # saiu da lista: remove
        tipo, nome = chave.split(":", 1)
        if not nome.startswith(prefixo):
            continue
        if tipo == "agente":
            (CLAUDE / "agents" / (nome + ".md")).unlink(missing_ok=True)
        elif tipo == "comando":
            (CLAUDE / "commands" / (nome + ".md")).unlink(missing_ok=True)
        else:
            shutil.rmtree(CLAUDE / "skills" / nome, ignore_errors=True)
    HUB.mkdir(parents=True, exist_ok=True)
    manifesto.write_text(json.dumps(agora))
    return sorted(agora)


def sincronizar_biblioteca():
    """Instala o que este editor escolheu na biblioteca da central (cw-bib-*). None se não deu para falar com a central."""
    if not central.configurada():
        return None
    try:
        itens = central.rpc("cw_bib_minhas", timeout=8) or []
    except central.ErroCentral:
        return None
    return _instalar([i for i in itens if i.get("conteudo")], PREFIXO_BIB, MANIFESTO_BIB)


def cmd_sincronizar(_a):
    if sincronizar(silencioso=False) is None:
        print("Não consegui falar com a central (conectado? rode central.py quem).")
        return
    bib = sincronizar_biblioteca()
    print("Da biblioteca: {}".format(", ".join(bib) if bib else "(nada escolhido)"))


def main():
    ap = argparse.ArgumentParser(description="Kit do time CW Studios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("listar")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("compartilhar")
    p.add_argument("--nomes")
    sub.add_parser("sincronizar")
    a = ap.parse_args()
    {"listar": cmd_listar, "compartilhar": cmd_compartilhar, "sincronizar": cmd_sincronizar}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except central.ErroCentral as e:
        sys.exit("central: {}".format(e))
