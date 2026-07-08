"""Self-learning engine.

The bot remembers every resolved trade and the *pattern signature* that
produced it. From those it derives, per signature:

  * a Bayesian-smoothed confidence (win probability estimate), and
  * hard "lessons": signatures whose real win-rate falls below a threshold over
    a meaningful sample are BLOCKED so the same mistake is not repeated.

Because all stats live in SQLite, the bot never forgets what it has learned —
restarting the process keeps every lesson. Confidence is used two ways:
  * signals below CONFIDENCE_FLOOR are hidden, and
  * signals are ranked by confidence in the UI.

Signatures are computed at two granularities so learning generalises: a
`coarse` signature (machine + a couple of dominant features) accumulates
samples fast and drives the blocklist; a `fine` signature refines confidence
once enough data exists.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config, database as db


def _sig_coarse(f: dict) -> str:
    """Dominant-feature signature — the one the strategy doc calls out as
    decisive per machine, so lessons form quickly and meaningfully."""
    if f.get("machine") == "long":
        return f"L|fib={f.get('fib_bucket')}|ad={f.get('ad_rising')}|dow={f.get('dow')}"
    return f"S|fib={f.get('fib_bucket')}|usdtd={f.get('usdtd_pos_bucket')}|sar={f.get('sar_confirm')}"


def _sig_fine(f: dict) -> str:
    return (f"{f.get('machine')}|reg={f.get('regime')}|fib={f.get('fib_bucket')}"
            f"|rH={f.get('rsi_htf_bucket')}|rL={f.get('rsi_ltf_bucket')}"
            f"|ud={f.get('usdtd_pos_bucket')}|ad={f.get('ad_rising')}"
            f"|sar={f.get('sar_confirm')}|dow={f.get('dow')}")


def _bayes(wins: int, losses: int) -> float:
    a, b = config.LEARN_PRIOR_ALPHA, config.LEARN_PRIOR_BETA
    return (wins + a) / (wins + losses + a + b)


def evaluate(features: dict) -> dict:
    """Return {confidence, allowed, reason, coarse, fine} for a candidate signal."""
    coarse = _sig_coarse(features)
    fine = _sig_fine(features)

    cs = db.pattern_stat(coarse)
    fs = db.pattern_stat(fine)

    # Base confidence from the strategy's documented priors per machine.
    base = 0.62 if features.get("machine") == "long" else 0.61

    conf = base
    reason = "prior"
    allowed = True

    if cs:
        n = cs["wins"] + cs["losses"]
        conf = _bayes(cs["wins"], cs["losses"])
        reason = f"coarse n={n}"
        if n >= config.LEARN_MIN_SAMPLES and (cs["wins"] / n) < config.LEARN_BLOCK_WINRATE:
            allowed = False
            reason = f"BLOCKED (win {cs['wins']}/{n})"

    # Refine with the fine signature once it has its own evidence.
    if fs:
        n = fs["wins"] + fs["losses"]
        if n >= config.LEARN_MIN_SAMPLES:
            conf = 0.5 * conf + 0.5 * _bayes(fs["wins"], fs["losses"])
            reason += f" +fine n={n}"

    return {
        "confidence": round(conf, 3),
        "allowed": allowed,
        "reason": reason,
        "coarse": coarse,
        "fine": fine,
    }


def record_outcome(features: dict, won: bool, r_multiple: float):
    """Update pattern stats and (re)derive lessons after a trade resolves."""
    ts = datetime.now(timezone.utc).isoformat()
    coarse = _sig_coarse(features)
    fine = _sig_fine(features)
    db.bump_pattern(coarse, won, r_multiple, ts)
    db.bump_pattern(fine, won, r_multiple, ts)
    _derive_lesson(coarse, features, ts)


def _derive_lesson(signature: str, features: dict, ts: str):
    st = db.pattern_stat(signature)
    if not st:
        return
    n = st["wins"] + st["losses"]
    if n < config.LEARN_MIN_SAMPLES:
        return
    win_rate = st["wins"] / n
    machine = "LONG" if features.get("machine") == "long" else "SHORT"
    human = _humanize(features)
    if win_rate < config.LEARN_BLOCK_WINRATE:
        db.add_lesson(
            signature, "BLOCK",
            f"❌ Diblokir: sinyal {machine} pola [{human}] hanya menang "
            f"{st['wins']}/{n} ({win_rate*100:.0f}%). Pola ini dihindari.",
            round(win_rate, 3), n, ts,
        )
    elif win_rate >= 0.65:
        db.add_lesson(
            signature, "FAVOR",
            f"✅ Diutamakan: sinyal {machine} pola [{human}] menang "
            f"{st['wins']}/{n} ({win_rate*100:.0f}%). Diberi prioritas.",
            round(win_rate, 3), n, ts,
        )


def _humanize(f: dict) -> str:
    parts = [f"fib {f.get('fib_bucket')}"]
    if f.get("machine") == "long":
        parts.append(f"A/D {'naik' if f.get('ad_rising') else 'datar'}")
        dow = ["Sen", "Sel", "Rab", "Kam", "Jum", "Sab", "Min"][f.get("dow", 0)]
        parts.append(dow)
    else:
        parts.append(f"USDT.D {f.get('usdtd_pos_bucket')}")
        parts.append(f"SAR {'ok' if f.get('sar_confirm') else 'no'}")
    return ", ".join(parts)


def blocked_patterns() -> list[dict]:
    return [l for l in db.lessons(50) if l["kind"] == "BLOCK"]
