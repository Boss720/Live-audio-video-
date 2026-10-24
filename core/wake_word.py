"""Wake-word detection for voice gating.

The matcher answers one narrow question: does this ASR transcript contain the
configured wake word? It is deliberately tolerant — ASR routinely mangles short
names — because a false negative (ignoring the user) is cheaper than a false
positive (interrupting a conversation), but the word must still be *present*
in some form; the model's system prompt handles the harder question of whether
the utterance is actually directed at the agent.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(text: str) -> str:
    """Lowercase and collapse every non-alphanumeric run to a single space."""
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def _levenshtein(a: str, b: str) -> int:
    """Classic edit distance, O(min(len)) space — tokens here are short."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


_SOUNDEX_CODES = {
    **dict.fromkeys("bfpv", "1"),
    **dict.fromkeys("cgjkqsxz", "2"),
    **dict.fromkeys("dt", "3"),
    "l": "4",
    **dict.fromkeys("mn", "5"),
    "r": "6",
}


def _soundex(word: str) -> str:
    """Tiny American Soundex: first letter plus up to three consonant digits."""
    letters = [c for c in word.lower() if c.isalpha()]
    if not letters:
        return ""
    out = [letters[0].upper()]
    last = _SOUNDEX_CODES.get(letters[0], "")
    for c in letters[1:]:
        code = _SOUNDEX_CODES.get(c, "")
        if code and code != last:
            out.append(code)
        if c not in "hw":  # h/w do not break a run of equal codes
            last = code
    return ("".join(out) + "000")[:4]


# Longest run of adjacent transcript tokens joined into one candidate, so ASR
# splits like "Lee Roy" or "Le Roi" can still match "Leroy".
_MAX_JOINED_TOKENS = 3


def _edit_tolerance(length: int) -> int:
    """Allowed edit distance: 1 for short names, 2 only for long ones."""
    return 1 if length < 9 else 2


def _candidate_matches(candidate: str, wake: str) -> bool:
    """Compare one transcript candidate (a token or joined tokens) to a wake word."""
    if candidate == wake:
        return True

    # Fuzzy matching needs enough letters that one edit does not turn the word
    # into a different one. At three characters ("max" -> "map", "tax") it
    # already does, so short wake words rely on exact matching plus aliases.
    if len(wake) < 4 or len(candidate) < 4:
        return False

    distance = _levenshtein(candidate, wake)
    if distance <= _edit_tolerance(len(wake)):
        return True

    # Near-homophone: same Soundex key AND still close in spelling. The edit
    # cap matters: Soundex alone equates "Leroy" with "Laura" (both L600).
    return distance <= 2 and _soundex(candidate) == _soundex(wake)


def _matches_one(normalized_text: str, normalized_wake: str) -> bool:
    """Match a single already-normalized wake term against a transcript."""
    if not normalized_text or not normalized_wake:
        return False

    tokens = normalized_text.split()
    wake_tokens = normalized_wake.split()

    # Whole-token phrase match ("hey computer"); never a substring, so "max"
    # does not fire on "maximum".
    width = len(wake_tokens)
    if any(tokens[i : i + width] == wake_tokens for i in range(len(tokens) - width + 1)):
        return True

    # Single-word wake words may be split or fuzzed by ASR. Multi-word phrases
    # stay exact apart from joining ("heycomputer").
    joined_wake = "".join(wake_tokens)
    for size in range(1, _MAX_JOINED_TOKENS + 1):
        for i in range(len(tokens) - size + 1):
            candidate = "".join(tokens[i : i + size])
            if width > 1:
                if candidate == joined_wake:
                    return True
            elif _candidate_matches(candidate, joined_wake):
                return True
    return False


def contains_wake_word(
    text: str, wake_word: str, aliases: Sequence[str] = ()
) -> bool:
    """Return True if ``text`` appears to contain ``wake_word`` or an alias.

    Matching rules, in order:
    1. Whole-token match — handles "hey bongo", "thanks, Bongo!", and
       multi-word wake phrases. Never a substring ("max" != "maximum").
    2. Joined adjacent tokens (up to 3) — "Lee Roy" / "Le Roi" match "Leroy".
    3. Fuzzy match — edit distance <= 1 (<= 2 for wake words of 9+ letters),
       or the same Soundex key within edit distance 2 (near-homophones).
       Disabled for wake words shorter than 4 characters, where one edit
       changes the word too easily. A bare "roy" does not match "Leroy", and
       "laura"/"really" are rejected despite sharing a Soundex key or letters.
    4. The same rules against each alias.

    Aliases exist because short wake words are the ones ASR mangles most and
    the ones fuzzy matching can help least. "max" is heard as "Mac", "Mack",
    "macs", "marks"; none of those is within one edit, and widening the
    threshold far enough to catch them would also catch "map" and "tax".
    Listing the spellings that actually occur is both safer and more effective
    than guessing at them.
    """
    normalized_text = _normalize(text)
    if not normalized_text:
        return False

    terms = [wake_word, *aliases]
    return any(_matches_one(normalized_text, _normalize(term)) for term in terms)
