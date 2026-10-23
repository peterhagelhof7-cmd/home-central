"""Heizungsverlauf als PNG fuer den Bot-Befehl 'heizung verlauf [stunden]'.

Quelle ist die Verlaufs-Datenbank (history), Zuordnung ueber die Rollen
(Einstellungen -> Heizung). Zwei Felder uebereinander statt zweiter y-Achse:
oben Kessel/Puffer/Warmwasser, unten Abgas (anderer Wertebereich). Phasen mit
Feuer (ab "Anheizen"/"Heizen" bis "Feuer Aus") sind grau hinterlegt.

Braucht python3-matplotlib (deploy/install.sh); fehlt es, meldet der Bot das.
"""
import io
import time
from datetime import datetime

from . import heizung, history
from .tz import TZ

DEFAULT_HOURS = 12
MAX_HOURS = 7 * 24

# (Rolle, Beschriftung) - Reihenfolge = feste Farbreihenfolge (dataviz-Referenzpalette)
TOP = [("kessel", "Kessel"), ("puffer_oben", "Puffer oben"), ("puffer_mitte", "Puffer mitte"),
       ("puffer_unten", "Puffer unten"), ("warmwasser", "Warmwasser")]
BOTTOM = ("abgas", "Abgas")
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
BOTTOM_COLOR = "#6250d6"   # eigener Farbton, damit Abgas nicht wie "Kessel" aussieht
SURFACE, INK, INK2, GRID, FIRE = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#ecebe7"


class NoMatplotlib(RuntimeError):
    pass


def fire_spans(since: int, until: int) -> list[tuple[int, int]]:
    """Zeitraeume mit Feuer aus den aufgezeichneten Kessel-Zustaenden."""
    st = [s for s in history.states_between(since - 2 * 86400, until) if s["src"] == "hz" and s["key"] == "state"]
    spans, start = [], None
    for s in st:
        if start is None and heizung.is_burning(s["value"]):
            start = s["ts"]
        elif start is not None and heizung.is_fire_off(s["value"]):
            spans.append((start, s["ts"]))
            start = None
    if start is not None:
        spans.append((start, until))
    return [(max(a, since), b) for a, b in spans if b > since]


def _series(roles: dict[str, str], since: int, until: int) -> dict[str, dict]:
    """{rolle: {"unit", "points"}} fuer die zugeordneten Rollen mit Daten."""
    sids = {s["key"]: s["id"] for s in history.series_list() if s["src"] == "hz"}
    want = {sids[k]: rid for rid, k in roles.items() if k in sids}
    out = {}
    for q in history.query(list(want), since, until, points=300):
        pts = q["points"]
        if not pts:
            continue
        # Der Recorder schreibt nur Aenderungen (spaetestens alle KEEPALIVE): ist der
        # letzte Punkt juenger, gilt der Wert bis jetzt -> Linie bis zum rechten Rand
        if until - pts[-1][0] <= history.KEEPALIVE + history.STEP:
            pts = pts + [[until, pts[-1][1]]]
        out[want[q["id"]]] = {"unit": q["unit"], "points": pts}
    return out


def _spread(ys: list[float], gap: float) -> list[float]:
    """Endbeschriftungen senkrecht auseinanderschieben (Reihenfolge bleibt)."""
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    out = list(ys)
    for a, b in zip(order, order[1:]):
        if out[b] - out[a] < gap:
            out[b] = out[a] + gap
    return out


