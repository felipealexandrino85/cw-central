"""faster_whisper "de mentira" para os scripts dos estilos rodarem no servidor: transcreve no whisper-large-v3 da Groq
(pela função Ferramentas da central, a mesma do podcast) em vez do Whisper no processador, que é bem mais lento.
Mesma interface que os scripts usam: WhisperModel(...).transcribe(audio, language=..., word_timestamps=True) devolve
(segmentos com .start/.end/.text/.words[.word/.start/.end/.probability], info). Se a Groq falhar, usa o faster-whisper de verdade.
Fica ativo pelo PYTHONPATH que a esteira põe (não vale para "python -I")."""
import base64
import os
import subprocess
import sys
import tempfile
import time
import wave
from types import SimpleNamespace

_SHIM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PEDACO = 600   # a Groq aceita até ~25 MB por pedido: 10 min em mp3 de 32 kbps cabe com folga


def _whisper_de_verdade():
    import importlib.machinery
    import importlib.util
    caminhos = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != _SHIM]
    spec = importlib.machinery.PathFinder.find_spec("faster_whisper", caminhos)
    if spec is None:
        raise ImportError("faster-whisper não está instalado")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _para_wav(audio, destino):
    if isinstance(audio, (str, os.PathLike)):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(audio), "-ac", "1", "-ar", "16000", destino], check=True)
        return
    import numpy as np
    x = np.clip(np.asarray(audio, dtype="float32"), -1, 1)
    with wave.open(destino, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def _groq(audio, language):
    pasta = os.environ.get("CW_SCRIPTS") or ""
    if pasta and pasta not in sys.path:
        sys.path.insert(0, pasta)
    import ferramentas_mcp
    tmp = tempfile.mkdtemp(prefix="groq-")
    wav = os.path.join(tmp, "a.wav")
    _para_wav(audio, wav)
    with wave.open(wav) as w:
        dur = w.getnframes() / float(w.getframerate())
    segs = []
    for k in range(int(dur // _PEDACO) + 1):
        ini = k * _PEDACO
        if ini >= dur:
            break
        mp3 = os.path.join(tmp, "p{}.mp3".format(k))
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(ini), "-t", str(_PEDACO), "-i", wav, "-ac", "1", "-ar", "16000",
                        "-b:a", "32k", mp3], check=True)
        r = None
        for tentativa in range(5):
            try:
                r = ferramentas_mcp.chamar({"acao": "transcrever", "ferramenta": "groq", "modelo": "whisper-large-v3", "idioma": language or "pt",
                                            "nome": os.path.basename(mp3), "audio_b64": base64.b64encode(open(mp3, "rb").read()).decode(),
                                            "trabalho": "estilo"}, timeout=300)
                if r.get("erro"):
                    raise RuntimeError(r["erro"])
                break
            except Exception:
                if tentativa == 4:
                    raise
                time.sleep(4 * (tentativa + 1))   # limite de pedidos por minuto: espera e tenta de novo
        palavras = [SimpleNamespace(word=" " + (w.get("p") or "").strip(), start=round(w["i"] + ini, 3), end=round(w["f"] + ini, 3), probability=1.0)
                    for w in r.get("palavras") or []]
        for sg in r.get("segmentos") or []:
            a, b = sg["inicio"] + ini, sg["fim"] + ini
            segs.append(SimpleNamespace(start=a, end=b, text=" " + (sg.get("texto") or "").strip(),
                                        words=[w for w in palavras if a - 0.05 <= w.start < b + 0.05]))
        if not r.get("segmentos") and palavras:
            segs.append(SimpleNamespace(start=palavras[0].start, end=palavras[-1].end, text=" ".join(w.word.strip() for w in palavras), words=palavras))
    return segs, SimpleNamespace(language=language or "pt", duration=dur)


class WhisperModel:
    def __init__(self, *a, **k):
        self._a, self._k, self._local = a, k, None

    def transcribe(self, audio, language=None, word_timestamps=False, **k):
        try:
            segs, info = _groq(audio, language)
            return iter(segs), info
        except Exception as e:
            print("transcrição na Groq falhou ({}); usando o Whisper do servidor".format(e), file=sys.stderr)
            if self._local is None:
                self._local = _whisper_de_verdade().WhisperModel(*self._a, **self._k)
            return self._local.transcribe(audio, language=language, word_timestamps=word_timestamps, **k)
