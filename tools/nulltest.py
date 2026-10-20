#!/Users/olivierbruchez/Github/prince-org-archiver/.venv/bin/python
"""Test d'annulation entre deux fichiers audio: même source ou non ?

Usage:
    nulltest.py reference.flac candidat.flac [--write sortie.flac]

Aligne le candidat sur la référence à l'échantillon près, cherche le gain optimal,
puis mesure ce qui reste après soustraction. Avec --write, écrit le candidat aligné
et mis à niveau, prêt à être inversé dans un éditeur.

Lecture des résultats:
   au-delà de -30 dB : même source numérique, à un gain près
   -10 à -30 dB      : parenté probable, mais une génération ou un traitement diffère
   au-dessus de -6 dB: rien de commun, ou alignement impossible (vitesse différente)

Un décalage qui dérive au fil du morceau signale une différence de vitesse: le rapport
est alors donné en pourcentage et en cents.

Dépendances: numpy, ffmpeg.
"""
import subprocess, sys, os
try:
    import numpy as np
    np.frombuffer  # le numpy de Homebrew est cassé: paquet vide sans __init__.py
except AttributeError:
    sys.exit("numpy est cassé pour cet interpréteur. Lancez le script avec\n"
             "  ~/Github/prince-org-archiver/.venv/bin/python " + __file__ + " ...\n"
             "ou réparez le numpy système avec: brew reinstall numpy")

SR = 44100


def decode(path, channels=1):
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", str(channels), "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode()[:400])
    return np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64)


def coarse_offset(a, b, start_s=60, dur_s=40):
    i0 = int(start_s * SR)
    exc = a[i0:i0 + int(dur_s * SR)]
    if len(exc) < SR:
        i0 = 0
        exc = a[:min(len(a), int(dur_s * SR))]
    exc = exc - exc.mean()
    n = 1 << int(np.ceil(np.log2(len(b) + len(exc))))
    C = np.fft.irfft(np.fft.rfft(b - b.mean(), n) * np.conj(np.fft.rfft(exc, n)), n)
    return int(np.argmax(C[:len(b)])) - i0


def local_offset(a, b, t_s, guess, dur_s=12, search_s=2.0):
    i0 = int(t_s * SR)
    exc = a[i0:i0 + int(dur_s * SR)]
    if len(exc) < int(dur_s * SR) * 0.9:
        return None, 0.0
    exc = exc - exc.mean()
    j0 = max(0, i0 + guess - int(search_s * SR))
    j1 = min(len(b), i0 + guess + int(dur_s * SR) + int(search_s * SR))
    seg = b[j0:j1]
    if len(seg) < len(exc) * 1.2:
        return None, 0.0
    seg = seg - seg.mean()
    n = 1 << int(np.ceil(np.log2(len(seg) + len(exc))))
    C = np.fft.irfft(np.fft.rfft(seg, n) * np.conj(np.fft.rfft(exc, n)), n)
    k = int(np.argmax(C[:len(seg) - len(exc)]))
    norm = np.sqrt(np.sum(exc ** 2) * np.sum(seg[k:k + len(exc)] ** 2)) + 1e-30
    return (j0 + k) - i0, C[k] / norm


def depth(x, y):
    g = np.dot(x, y) / (np.dot(y, y) + 1e-30)
    r = x - g * y
    d = 20 * np.log10((np.sqrt(np.mean(r ** 2)) + 1e-30) / (np.sqrt(np.mean(x ** 2)) + 1e-30))
    return d, g


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    ref, cand = sys.argv[1], sys.argv[2]
    a, b = decode(ref), decode(cand)
    print(f"référence : {os.path.basename(ref)}  ({len(a)/SR:.3f} s)")
    print(f"candidat  : {os.path.basename(cand)}  ({len(b)/SR:.3f} s)")

    g0 = coarse_offset(a, b)
    dur = len(a) / SR
    anchors = [t for t in np.linspace(0.08 * dur, 0.92 * dur, 8)]
    rows = [(t,) + local_offset(a, b, t, g0) for t in anchors]
    rows = [r for r in rows if r[1] is not None]
    offs = np.array([r[1] for r in rows], float)
    cors = np.array([r[2] for r in rows], float)
    print(f"\ndécalage local aux points de contrôle (corrélation) :")
    print("  " + "  ".join(f"{t:.0f}s:{o:+d}/{c:.2f}" for t, o, c in rows))

    if len(rows) >= 3 and (cors > 0.30).sum() >= 3:
        ts = np.array([r[0] for r in rows], float)
        sel = cors > 0.30
        slope = np.polyfit(ts[sel], offs[sel], 1)[0]
        ratio = 1 + slope / SR
        if abs(slope) > 0.5:
            print(f"  dérive {slope:+.2f} éch/s  ->  vitesse {(ratio-1)*100:+.4f} % "
                  f"({1200*np.log2(ratio):+.2f} cents)")
        else:
            print("  pas de dérive : vitesses identiques")

    off = int(np.median(offs))
    best = (off, 1e9, 1.0)
    for o in range(off - 4, off + 5):
        y = b[o:] if o >= 0 else np.concatenate([np.zeros(-o), b])
        m = min(len(a), len(y))
        s = slice(int(0.05 * m), int(0.95 * m))
        d, g = depth(a[:m][s], y[:m][s])
        if d < best[1]:
            best = (o, d, g)
    off, d, g = best
    print(f"\nmeilleur décalage : {off:+d} échantillons ({off/SR*1000:+.1f} ms)")
    print(f"gain optimal      : {g:.5f}  ({20*np.log10(abs(g)+1e-30):+.3f} dB)")
    print(f"résidu            : {d:+.2f} dB sous le signal")
    if d < -30:
        print("-> même source numérique, à un simple gain près")
    elif d < -10:
        print("-> parenté probable, mais une génération ou un traitement diffère")
    else:
        print("-> rien de commun, ou alignement impossible")

    if "--write" in sys.argv:
        out = sys.argv[sys.argv.index("--write") + 1]
        st = decode(cand, channels=2).reshape(-1, 2)
        if off >= 0:
            st = st[off:]
        else:
            st = np.vstack([np.zeros((-off, 2)), st])
        need = len(a) - len(st)
        if need > 0:
            st = np.vstack([st, np.zeros((need, 2))])
        st = (st[:len(a)] * g).astype(np.float32)
        p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR),
                              "-ac", "2", "-i", "-", "-c:a", "flac", "-sample_fmt", "s32", out],
                             stdin=subprocess.PIPE)
        p.communicate(st.tobytes())
        print(f"\nécrit : {out}  (aligné et mis à niveau, à inverser dans l'éditeur)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