def render(hours: int = DEFAULT_HOURS, now_ts: float | None = None) -> tuple[bytes, str]:
    """PNG + Bildunterschrift. ValueError = nichts zu zeigen (Text an den Nutzer)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise NoMatplotlib("python3-matplotlib fehlt auf dem Server") from e

    hours = max(1, min(MAX_HOURS, int(hours)))
    until = int(now_ts or time.time())
    since = until - hours * 3600
    roles = history.roles()
    if not roles:
        raise ValueError("keine Werte zugeordnet (home-central → Heizung → Zuordnung)")
    data = _series(roles, since, until)
    top = [(rid, label, COLORS[i]) for i, (rid, label) in enumerate(TOP) if rid in data]
    has_bottom = BOTTOM[0] in data
    if not top and not has_bottom:
        raise ValueError(f"keine aufgezeichneten Werte in den letzten {hours} h")

    def dt(ts):
        return datetime.fromtimestamp(ts, TZ)

    rows = (2 if top and has_bottom else 1)
    fig, axes = plt.subplots(rows, 1, figsize=(9, 6.4 if rows == 2 else 4.2), dpi=110, sharex=True,
                             gridspec_kw={"height_ratios": [3, 1.3]} if rows == 2 else None,
                             facecolor=SURFACE)
    axes = list(axes) if rows == 2 else [axes]
    spans = fire_spans(since, until)

    def style(ax, title, pad=6):
        ax.set_facecolor(SURFACE)
        ax.set_title(title, loc="left", fontsize=11, color=INK, pad=pad)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(INK2)
        ax.tick_params(colors=INK2, labelsize=9, length=0)
        ax.set_xlim(dt(since), dt(until))
        for a, b in spans:
            ax.axvspan(dt(a), dt(b), color=FIRE, linewidth=0, zorder=0)

    def plot(ax, rid, label, color):
        pts = data[rid]["points"]
        ax.plot([dt(t) for t, _ in pts], [v for _, v in pts], color=color, linewidth=2,
                solid_capstyle="round", label=label)

    if top:
        ax = axes[0]
        style(ax, f"Temperaturen (°C) – letzte {hours} h", pad=24)
        for rid, label, color in top:
            plot(ax, rid, label, color)
        # Endbeschriftung in Textfarbe (Linienfarben haben teils < 3:1 Kontrast)
        lo, hi = ax.get_ylim()
        last = [data[rid]["points"][-1][1] for rid, _, _ in top]
        ys = _spread(last, (hi - lo) * 0.06)
        for (rid, label, color), v, y in zip(top, last, ys):
            ax.annotate(f"{label} {v:.0f}".replace(".", ","), xy=(1.0, y), xycoords=("axes fraction", "data"),
                        xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=INK,
                        annotation_clip=False)
            ax.plot([1.0], [y], marker="o", markersize=5, color=color, transform=ax.get_yaxis_transform(),
                    clip_on=False)
        # Legende zwischen Titel und Feld, damit sie keine Linie verdeckt
        ax.legend(loc="lower left", fontsize=8.5, frameon=False, ncol=len(top), labelcolor=INK2,
                  bbox_to_anchor=(0, 1.0), borderaxespad=0.2, handlelength=1.6)
    if has_bottom:
        ax = axes[-1]
        rid, label = BOTTOM
        style(ax, f"{label} (°C)")
        plot(ax, rid, label, BOTTOM_COLOR)
        ax.set_ylim(bottom=0)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=TZ))
    axes[-1].xaxis.set_major_locator(mdates.AutoDateLocator(tz=TZ, minticks=4, maxticks=8))
    if spans:
        axes[-1].annotate("grau = Feuer an", xy=(0, -0.28 if rows == 2 else -0.14), xycoords="axes fraction",
                          fontsize=8.5, color=INK2)
    fig.tight_layout()
    fig.subplots_adjust(right=0.84)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)

    first = min(p[0][0] for p in (d["points"] for d in data.values()))
    end = f"{dt(until):%H:%M}" if dt(since).date() == dt(until).date() else f"{dt(until):%d.%m. %H:%M}"
    cap = f"\U0001F525 Heizung – Verlauf {dt(since):%d.%m. %H:%M} bis {end}"
    if first > since + 1800:
        cap += f"\n(Aufzeichnung erst ab {dt(first):%H:%M})"
    return buf.getvalue(), cap
