"""Customer messages the backend answers before any model call (Fresh-search task).

A greeting alone ("salam", "hi") and a fresh-search command ("nayi search", "purani
requirements hata do") need no extraction: they are matched here, deterministically, on the
customer's own words. A message that also states a request ("salam, DHA mein flat chahiye")
is NOT a greeting alone and still goes to the model; only its command part, if any, is acted
on first. A listing code asked for by itself ("DH6-103 dikhao") is looked up in code. Pure
functions, no database, no logging of message text.
"""

import re

from app.grounding import LISTING_CODE

NEW_SEARCH, CLEAR_OLD = "new_search", "clear_old"
SALAM, HELLO = "salam", "hello"

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
# Words that, alone, say "hello". A reply that opens with a thank-you is wrong for these.
_SALAM_WORDS = frozenset({
    "salam", "salaam", "slam", "salams", "assalam", "asalam", "assalamu", "assalamualaikum",
    "assalamualaikam", "assalamualaikom", "assalamoalaikum", "assalamo", "alaikum", "alaikam",
    "alaykum", "walaikum", "walaikumassalam", "waalaikum", "aoa", "asslam", "assalaam",
    "اسلام", "السلام", "علیکم", "سلام",
})
_HELLO_WORDS = frozenset({"hi", "hii", "hiii", "hello", "helo", "hellooo", "hey", "heya",
                          "hy", "hye", "hola", "greetings", "morning", "evening", "afternoon",
                          "good", "gm", "ہیلو"})
# Fillers that may sit around a greeting or a command without adding a request.
_FILLER = frozenset({"o", "u", "wa", "ji", "jee", "please", "plz", "pls", "sir", "sahab",
                     "sahib", "bhai", "bro", "dear", "kar", "karo", "kardo", "do", "dein",
                     "den", "de", "ab", "bas", "sab", "ka", "ke", "ki", "se", "aur", "hai",
                     "hain", "ok", "okay", "shukriya", "thanks", "thank", "you", "mujhe",
                     "mjhy", "main", "mein", "mai", "to", "the", "my", "all", "a", "an", "karni",
                     "karna", "karein", "chahiye", "chahta", "chahti", "shuru", "ek", "jao",
                     "jayen", "jaiye"})

_GAP = r"(?:\W+(?:\w+\W+){0,3})?"
_OLD = (r"(?:purani|puraani|purane|puranay|pichli|pichhli|pehli|old|previous|earlier|last)"
        r"\s+(?:saari\s+|sab\s+|all\s+)?"
        r"(?:requirements?|requirment|filters?|search(?:es)?|batein|baatein|details?)")
_DROP = (r"(?:hata|hatao|hataa|hata\s*do|hata\s*den|hata\s*dein|clear|delete|remove|reset|"
         r"bhool|bhul|cancel|ignore|khatam)")
_CLEAR_OLD = [re.compile(p, re.IGNORECASE) for p in (
    _OLD + _GAP + _DROP,
    _DROP + _GAP + _OLD,
    r"\b(?:sab|saari|all|everything)\s+(?:kuch\s+)?(?:requirements?\s+)?" + _DROP + r"\b",
    r"^\W*(?:reset|clear\s+all|clear\s+everything)\W*$",
)]
_NEW_SEARCH = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:nayi|naya|nai|naye|new|fresh)\s+(?:search|talash|requirements?|shuruat)\b",
    r"\b(?:start|begin)\s+(?:over|again|afresh)\b",
    r"\bnaye\s+sire?y?\s+se\b",
    r"\bdobara\s+(?:se\s+)?shuru\b",
)]


# A fresh-search command is recognised only when it is plainly affirmative. Any negation
# ("mat hata do", "do not clear", "nayi search nahi chahiye"), question ("kya ... sakte ho?")
# or condition ("agar ...") makes it ambiguous, and an ambiguous command must not change
# state: the message goes to the model instead. "na" is also Urdu's polite "please", so it
# counts as ambiguous too.
_NEGATORS = frozenset({"mat", "na", "nahi", "nahin", "nhi", "nahe", "no", "nope", "not", "dont",
                       "donot", "never", "without", "bina", "kabhi"})
_DOUBT = frozenset({"kya", "can", "could", "should", "would", "shall", "kaise", "kyun", "kyon",
                    "why", "how", "agar", "if", "warna", "sirf", "only"})


def _tokens(text):
    return _TOKEN.findall((text or "").casefold())


