#!/Users/olivierbruchez/Github/prince-org-archiver/.venv/bin/python
"""Détecte les signatures d'une source dégradée (encodage avec perte, gravure vinyle).

Usage:
    lossycheck.py fichier.flac [fichier2.flac ...]
    lossycheck.py --png dossier/ *.flac     # écrit aussi un spectrogramme par fichier

Sortie, par fichier:
  - coupure haute par trame (percentiles), indépendante du niveau
  - part de trames sans aucune énergie entre 16 et 20 kHz
  - plancher du spectre moyen, pour repérer une coupure franche
Un encodage avec perte donne une coupure basse et stable, et beaucoup de trames vides
en haut. Un transfert analogique garde un plancher de bruit continu jusqu'en haut.

Dépendances: numpy, ffmpeg (et sox pour --png).
"""
import subprocess, sys, os, json
try:
    import numpy as np
    np.frombuffer  # le numpy de Homebrew est cassé: paquet vide sans __init__.py
except AttributeError:
    sys.exit("numpy est cassé pour cet interpréteur. Lancez le script avec\n"
             "  ~/Github/prince-org-archiver/.venv/bin/python " + __file__ + " ...\n"
             "ou réparez le numpy système avec: brew reinstall numpy")

SR = 44100
N = 4096


def decode(path):
    """Décode n'importe quel format en mono float64 à 44,1 kHz."""
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode()[:400])
    return np.frombuffer(p.stdout, dtype=np.float32).astype(np.float64)


def analyse(x):
    freqs = np.fft.rfftfreq(N, 1 / SR)
    win = np.hanning(N)
    cuts, dead, tot = [], 0, 0
    acc = np.zeros(N // 2 + 1)
    for i in range(0, len(x) - N, N):
        seg = x[i:i + N] * win
        S = np.abs(np.fft.rfft(seg))
        if S.max() < 1e-6:
            continue
        acc += S ** 2
        db = 20 * np.log10(S + 1e-30)
        above = np.where(db > db.max() - 60)[0]
        cuts.append(freqs[above[-1]] if len(above) else 0.0)
        hf = S[(freqs >= 16000) & (freqs < 20000)]
        if 20 * np.log10(np.sqrt(np.mean(hf ** 2)) / (N / 4) + 1e-30) < -110:
            dead += 1
        tot += 1
    acc /= max(tot, 1)
    mean_db = 10 * np.log10(acc + 1e-30)
    mean_db -= mean_db.max()
    idx = np.where(mean_db > -70)[0]
    return np.array(cuts), dead / max(tot, 1), (freqs[idx[-1]] if len(idx) else 0.0), tot


def verdict(cuts, dead, floor):
    q = np.percentile(cuts, [50, 95])
    if dead > 0.20:
        return "ENCODAGE AVEC PERTE très probable"
    if dead > 0.05 or q[1] < 17000:
        return "source dégradée probable"
    if floor < 16500:
        return "coupure franche du spectre moyen, à examiner"
    return "pas de signature de codec"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    png = "--png" in sys.argv
    if not args:
        print(__doc__)
        return 1
    for path in args:
        try:
            x = decode(path)
        except Exception as e:
            print(f"{os.path.basename(path)} : erreur, {e}")
            continue
        cuts, dead, floor, tot = analyse(x)
        q = np.percentile(cuts, [5, 25, 50, 75, 95])
        print(f"\n{os.path.basename(path)}")
        print(f"  durée {len(x)/SR:7.2f} s, {tot} trames analysées")
        print(f"  coupure par trame  5% {q[0]:6.0f}  25% {q[1]:6.0f}  50% {q[2]:6.0f}"
              f"  75% {q[3]:6.0f}  95% {q[4]:6.0f} Hz")
        print(f"  trames sans énergie 16-20 kHz : {100*dead:5.1f} %")
        print(f"  spectre moyen, plancher -70 dB à {floor:6.0f} Hz")
        print(f"  -> {verdict(cuts, dead, floor)}")
        if png:
            out = os.path.splitext(path)[0] + " (spectrogramme).png"
            subprocess.run(["sox", path, "-n", "spectrogram", "-o", out,
                            "-x", "1400", "-y", "600", "-z", "100",
                            "-t", os.path.basename(path)], capture_output=True)
            print(f"  spectrogramme : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
