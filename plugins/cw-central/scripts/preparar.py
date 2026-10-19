#!/usr/bin/env python3
"""Pré-produção de um vídeo na máquina do editor, na hora de pegar o trabalho.

Acha o material na pasta padrão do Drive (CW Studios - Produção › 0. ENVIE AQUI - Demandas › <CHAVE>), copia para a
pasta de trabalho (o que também força o download do Drive), mede cada arquivo, transcreve se a máquina tiver
transcritor, escreve o BRIEF.md (pedido, regras do time, material) e abre o vídeo na central na etapa Preparo.

Só biblioteca padrão (o transcritor roda no ambiente do cw-cortes, se existir).

uso: preparar.py CHAVE [--destino PASTA] [--sem-transcricao] [--modelo small] [--json]
     CHAVE = card do Jira (SCC-1762) ou demanda da central (D-0012 ou 12)
Sai com código 2 quando não acha o material (a mensagem diz onde colocar).
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import central  # noqa: E402
import registro  # noqa: E402

VIDEO = {".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi", ".mxf", ".mts"}
AUDIO = {".wav", ".mp3", ".m4a", ".aac", ".flac"}
DOCS = {".txt", ".md", ".docx", ".pdf", ".gdoc", ".rtf", ".srt"}
IMAGENS = {".png", ".jpg", ".jpeg", ".svg", ".webp"}
PASTA_PADRAO = "CW Studios - Produção/0. ENVIE AQUI - Demandas"
VENV_PY = Path(os.path.expanduser("~/.cw-cortes/venv/bin/python"))


def normalizar(chave):
    chave = chave.strip().upper()
    m = re.fullmatch(r"(?:D-?)?0*(\d{1,5})", chave)
    return "D-{:04d}".format(int(m.group(1))) if m else chave


def pasta_entrada():
    """Pasta de entrada do time no Google Drive sincronizado (ou CW_PASTA_ENTRADA)."""
    if os.environ.get("CW_PASTA_ENTRADA"):
        return Path(os.path.expanduser(os.environ["CW_PASTA_ENTRADA"]))
    relativa = central.config().get("pasta_entrada") or PASTA_PADRAO
    raizes = []
    for g in glob.glob(os.path.expanduser("~/Library/CloudStorage/GoogleDrive-*")) + glob.glob("/mnt/*/"):
        for sub in ("Drives compartilhados", "Shared drives", "Meu Drive", "My Drive", ""):
            raizes.append(Path(g) / sub)
    for r in raizes:
        if (r / relativa).is_dir():
            return r / relativa
    return None


def achar_pasta(entrada, chave):
    if not entrada:
        return None
    exata = entrada / chave
    if exata.is_dir():
        return exata
    for p in sorted(entrada.iterdir()):
        if p.is_dir() and re.match(re.escape(chave) + r"\b", p.name, re.I):
            return p
    return None


def arquivos(pasta):
    saida = {"videos": [], "audios": [], "docs": [], "imagens": []}
    for p in sorted(pasta.rglob("*")):
        if not p.is_file() or p.name.startswith(".") or len(p.relative_to(pasta).parts) > 3:
            continue
        ext = p.suffix.lower()
        grupo = "videos" if ext in VIDEO else "audios" if ext in AUDIO else "docs" if ext in DOCS else "imagens" if ext in IMAGENS else None
        if grupo:
            saida[grupo].append(p)
    return saida


def copiar(origem, destino):
    """Copia (forçando o download do Drive); pula se já existe com o mesmo tamanho."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists() and destino.stat().st_size == origem.stat().st_size:
        return False
    tmp = destino.with_name(destino.name + ".parcial")
    shutil.copy2(origem, tmp)
    os.replace(tmp, destino)
    return True


def medir(arquivo):
    if not shutil.which("ffprobe"):
        return {}
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate,channels",
                        "-of", "json", str(arquivo)], capture_output=True, text=True, timeout=60)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        return {}
    info = {"duracao_s": round(float(d.get("format", {}).get("duration") or 0), 1)}
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and "largura" not in info:
            num, _, den = (s.get("r_frame_rate") or "0/1").partition("/")
            info.update({"largura": s.get("width"), "altura": s.get("height"),
                         "fps": round(float(num) / float(den or 1), 2) if float(den or 1) else None})
        if s.get("codec_type") == "audio":
            info["audio_canais"] = s.get("channels")
    return info


def transcritor():
    """Comando de transcrição disponível nesta máquina, ou None."""
    if VENV_PY.exists():
        ok = subprocess.run([str(VENV_PY), "-c", "import faster_whisper"], capture_output=True).returncode == 0
        if ok:
            return [str(VENV_PY), str(AQUI / "transcrever.py")]
    return None


