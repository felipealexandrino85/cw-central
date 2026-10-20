#!/usr/bin/env python3
"""Publicação: manda um corte pronto para a fila da central. A produção aprova (ou recusa) na página e o publicador
posta no Instagram, TikTok e YouTube Shorts.

Só biblioteca padrão (ffmpeg/ffprobe, se houver, para duração e prévia). O arquivo é copiado para a pasta
"1. Para aprovar" da publicação no Google Drive compartilhado (o Drive para computador sobe sozinho).

  enviar --arquivo CORTE.mp4 --titulo "..." --legenda "..." [--titulo-youtube "..."] [--episodio EP52] [--jira SCC-1]
         [--redes instagram,tiktok,youtube] [--agendar "2026-10-21 18:00"]
  minhas                       situação dos seus envios (aguardando, aprovado, postado, recusado + motivo)
  pasta                        mostra onde fica a pasta da publicação nesta máquina
"""
import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import central  # noqa: E402

PASTA_PADRAO = "CW Studios - Produção/CW Cast - Cortes/11. PUBLICAÇÃO"
PARA_APROVAR = "1. Para aprovar"
SUBS = ("Drives compartilhados", "Shared drives", "Meu Drive", "My Drive", "")


def pasta_publicacao():
    """<drive compartilhado>/…/11. PUBLICAÇÃO nesta máquina (a primeira raiz do Drive em que a pasta-mãe existe)."""
    if os.environ.get("CW_PASTA_PUBLICACAO"):
        return Path(os.path.expanduser(os.environ["CW_PASTA_PUBLICACAO"]))
    relativa = central.config().get("pasta_publicacao") or PASTA_PADRAO
    try:
        import registro
        raizes = registro.raizes_drive()
    except Exception:
        raizes = sorted((Path.home() / "Library" / "CloudStorage").glob("GoogleDrive-*"))
    mae = Path(relativa).parent
    for r in raizes:
        for sub in SUBS:
            base = Path(r) / sub if sub else Path(r)
            if (base / mae).is_dir():
                return base / relativa
    return None


def slug(texto):
    import unicodedata
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:50] or "corte"