def greeting_kind(text):
    """SALAM / HELLO when the whole text is a greeting and nothing else, else None. Thanks
    alone, an empty text, or a greeting followed by any request word is not a greeting."""
    tokens = _tokens(text)
    if not tokens:
        return None
    kinds = set()
    for token in tokens:
        if token in _SALAM_WORDS:
            kinds.add(SALAM)
        elif token in _HELLO_WORDS:
            kinds.add(HELLO)
        elif token not in _FILLER:
            return None
    if not kinds:
        return None
    return SALAM if SALAM in kinds else HELLO


def fresh_command(text):
    """(kind, standalone) for a fresh-search command in the text, else None. standalone:
    nothing but greetings and fillers is left once the command is taken out, so there is no
    request for the model to read."""
    text = text or ""
    words = set(_TOKEN.findall(text.casefold().replace("'", "").replace("\u2019", "")))
    if "?" in text or "\uff1f" in text or "\u061f" in text or words & (_NEGATORS | _DOUBT):
        return None
    for kind, patterns in ((CLEAR_OLD, _CLEAR_OLD), (NEW_SEARCH, _NEW_SEARCH)):
        for pattern in patterns:
            match = pattern.search(text)
            if match:
                rest = text[:match.start()] + " " + text[match.end():]
                standalone = all(t in _FILLER or t in _SALAM_WORDS or t in _HELLO_WORDS
                                 for t in _tokens(rest))
                return kind, standalone
    return None


YES, NO = "yes", "no"
_YES_CORE = frozenset({"haan", "han", "ha", "hn", "yes", "yeah", "yep", "ok", "okay", "bilkul",
                       "wohi", "wahi", "same", "keep", "sahi", "theek", "rakhein", "rakho"})
_NO_CORE = frozenset({"nahi", "nahin", "nai", "no", "nope", "na", "nhi", "nahe"})
_ANSWER_FILLER = _FILLER | {"ji", "jee", "bilkul", "wohi", "wahi", "chahiye", "chahie", "hai",
                            "rakhein", "rakho", "dein", "do", "same", "ab", "bhi", "mujhe"}


def confirmation(text):
    """YES / NO when the whole text is a plain answer to a keep-or-new-search question
    ("haan wohi chahiye", "nahi"), else None. "Nahi, kharidna hai" carries a request, so it
    is None and the model reads it (its explicit purpose replaces the old one)."""
    tokens = _tokens(text)
    if not tokens or any(t not in _ANSWER_FILLER | _YES_CORE | _NO_CORE for t in tokens):
        return None
    if any(t in _NO_CORE for t in tokens):
        return NO
    return YES if any(t in _YES_CORE for t in tokens) else None


# The customer asks for a person or a visit in their own words (the handoff triggers of R6 D4:
# "agent se baat", visit, booking). A question about documents alone is not one.
_AGENT_ASK = re.compile(
    r"\b(?:agent|broker|consultant|dealer|insaan|insan|human|banda|bande|call|phone|rabta|"
    r"contact|visit|appointment|meeting|milna|milwa\w*|book\w*)\b|\bkisi\s+se\s+baat\b",
    re.IGNORECASE)


def asks_for_agent(text):
    """True when the text asks to talk to an agent, to visit or to book (live 2026-10-18:
    "Is ghar ke documents clear hain?" opened a handoff; it only needs the agent's answer)."""
    return bool(_AGENT_ASK.search(text or ""))


# Words around a listing code that only say "show it" ("DH6-103 dikhao", "show me DH6-103").
_SHOW_WORDS = frozenset({
    "dikhao", "dikhayein", "dikhaein", "dikha", "dikhado", "dikhaiye", "dekho", "dekhao",
    "dekhna", "dekhni", "show", "me", "send", "bhejo", "bhejein", "bhej", "details", "detail",
    "info", "information", "about", "bare", "baare", "batao", "bataein", "bataiye", "property",
    "listing", "wali", "wala", "ye", "yeh", "this", "the", "kya", "is", "check"})


def listing_lookup(text):
    """The one listing code a message asks to see and nothing else ("DH6-103 dikhao",
    "show me dh6-103"), else None. "DH6-101 mein gas connection hai?" and "DH6-101 ki aur
    photos" carry a question the model reads, so they are not lookups. Live 2026-10-18:
    "DH6-103 dikhao" was answered with an old search."""
    upper = (text or "").upper()
    codes = list(dict.fromkeys(LISTING_CODE.findall(upper)))
    if len(codes) != 1 or not any(c.isalpha() for c in codes[0].split("-")[0]):
        return None
    rest = _tokens(LISTING_CODE.sub(" ", upper))
    return codes[0] if all(t in _SHOW_WORDS | _FILLER for t in rest) else None
