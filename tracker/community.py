"""Reading how Wikipedians reacted to an edit: who reverted it and why, blocks and deletions."""

import re

REVERT_TAGS = {"mw-rollback", "mw-undo", "mw-manual-revert"}
ANTIVANDAL_TAG_PREFIXES = ("mw-rollback", "huggle", "twinkle", "antivandal", "redwarn", "ultraviolet", "swviewer",
                           "wikishield")
# ClueBot NG today; the others patrolled older history (a page-history search goes back years).
ANTIVANDAL_BOTS = re.compile(r"^(?:cluebot|voabot|antivandalbot|martinbot|tawkerbot)", re.IGNORECASE)

GOODFAITH_SUMMARY = re.compile(r"good[\s-]?faith|\bagf\b|not vandalism", re.IGNORECASE)
VANDALISM_SUMMARY = re.compile(
    r"vandal|\brvv\b|\brv ?v\b|\bvand\b|nonsense|gibberish|\btest(?:ing)?\b|disruptive|unconstructive|obscen|"
    r"profan|troll|joke|silly|childish|abusive|\battack|hoax|\blta\b|long[- ]term abuse|libel", re.IGNORECASE)
QUALITY_SUMMARY = re.compile(
    r"unsourced|unreferenced|no source|without (?:a )?source|citation|\bcite\b|reliable|wp:rs\b|verif|"
    r"original research|wp:or\b|npov|\bpov\b|\bmos\b|manual of style|format|grammar|spelling|typo|consensus|"
    r"talk page|see talk|per talk|\bblp\b|incorrect|inaccurate|wrong|not notable|redundant|trivia|speculat|"
    r"rumou?r|crystal|copyvio|copyright|unexplained|undue|puffery|promotional|advert", re.IGNORECASE)
VANDAL_BLOCK = re.compile(
    r"vandal|not here|nothere|disrupt|abuse|troll|\blta\b|long[- ]term|harass|attack|nonsense|hoax", re.IGNORECASE)
VANDAL_DELETION = re.compile(r"\bG3\b|vandal|\bG10\b|attack|hoax|\bG1\b|nonsense", re.IGNORECASE)


def is_revert(user, tags):
    return bool(REVERT_TAGS.intersection(tags or [])) or bool(ANTIVANDAL_BOTS.match(user or ""))


def _clean_summary(comment, names):
    """Drop the automatic 'Reverted edits by X to last version by Y' parts, keep the reason."""
    text = comment or ""
    text = re.sub(r"\[\[(?:Special:Contrib(?:ution)?s/|User(?: talk)?:)[^\]]*\]\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\bto (?:the )?last (?:good )?(?:version|revision) by [^:;]*", " ", text, flags=re.IGNORECASE)
    for name in names:
        if name:
            text = text.replace(name, " ")
    return text


def classify_revert(reverter, comment, tags, vandal):
    """How the revert was labelled: vandalism, antivandal, revert, quality, goodfaith or self."""
    if reverter and vandal and reverter == vandal:
        return "self"
    if ANTIVANDAL_BOTS.match(reverter or ""):
        return "vandalism"
    reason = _clean_summary(comment, [vandal, reverter])
    if GOODFAITH_SUMMARY.search(reason):
        return "goodfaith"
    if VANDALISM_SUMMARY.search(reason):
        return "vandalism"
    if QUALITY_SUMMARY.search(reason):
        return "quality"
    if any(tag.lower().startswith(ANTIVANDAL_TAG_PREFIXES) for tag in tags or []):
        return "antivandal"
    return "revert"


def classify_block(reason):
    return "vandalism" if VANDAL_BLOCK.search(reason or "") else "other"


def classify_deletion(reason):
    return "vandalism" if VANDAL_DELETION.search(reason or "") else "other"
