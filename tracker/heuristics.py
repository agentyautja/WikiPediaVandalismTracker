"""Check 2, part 1: does the changed text itself look like vandalism?

Every rule that fires produces a finding with a weight between 0 and 1. Findings are
combined like independent clues:  score = 1 - (1 - w1) * (1 - w2) * ...

Words that were already in the article before the edit are ignored, so editing an
article that is *about* a rude word, a meme or a slur doesn't count as evidence.
"""

import re


def _words(*patterns):
    return re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.IGNORECASE)


SLURS = _words(
    r"n[i1!]gg(?:a|ah|as|az|er|ers|uh)", r"f[a@]gg?ots?", r"retard(?:s|ed)?", r"kikes?", r"trann(?:y|ies)",
    r"wetbacks?", r"ragheads?", r"towelheads?", r"gooks?", r"beaners?", r"spics?")
SWEARING = _words(
    r"f+u+c+k+\w*", r"motherf\w+", r"fck\w*", r"fuk\w*", r"sh[i1]t(?:s|ty|head|hole|ting)?", r"bullshit",
    r"b[i1]tch(?:es|y)?", r"c+u+n+t+s?", r"(?:ass|arse)holes?", r"dickheads?", r"cocksuck\w*", r"twats?",
    r"wank(?:er|ers|ing)?", r"whores?", r"sluts?", r"jizz", r"dildos?", r"blowjobs?", r"handjobs?", r"stfu",
    r"gtfo", r"wtf", r"suck\s+my\s+\w+")
RUDE = _words(
    r"ass", r"arse", r"dicks?", r"cocks?", r"pussy", r"penis(?:es)?", r"vaginas?", r"boobs?", r"butts?",
    r"buttholes?", r"poop\w*", r"poo", r"farts?", r"farted", r"farting", r"pee", r"peed", r"piss(?:ed|ing)?",
    r"crap(?:py)?", r"damn", r"sexy", r"horny", r"porn\w*", r"boners?", r"testicles?", r"orgasms?")
INSULTS = re.compile(
    r"(?<!\w)(?:is|are|was|were|be|am)\s+(?:(?:a|an|so|very|really|super|such\s+a|totally|the|big|fat|little|huge)\s+){0,3}"
    r"(?:gay|gae|gey|dumb|stupid|idiots?|losers?|morons?|noobs?|retarded|lame|trash|garbage|smelly|stinky|ugly|fat|"
    r"cringe|dorks?|nerds?|virgins?|poopy|poopheads?|dumbass(?:es)?|clowns?|bozos?|sus|weirdos?)(?!\w)",
    re.IGNORECASE)
SUCKS = _words(r"sucks?", r"smells?", r"stinks?")
CHATTER = _words(
    r"(?:was|were)\s+here", r"(?:hi|hello|hey|sup|yo|hai)\s+(?:mom|mum|dad|guys|everyone|everybody|there|people|"
    r"world|friends?|bro|chat|youtube)", r"ur\s+(?:mom|mum|mother|mama)", r"yo\s*(?:mama|momma)",
    r"(?:you|u)\s+(?:suck|smell|stink|r)", r"(?:like\s+and\s+)?subscribe\s+to", r"follow\s+me\s+on")
FIRST_PERSON = _words(
    r"i\s+(?:hate|love|like|am)", r"my\s+(?:mom|mum|dad|friend|brother|sister|name|teacher|girlfriend|boyfriend|"
    r"crush|bestie|bff)", r"me\s+and\s+my")
CHAT_SPEAK = _words(
    r"lo+l+", r"lmf?ao+", r"rofl", r"omg+", r"smh", r"idk", r"ikr", r"tbh", r"xd+", r"bru+h+", r"ha(?:ha)+h?",
    r"he(?:he)+", r"uwu", r"owo", r"poggers", r"yeet", r"bussin", r"no\s+cap", r"fr\s+fr", r"skill\s+issue")
