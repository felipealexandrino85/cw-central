#!/usr/bin/env python3
"""Transcreve um vídeo ou áudio em português com faster-whisper (roda no ambiente do cw-cortes).

Grava <saida>.txt (texto corrido), <saida>.srt (legenda com tempo) e <saida>.json (segmentos e palavras com tempo).
uso: ~/.cw-cortes/venv/bin/python transcrever.py ARQUIVO SAIDA_SEM_EXTENSAO [--modelo small]
"""
import argparse
import json
from pathlib import Path


def tempo_srt(s):
    ms = int(round(s * 1000))
    return "{:02d}:{:02d}:{:02d},{:03d}".format(ms // 3600000, ms % 3600000 // 60000, ms % 60000 // 1000, ms % 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arquivo")
    ap.add_argument("saida")
    ap.add_argument("--modelo", default="small")
    a = ap.parse_args()
    from faster_whisper import WhisperModel
    modelo = WhisperModel(a.modelo, device="auto", compute_type="int8")
    segmentos, _info = modelo.transcribe(a.arquivo, language="pt", word_timestamps=True, vad_filter=True)
    segs = [{"inicio": round(s.start, 2), "fim": round(s.end, 2), "texto": s.text.strip(),
             "palavras": [{"p": w.word.strip(), "i": round(w.start, 2), "f": round(w.end, 2)} for w in (s.words or [])]}
            for s in segmentos]
    saida = Path(a.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    Path(str(saida) + ".txt").write_text("\n".join(s["texto"] for s in segs) + "\n", encoding="utf-8")
    Path(str(saida) + ".srt").write_text("".join("{}\n{} --> {}\n{}\n\n".format(i, tempo_srt(s["inicio"]), tempo_srt(s["fim"]), s["texto"])
                                                 for i, s in enumerate(segs, 1)), encoding="utf-8")
    Path(str(saida) + ".json").write_text(json.dumps({"arquivo": a.arquivo, "modelo": a.modelo, "segmentos": segs},
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
    print(len(segs), "segmentos")


if __name__ == "__main__":
    main()
