"""Check 2, part 2: weigh all the evidence and give a verdict.

  confirmed  Wikipedians treated it as vandalism (reverted as vandalism or with an
             anti-vandalism tool, vandal blocked, hidden or deleted by admins), or it was
             reverted AND our own content/model checks agree.
  likely     not (yet) acted on, but independent checks agree: the added text looks like
             vandalism and the revert-risk model agrees.
  pending    passed check 1; check 2 is still collecting evidence (re-checked on a schedule).
  rejected   check 2 says it isn't vandalism: reverted as a good-faith mistake, or it stayed
             in the article with nothing suspicious about it.
"""

import re

import config

REVERT_STRENGTH = {"vandalism": 0.95, "antivandal": 0.8, "revert": 0.55, "quality": 0.25, "self": 0.1,
                   "goodfaith": -1.0}


def _short(text, limit=90):
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", text or "")   # [[WP:X|label]] -> label
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def community_evidence(community):
    """[(strength, description)] of what Wikipedians did about the edit; negative = 'not vandalism'."""
    evidence = []
    revert = community.get("revert")
    if revert:
        who = revert.get("user") or "someone"
        kind = revert.get("kind", "revert")
        evidence.append((REVERT_STRENGTH[kind], {
            "vandalism": f"Reverted as vandalism by {who}",
            "antivandal": f"Reverted by {who} with an anti-vandalism tool",
            "revert": f"Reverted by {who}",
            "quality": f"Reverted by {who}, but for content reasons",
            "self": "Undone by the same editor (a test edit?)",
            "goodfaith": f"{who} reverted it as a good-faith mistake",
        }[kind]))
    elif community.get("cleaned"):
        evidence.append((0.3, "Removed again by a later edit"))
    block = community.get("block")
    if block:
        strength = 0.9 if block.get("kind") == "vandalism" else 0.5
        evidence.append((strength, f"Editor blocked: {_short(block.get('reason')) or 'no reason given'}"))
    if community.get("hidden"):
        evidence.append((0.85, "Hidden by Wikipedia admins (revision deleted)"))
    deleted = community.get("deleted")
    if deleted:
        strength = 0.9 if deleted.get("kind") == "vandalism" else 0.4
        evidence.append((strength, f"Page deleted: {_short(deleted.get('reason')) or 'no reason given'}"))
    return evidence


def decide(edit, final):
    """(verdict, confidence 0-1, reasons) for a candidate edit."""
    badfaith = edit.get("badfaith") or 0.0
    risk = edit.get("revert_risk")
    model = risk if risk is not None else (edit.get("damaging") or 0.0)
    content = edit.get("content_score") or 0.0
    community = edit.get("community") or {}
    evidence = sorted(community_evidence(community), reverse=True)
    positive = evidence[0][0] if evidence and evidence[0][0] > 0 else 0.0
    best = evidence[0][1] if positive else ""
    goodfaith = next((text for strength, text in evidence if strength < 0), None)

    if goodfaith and positive < 0.85:
        verdict, why = "rejected", goodfaith
    elif positive >= 0.75:
        verdict, why = "confirmed", best
    elif positive >= 0.5 and (content >= 0.3 or model >= 0.6):
        verdict, why = "confirmed", f"{best}, and our own checks agree"
    elif positive >= 0.5:
        verdict, why = "likely", best
    elif content >= 0.45 and model >= 0.5:
        verdict, why = "likely", f"The added text looks like vandalism and the revert-risk model agrees ({model:.0%})"
    elif content >= 0.75:
        verdict, why = "likely", "The added text is obvious vandalism"
    elif positive >= 0.25 and model >= 0.8:
        verdict, why = "likely", f"{best}; revert-risk model {model:.0%}"
    elif community.get("survived"):
        verdict, why = "rejected", f"Still in the article after {config.SURVIVAL_MINUTES} min of other people editing it"
    elif final:
        hours = config.RECHECK_AT_MINUTES[-1] / 60
        verdict, why = "rejected", f"Never confirmed as vandalism within {hours:g} hours"
    else:
        verdict, why = "pending", "Waiting to see how Wikipedians react"

    remaining = 1.0
    for part in [0.35 * badfaith, 0.5 * model, content] + [s for s, _ in evidence if s > 0]:
        remaining *= 1 - part
    confidence = (1 - remaining) * (0.3 if goodfaith else 1.0)
    reasons = [why] + [text for _, text in evidence if text not in why]
    return verdict, round(confidence, 3), reasons