def fmt_dur(s):
    s = int(s or 0)
    return "{}:{:02d}".format(s // 60, s % 60) if s < 3600 else "{}:{:02d}:{:02d}".format(s // 3600, s % 3600 // 60, s % 60)


def main():
    ap = argparse.ArgumentParser(description="Pré-produção do vídeo ao pegar o trabalho")
    ap.add_argument("chave")
    ap.add_argument("--destino")
    ap.add_argument("--sem-transcricao", action="store_true")
    ap.add_argument("--modelo", default="small")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    chave = normalizar(a.chave)
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    eh_demanda = chave.startswith("D-")

    demanda = None
    if eh_demanda and central.configurada():
        try:
            demanda = central.rpc("cw_ver", p_id=int(chave[2:]))
        except central.ErroCentral as e:
            print("central: {}".format(e), file=sys.stderr)

    entrada = pasta_entrada()
    pasta = achar_pasta(entrada, chave)
    if not pasta:
        onde = str(entrada / chave) if entrada else "{} › {} (o Google Drive para computador precisa estar aberto)".format(
            "Drive compartilhado", (central.config().get("pasta_entrada") or PASTA_PADRAO).replace("/", " › ") + " › " + chave)
        msg = "Não achei o material de {}. Ele deve ficar em: {}".format(chave, onde)
        if demanda and demanda.get("link_bruto"):
            msg += "\nA demanda tem um link de material: {} (baixe e coloque na pasta, ou me diga o caminho local).".format(demanda["link_bruto"])
        print(msg)
        sys.exit(2)

    trabalho = Path(os.path.expanduser(a.destino or os.environ.get("CW_TRABALHO") or "~/CW-Work")) / "preparo" / chave
    material = arquivos(pasta)
    if not material["videos"] and not material["audios"]:
        print("A pasta {} existe, mas não tem vídeo nem áudio ainda.".format(pasta))
        sys.exit(2)

    titulo = (demanda or {}).get("titulo") or re.sub(r"^" + re.escape(chave) + r"[\s\-–_]*", "", pasta.name, flags=re.I) or chave
    tipo = (demanda or {}).get("tipo") or "outro"

    # abre o vídeo na central já na etapa Preparo (a sessão não abre outro sozinha depois)
    tid = None
    if sid:
        t = registro.trabalho_da_sessao(sid)
        if t:
            tid = t["id"]
            registro.gravar({"ev": "vincular", "id": tid, "jira": "" if eh_demanda else chave,
                             "demanda": chave if eh_demanda else "", "titulo": titulo, "tipo": tipo, "sessao": sid})
        else:
            tid = "{}-{}-{}".format(registro.datetime.now().strftime("%Y%m%d"), registro.slug(titulo)[:40], registro.uuid.uuid4().hex[:4])
            registro.gravar({"ev": "inicio", "id": tid, "tipo": tipo, "titulo": titulo, "area": (demanda or {}).get("area") or "",
                             "entrada": str(trabalho / "bruto"), "demanda": chave if eh_demanda else "",
                             "jira": "" if eh_demanda else chave, "sessao": sid, "auto": True})
        registro.marcar_etapa(sid, "preparo", "preparando o material de " + chave)

    print("Copiando o material de {} para {}".format(pasta, trabalho), flush=True)
    copiados = []
    for grupo in ("videos", "audios", "docs", "imagens"):
        for src in material[grupo]:
            dst = trabalho / "bruto" / src.relative_to(pasta)
            novo = copiar(src, dst)
            info = medir(dst) if grupo in ("videos", "audios") else {}
            copiados.append({"grupo": grupo, "arquivo": str(dst), "nome": str(src.relative_to(pasta)), **info})
            print("  {} {}{}".format("copiado" if novo else "já estava", src.relative_to(pasta),
                                     " ({})".format(fmt_dur(info["duracao_s"])) if info.get("duracao_s") else ""), flush=True)

    transcricoes = []
    cmd = None if a.sem_transcricao else transcritor()
    if cmd:
        if sid:
            registro.marcar_etapa(sid, "transcricao", "transcrevendo o material de " + chave)
        for c in [x for x in copiados if x["grupo"] in ("videos", "audios")][:6]:
            saida = trabalho / "transcricao" / Path(c["nome"]).with_suffix("").name
            if Path(str(saida) + ".txt").exists():
                transcricoes.append(str(saida) + ".txt")
                continue
            print("Transcrevendo {} (modelo {})...".format(c["nome"], a.modelo), flush=True)
            r = subprocess.run(cmd + [c["arquivo"], str(saida), "--modelo", a.modelo], capture_output=True, text=True)
            if r.returncode == 0:
                transcricoes.append(str(saida) + ".txt")
            else:
                print("  não consegui transcrever: {}".format((r.stderr or r.stdout).strip()[-300:]), flush=True)

    regras = [r for r in registro.regras_do_time() if not r.get("tipo") or r.get("tipo") == tipo]
    url_jira = (central.config().get("jira") or "https://cardapio-web.atlassian.net").rstrip("/")
    linhas = ["# {} · {}".format(chave, titulo), ""]
    if eh_demanda and demanda:
        linhas += ["- **Origem:** demanda da Central CW Studios", "- **Tipo:** {}".format(tipo),
                   "- **Publicação:** {}".format(demanda.get("perfil") or "–"), "- **Prazo:** {}".format(demanda.get("prazo") or "sem prazo"),
                   "- **Prioridade:** {}".format(demanda.get("prioridade")),
                   "- **Pediu:** {} ({}){}".format(demanda.get("solicitante_nome") or "", demanda.get("solicitante_email"),
                                                   " · " + demanda["area"] if demanda.get("area") else "")]
        if demanda.get("link_roteiro"):
            linhas.append("- **Roteiro:** {}".format(demanda["link_roteiro"]))
        linhas += ["", "## O pedido", "", demanda.get("descricao") or "(sem descrição)"]
        ajustes = [e for e in demanda.get("eventos") or [] if e.get("tipo") == "refazer"]
        if ajustes:
            linhas += ["", "## Ajuste pedido (o que mudar agora)", "", ajustes[-1].get("detalhe") or ""]
    else:
        linhas += ["- **Origem:** card do Jira {}/browse/{}".format(url_jira, chave),
                   "- Se o conector do Jira estiver disponível, leia a descrição do card e complete este brief "
                   "(CTA, nomes, prazo). Senão, pergunte ao editor só o que faltar."]
    linhas += ["", "## Material ({})".format(pasta), "", "| Arquivo | Duração | Resolução | Áudio |", "|---|---|---|---|"]
    for c in copiados:
        if c["grupo"] in ("videos", "audios"):
            res = "{}×{} {}fps".format(c.get("largura"), c.get("altura"), c.get("fps")) if c.get("largura") else "–"
            linhas.append("| {} | {} | {} | {} |".format(c["nome"], fmt_dur(c.get("duracao_s")), res,
                                                        "{} canal(is)".format(c["audio_canais"]) if c.get("audio_canais") else "sem áudio"))
    outros = [c["nome"] for c in copiados if c["grupo"] in ("docs", "imagens")]
    if outros:
        linhas += ["", "Outros arquivos: " + ", ".join(outros)]
    linhas += ["", "## Transcrição", ""]
    linhas += ["- {}".format(t) for t in transcricoes] or ["Sem transcrição automática nesta máquina: o seu fluxo de edição transcreve."]
    if regras:
        linhas += ["", "## Regras do time (valem para este vídeo)", ""] + ["- {}".format(r["texto"]) for r in regras]
    linhas += ["", "## Próximo passo", "", "Material em `{}`. Siga o seu fluxo de edição a partir daqui; ao terminar, diga "
               "\"entreguei\" para fechar o vídeo na central.".format(trabalho / "bruto")]
    trabalho.mkdir(parents=True, exist_ok=True)
    (trabalho / "BRIEF.md").write_text("\n".join(linhas) + "\n", encoding="utf-8")
    (trabalho / "manifesto.json").write_text(json.dumps({"chave": chave, "titulo": titulo, "tipo": tipo, "origem": str(pasta),
                                                         "material": copiados, "transcricoes": transcricoes, "registro": tid},
                                                        ensure_ascii=False, indent=1), encoding="utf-8")
    if sid:
        registro.atualizar_status(sid, "sinal")

    resumo = {"chave": chave, "titulo": titulo, "tipo": tipo, "pasta_trabalho": str(trabalho), "brief": str(trabalho / "BRIEF.md"),
              "videos": [c["arquivo"] for c in copiados if c["grupo"] == "videos"], "transcricoes": transcricoes, "registro": tid}
    if a.json:
        print(json.dumps(resumo, ensure_ascii=False, indent=1))
    else:
        print("\nPronto: {} preparado em {}".format(chave, trabalho))
        print("Brief: {}".format(resumo["brief"]))
        print("{} vídeo(s), {} transcrição(ões){}".format(len(resumo["videos"]), len(transcricoes),
                                                        "" if cmd or a.sem_transcricao else " (sem transcritor nesta máquina)"))


if __name__ == "__main__":
    main()
