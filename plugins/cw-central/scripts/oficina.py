#!/usr/bin/env python3
"""Oficina no servidor: skills do sistema e agentes que trabalham sozinhos (conta Claude do CW Studios).

- instalar_skills(): as skills aprovadas na Oficina viram skills do Claude (cw-sk-<slug>) — usado pelo Titeu e pelos robôs.
- Criador de skills: pega um pedido (criar ou melhorar), escreve a skill, testa com o exemplo e manda o resultado para aprovar.
- Agentes que trabalham: vigiam a pasta "1. ENVIE AQUI/<pasta>" ou a agenda, ou recebem pedidos da página; produzem,
  mandam o resultado para aprovar e, aprovado, entregam (Publicação ou pasta do Drive).

Roda dentro da esteira (esteira.py chama ligar(sys.modules[__name__])): usa o claude(), srv() e log() de lá.
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import central  # noqa: E402
import kit  # noqa: E402

HOME = Path.home()
SKILLS = kit.CLAUDE / "skills"
PREFIXO = "cw-sk-"
BRT = timezone(timedelta(hours=-3))
LIMITE_UP = 50 * 1024 * 1024
# chaves de verdade (longas); o nome técnico "cw-sk-..." das skills não conta
SEGREDO = re.compile(r"(?<![\w-])(cw_[A-Za-z0-9]{24,}|sbp_[A-Za-z0-9]{20,}|ATATT[A-Za-z0-9_\-=]{20,}|sk-(?:ant-|or-)?[A-Za-z0-9_\-]{24,}"
                     r"|ghp_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})")
_SK = {"ts": 0}
_lock_inst = threading.Lock()


# ------------------------------------------------------------------------------------------- skills do sistema
def instalar_skills(forcar=False, log=print):
    """Instala/atualiza as skills aprovadas na Oficina como cw-sk-<slug> (no máximo a cada 10 min)."""
    if not forcar and time.time() - _SK["ts"] < 600:
        return
    with _lock_inst:
        _SK["ts"] = time.time()
        try:
            itens = central.rpc("cw_skills_sistema", timeout=60) or []
            kit._instalar(itens, PREFIXO, kit.HUB / "skills_sistema.json")
            for it in itens:
                pasta = SKILLS / (PREFIXO + re.sub(r"[^a-z0-9-]+", "-", it["nome"].lower()).strip("-"))
                faltam = [a for a in it.get("anexos") or [] if not pasta.joinpath(a["caminho"]).exists()]
                if faltam:
                    kit._baixar_anexos(pasta, faltam)
        except Exception as e:
            log("skills da Oficina:", e)


def _empacotar(raiz):
    """Pasta da skill → (conteudo do SKILL.md, arquivos de texto, anexos binários já enviados à central)."""
    raiz = Path(raiz)
    conteudo = (raiz / "SKILL.md").read_text(encoding="utf-8") if (raiz / "SKILL.md").exists() else ""
    arquivos, anexos, total = {}, [], 0
    for f in sorted(raiz.rglob("*")):
        rel = f.relative_to(raiz).as_posix()
        if not f.is_file() or rel in ("SKILL.md", "NOTAS.md") or kit.IGNORAR.search(rel) or f.is_symlink():
            continue
        if f.stat().st_size > kit.LIMITE_ANEXO:
            raise RuntimeError("{} passa de 50 MB".format(rel))
        texto = None
        if kit.TEXTO.search(f.name) or f.suffix.lower() in (".ass", ".css", ".html", ".srt", ".vtt", ".xml", ".svg"):
            try:
                texto = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                texto = None
        if texto is not None and len(texto) <= 300_000:
            if SEGREDO.search(texto):
                raise RuntimeError("{} parece ter uma senha ou chave: tire antes".format(rel))
            arquivos[rel] = texto
            total += len(texto)
        else:
            anexos.append({"caminho": rel, "sha256": kit._subir_anexo(f), "tamanho": f.stat().st_size})
    if SEGREDO.search(conteudo):
        raise RuntimeError("o SKILL.md parece ter uma senha ou chave")
    if total > 3_000_000:
        raise RuntimeError("a skill tem texto demais (mais de 3 MB)")
    return conteudo, arquivos, anexos


def _subir_resultado(arq, caminho):
    """Arquivo de resultado → bucket "oficina" (vídeo grande ganha uma prévia leve). Devolve o caminho no bucket ou None."""
    import urllib.request
    arq = Path(arq)
    if arq.stat().st_size > LIMITE_UP:
        if not re.search(r"\.(mp4|mov|mkv|webm|m4v)$", arq.name, re.I):
            return None
        leve = arq.with_name(arq.stem + "_previa.mp4")
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(arq), "-vf", "scale=-2:960", "-c:v", "libx264", "-crf", "28",
                        "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k", str(leve)],
                       env=dict(os.environ, PATH="/usr/local/bin:" + os.environ.get("PATH", "")))
        if not leve.exists() or leve.stat().st_size > LIMITE_UP:
            return None
        arq, caminho = leve, re.sub(r"\.[^.]+$", "_previa.mp4", caminho)
    url = kit._funcao({"acao": "resultado", "caminho": caminho}).get("url")
    if not url:
        return None
    tipo = mimetypes.guess_type(arq.name)[0] or "application/octet-stream"
    req = urllib.request.Request(url, data=arq.read_bytes(), method="PUT", headers={"Content-Type": tipo, "x-upsert": "true"})
    with urllib.request.urlopen(req, timeout=600):
        pass
    return caminho


def _nome_seguro(n):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", n).strip("-")[:100] or "arquivo"


def _resultados(pasta_saida, prefixo, limite=8):
    """Arquivos de saída → [{nome, caminho (bucket), tipo, local}] (texto curto vai inteiro)."""
    out = []
    for f in sorted(Path(pasta_saida).rglob("*"))[:40]:
        if not f.is_file() or f.name.startswith(".") or len(out) >= limite:
            continue
        item = {"nome": f.name, "local": str(f), "tipo": mimetypes.guess_type(f.name)[0] or ""}
        if f.suffix.lower() in (".md", ".txt", ".json", ".csv", ".srt") and f.stat().st_size <= 60_000:
            item["texto"] = f.read_text(encoding="utf-8", errors="replace")
        try:
            item["caminho"] = _subir_resultado(f, "{}/{}".format(prefixo, _nome_seguro(f.name)))
        except Exception as e:
            item["erro"] = str(e)[:200]
        out.append(item)
    return out


CRIADOR = """Você é o Criador de skills da Oficina do CW Studios (estúdio de vídeo da Cardápio Web), sem ninguém olhando: não pergunte
nada, decida. Uma skill é um pacote que ensina o Claude a fazer uma tarefa: SKILL.md (instruções) + scripts e arquivos de apoio.
Pedido de {quem}: {pedido}
Skill: "{nome}" ({situacao}).
Referências enviadas: {refs}
Pasta da skill (edite aqui): {skill}
Regras para a skill:
- SKILL.md começa com o cabeçalho
  ---
  name: {nome_tecnico}
  description: <quando usar esta skill, em uma frase com as palavras que o time usaria no pedido>
  ---
  e depois as instruções passo a passo, com valores concretos (tamanhos, cores, tempos), e onde fica o resultado.
