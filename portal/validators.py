"""Format and checksum rules for IDs and bank details, plus company name matching."""
import hashlib
import re

B36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BAD_EIN_PREFIX = {"00", "07", "08", "09", "17", "18", "19", "28", "29", "49", "69", "70", "78", "79", "89"}


def gstin_check(first14):
    total = 0
    for i, ch in enumerate(first14[:14]):
        product = B36.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return B36[(36 - total % 36) % 36]


def is_gstin(v):
    return bool(re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", v)) and gstin_check(v[:14]) == v[14]


def make_gstin(state, pan):
    base = state + pan + "1Z"
    return base + gstin_check(base)


def is_pan(v):
    return bool(re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", v))


def is_ifsc(v):
    return bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", v))


def is_udyam(v):
    return bool(re.fullmatch(r"UDYAM-[A-Z]{2}-\d{2}-\d{7}", v))


def is_ein(v):
    return bool(re.fullmatch(r"\d{2}-?\d{7}", v)) and v[:2] not in BAD_EIN_PREFIX


def is_aba(v):
    if not re.fullmatch(r"\d{9}", v):
        return False
    d = [int(c) for c in v]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


def make_aba(first8):
    return next(first8 + str(c) for c in range(10) if is_aba(first8 + str(c)))


def _gb_vat_sum(digits):
    return sum(int(digits[i]) * (8 - i) for i in range(7))


def is_gb_vat(v):
    if not re.fullmatch(r"GB\d{9}", v):
        return False
    digits = v[2:]
    total = _gb_vat_sum(digits) + int(digits[7:])
    return total % 97 == 0 or (total + 55) % 97 == 0


def make_gb_vat(first7):
    return f"GB{first7}{(97 - _gb_vat_sum(first7) % 97) % 97:02d}"


def _mod97(s):
    r = 0
    for ch in s:
        n = B36.index(ch)
        r = (r * 10 + n) % 97 if n < 10 else (r * 100 + n) % 97
    return r


def is_iban(v):
    return bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", v)) and _mod97(v[4:] + v[:4]) == 1


def make_iban(country, bban):
    return f"{country}{98 - _mod97(bban + country + '00'):02d}{bban}"


def is_de_vat(v):
    return bool(re.fullmatch(r"DE\d{9}", v))


def looks_like_gstin(v):
    """Right shape, whether or not the check digit is right."""
    return bool(re.fullmatch(r"(0[1-9]|[12]\d|3[0-8])[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", v))


TAX_CHECK = {"IN": is_gstin, "US": is_ein, "GB": is_gb_vat, "DE": is_de_vat}


def clean_id(v):
    return re.sub(r"\s", "", v or "").upper()


def acct_hash(v):
    """Lets us compare bank accounts across vendors without storing the number."""
    return hashlib.sha256(clean_id(v).encode()).hexdigest()


def acct_last4(v):
    c = clean_id(v)
    return "XXXX" + c[-4:] if c else ""


# Company name matching
_SUFFIX = {"pvt", "private", "ltd", "limited", "llp", "llc", "inc", "incorporated", "corp", "corporation", "plc", "co", "company", "the", "and", "gmbh"}
NAME_SAME = 0.9
NAME_LOOKALIKE = 0.8


def _tokens(s):
    return " ".join(sorted(t for t in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().replace("&", " ")).split() if t not in _SUFFIX))


def _levenshtein(a, b):
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def name_score(a, b):
    x, y = _tokens(a), _tokens(b)
    if not x or not y:
        return 0.0
    return 1 - _levenshtein(x, y) / max(len(x), len(y))


# Used where a person's entry is compared with the legal name, and the percentage is shown.
NAME_MATCH = 0.92
NAME_PARTIAL = 0.55
_LEGAL_WORDS = re.compile(r"\b(pvt|private|ltd|limited|llc|inc|gmbh|corp|corporation|co|plc|llp)\b")


def normalise_name(s):
    text = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return " ".join(_LEGAL_WORDS.sub(" ", text).split())


def name_similarity(a, b):
    """The better of word overlap and edit distance, from 0 to 1."""
    x, y = normalise_name(a), normalise_name(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    p, q = set(x.split()), set(y.split())
    overlap = len(p & q) / len(p | q)
    return max(overlap, 1 - _levenshtein(x, y) / max(len(x), len(y)))
