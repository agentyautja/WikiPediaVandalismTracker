"""Broad categories (Politics, Science, Sports, ...) for an article.

First choice: Wikimedia's article-topic model (Lift Wing "outlink-topic-model"), whose topics
look like "History_and_Society.Politics_and_government". If it has nothing to say (brand-new
or deleted pages), the page's own Wikipedia categories are matched against keywords instead.
"""

import re

# (category, topic-model prefixes, keywords for Wikipedia category names; matched at word starts)
CATEGORIES = [
    ("Politics", ["History_and_Society.Politics_and_government"],
     ["politic", "election", "parliament", "government", "minister", "president", "senator", "congress", "legislat",
      "mayor", "governor", "diplomat"]),
    ("Science", ["STEM.Biology", "STEM.Chemistry", "STEM.Physics", "STEM.Mathematics", "STEM.Earth_and_environment",
                 "STEM.Space"],
     ["physic", "chemi", "biolog", "mathemat", "astronom", "geolog", "species", "genera", "scientist", "zoolog",
      "botan", "ecolog", "planet", "fauna", "flora"]),
    ("Technology", ["STEM.Computing", "STEM.Technology", "STEM.Engineering", "STEM.Libraries_&_Information",
                    "Culture.Media.Software"],
     ["computer", "software", "internet", "technolog", "engineer", "electronic", "programming", "website"]),
    ("Health & Medicine", ["STEM.Medicine_&_Health"],
     ["disease", "medic", "health", "syndrome", "disorder", "hospital", "physician"]),
    ("Sports", ["Culture.Sports"],
     ["sport", "football", "soccer", "basketball", "baseball", "cricket", "tennis", "golf", "rugby", "hockey",
      "olymp", "athlet", "boxing", "boxer", "wrestl", "racing", "cyclist", "swimmer", r"f\.c\.", "league", "nfl",
      "nba", "fifa", "uefa", "formula one", "motorsport", "martial art"]),
    ("Music", ["Culture.Media.Music"],
     ["music", "album", r"songs?\b", "songwriter", "singer", r"bands?\b", "rapper", "guitarist", "composer",
      "record label"]),
    ("Film & TV", ["Culture.Media.Films", "Culture.Media.Television", "Culture.Media.Radio",
                   "Culture.Media.Entertainment"],
     ["film", "television", "tv series", "actor", "actress", "sitcom", "anime", "netflix", "episode", "radio"]),
    ("Video Games", ["Culture.Media.Video_games"], ["video game", "esports", "nintendo", "playstation", "xbox"]),
    ("Internet Culture", ["Culture.Internet_culture"], ["internet meme", "youtuber", "streamer", "influencer"]),
    ("Arts & Literature", ["Culture.Literature", "Culture.Media.Books", "Culture.Performing_arts",
                           "Culture.Visual_arts", "Culture.Linguistics"],
     ["novel", r"books?\b", "writer", "poet", "author", "painting", "painter", "artist", "sculpt", "theat",
      "comic", "literat", "architect", "fashion", "language"]),
    ("Religion & Philosophy", ["Culture.Philosophy_and_religion"],
     ["religio", "church", "christian", "islam", "muslim", "hindu", "buddhis", "jewish", "judaism", "philosoph",
      "theolog", "bishop", "mosque", "deities"]),
    ("History", ["History_and_Society.History"], ["history", "ancient", "medieval", "empire", "dynast", "archaeolog"]),
    ("Military", ["History_and_Society.Military_and_warfare"],
     ["military", r"wars?\b", r"battles?\b", "army", "armies", "navy", "naval", "air force", "weapon"]),
    ("Business", ["History_and_Society.Business_and_economics"],
     ["compan", "business", "brand", "econom", r"banks?\b", "retail", "corporat", "manufactur"]),
    ("Education", ["History_and_Society.Education"], ["school", "universit", "college", "education"]),
    ("Society", ["History_and_Society.Society", "History_and_Society.Transportation"],
     ["society", "social", "crime", r"laws?\b", "transport", "railway", "airline", r"roads?\b"]),
    ("Food & Drink", ["Culture.Food_and_drink"],
     ["food", "cuisine", "dish", "drink", "beverage", "restaurant", "brewer", "wine"]),
    ("Geography", ["Geography.Geographical"],
     ["populated places", "cities", "towns", "villages", "rivers", "mountains", "islands", "countries",
      "geography", "municipalit", "districts", "provinces", "counties"]),
    ("People", ["Culture.Biography"], ["births", "deaths", "living people", "people from", "alumni"]),
]
ALL_CATEGORIES = [name for name, _, _ in CATEGORIES] + ["Other"]

_KEYWORDS = {name: re.compile(r"\b(?:" + "|".join(words) + ")", re.IGNORECASE) for name, _, words in CATEGORIES}
# Biography bookkeeping categories ("Harvard University alumni", "1990 births") only mean "People".
_BIO_ONLY = re.compile(r"alumni|educated at|faculty|people from|births|deaths|living people|burials", re.IGNORECASE)


def from_topics(topics, min_score=0.5):
    """Map topic-model output [(topic, score)] to our broad categories."""
    topics = [topic for topic, score in topics if score >= min_score]
    found = [name for name, prefixes, _ in CATEGORIES if any(t.startswith(tuple(prefixes)) for t in topics)]
    # "Culture.Media.Media*" is the model's catch-all for media; only call it Film & TV when
    # nothing more specific (music, books, games, software, ...) was predicted.
    specific_media = any(t.startswith(("Culture.Media.", "Culture.Literature")) and t != "Culture.Media.Media*"
                         for t in topics)
    if "Culture.Media.Media*" in topics and not specific_media and "Film & TV" not in found:
        found.append("Film & TV")
    # Same for "STEM.STEM*": plain Science unless a more specific STEM field was predicted.
    if "STEM.STEM*" in topics and not any(t.startswith("STEM.") and t != "STEM.STEM*" for t in topics):
        found.append("Science")
    # Almost every article gets a region ("Geography.Regions.Europe..."); only a page that is
    # *nothing but* regions is about a place.
    if topics and all(t.startswith("Geography.") for t in topics) and "Geography" not in found:
        found.append("Geography")
    return [name for name, _, _ in CATEGORIES if name in found]


def from_wiki_categories(names):
    """Map Wikipedia category names (e.g. "American male singers") to our broad categories."""
    found = []
    for name, _, _ in CATEGORIES:
        relevant = names if name == "People" else [n for n in names if not _BIO_ONLY.search(n)]
        if any(_KEYWORDS[name].search(n) for n in relevant):
            found.append(name)
    return found
