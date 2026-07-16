"""Macro-news screening — how high-impact economic data maps to a crypto bias.

This is a *screening / analysis* layer, deliberately kept OFF the live trade
engine. It reads the same ForexFactory High-Impact feed the dashboard already
shows and answers the question the trader keeps asking:

    "Kalau CPI turun / inflasi turun / suku bunga dipotong, dampaknya ke crypto
     bagus atau tidak?"

The model is the **monetary-policy / liquidity channel**, which is the dominant
driver for Bitcoin & alts over the last cycle:

  * Data that pushes the central bank toward EASING (inflation cooling, rate
    cuts, softer jobs) drains money out of "risk-off" and into risk assets →
    **RISK_ON → bullish crypto**.
  * Data that pushes toward TIGHTENING (hot inflation, rate hikes, red-hot jobs)
    → **RISK_OFF → bearish crypto**.

Each event is scored on a signed scale (roughly -1 … +1) where **positive =
bullish for crypto**. The score combines:

  * *direction of the indicator* (does a higher print help or hurt crypto), and
  * *the surprise* (actual vs forecast) or, when screening an upcoming event,
    the *expectation* (forecast vs previous).

Nothing here places or blocks a trade; it only labels events and feeds the
3-year backtest (`scripts/backtest_news.py`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Event taxonomy
# --------------------------------------------------------------------------
# ``sign`` = +1 when a HIGHER print is bullish for crypto, -1 when a higher
# print is bearish. ``weight`` = how strongly this release moves the crypto
# liquidity thesis (rate decisions & inflation dominate).
#
# Matching is by keyword against the event title (case-insensitive). Order
# matters: the first pattern that matches wins, so put the specific ones first.

@dataclass(frozen=True)
class EventType:
    key: str
    label: str
    sign: int          # +1 higher-is-bullish, -1 higher-is-bearish
    weight: float      # 0..1 importance for the crypto liquidity thesis
    patterns: tuple[str, ...]


# NB: a higher interest rate / higher inflation is BEARISH for crypto → sign -1.
#     a higher unemployment / more jobless claims is DOVISH → BULLISH → sign +1.
EVENT_TYPES: tuple[EventType, ...] = (
    # ---- Monetary policy (highest weight) ----
    EventType("rate_decision", "Keputusan Suku Bunga", -1, 1.00, (
        "federal funds rate", "fomc statement", "rate statement", "rate decision",
        "official bank rate", "overnight rate", "cash rate target", "cash rate",
        "official cash rate", "main refinancing rate", "deposit facility rate",
        "interest rate decision", "monetary policy statement", "policy rate",
        "prime loan rate",
    )),
    EventType("fomc_comm", "Komunikasi Bank Sentral", -1, 0.55, (
        "fomc press conference", "fomc economic projections", "fomc meeting minutes",
        "monetary policy report", "press conference", "monetary policy statement",
        "testifies", "member speaks", "gov ", "governor", "president speaks",
        "chair", "speaks",
    )),
    # ---- Inflation (very high weight; cooler = bullish) ----
    EventType("inflation", "Inflasi", -1, 0.90, (
        "core cpi", "cpi", "core pce", "pce price", "core ppi", "ppi",
        "consumer price", "producer price", "inflation rate", "hicp", "gdp price",
        "employment cost", "unit labor cost", "import prices", "wage price",
    )),
    # ---- Labour: softer labour = dovish = bullish (sign +1 handled per line) ----
    EventType("unemployment", "Tingkat Pengangguran", +1, 0.55, (
        "unemployment rate", "jobless rate",
    )),
    EventType("jobless_claims", "Klaim Pengangguran", +1, 0.35, (
        "unemployment claims", "jobless claims", "initial claims", "continuing claims",
    )),
    EventType("payrolls", "Data Ketenagakerjaan", -1, 0.70, (
        "non-farm employment", "nonfarm", "non farm", "employment change",
        "adp ", "payrolls", "employment report",
    )),
    EventType("earnings", "Pendapatan/Upah", -1, 0.35, (
        "average hourly earnings", "average earnings", "wage growth",
    )),
    # ---- Growth / activity: modest risk-on when strong (sign +1, low weight) ----
    EventType("growth", "Pertumbuhan/Aktivitas", +1, 0.30, (
        "gdp", "retail sales", "core retail", "durable goods", "industrial production",
        "manufacturing production", "pmi", "ism ", "business confidence",
        "consumer confidence", "consumer sentiment", "trade balance", "factory orders",
        "building permits", "housing starts", "home sales", "nonfarm productivity",
    )),
)

# When ``actual`` beats/misses this fraction of the |forecast| (or a flat floor),
# we call it a real surprise; below that it is "sesuai perkiraan" (in line).
_SURPRISE_EPS = 0.02        # relative
_SURPRISE_FLOOR = 0.05      # absolute floor for values near zero

# Score thresholds for the RISK label.
RISK_ON_TH = 0.15
RISK_OFF_TH = -0.15


def classify_type(title: str) -> EventType | None:
    """Return the EventType whose keyword matches ``title`` (specific first)."""
    t = (title or "").lower()
    for et in EVENT_TYPES:
        for pat in et.patterns:
            if pat in t:
                return et
    return None


# --------------------------------------------------------------------------
# Number parsing (ForexFactory-style: "3.2%", "250K", "1.35M", "<0.1%", "-0.3")
# --------------------------------------------------------------------------
_NUM_RE = re.compile(r"[-+]?\d*\.?\d+")
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


def parse_number(raw) -> float | None:
    """Parse a ForexFactory numeric field into a float (or None if empty)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().lower()
    if not s or s in {"n/a", "-", "tentative"}:
        return None
    m = _NUM_RE.search(s.replace(",", ""))
    if not m:
        return None
    try:
        val = float(m.group())
    except ValueError:
        return None
    for suf, mul in _SUFFIX.items():
        if suf in s:
            val *= mul
            break
    return val