- Ela roda num servidor Linux sem tela: python3, ffmpeg, ffprobe, node e os pacotes já instalados (numpy, opencv, pillow, faster-whisper).
  Não dependa de app de desktop (Premiere, After, CapCut) nem de instalar programa.
- Scripts e arquivos (fontes, sons, logos, modelos) ficam DENTRO da pasta da skill e são citados pelo caminho relativo a ela.
  Arquivos da marca Cardápio Web podem ser lidos de {biblioteca} (só leitura) — copie para a skill só o que ela precisa.
- Nada de senhas, tokens ou chaves. Nada que publique em rede social ou mande algo para fora.
- Valem as regras do time (aprovação humana antes de publicar).
Escreva também NOTAS.md (fora do SKILL.md, para o time, até 10 linhas): o que a skill faz, o que mudou nesta versão e limitações.
Não precisa testar: o teste é o próximo passo, automático.
Responda APENAS com JSON: {{"ok": true, "notas": "uma linha"}}"""

TESTE = """Teste da skill "{nome}" ({nome_tecnico}, instalada agora), sem ninguém olhando: não pergunte nada.
Use a skill para fazer isto: {exemplo}
{refs}Grave TUDO o que for resultado em {saida} (vídeo, imagem, texto em .md). Se faltar algo para o teste, faça o melhor exemplo possível
e diga o que faltou. Não publique nada nem mande para fora.
Responda APENAS com JSON: {{"ok": true, "resumo": "o que fez e como ficou, em até 4 linhas", "arquivos": ["..."]}}"""


def criar_skill(E, t):
    v, s = t["versao"], t["skill"]
    pasta = E.BASE / "oficina" / "skill-v{}".format(v["id"])
    shutil.rmtree(pasta, ignore_errors=True)
    pasta.mkdir(parents=True)
    passo = lambda x: E.srv("skill_versao", id=v["id"], etapa=x)
    teste_dir = None
    try:
        passo("baixando as referências")
        refs = E._baixar_referencias(v, pasta / "refs")
        mostrar = []
        for r in refs:
            if E.VIDEO.search(r.name) or r.suffix.lower() == ".webm":
                mostrar += E._folhas(r, pasta / "refs") + [r]
            else:
                mostrar.append(r)
        skill = pasta / "skill"
        skill.mkdir()
        for rel, txt in (s.get("arquivos") or {}).items():
            alvo = (skill / rel).resolve()
            if skill.resolve() in alvo.parents:
                alvo.parent.mkdir(parents=True, exist_ok=True)
                alvo.write_text(txt, encoding="utf-8")
        if s.get("anexos"):
            kit._baixar_anexos(skill, s["anexos"])
        if s.get("conteudo"):
            (skill / "SKILL.md").write_text(s["conteudo"], encoding="utf-8")
        nome_tecnico = PREFIXO + s["slug"]
        passo("escrevendo a versão {}".format(v["versao"]))
        texto = E.claude(CRIADOR.format(
            quem=(v.get("pedido_por") or "o time").split("@")[0], pedido=v.get("pedido") or "", nome=s["nome"],
            situacao="skill nova" if not s.get("versao_atual") else "melhorar a versão {}".format(s["versao_atual"]),
            refs=", ".join(str(x) for x in mostrar) or "nenhuma", skill=skill, nome_tecnico=nome_tecnico, biblioteca=E.BIBLIOTECA_CW),
            pasta, v.get("modelo") or "sonnet", limite=2400, extra_dirs=[E.BIBLIOTECA_CW], trabalho="skill {} v{}".format(s["slug"], v["versao"]))
        if not (skill / "SKILL.md").exists():
            raise RuntimeError("o Criador não escreveu o SKILL.md")
        notas = (skill / "NOTAS.md").read_text(encoding="utf-8").strip() if (skill / "NOTAS.md").exists() else ""
        if not notas:
            try:
                notas = E.json_final(texto).get("notas") or ""
            except Exception:
                notas = ""
        # teste: instala a versão com outro nome e pede um exemplo de uso
        passo("testando a skill")
        teste_dir = SKILLS / "{}-teste-{}".format(nome_tecnico, v["id"])
        shutil.rmtree(teste_dir, ignore_errors=True)
        shutil.copytree(skill, teste_dir, ignore=shutil.ignore_patterns("NOTAS.md"))
        (teste_dir / "SKILL.md").write_text(kit.renomear((skill / "SKILL.md").read_text(encoding="utf-8"), teste_dir.name), encoding="utf-8")
        saida = pasta / "teste" / "saida"
        saida.mkdir(parents=True)
        exemplo = v.get("exemplo") or "um exemplo realista do que ela faz, com os arquivos de referência se houver"
        r = {}
        try:
            r = E.json_final(E.claude(TESTE.format(nome=s["nome"], nome_tecnico=teste_dir.name, exemplo=exemplo, saida=saida,
                                                   refs=("Arquivos de entrada: {}\n".format(", ".join(str(x) for x in refs)) if refs else "")),
                                      pasta / "teste", v.get("modelo") or "sonnet", limite=2400, extra_dirs=[teste_dir, pasta / "refs", E.BIBLIOTECA_CW],
                                      trabalho="teste da skill {} v{}".format(s["slug"], v["versao"])))
        except Exception as e:
            r = {"resumo": "o teste não terminou: {}".format(e)}
        passo("guardando a versão")
        arquivos_res = _resultados(saida, "skill-{}".format(v["id"]))
        conteudo, arquivos, anexos = _empacotar(skill)
        E.srv("skill_versao", id=v["id"], estado="aguardando", conteudo=conteudo, arquivos=arquivos, anexos=anexos, notas=notas[:3000],
              resultado={"texto": (r.get("resumo") or "")[:3000], "arquivos": [{k: x.get(k) for k in ("nome", "caminho", "tipo", "texto", "erro")} for x in arquivos_res]},
              etapa=None)
        E.log("skill", s["slug"], "versão", v["versao"], "pronta para aprovar")
    except Exception as ex:
        E.log("skill", s.get("slug"), "falhou:", traceback.format_exc()[-600:])
        E.srv("skill_versao", id=v["id"], estado="erro", erro=str(ex)[:900])
    finally:
        if teste_dir:
            shutil.rmtree(teste_dir, ignore_errors=True)


def criador(E):
    while True:
        try:
            t = E.srv("skill_pegar")
            if t:
                criar_skill(E, t)
                continue
        except Exception as e:
            E.log("criador de skills:", e)
        time.sleep(30)


# ------------------------------------------------------------------------------------------- agentes que trabalham
_AG = {"ts": 0, "lista": {}}
_TAM = {}


def agentes(E, forcar=False):
    if forcar or time.time() - _AG["ts"] > 60:
        try:
            lista = E.srv("agentes_trabalho") or []
            _AG["lista"] = {a["id"]: a for a in lista}
            _AG["ts"] = time.time()
            for a in lista:   # skills de Biblioteca/Kit escolhidas no agente: o texto basta
                for sk in a.get("skills") or []:
                    if sk.get("conteudo") and not (SKILLS / sk["nome"] / "SKILL.md").exists():
                        (SKILLS / sk["nome"]).mkdir(parents=True, exist_ok=True)
                        (SKILLS / sk["nome"] / "SKILL.md").write_text(kit.renomear(sk["conteudo"], sk["nome"]), encoding="utf-8")
        except Exception as e:
            E.log("agentes:", e)
    return _AG["lista"]


def pasta_entrada(E, a):
    return E.DRIVE / "CW Cast - Cortes" / "1. ENVIE AQUI" / a["pasta"]


def vigiar_e_agendar(E):
    """Pastas de entrada (arquivo/pasta nova e estável vira trabalho) e agenda (uma vez por dia, na hora marcada)."""
    while True:
        try:
            agora = datetime.now(BRT)
            for a in agentes(E).values():
                if a.get("gatilho") == "pasta" and a.get("pasta"):
                    p = pasta_entrada(E, a)
                    p.mkdir(parents=True, exist_ok=True)
                    for item in sorted(p.iterdir()):
                        if item.name.startswith((".", "~")) or item.name.lower() in ("desktop.ini",):
                            continue
                        tam = item.stat().st_size if item.is_file() else sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                        chave = "pasta:" + item.name
                        if _TAM.get((a["id"], chave)) != tam or tam == 0:   # espera parar de crescer (upload em andamento)
                            _TAM[(a["id"], chave)] = tam
                            continue
                        r = E.srv("tarefa_nova", agente=a["id"], origem="pasta", chave=chave,
                                  entrada={"arquivo": str(item.relative_to(E.DRIVE)), "texto": ""})
                        if r and r.get("id"):
                            E.log("agente", a["nome"], "recebeu", item.name)
                ag = a.get("agenda") or {}
                if a.get("gatilho") == "agenda" and agora.isoweekday() in (ag.get("dias") or []) and agora.strftime("%H:%M") >= (ag.get("hora") or "08:00"):
                    E.srv("tarefa_nova", agente=a["id"], origem="agenda", chave="agenda:" + agora.strftime("%Y-%m-%d"),
                          entrada={"texto": ag.get("pedido") or a.get("missao") or ""})
        except Exception as e:
            E.log("vigia dos agentes:", e)
        time.sleep(90)


PAPEIS = {"video": "edição de vídeo", "conteudo": "conteúdo e texto (legendas, roteiros, posts, e-mails)", "design": "imagens e design",
          "dados": "análise e relatórios", "operacao": "operação e organização do time", "outro": "tarefas gerais"}
SAIDAS = {"video": "vídeo final .mp4 em 1080x1920 quando for vídeo vertical, imagens, e textos em .md",
          "design": "imagens .png/.jpg na resolução pedida, e um .md explicando cada uma",
          "dados": "relatório em .md (com números e conclusões) e, se fizer sentido, planilha .csv",
          "conteudo": "os textos em .md, um arquivo por peça", "operacao": "o resultado em .md (listas, planos, respostas)", "outro": "o resultado em .md e os arquivos que fizer"}

TRABALHO = """Você é {nome}, um agente do CW Studios (time de conteúdo da Cardápio Web) com o papel de {papel}, trabalhando sozinho
no servidor: ninguém está olhando, então não pergunte nada — decida e siga.
Missão: {missao}
Como trabalhar:
{instrucoes}
Skills que você usa (já instaladas; use-as pelo nome): {skills}
Valem as regras e lições do time. Nada é publicado sem aprovação: você só prepara o resultado.
TRABALHO: {texto}
{entrada}{refs}{ajuste}{higgs}
{estilo}{etapa}Pasta de trabalho: {pasta}. Ponha o que vai ser entregue em {saida} ({formato}).
Responda APENAS com JSON: {{"ok": true, "resumo": "o que fez, em até 4 linhas", "titulo": "título curto do resultado",
"legenda": "legenda do post, se for vídeo para as redes", "arquivos": ["saida/..."]}}"""


def executar(E, item):
    t = item["tarefa"]
    a = agentes(E).get(item["agente"]) or agentes(E, forcar=True).get(item["agente"])
    passo = lambda x: E.srv("tarefa", id=t["id"], etapa=x)
    try:
        if not a:
            raise RuntimeError("o agente foi desligado")
        faltam = [k["nome"] for k in (a.get("skills") or []) + (a.get("estilos") or []) if (k.get("nome") or "").startswith(PREFIXO)
                  and not (SKILLS / k["nome"] / "SKILL.md").exists()]
        instalar_skills(forcar=bool(faltam), log=E.log)   # skill aprovada há pouco: instala na hora
        pasta = E.BASE / "agentes" / "t{}".format(t["id"])
        if not t.get("ajuste") and not int(t.get("etapa_idx") or 0):   # começo do trabalho (não ajuste nem etapa seguinte): pasta limpa
            shutil.rmtree(pasta, ignore_errors=True)
        saida = pasta / "saida"
        saida.mkdir(parents=True, exist_ok=True)
        ent = t.get("entrada") or {}
        extra = [E.BIBLIOTECA_CW]
        entrada_txt = ""
        if ent.get("arquivo"):
            alvo = E.DRIVE / ent["arquivo"]
            entrada_txt = "Material recebido na pasta do Drive: {}\n".format(alvo)
            extra.append(alvo if alvo.is_dir() else alvo.parent)
        passo("baixando o material")
        refs = E._baixar_referencias({"referencias": ent.get("referencias") or [], "links": ent.get("links") or []}, pasta / "refs")
        prints = E.baixar_prints({"ajuste_imagens": t.get("ajuste_imagens") or []}, pasta) if t.get("ajuste") else []
        if refs:
            extra.append(pasta / "refs")
        higgs = int(a.get("higgs") or 0)
        # estilo de edição: o escolhido no pedido ou o padrão do agente (o processo das etapas é o mesmo)
        estilos = a.get("estilos") or []
        escolhido = (ent.get("estilo") or {}).get("nome")
        estilo = next((x for x in estilos if x.get("titulo") == escolhido), None) or (estilos[int(a.get("estilo_padrao") or 0)] if estilos else None)
        a = dict(a, _estilo=estilo)
        etapas = [dict(e, skill=(estilo or {}).get("nome") if e.get("skill") == "estilo" else e.get("skill")) for e in a.get("etapas") or []]
        i = min(int(t.get("etapa_idx") or 0), max(len(etapas) - 1, 0))
        while True:   # sem etapas: uma rodada só; com etapas: segue em ordem até uma que pare para aprovar (a última sempre para)
            et = etapas[i] if etapas else None
            r = _rodada(E, a, t, et, i, etapas, ent, entrada_txt, refs, prints, higgs, pasta, saida, extra)
            ultima = not etapas or i >= len(etapas) - 1
            if ultima or et.get("aprovar"):
                break
            i += 1
            t = dict(t, ajuste=None)
            prints = []
        passo("guardando o resultado")
        arquivos = _resultados(saida, "tarefa-{}".format(t["id"]))
        rotulo = "Etapa {} de {}: {} · esperando aprovação".format(i + 1, len(etapas), et["nome"]) if etapas else "esperando aprovação"
        if estilo:
            rotulo += " · estilo " + estilo.get("titulo", "")
        E.srv("tarefa", id=t["id"], estado="previa", etapa=rotulo, etapa_idx=i, etapas_total=len(etapas),
              resultado={"texto": (r.get("resumo") or "")[:3000], "titulo": (r.get("titulo") or "")[:200], "legenda": (r.get("legenda") or "")[:2200],
                         "etapa": et["nome"] if etapas else "", "arquivos": arquivos})
        E.log("agente", a["nome"], "trabalho", t["id"], "pronto para aprovar", rotulo)
    except Exception as ex:
        E.log("trabalho", t["id"], "falhou:", traceback.format_exc()[-600:])
        E.srv("tarefa", id=t["id"], estado="erro", erro=str(ex)[:900])


def _rodada(E, a, t, et, i, etapas, ent, entrada_txt, refs, prints, higgs, pasta, saida, extra):
    """Uma chamada do Claude: o trabalho inteiro (agente sem etapas) ou uma etapa."""
    passo = lambda x: E.srv("tarefa", id=t["id"], etapa=x)
    if et:
        passo("Etapa {} de {}: {}".format(i + 1, len(etapas), et["nome"]))
        plano = "\n".join("  {}. {}{}".format(n + 1, e["nome"], " (você está aqui)" if n == i else "") for n, e in enumerate(etapas))
        etapa_txt = ("ETAPA {} de {}: {}. Faça SÓ esta etapa{}.\n{}\nPlano do trabalho:\n{}\nAs etapas anteriores já foram feitas: o que elas "
                     "produziram está na pasta de trabalho.\n").format(
            i + 1, len(etapas), et["nome"], (" usando a skill " + et["skill"]) if et.get("skill") else "",
            et.get("instrucao") or "", plano)
    else:
        passo("trabalhando")
        etapa_txt = ""
    texto = E.claude(TRABALHO.format(
        nome=a["nome"], papel=PAPEIS.get(a.get("papel") or "video", "tarefas gerais"), formato=SAIDAS.get(a.get("papel") or "video", SAIDAS["outro"]),
        missao=a.get("missao") or "", instrucoes=a.get("instrucoes") or "(sem instruções extras)",
        skills=", ".join(s["nome"] for s in a.get("skills") or []) or "(nenhuma: use o que o Claude já sabe)",
        texto=ent.get("texto") or ("processe o material recebido" if ent.get("arquivo") else ""),
        entrada=entrada_txt, refs=("Referências: {}\n".format(", ".join(str(x) for x in refs)) if refs else ""),
        ajuste=("AJUSTE PEDIDO PELO TIME (você já fez este trabalho; parta do que está na pasta e mude SÓ isto): {}\n{}".format(
            t["ajuste"], E.texto_prints(prints)) if t.get("ajuste") else ""),
        higgs=("Higgsfield liberado: até {} geração(ões) (ferramenta cw-studios › gerar).".format(higgs) if higgs else "Higgsfield: não use."),
        estilo=("ESTILO DE EDIÇÃO deste trabalho: \"{}\" — o acabamento (visual, legenda, gancho, cores, sons, trilha) segue a skill {}. "
                "O processo (o que selecionar, cortar, enquadrar) é o das etapas/instruções.\n".format(a["_estilo"]["titulo"], a["_estilo"]["nome"])
                if a.get("_estilo") and a["_estilo"].get("nome") else ""),
        etapa=etapa_txt, pasta=pasta, saida=saida), pasta, (et or {}).get("modelo") or a.get("modelo") or "sonnet", ao_passo=passo, limite=3600,
        extra_dirs=extra, higgs=higgs, papel=a.get("papel") or "video", trabalho="agente {} · trabalho {}".format(a["nome"], t["id"]))
    try:
        return E.json_final(texto)
    except Exception:
        return {"resumo": texto[-1500:]}


def operario(E):
    while True:
        try:
            item = E.srv("tarefa_pegar")
            if item:
                executar(E, item)
                continue
        except Exception as e:
            E.log("operário dos agentes:", e)
        time.sleep(15)


def entregar(E, item):
    t = item["tarefa"]
    a = agentes(E).get(item["agente"]) or agentes(E, forcar=True).get(item["agente"]) or {}
    res = t.get("resultado") or {}
    arquivos = [Path(x["local"]) for x in res.get("arquivos") or [] if x.get("local") and Path(x["local"]).is_file()]
    try:
        if a.get("entrega") == "publicacao":
            video = next((f for f in arquivos if re.search(r"\.(mp4|mov)$", f.name, re.I)), None)
            if not video:
                raise RuntimeError("não há vídeo .mp4 para mandar à Publicação")
            saida = subprocess.run([sys.executable, str(AQUI / "publicar.py"), "enviar", "--arquivo", str(video),
                                    "--titulo", (res.get("titulo") or a.get("nome") or "Vídeo")[:120], "--legenda", res.get("legenda") or res.get("titulo") or ""]
                                   + (["--destino", a["destino"]] if a.get("destino") else []), capture_output=True, text=True, timeout=900)
            m = re.search(r"P-(\d+)", saida.stdout)
            if not m:
                raise RuntimeError("não consegui mandar para a Publicação: " + (saida.stderr or saida.stdout)[-300:])
            E.srv("tarefa", id=t["id"], estado="entregue", etapa="na Publicação", publicacao_id=int(m.group(1)), entrega_info="Publicação P-" + m.group(1))
        elif a.get("entrega") == "pasta":
            rel = (a.get("destino") or "2. PARA APROVAR/{}".format(a.get("nome") or "agente")).strip("/")
            if ".." in Path(rel).parts:
                raise RuntimeError("pasta de destino inválida")
            destino = E.DRIVE / "CW Cast - Cortes" / rel / "{} - {}".format(datetime.now(BRT).strftime("%Y-%m-%d"), _nome_seguro(res.get("titulo") or "trabalho {}".format(t["id"])))
            destino.mkdir(parents=True, exist_ok=True)
            for f in arquivos:
                shutil.copyfile(f, destino / f.name)
            E.srv("tarefa", id=t["id"], estado="entregue", etapa="na pasta", entrega_info=str(destino.relative_to(E.DRIVE)))
        else:
            E.srv("tarefa", id=t["id"], estado="entregue", etapa="aprovado", entrega_info="aprovado na Oficina")
    except Exception as ex:
        E.srv("tarefa", id=t["id"], estado="erro", erro=("entrega: " + str(ex))[:900])


def entregador(E):
    while True:
        try:
            item = E.srv("tarefa_aprovada")
            if item:
                entregar(E, item)
                continue
        except Exception as e:
            E.log("entregador dos agentes:", e)
        time.sleep(20)


def ligar(E, operarios=2):
    try:
        E.srv("tarefas_orfas")
    except Exception as e:
        E.log("órfãos da Oficina:", e)
    threading.Thread(target=instalar_skills, kwargs={"forcar": True, "log": E.log}, daemon=True).start()
    threading.Thread(target=criador, args=(E,), daemon=True).start()
    threading.Thread(target=vigiar_e_agendar, args=(E,), daemon=True).start()
    threading.Thread(target=entregador, args=(E,), daemon=True).start()
    for _ in range(operarios):
        threading.Thread(target=operario, args=(E,), daemon=True).start()
    E.log("Oficina ligada:", operarios, "operário(s) para os agentes")
