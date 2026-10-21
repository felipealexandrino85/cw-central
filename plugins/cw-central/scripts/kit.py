#!/usr/bin/env python3
"""Kit do time: compartilha os agentes e skills do editor com a Central e instala o kit oficial no Claude dele.

Só biblioteca padrão. O que vem do kit é gravado com o prefixo cw-kit- (em ~/.claude/agents e ~/.claude/skills) para
não colidir com os agentes do próprio editor; o que não está mais no kit é removido.

  listar                         mostra os agentes e skills deste editor que podem ser compartilhados
  compartilhar [--nomes a,b]     envia (todos, ou só os nomes indicados) para a gestão avaliar na aba Kit do time
  compartilhar --pasta P --kit K envia um PACOTE preparado (skills/<nome>/..., agents/<nome>.md, REQUISITOS.md) para o
                                 KIT - Cardápio Web (K=cardapio-web) ou o KIT - CW Cast (K=cw-cast), com anexos (fontes,
                                 logos, vinheta, sons, LUTs... até 50 MB cada) guardados na central
  sincronizar                    instala/atualiza o kit oficial e o que você escolheu na biblioteca (roda sozinho ao abrir a sessão)
  biblioteca [termo]             procura agentes/skills nos repositórios que a gestão conectou
  puxar ID / tirar ID            põe (ou tira) um item da biblioteca no seu Claude
  plugin ID                      instala um plugin da biblioteca (catálogo + /plugin install), vale ao reiniciar o Claude
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import central  # noqa: E402

CLAUDE = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
HUB = Path(os.environ.get("CW_CORTES_HOME", Path.home() / ".cw-cortes"))
MANIFESTO = HUB / "kit.json"
MANIFESTO_BIB = HUB / "biblioteca.json"
CACHE_ANEXOS = HUB / "kit-anexos"     # anexos já baixados, pelo sha256 (não baixa de novo a cada versão)
LIMITE_ANEXO = 50 * 1024 * 1024
KITS = {"cardapio-web": "KIT - Cardápio Web", "cw-cast": "KIT - CW Cast"}
IGNORAR = re.compile(r"(^|/)(\.DS_Store|\.git(/|$)|__pycache__/|node_modules/|Thumbs\.db)")
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


def _funcao(corpo, timeout=60):
    cfg = central.config()
    req = urllib.request.Request(cfg["url"] + "/functions/v1/kit-anexos", data=json.dumps(corpo).encode(), method="POST",
                                 headers={"apikey": cfg["chave"], "Authorization": "Bearer " + cfg["chave"],
                                          "x-cw-token": cfg["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("erro")
        except (ValueError, AttributeError):
            msg = None
        raise central.ErroCentral(msg or "anexos do kit: HTTP {}".format(e.code))


def _sha(arq):
    h = hashlib.sha256()
    with open(arq, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _subir_anexo(arq):
    """Sobe um arquivo para a central (se ainda não estiver lá). Devolve o sha256."""
    sha, tam = _sha(arq), arq.stat().st_size
    r = _funcao({"acao": "enviar", "sha256": sha, "tamanho": tam})
    if not r.get("ja_existe"):
        req = urllib.request.Request(r["url"], data=arq.read_bytes(), method="PUT",
                                     headers={"Content-Type": "application/octet-stream", "x-upsert": "false"})
        with urllib.request.urlopen(req, timeout=600):
            pass
    return sha


def _item_do_pacote(tipo, nome, principal, pasta, requisitos_gerais):
    txt = principal.read_text(encoding="utf-8", errors="replace")
    fm, extras, anexos, total = frontmatter(txt), {}, [], 0
    requisitos = requisitos_gerais
    for f in sorted(pasta.rglob("*")) if pasta else []:
        rel = str(f.relative_to(pasta)).replace(os.sep, "/")
        if f == principal or not f.is_file() or IGNORAR.search(rel):
            continue
        if rel == "REQUISITOS.md":
            requisitos = f.read_text(encoding="utf-8", errors="replace")
        if TEXTO.search(f.name) and f.stat().st_size <= LIMITE_ARQ and total + f.stat().st_size <= LIMITE_TOTAL:
            extras[rel] = limpar(f.read_text(encoding="utf-8", errors="replace"))
            total += f.stat().st_size
        elif f.stat().st_size <= LIMITE_ANEXO:
            print("  subindo {} ({:.1f} MB)…".format(rel, f.stat().st_size / 1e6), flush=True)
            anexos.append({"caminho": rel, "sha256": _subir_anexo(f), "tamanho": f.stat().st_size})
        else:
            print("  ficou de fora (acima de 50 MB): " + rel)
    return {"tipo": tipo, "nome": fm.get("name") or nome, "descricao": fm.get("description", "")[:600], "conteudo": limpar(txt),
            "arquivos": extras, "anexos": anexos, "requisitos": requisitos}


def pacote(raiz, kit):
    """Itens de um pacote preparado: skills/<nome>/SKILL.md (+ arquivos), agents/<nome>.md e REQUISITOS.md geral."""
    raiz = Path(raiz).expanduser().resolve()
    if not raiz.is_dir():
        raise SystemExit("pasta do pacote não encontrada: {}".format(raiz))
    geral = (raiz / "REQUISITOS.md").read_text(encoding="utf-8", errors="replace") if (raiz / "REQUISITOS.md").is_file() else None
    itens = []
    for skill in sorted((raiz / "skills").glob("*/SKILL.md")) if (raiz / "skills").is_dir() else []:
        itens.append(_item_do_pacote("skill", skill.parent.name, skill, skill.parent, geral))
    for ag in sorted((raiz / "agents").glob("*.md")) if (raiz / "agents").is_dir() else []:
        itens.append(_item_do_pacote("agente", ag.stem, ag, None, geral))
    for i in itens:
        i["kit"] = kit
    return itens


def cmd_compartilhar(a):
    if a.pasta:
        if a.kit not in KITS:
            raise SystemExit("diga o kit: --kit cardapio-web (KIT - Cardápio Web) ou --kit cw-cast (KIT - CW Cast)")
        itens = pacote(a.pasta, a.kit)
    else:
        itens = locais()
    if a.nomes:
        quer = {n.strip() for n in a.nomes.split(",") if n.strip()}
        itens = [i for i in itens if i["nome"] in quer]
    if not itens:
        print("Nada para compartilhar.")
        return
    for i in itens:
        i["hash"] = hashlib.sha256((i["conteudo"] + json.dumps(i["arquivos"], sort_keys=True)
                                    + json.dumps(i.get("anexos") or [], sort_keys=True)).encode()).hexdigest()[:16]
        i.pop("origem", None)
        if not a.pasta and a.kit in KITS:
            i["kit"] = a.kit
    n = central.rpc("cw_kit_enviar", timeout=60, p_itens=itens)
    print("Enviado para a gestão{}: {} item(ns) novo(s) ou alterado(s) de {} ({}).".format(
        " (" + KITS[a.kit] + ")" if a.kit in KITS else "", n, len(itens), ", ".join(i["nome"] for i in itens)))


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
            if item.get("anexos"):
                _baixar_anexos(pasta, item["anexos"])
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


def _baixar_anexos(pasta, anexos):
    """Põe os anexos na pasta da skill (do cache local; o que falta baixa da central)."""
    CACHE_ANEXOS.mkdir(parents=True, exist_ok=True)
    faltam = [a["sha256"] for a in anexos if not (CACHE_ANEXOS / a["sha256"]).exists()]
    if faltam:
        try:
            links = _funcao({"acao": "baixar", "sha256s": faltam}, timeout=30).get("links") or {}
        except central.ErroCentral:
            links = {}
        for sha, url in links.items():
            tmp = CACHE_ANEXOS / (sha + ".parcial")
            try:
                with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as f:
                    shutil.copyfileobj(r, f)
                if _sha(tmp) == sha:
                    tmp.replace(CACHE_ANEXOS / sha)
            except (OSError, urllib.error.URLError):
                pass
            finally:
                tmp.unlink(missing_ok=True)
    for a in anexos:
        origem, alvo = CACHE_ANEXOS / a["sha256"], (pasta / a["caminho"]).resolve()
        if origem.exists() and pasta.resolve() in alvo.parents:   # nunca grava fora da pasta da skill
            alvo.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origem, alvo)


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


CATALOGO = HUB / "catalogo.json"
# itens de manutenção de software que vêm junto dos repositórios (não ajudam a editar vídeo)
DE_PROGRAMADOR = re.compile(r"pull request|github issue|\bci\b|release|maintainer|fastapi|\bvite\b|hmr|triage|merg", re.I)


def util_na_edicao(i):
    texto = "{} {}".format(i.get("nome") or "", i.get("descricao") or "")
    return bool((i.get("descricao") or "").strip()) and i.get("repo") != "felipealexandrino85/cw-central" and not DE_PROGRAMADOR.search(texto)


def catalogo(max_idade=3600):
    """Ferramentas (APIs da empresa) e itens da biblioteca da central, para o Claude recomendar no meio da edição.
    Guarda uma cópia local e só pergunta de novo à central a cada hora (a estação abre uma sessão por mensagem)."""
    import time
    try:
        guardado = json.loads(CATALOGO.read_text())
        if time.time() - guardado.get("ts", 0) < max_idade:
            return guardado
    except (OSError, ValueError):
        guardado = None
    if not central.configurada():
        return guardado
    novo = {"ts": time.time(), "biblioteca": [], "ferramentas": []}
    try:
        novo["biblioteca"] = [i for i in (central.rpc("cw_bib_buscar", timeout=6, p_termo="") or []) if util_na_edicao(i)]
    except central.ErroCentral:
        return guardado
    try:
        import ferramentas_mcp
        novo["ferramentas"] = [f for f in (ferramentas_mcp.chamar({"acao": "listar"}, timeout=6).get("ferramentas") or []) if f.get("configurada")]
    except Exception:
        novo["ferramentas"] = (guardado or {}).get("ferramentas") or []
    try:
        HUB.mkdir(parents=True, exist_ok=True)
        CATALOGO.write_text(json.dumps(novo, ensure_ascii=False))
    except OSError:
        pass
    return novo


def texto_catalogo(cat):
    """O catálogo em poucas linhas, com a regra de quando recomendar."""
    if not cat or not (cat.get("biblioteca") or cat.get("ferramentas")):
        return None
    linhas = ["Recursos da CW Studios para recomendar durante a edição. Quando o pedido do editor combinar com um destes "
              "(ex.: gerar voz ou narração, imagem ou vídeo com IA, animação/motion em HTML), diga em uma linha antes de "
              "improvisar: \"Tem o <nome> no time — quer usar?\". Não force: só quando ajudar de verdade."]
    if cat.get("ferramentas"):
        linhas.append("Ferramentas (APIs pagas da conta da empresa, pelo conector cw-studios: listar → gerar → status → baixar; "
                      "antes de gerar, confirme com o editor o que vai gerar e o custo):")
        for f in cat["ferramentas"]:
            linhas.append("- {} ({}): {}{}".format(f.get("nome"), f.get("ferramenta"), (f.get("descricao") or "")[:160],
                                                   " · ~US$ {}/geração".format(f["custo_estimado_usd"]) if f.get("custo_estimado_usd") else ""))
    tem = [i for i in cat.get("biblioteca") or [] if i.get("no_meu_claude")]
    falta = [i for i in cat.get("biblioteca") or [] if not i.get("no_meu_claude")]
    if tem:
        linhas.append("Já no Claude deste editor (use direto, prefixo cw-bib-):")
        linhas += ["- {} cw-bib-{}: {}".format(i["tipo"], i["nome"], i["descricao"][:140]) for i in tem[:40]]
    if falta:
        linhas.append("Na biblioteca, ainda não adicionados por este editor. Se ele topar, adicione com "
                      "`python3 \"{}\" puxar <id>` (vale a partir da próxima mensagem/sessão); plugin se instala pela página "
                      "da Biblioteca:".format(Path(__file__).resolve()))
        linhas += ["- [{}] {} {} ({}): {}".format(i["id"], i["tipo"], i["nome"], i.get("repo") or "", i["descricao"][:140]) for i in falta[:40]]
    return "\n".join(linhas)


def cmd_sincronizar(_a):
    if sincronizar(silencioso=False) is None:
        print("Não consegui falar com a central (conectado? rode central.py quem).")
        return
    bib = sincronizar_biblioteca()
    print("Da biblioteca: {}".format(", ".join(bib) if bib else "(nada escolhido)"))


def cmd_biblioteca(a):
    itens = central.rpc("cw_bib_buscar", p_termo=a.termo or "") or []
    if not itens:
        print("Nada encontrado na biblioteca{}.".format(" para '{}'".format(a.termo) if a.termo else ""))
        return
    for i in itens:
        print("{:>5}  [{}] {}{}  · {}{}".format(i["id"], i["tipo"], i["nome"], " ✓ no seu Claude" if i["no_meu_claude"] else "",
                                             i["repo"], " · " + i["descricao"][:90] if i.get("descricao") else ""))


def cmd_puxar(a, quer=True):
    nome = central.rpc("cw_bib_escolher_token", p_item=int(a.id), p_quer=quer)
    instalados = sincronizar_biblioteca() or []
    print("{} {}. {} No seu Claude agora: {}".format(
        "Puxado:" if quer else "Tirado:", nome,
        "Fica disponível na próxima sessão do Claude (prefixo {}).".format(PREFIXO_BIB) if quer else "Já foi removido dos arquivos do seu Claude.",
        ", ".join(n.split(":", 1)[1] for n in instalados) or "(nada da biblioteca)"))


def cmd_plugin(a):
    info = central.rpc("cw_bib_plugin", p_item=int(a.id))
    if not info.get("catalogo"):
        sys.exit("o repositório {} não é um catálogo de plugins; peça à gestão o jeito de instalar".format(info["repo"]))
    claude = shutil.which("claude") or next((str(c) for c in [Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude",
                                                               Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")] if c.exists()), None)
    if not claude:
        sys.exit("não achei o comando claude; rode no Claude: /plugin marketplace add {} e /plugin install {}@{}".format(
            info["repo"], info["plugin"], info["catalogo"]))
    import subprocess
    for cmd in ([claude, "plugin", "marketplace", "add", info["repo"]],
                [claude, "plugin", "install", "{}@{}".format(info["plugin"], info["catalogo"]), "--scope", "user"]):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        saida = (r.stdout + r.stderr).strip().splitlines()
        if r.returncode and not any("already" in l.lower() or "já" in l.lower() for l in saida):
            sys.exit("não consegui: {}".format(saida[-1] if saida else r.returncode))
    print("Plugin {} instalado (catálogo {}). Feche e abra o Claude para ele começar a valer.".format(info["plugin"], info["catalogo"]))


def main():
    ap = argparse.ArgumentParser(description="Kit do time CW Studios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("listar")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("compartilhar")
    p.add_argument("--nomes")
    p.add_argument("--pasta", help="pacote preparado (skills/, agents/, REQUISITOS.md)")
    p.add_argument("--kit", choices=sorted(KITS), help="cardapio-web ou cw-cast")
    sub.add_parser("sincronizar")
    p = sub.add_parser("biblioteca")
    p.add_argument("termo", nargs="?")
    p = sub.add_parser("puxar")
    p.add_argument("id")
    p = sub.add_parser("tirar")
    p.add_argument("id")
    p = sub.add_parser("plugin")
    p.add_argument("id")
    a = ap.parse_args()
    {"listar": cmd_listar, "compartilhar": cmd_compartilhar, "sincronizar": cmd_sincronizar, "biblioteca": cmd_biblioteca,
     "puxar": cmd_puxar, "tirar": lambda x: cmd_puxar(x, quer=False), "plugin": cmd_plugin}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except central.ErroCentral as e:
        sys.exit("central: {}".format(e))