@dataclass
class Assessment:
    """Result of scoring one economic event for its crypto impact."""
    matched: bool
    type_key: str
    type_label: str
    bias: str                 # RISK_ON / RISK_OFF / NEUTRAL
    verdict: str              # bagus / buruk / netral (for crypto)
    score: float              # signed, +bullish
    reason: str               # human-readable (Indonesian)
    basis: str                # "surprise" (actual vs forecast) or "expectation"
    surprise: float | None = None
    fields: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "matched": self.matched,
            "type_key": self.type_key,
            "type_label": self.type_label,
            "bias": self.bias,
            "verdict": self.verdict,
            "score": round(self.score, 3),
            "reason": self.reason,
            "basis": self.basis,
            "surprise": round(self.surprise, 4) if self.surprise is not None else None,
        }


def _bias_from_score(score: float) -> tuple[str, str]:
    if score >= RISK_ON_TH:
        return "RISK_ON", "bagus"
    if score <= RISK_OFF_TH:
        return "RISK_OFF", "buruk"
    return "NEUTRAL", "netral"


def _direction_word(sign_signal: int) -> str:
    """sign_signal: +1 value went up, -1 value went down, 0 flat."""
    return "naik" if sign_signal > 0 else "turun" if sign_signal < 0 else "flat"


