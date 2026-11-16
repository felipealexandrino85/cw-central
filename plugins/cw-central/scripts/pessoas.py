#!/usr/bin/env python3
"""Pessoas conhecidas da esteira: a ficha (rosto SFace + voz CAM++) de alguém que aparece em vários episódios, e quanto de um
trecho é a voz dessa pessoa. Primeiro uso: o Glautin, que corta só o Glauton (16/11/2026).

A ficha é dado pessoal (biometria): fica em ~/.cw-cortes/pessoas/<nome>.json da máquina que produz, NUNCA no repositório
(o cw-central é público). Este arquivo só tem o código.

  python3 pessoas.py ficha <nome> <video> [<video> ...]     monta a ficha com vídeos em que só a pessoa fala
  python3 pessoas.py medir <nome> <video> [--de s --ate s]  parte do trecho com a voz dela (JSON)
"""
import argparse
import json
import sys
from pathlib import Path

import falante

PASTA = Path.home() / ".cw-cortes" / "pessoas"
VOZ_MESMA = 0.50   # semelhança CAM++ com a ficha para contar como a voz da pessoa. Calibrado em 16/11/2026 (Glauton): vídeos dele
                   # fora da ficha 81–93% das janelas ≥ 0,50; episódio sem ele (CW Cast #50) 0–14%
CORTE_MIN = 0.65   # parte mínima da fala de um corte que tem de ser a voz da pessoa (deixa pergunta curta de quem entrevista)
ROSTO_MESMO = 0.55  # SFace: o mesmo limite do host no mapa do episódio (mesma pessoa 0,84–0,95)


def _centro(X, corta=30):
    import numpy as np
    X = np.asarray(X, float)
    c = X.mean(0)
    c /= np.linalg.norm(c) + 1e-9
    d = X @ c
    c = X[d >= np.percentile(d, corta)].mean(0)   # tira o que está mais longe (outra pessoa, música alta, risada)
    return c / (np.linalg.norm(c) + 1e-9)


def ficha(nome, videos):
    import numpy as np
    rostos, vozes = [], []
    for v in videos:
        for _, q in falante._rostos_keyframes(v, cada=0.5):
            if q:
                rostos.append(max(q, key=lambda x: x[3])[2])   # o rosto maior do quadro
        a = falante._audio(v)
        ts, _ = falante._janelas_voz(a)
        if ts:
            vozes.extend(falante._impressoes(a, ts))
    if len(rostos) < 20 or len(vozes) < 40:
        raise SystemExit("pouco material: {} rostos e {} janelas de voz (use vídeos em que só a pessoa fala)".format(len(rostos), len(vozes)))
    R, V = _centro(rostos), _centro(vozes)
    sv = np.asarray(vozes) @ V
    f = {"nome": nome, "rosto": [round(float(x), 5) for x in R], "voz": [round(float(x), 5) for x in V],
         "n_rostos": len(rostos), "n_voz": len(vozes), "voz_mediana": round(float(np.median(sv)), 3),
         "voz_p10": round(float(np.percentile(sv, 10)), 3), "videos": [Path(v).name for v in videos]}
    PASTA.mkdir(parents=True, exist_ok=True)
    (PASTA / (nome + ".json")).write_text(json.dumps(f), encoding="utf-8")
    return f


def carregar(nome):
    try:
        return json.loads((PASTA / (nome + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def voz_no_video(video, f, de=None, ate=None):
    """[(t, semelhança com a voz da ficha)] a cada 0,5 s com fala (t no tempo do vídeo)."""
    import numpy as np
    a = falante._audio(video, de, (ate - de) if (de is not None and ate) else None)
    ts, _ = falante._janelas_voz(a)
    if not ts:
        return []
    E = falante._impressoes(a, ts)
    s = E @ np.asarray(f["voz"], float)
    t0 = de or 0.0
    return [(round(t0 + float(t), 2), round(float(x), 3)) for t, x in zip(ts, s)]


def parte(serie, ini, fim, limite=VOZ_MESMA):
    """Parte das janelas com fala do trecho que são a voz da pessoa (None: sem fala medida)."""
    xs = [s for t, s in serie if ini <= t <= fim]
    return round(sum(s >= limite for s in xs) / len(xs), 3) if xs else None


def rosto_no_mapa(mapa, f):
    """Id da pessoa da ficha no mapa de quem é quem do episódio (falante.mapa_episodio), ou None."""
    import numpy as np
    r = np.asarray(f["rosto"], float)
    melhor = max(((float(np.asarray(p["rosto"], float) @ r), p["id"]) for p in (mapa or {}).get("pessoas") or []), default=(0, None))
    return melhor[1] if melhor[0] >= ROSTO_MESMO else None


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ficha")
    a.add_argument("nome")
    a.add_argument("videos", nargs="+")
    m = sub.add_parser("medir")
    m.add_argument("nome")
    m.add_argument("video")
    m.add_argument("--de", type=float)
    m.add_argument("--ate", type=float)
    x = ap.parse_args()
    if x.cmd == "ficha":
        f = ficha(x.nome, x.videos)
        print(json.dumps({k: v for k, v in f.items() if k not in ("rosto", "voz")}, ensure_ascii=False))
        return 0
    f = carregar(x.nome)
    if not f:
        sys.exit("sem ficha de {} em {}".format(x.nome, PASTA))
    serie = voz_no_video(x.video, f, x.de, x.ate)
    xs = [s for _, s in serie]
    print(json.dumps({"janelas": len(xs), "parte": parte(serie, -1, 1e9),
                      "mediana": round(sorted(xs)[len(xs) // 2], 3) if xs else None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