MEMES = _words(
    r"skibidi", r"rizz(?:ler)?", r"gyatt?", r"fanum\s+tax", r"sigma\s+(?:male|boy|grindset)", r"mewing", r"amogus",
    r"sussy", r"deez\s+nuts", r"ligma", r"sugma", r"six\s+seven", r"poopoo", r"peepee", r"poopheads?",
    r"big\s+chungus", r"looksmax\w*", r"brainrot", r"aura\s+points", r"hawk\s+tuah", r"tralalero", r"bombardiro",
    r"tung\s+tung\s+tung", r"cappuccina", r"only\s+in\s+ohio")
REPEATED_CHARS = re.compile(r"([a-z!?])\1{5,}", re.IGNORECASE)
REPEATED_SYLLABLES = re.compile(r"(?<!\w)(ha|he|lo|la|xd|ya|ah|ja)\1{3,}(?!\w)", re.IGNORECASE)
REPEATED_WORDS = re.compile(r"(?<!\w)(\w{2,})(?:\s+\1(?!\w)){3,}", re.IGNORECASE)
KEYBOARD_MASH = re.compile(r"asdf|sdfg|dfgh|fghj|ghjk|hjkl|qwert|zxcv|xcvb|cvbn|vbnm|jkjk|lkjh", re.IGNORECASE)
LATIN_WORD = re.compile(r"(?<![^\W\d_])[A-Za-z]{7,}(?![^\W\d_])")
EMOJI = re.compile("[\U0001F300-\U0001FAFF❤]")

# Tags that Wikipedia's own edit filters (AbuseFilter) put on suspicious edits.
FILTER_TAGS = {
    "possible vandalism": 0.5,
    "possible libel or vandalism": 0.45,
    "reverting anti-vandal bot": 0.6,
    "mw-blank": 0.45,
    "blanking": 0.4,
    "mw-replace": 0.35,
    "shouting": 0.3,
    "Section blanking": 0.3,
    "removal of speedy deletion templates": 0.3,
    "bad external": 0.2,
    "possible link spam": 0.2,
    "possible unexplained content removal": 0.15,
    "references removed": 0.1,
}