def assess_event(title: str, actual=None, forecast=None, previous=None) -> Assessment:
    """Score one event for crypto.

    * Historical / released event → use the **surprise** (actual vs forecast,
      fallback actual vs previous). This is what actually moved the market.
    * Upcoming event (no actual) → use the **expectation** (forecast vs previous)
      so the screen can say "kalau rilis sesuai perkiraan, dampaknya ...".
    """
    et = classify_type(title)
    a = parse_number(actual)
    f = parse_number(forecast)
    p = parse_number(previous)

    if et is None:
        return Assessment(False, "other", "Lainnya", "NEUTRAL", "netral", 0.0,
                          "Jenis rilis tidak dikenali oleh model makro.", "none",
                          fields={"actual": a, "forecast": f, "previous": p})

    # Pick the comparison base.
    if a is not None and f is not None:
        base, comp, basis = a, f, "surprise"      # actual vs forecast
    elif a is not None and p is not None:
        base, comp, basis = a, p, "surprise"      # actual vs previous
    elif f is not None and p is not None:
        base, comp, basis = f, p, "expectation"   # forecast vs previous (upcoming)
    else:
        return Assessment(True, et.key, et.label, "NEUTRAL", "netral", 0.0,
                          f"{et.label}: angka belum tersedia untuk dinilai.", "none",
                          fields={"actual": a, "forecast": f, "previous": p})

    denom = max(abs(comp), _SURPRISE_FLOOR)
    surprise = (base - comp) / denom              # >0 print HIGHER than base
    move = 0
    if surprise > _SURPRISE_EPS:
        move = 1
    elif surprise < -_SURPRISE_EPS:
        move = -1

    # crypto score = (did the print go up/down) * (higher-is-bullish?) * weight,
    # scaled by how big the surprise was (capped so one print can't dominate).
    mag = min(abs(surprise), 1.0)
    score = et.sign * move * et.weight * (0.5 + 0.5 * mag)
    bias, verdict = _bias_from_score(score)

    # ---- Reason (Indonesian, references the trader's own examples) ----
    dir_word = _direction_word(move)
    what = "lebih tinggi dari perkiraan" if basis == "surprise" and move > 0 else \
           "lebih rendah dari perkiraan" if basis == "surprise" and move < 0 else \
           "diperkirakan naik" if basis == "expectation" and move > 0 else \
           "diperkirakan turun" if basis == "expectation" and move < 0 else \
           "sesuai perkiraan"

    if et.key in ("inflation",):
        effect = ("inflasi mendingin → peluang bank sentral melonggarkan → likuiditas "
                  "naik → BULLISH crypto") if score > 0 else \
                 ("inflasi memanas → bank sentral cenderung hawkish → likuiditas "
                  "tertekan → BEARISH crypto") if score < 0 else \
                 "inflasi sesuai perkiraan → dampak ke crypto minim"
    elif et.key == "rate_decision":
        effect = ("suku bunga dipotong / lebih rendah → uang murah → BULLISH crypto"
                  if score > 0 else
                  "suku bunga dinaikkan / lebih tinggi → uang ketat → BEARISH crypto"
                  if score < 0 else "suku bunga sesuai perkiraan → dampak minim")
    elif et.key in ("unemployment", "jobless_claims"):
        effect = ("tenaga kerja melemah → bank sentral cenderung dovish → BULLISH crypto"
                  if score > 0 else
                  "tenaga kerja kuat → bank sentral cenderung hawkish → BEARISH crypto"
                  if score < 0 else "data tenaga kerja sesuai perkiraan")
    elif et.key in ("payrolls", "earnings"):
        effect = ("ketenagakerjaan/upah melambat → dovish → BULLISH crypto"
                  if score > 0 else
                  "ketenagakerjaan/upah panas → hawkish → BEARISH crypto"
                  if score < 0 else "data ketenagakerjaan sesuai perkiraan")
    elif et.key == "growth":
        effect = ("pertumbuhan solid → selera risiko naik → mildly BULLISH crypto"
                  if score > 0 else
                  "pertumbuhan melemah → selera risiko turun → mildly BEARISH crypto"
                  if score < 0 else "pertumbuhan sesuai perkiraan")
    else:  # central-bank communication — direction unknown until spoken
        effect = "pidato/komunikasi bank sentral → tunggu nada hawkish/dovish"
        score = 0.0
        bias, verdict = "NEUTRAL", "netral"

    reason = f"{et.label} {what} ({dir_word}); {effect}."
    return Assessment(True, et.key, et.label, bias, verdict, score, reason, basis,
                      surprise=surprise,
                      fields={"actual": a, "forecast": f, "previous": p})


def aggregate_day(assessments: list[Assessment]) -> dict:
    """Combine several event assessments (e.g. all events on one day) into a
    single crypto bias for that session."""
    scored = [x for x in assessments if x.matched and x.basis != "none"]
    net = sum(x.score for x in scored)
    bias, verdict = _bias_from_score(net)
    top = max(scored, key=lambda x: abs(x.score), default=None)
    return {
        "net_score": round(net, 3),
        "bias": bias,
        "verdict": verdict,
        "n_events": len(scored),
        "driver": top.type_label if top else None,
        "driver_reason": top.reason if top else None,
    }
