import re

SUFFIXES = (
    "st|street|ave|avenue|rd|road|blvd|boulevard|ln|lane|dr|drive|ct|court|pl|place|way|pkwy|parkway|hwy|highway"
)
HASHTAG = re.compile(r"(?<![\w&])#\w+")
ADDRESS = re.compile(
    rf"\b\d{{1,6}}(?:-\d{{1,6}})?\s+((?:[A-Za-z0-9'.\-]+\s+){{1,3}}?)(?:{SUFFIXES})\b",
    re.I,
)
PO_BOX = re.compile(r"\bP\.?\s?O\.?\s+box\b", re.I)
NOT_STREET = {
    "min", "mins", "minute", "minutes", "hour", "hours", "hr", "hrs", "day", "days", "week", "weeks",
    "month", "months", "year", "years", "mile", "miles", "km", "the", "a", "an", "of", "in", "on", "to",
    "and", "friends", "people", "best", "great", "new", "more", "hike", "hikes", "stop", "stops",
}


def _has_address(text: str) -> bool:
    for m in ADDRESS.finditer(text):
        words = [w.strip(".'-").lower() for w in m.group(1).split()]
        if not any(w in NOT_STREET for w in words):
            return True
    return False


def validate_caption(text: str, home_names: list[str], hashtag_count: int = 3) -> list[str]:
    problems = []
    if not text or not text.strip():
        return ["caption is empty"]
    if len(text) >= 2200:
        problems.append("caption must be under 2200 characters")
    n = len(HASHTAG.findall(text))
    if n != hashtag_count:
        problems.append(f"caption must contain exactly {hashtag_count} hashtags, found {n}")
    for name in home_names:
        if name and re.search(rf"(?<!\w){re.escape(name.strip())}(?!\w)", text, re.I):
            problems.append(f"caption must not mention '{name}'")
    if _has_address(text) or PO_BOX.search(text):
        problems.append("caption must not contain a street address")
    return problems