def duracao(arquivo):
    if not shutil.which("ffprobe"):
        return None
    try:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(arquivo)],
                                    capture_output=True, text=True, timeout=30).stdout.strip() or 0) or None
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def previa(arquivo, dur):
    """3 quadros (15%, 50%, 85%) lado a lado, em data URL, para o card da aprovação."""
    if not dur or not shutil.which("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        saida = os.path.join(tmp, "previa.jpg")
        args = ["ffmpeg", "-nostdin", "-v", "error"]
        for f in (0.15, 0.5, 0.85):
            args += ["-ss", "{:.2f}".format(dur * f), "-i", str(arquivo)]
        args += ["-filter_complex", "[0:v]scale=-2:300[a];[1:v]scale=-2:300[b];[2:v]scale=-2:300[c];[a][b][c]hstack=3",
                 "-frames:v", "1", "-q:v", "6", "-y", saida]
        try:
            if subprocess.run(args, capture_output=True, timeout=90).returncode or not os.path.exists(saida):
                return None
        except (subprocess.SubprocessError, OSError):
            return None
        return "data:image/jpeg;base64," + base64.b64encode(open(saida, "rb").read()).decode()


def agendamento(texto):
    if not texto:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d/%m %H:%M"):
        try:
            d = datetime.strptime(texto.strip(), fmt)
            if fmt == "%d/%m %H:%M":
                d = d.replace(year=datetime.now().year)
            return d.astimezone().isoformat()          # horário desta máquina (Brasília)
        except ValueError:
            continue
    sys.exit("não entendi o horário '{}': use 2026-10-21 18:00 ou 21/10 18:00".format(texto))


def cmd_enviar(a):
    origem = Path(os.path.expanduser(a.arquivo))
    if not origem.is_file():
        sys.exit("não achei o arquivo {}".format(origem))
    if origem.suffix.lower() not in (".mp4", ".mov"):
        sys.exit("as redes aceitam .mp4 (ou .mov); exporte o corte em mp4")
    base = pasta_publicacao()
    if not base:
        sys.exit("não achei o Google Drive do CW Studios nesta máquina (pasta 'CW Studios - Produção/CW Cast - Cortes'). "
                 "Confira se o Google Drive para computador está aberto e com o drive compartilhado disponível.")
    destino_pasta = base / PARA_APROVAR
    destino_pasta.mkdir(parents=True, exist_ok=True)
    nome = "{}_{}_{}{}".format(datetime.now().strftime("%Y-%m-%d"), slug(a.titulo), os.urandom(2).hex(), origem.suffix.lower())
    dur = duracao(origem)
    avisos = []
    if dur and dur > 180:
        avisos.append("tem {:.0f} s: o YouTube só trata como Shorts até 3 min".format(dur))
    if dur and dur > 90:
        avisos.append("Reels acima de 90 s não aparecem na aba Reels de quem não segue")
    imagem = previa(origem, dur)
    shutil.copy2(origem, destino_pasta / nome)
    redes = [r.strip() for r in (a.redes or "instagram,tiktok,youtube").split(",") if r.strip()]
    pid = central.rpc("cw_pub_enviar", timeout=30, p_dados={
        "titulo": a.titulo, "legenda": a.legenda or "", "titulo_youtube": a.titulo_youtube or a.titulo[:100],
        "episodio": a.episodio, "jira": a.jira, "redes": redes, "arquivo": nome,
        "pasta": str(Path(central.config().get("pasta_publicacao") or PASTA_PADRAO) / PARA_APROVAR),
        "tamanho": origem.stat().st_size, "duracao_s": round(dur, 1) if dur else None, "previa": imagem,
        "agendado_para": agendamento(a.agendar)})
    print("Enviado para aprovação: P-{:04d} \"{}\" ({}). Arquivo no Drive: {}/{}".format(
        pid, a.titulo, ", ".join(redes), PARA_APROVAR, nome))
    for av in avisos:
        print("Atenção:", av)


ESTADOS = {"aguardando": "aguardando aprovação", "aprovado": "aprovado, vai ser postado", "postando": "postando agora",
           "postado": "postado", "recusado": "recusado", "erro": "deu erro ao postar", "cancelado": "cancelado"}


def cmd_minhas(_a):
    itens = central.rpc("cw_pub_minhas") or []
    if not itens:
        print("Nenhum corte enviado para publicação ainda.")
    for x in itens:
        links = ", ".join("{}: {}".format(r, v.get("link") or v.get("estado")) for r, v in (x.get("resultados") or {}).items())
        print("P-{:04d} {} · {}{}{}".format(x["id"], x["titulo"], ESTADOS.get(x["estado"], x["estado"]),
                                          " · motivo: " + x["motivo"] if x.get("motivo") else "", " · " + links if links else ""))


def cmd_pasta(_a):
    p = pasta_publicacao()
    print(p / PARA_APROVAR if p else "não achei a pasta do Drive nesta máquina")


def main():
    ap = argparse.ArgumentParser(description="Publicação CW Studios")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("enviar")
    p.add_argument("--arquivo", required=True)
    p.add_argument("--titulo", required=True)
    p.add_argument("--legenda", default="")
    p.add_argument("--titulo-youtube")
    p.add_argument("--episodio")
    p.add_argument("--jira")
    p.add_argument("--redes")
    p.add_argument("--agendar")
    sub.add_parser("minhas")
    sub.add_parser("pasta")
    a = ap.parse_args()
    {"enviar": cmd_enviar, "minhas": cmd_minhas, "pasta": cmd_pasta}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except central.ErroCentral as e:
        sys.exit("central: {}".format(e))
