"""Logging + barre de progression partages par les scripts du pipeline.

- vprint(...)  : comme print() (stdout), mais seulement avec -v / --verbose
- eprint(...)  : toujours affiche, sur stderr (erreurs, questions interactives)
- Progress     : barre de progression sur stderr, imbricable, sans dependance

Barre + erreurs sur stderr : run.sh peut rediriger le stdout de Blender vers
/dev/null hors verbose sans perdre la barre ni les erreurs.
"""
import shutil
import sys
import time


def _cli_args():
    # Sous Blender : arguments apres "--" ; en Python simple : sys.argv[1:]
    return sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]


VERBOSE = any(a in ("-v", "--verbose") for a in _cli_args())
_stack = []          # barres actives (la derniere est la plus interne)
_drawn = False       # une barre est actuellement affichee sur la ligne courante


def _clear():
    global _drawn
    if _drawn:
        sys.stdout.flush()
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        _drawn = False


def vprint(*args, **kwargs):
    """print() uniquement en mode verbose (efface/redessine la barre autour)."""
    if not VERBOSE:
        return
    _clear()
    print(*args, **kwargs)
    sys.stdout.flush()
    if _stack:
        _stack[-1].draw(force=True)


def eprint(*args, **kwargs):
    """print() toujours affiche, sur stderr (erreurs, questions)."""
    _clear()
    kwargs.setdefault("file", sys.stderr)
    kwargs.setdefault("flush", True)
    print(*args, **kwargs)
    if _stack:
        _stack[-1].draw(force=True)


def _fmt_time(s):
    s = int(s)
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class Progress:
    """Barre de progression.

        with Progress(len(items), "Objets") as bar:
            for it in items:
                ...
                bar.update()

    - Imbricable : le prefixe affiche les barres parentes ("Interieurs 3/12 > Decor ...").
    - disable=True : ne fait rien (utile pour une barre a 1 seul element).
    - `bar.total` peut etre modifie en cours de route (file d'attente dynamique).
    - Hors terminal (log, pipe) : une ligne tous les 10 % au lieu de la barre en place.
    """
    WIDTH = 28

    def __init__(self, total, label="", disable=False):
        self.total = max(int(total), 0)
        self.label = label
        self.n = 0
        self.disable = disable
        self.t0 = time.time()
        self._tty = sys.stderr.isatty()
        self._last_draw = 0.0
        self._last_decile = -1

    # -- contexte -----------------------------------------------------------
    def __enter__(self):
        if not self.disable:
            _stack.append(self)
            self.draw(force=True)
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        global _drawn
        if self.disable or self not in _stack:
            return
        self.draw(force=True)
        if self._tty:
            sys.stderr.write("\n")
            sys.stderr.flush()
        _drawn = False
        _stack.remove(self)
        if _stack:
            _stack[-1].draw(force=True)

    # -- API ------------------------------------------------------------------
    def update(self, n=1):
        self.n += n
        self.draw()

    def set_label(self, label):
        self.label = label
        self.draw(force=True)

    # -- rendu ----------------------------------------------------------------
    def _prefix(self):
        parts = [f"{b.label} {b.n}/{b.total}" for b in _stack if b is not self]
        return (" > ".join(parts) + " > ") if parts else ""

    def draw(self, force=False):
        global _drawn
        if self.disable:
            return
        now = time.time()
        frac = (self.n / self.total) if self.total else 1.0
        frac = min(frac, 1.0)
        elapsed = now - self.t0
        eta = (elapsed / self.n * (self.total - self.n)) if self.n and self.total > self.n else 0
        text = (f"{self._prefix()}{self.label} {self.n}/{self.total} "
                f"{int(frac * 100):3d}% {_fmt_time(elapsed)}"
                + (f" ETA {_fmt_time(eta)}" if eta else ""))

        if not self._tty:
            decile = int(frac * 10)
            if decile != self._last_decile:
                self._last_decile = decile
                print(f"[progress] {text}", file=sys.stderr, flush=True)
            return

        if not force and now - self._last_draw < 0.05 and self.n < self.total:
            return  # limite le rafraichissement
        self._last_draw = now
        filled = int(self.WIDTH * frac)
        bar = "█" * filled + "░" * (self.WIDTH - filled)
        cols = shutil.get_terminal_size((100, 20)).columns
        line = f"[{bar}] {text}"
        sys.stderr.write("\r\033[K" + line[:max(cols - 1, 20)])
        sys.stderr.flush()
        _drawn = True