def _strip_markup(text):
    """Roughly turn wikitext into the words a reader would see."""
    text = re.sub(r"<ref[^>]*/>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<ref[^>]*>.*?</ref>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    for _ in range(2):
        text = re.sub(r"\{\{[^{}]*\}\}", " ", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", text)
    text = re.sub(r"\[https?://\S+\s*([^\]]*)\]", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    return re.sub(r"'{2,}", "", text)


def _in_old(phrase, old_lower):
    return re.search(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)", old_lower) is not None


def _new_hits(pattern, text, old_lower, limit=3, last_word_only=False):
    """Matches of `pattern` in the added text that were not in the article before."""
    hits = []
    for match in pattern.finditer(text):
        found = " ".join(match.group(0).split())
        checked = found.split()[-1] if last_word_only else found
        if found.lower() in (h.lower() for h in hits) or _in_old(checked, old_lower):
            continue
        hits.append(found)
        if len(hits) >= limit:
            break
    return hits


def _quote(words):
    return ", ".join(f"“{w}”" for w in dict.fromkeys(words))


def _mask(word):
    return word[0] + "*" * (len(word) - 1)


def _gibberish(prose, old_lower):
    found = []
    for token in LATIN_WORD.findall(prose):
        low = token.lower()
        if token.isupper() or low in (f.lower() for f in found):   # acronyms, roman numerals
            continue
        vowels = sum(ch in "aeiouy" for ch in low)
        if vowels == 0 or KEYBOARD_MASH.search(low) or (len(low) >= 12 and vowels / len(low) < 0.12):
            if not _in_old(token, old_lower):
                found.append(token)
        if len(found) >= 3:
            break
    return found


def _shouting(prose, old_lower):
    for run in re.finditer(r"(?:(?<![\w])[A-Z]{2,}[!?.,]*\s+){2,}[A-Z]{2,}(?!\w)", prose):
        text = " ".join(run.group(0).split())
        if sum(ch.isalpha() for ch in text) >= 12 and text.lower() not in old_lower:
            return text
    return None


def analyze(added, removed, old_text, edit, context=""):
    """Findings (list of dicts) for one edit.

    `added`/`removed` are the changed words, `context` the new text around each change (so phrases
    like "... is a stupid ..." are seen whole), `edit` holds tags, oldlen, newlen, comment, draft_scores.
    """
    old_lower = (old_text or "").lower()
    prose = _strip_markup(added)
    sentences = _strip_markup(context) if context else prose
    findings = []

    def add(code, weight, label):
        findings.append({"code": code, "weight": weight, "label": label})

    if hits := _new_hits(SLURS, prose, old_lower):
        add("slur", 0.75, "Slur added: " + _quote(_mask(h) for h in hits))
    if hits := _new_hits(SWEARING, prose, old_lower):
        add("swearing", 0.6, "Swearing added: " + _quote(hits))
    if hits := _new_hits(RUDE, prose, old_lower):
        add("rude", 0.35, "Rude words added: " + _quote(hits))
    if hits := _new_hits(INSULTS, sentences, old_lower, last_word_only=True):
        add("insult", 0.5, "Insult: " + _quote(hits))
    elif hits := _new_hits(SUCKS, prose, old_lower):
        add("insult", 0.35, "Insult: " + _quote(hits))
    if hits := _new_hits(CHATTER, sentences, old_lower):
        add("chatter", 0.45, "Talking to the reader: " + _quote(hits))
    if hits := _new_hits(FIRST_PERSON, sentences, old_lower):
        add("first_person", 0.25, "Personal message: " + _quote(hits))
    if hits := _new_hits(CHAT_SPEAK, prose, old_lower):
        add("chat_speak", 0.4, "Chat speak: " + _quote(hits))
    if hits := _new_hits(MEMES, prose, old_lower):
        add("meme", 0.45, "Meme words: " + _quote(hits))
    if hits := (_new_hits(REPEATED_CHARS, prose, old_lower) or _new_hits(REPEATED_SYLLABLES, prose, old_lower)
                or _new_hits(REPEATED_WORDS, prose, old_lower)):
        add("repetition", 0.4, "Repeated characters/words: " + _quote(h[:30] for h in hits))
    if hits := _gibberish(prose, old_lower):
        add("gibberish", 0.45, "Keyboard mashing: " + _quote(h[:30] for h in hits))
    if shout := _shouting(prose, old_lower):
        add("shouting", 0.35, "SHOUTING: " + _quote([shout[:60]]))
    emoji = [ch for ch in EMOJI.findall(added) if ch not in old_lower]
    if len(emoji) >= 2:
        add("emoji", 0.35, "Emoji spam: " + "".join(emoji[:6]))

    old_len, new_len = edit.get("oldlen") or 0, edit.get("newlen") or 0
    if old_len >= 1000 and new_len <= old_len * 0.1:
        add("blanking", 0.6, f"Blanked the page ({old_len:,} → {new_len:,} bytes)")
    elif len(removed) >= 1500 and len(added.strip()) <= 50 and not (edit.get("comment") or "").strip():
        add("mass_removal", 0.35, f"Removed {len(removed):,} characters without explaining why")

    for tag in edit.get("tags") or []:
        if tag in FILTER_TAGS:
            add("filter", FILTER_TAGS[tag], f"Wikipedia's edit filter tagged it: “{tag}”")

    draft = edit.get("draft_scores") or {}
    if (p := draft.get("vandalism") or 0) >= 0.3:
        add("draft_vandalism", min(p, 0.8), f"New-page model: {p:.0%} vandalism")
    if (p := draft.get("attack") or 0) >= 0.3:
        add("draft_attack", min(p, 0.8), f"New-page model: {p:.0%} attack page")

    if (added.strip() and removed.strip() and not re.search(r"[^\W\d_]", added + removed)
            and not (edit.get("comment") or "").strip()):
        add("numbers", 0.1, "Only changed numbers, no explanation (sneaky vandalism?)")
    return findings


def score(findings):
    remaining = 1.0
    for finding in findings:
        remaining *= 1 - finding["weight"]
    return round(1 - remaining, 3)
