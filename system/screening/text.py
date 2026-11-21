"""Text primitives: normalisation, dates, countries and the SOP's name-matching rule (principle 8).

Everything here is pure and deterministic.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

# ---------------------------------------------------------------- normalising


def fold(s: str) -> str:
    """Lowercase, strip accents, drop anything in brackets, punctuation -> space."""
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", s or "")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ß", "ss").replace("ø", "o").replace("æ", "ae").replace("đ", "d").replace("ł", "l")
    s = re.sub(r"['’`]", "", s)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def as_list(v) -> list[str]:
    """Fields arrive as JSON lists or as '; '-joined strings (CSV export)."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    return [p.strip() for p in s.split(";") if p.strip()]


# ---------------------------------------------------------------- dates


@dataclass(frozen=True)
class D:
    """A possibly partial date. years: candidate birth/event years (age-derived dates give two)."""

    years: tuple[int, ...]
    month: int | None = None
    day: int | None = None

    @property
    def year(self) -> int:
        return self.years[0]

    def text(self) -> str:
        if len(self.years) > 1:
            return " or ".join(str(y) for y in self.years)
        out = f"{self.year:04d}"
        if self.month:
            out += f"-{self.month:02d}"
            if self.day:
                out += f"-{self.day:02d}"
        return out

    def latest_ordinal(self) -> tuple[int, int, int]:
        """Latest calendar point this date could mean (for 'certainly before' tests)."""
        y = max(self.years)
        return (y, self.month or 12, self.day or 31)

    def earliest_ordinal(self) -> tuple[int, int, int]:
        y = min(self.years)
        return (y, self.month or 1, self.day or 1)


MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"

_DATE_PATTERNS = [
    # 2024-03-05, 2024-3-5, 2024-03, 2024/03/05, 2024.03.05
    (re.compile(r"\b(\d{4})([-/.])(\d{1,2})(?:\2(\d{1,2}))?\b"), lambda m: (m[1], m[3], m[4])),
    # 05/03/2024, 05.03.2024, 05-03-2024 (day first: UK data)
    (re.compile(r"\b(\d{1,2})([/.-])(\d{1,2})\2(\d{4})\b"), lambda m: (m[4], m[3], m[1])),
    # March 5, 2024 / March 05 2024 / May-5-2024
    (re.compile(rf"\b({_MON})[\s-]+(\d{{1,2}})(?:st|nd|rd|th)?,?[\s-]+(\d{{4}})\b", re.I), lambda m: (m[3], m[1], m[2])),
    # 5 March 2024 / 5th of March, 2024 / 3-May-1970
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s-]+(?:of\s+)?({_MON}),?[\s-]+(\d{{4}})\b", re.I), lambda m: (m[3], m[2], m[1])),
    # March 2024 / March, 2024
    (re.compile(rf"\b({_MON}),?\s+(\d{{4}})\b", re.I), lambda m: (m[2], m[1], None)),
    # bare year
    (re.compile(r"\b(1[89]\d\d|20\d\d)\b"), lambda m: (m[1], None, None)),
]


def _mk(y, mo, d) -> D | None:
    try:
        y = int(y)
        if isinstance(mo, str) and not mo.isdigit():
            mo = MONTHS[mo.lower().rstrip(".")[:3]]
        mo = int(mo) if mo else None
        d = int(d) if d else None
    except (KeyError, ValueError):
        return None
    if mo is not None and not 1 <= mo <= 12:
        return None
    if d is not None and not 1 <= d <= 31:
        return None
    if not 1800 <= y <= 2100:
        return None
    return D((y,), mo, d)


def find_dates(text: str) -> list[tuple[int, int, D]]:
    """All dates in free text as (start, end, D), longest match wins, left to right."""
    taken: list[tuple[int, int]] = []
    out = []
    for rx, parts in _DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            d = _mk(*parts(m))
            if d:
                taken.append((m.start(), m.end()))
                out.append((m.start(), m.end(), d))
    return sorted(out, key=lambda t: t[0])


def parse_date(s: str) -> D | None:
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"(1[89]|20)\d{6}", s):  # 19700503, only as a whole field value
        return _mk(s[:4], s[4:6], s[6:])
    found = find_dates(s)
    return found[0][2] if found else None


def age_to_years(age: int, at: D) -> D:
    """Someone aged 45 in March 2019 was born in 1973 or 1974."""
    y = at.year - age
    return D((y - 1, y))


# ---------------------------------------------------------------- countries

_COUNTRY_NAMES = """
Afghanistan|Albania|Algeria|Andorra|Angola|Antigua and Barbuda|Argentina|Armenia|Australia|Austria|Azerbaijan|
Bahamas|Bahrain|Bangladesh|Barbados|Belarus|Belgium|Belize|Benin|Bhutan|Bolivia|Bosnia and Herzegovina|Botswana|
Brazil|Brunei|Bulgaria|Burkina Faso|Burundi|Cambodia|Cameroon|Canada|Cape Verde|Central African Republic|Chad|Chile|
China|Colombia|Comoros|Congo|Democratic Republic of the Congo|Costa Rica|Cote D'Ivoire|Croatia|Cuba|Cyprus|
Czech Republic|Denmark|Djibouti|Dominica|Dominican Republic|Ecuador|Egypt|El Salvador|Equatorial Guinea|Eritrea|
Estonia|Eswatini|Ethiopia|Fiji|Finland|France|Gabon|Gambia|Georgia|Germany|Ghana|Greece|Grenada|Guatemala|Guinea|
Guinea-Bissau|Guyana|Haiti|Honduras|Hungary|Iceland|India|Indonesia|Iran|Iraq|Ireland|Israel|Italy|Jamaica|Japan|
Jordan|Kazakhstan|Kenya|Kiribati|Kosovo|Kuwait|Kyrgyzstan|Laos|Latvia|Lebanon|Lesotho|Liberia|Libya|Liechtenstein|
Lithuania|Luxembourg|Madagascar|Malawi|Malaysia|Maldives|Mali|Malta|Marshall Islands|Mauritania|Mauritius|Mexico|
Micronesia|Moldova|Monaco|Mongolia|Montenegro|Morocco|Mozambique|Myanmar|Namibia|Nauru|Nepal|Netherlands|New Zealand|
Nicaragua|Niger|Nigeria|North Korea|North Macedonia|Norway|Oman|Pakistan|Palau|Palestine|Panama|Papua New Guinea|
Paraguay|Peru|Philippines|Poland|Portugal|Qatar|Romania|Russia|Rwanda|Saint Kitts and Nevis|Saint Lucia|
Saint Vincent and the Grenadines|Samoa|San Marino|Sao Tome and Principe|Saudi Arabia|Senegal|Serbia|Seychelles|
Sierra Leone|Singapore|Slovakia|Slovenia|Solomon Islands|Somalia|South Africa|South Korea|South Sudan|Spain|
Sri Lanka|Sudan|Suriname|Sweden|Switzerland|Syria|Taiwan|Tajikistan|Tanzania|Thailand|Timor-Leste|Togo|Tonga|
Trinidad and Tobago|Tunisia|Turkey|Turkmenistan|Tuvalu|Uganda|Ukraine|United Arab Emirates|United Kingdom|
United States|Uruguay|Uzbekistan|Vanuatu|Vatican City|Venezuela|Vietnam|Yemen|Zambia|Zimbabwe|
Hong Kong|Macao|Puerto Rico|Jersey|Guernsey|Isle of Man|Gibraltar|Bermuda|Cayman Islands|British Virgin Islands|
Northern Mariana Islands|Guam|Greenland|Faroe Islands|Aruba|Curacao|New Caledonia|French Polynesia|Western Sahara
"""
COUNTRIES = [c.strip() for c in _COUNTRY_NAMES.replace("\n", "").split("|") if c.strip()]

_ALIASES = {
    # codes
    "US": "United States", "USA": "United States", "GB": "United Kingdom", "UK": "United Kingdom",
    "NG": "Nigeria", "DE": "Germany", "ES": "Spain", "FR": "France", "IT": "Italy", "BD": "Bangladesh",
    "IE": "Ireland", "PL": "Poland", "IN": "India", "CA": "Canada", "AU": "Australia", "NL": "Netherlands",
    "KE": "Kenya", "AT": "Austria", "PK": "Pakistan", "BR": "Brazil", "MX": "Mexico", "PT": "Portugal",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "CH": "Switzerland", "BE": "Belgium",
    "ZA": "South Africa", "GH": "Ghana", "AE": "United Arab Emirates", "CN": "China", "JP": "Japan",
    "RU": "Russia", "TR": "Turkey", "SG": "Singapore", "NZ": "New Zealand", "AR": "Argentina", "CO": "Colombia",
    # long / variant forms
    "United States of America": "United States", "U.S.": "United States", "U.S.A.": "United States",
    "Great Britain": "United Kingdom", "England": "United Kingdom", "Scotland": "United Kingdom",
    "Wales": "United Kingdom", "Northern Ireland": "United Kingdom", "Britain": "United Kingdom",
    "Russian Federation": "Russia", "Republic of Korea": "South Korea", "Korea, Republic of": "South Korea",
    "Korea": "South Korea", "Czechia": "Czech Republic", "Ivory Coast": "Cote D'Ivoire", "Côte d'Ivoire": "Cote D'Ivoire",
    "Palestina": "Palestine", "State of Palestine": "Palestine", "Viet Nam": "Vietnam", "Holland": "Netherlands",
    "The Netherlands": "Netherlands", "Macau": "Macao", "Macao Special Administrative Region of China": "Macao",
    "Hong Kong Special Administrative Region of China": "Hong Kong", "Burma": "Myanmar", "Swaziland": "Eswatini",
    "Macedonia": "North Macedonia", "Turkiye": "Turkey", "Türkiye": "Turkey", "UAE": "United Arab Emirates",
    "Syrian Arab Republic": "Syria", "Iran, Islamic Republic of": "Iran", "DRC": "Democratic Republic of the Congo",
    # demonyms
    "American": "United States", "British": "United Kingdom", "English": "United Kingdom", "Scottish": "United Kingdom",
    "Welsh": "United Kingdom", "Nigerian": "Nigeria", "German": "Germany", "Spanish": "Spain", "French": "France",
    "Italian": "Italy", "Bangladeshi": "Bangladesh", "Irish": "Ireland", "Polish": "Poland", "Indian": "India",
    "Canadian": "Canada", "Australian": "Australia", "Dutch": "Netherlands", "Kenyan": "Kenya", "Austrian": "Austria",
    "Pakistani": "Pakistan", "Brazilian": "Brazil", "Mexican": "Mexico", "Portuguese": "Portugal", "Swedish": "Sweden",
    "Norwegian": "Norway", "Danish": "Denmark", "Finnish": "Finland", "Swiss": "Switzerland", "Belgian": "Belgium",
    "South African": "South Africa", "Ghanaian": "Ghana", "Emirati": "United Arab Emirates", "Chinese": "China",
    "Japanese": "Japan", "Russian": "Russia", "Turkish": "Turkey", "Singaporean": "Singapore",
    "New Zealander": "New Zealand", "Argentine": "Argentina", "Argentinian": "Argentina", "Colombian": "Colombia",
    "Peruvian": "Peru", "Chilean": "Chile", "Venezuelan": "Venezuela", "Ecuadorian": "Ecuador", "Egyptian": "Egypt",
    "Moroccan": "Morocco", "Ukrainian": "Ukraine", "Romanian": "Romania", "Greek": "Greece", "Hungarian": "Hungary",
    "Czech": "Czech Republic", "Serbian": "Serbia", "Croatian": "Croatia", "Jamaican": "Jamaica", "Filipino": "Philippines",
    "Malaysian": "Malaysia", "Thai": "Thailand", "Vietnamese": "Vietnam", "Indonesian": "Indonesia",
    "Sri Lankan": "Sri Lanka", "Nepalese": "Nepal", "Afghan": "Afghanistan", "Iranian": "Iran", "Iraqi": "Iraq",
    "Syrian": "Syria", "Lebanese": "Lebanon", "Jordanian": "Jordan", "Saudi": "Saudi Arabia", "Israeli": "Israel",
    "Somali": "Somalia", "Ethiopian": "Ethiopia", "Ugandan": "Uganda", "Tanzanian": "Tanzania", "Zimbabwean": "Zimbabwe",
    "Cameroonian": "Cameroon", "Senegalese": "Senegal", "Sudanese": "Sudan", "Eritrean": "Eritrea",
    "Icelandic": "Iceland", "Estonian": "Estonia", "Latvian": "Latvia", "Lithuanian": "Lithuania", "Albanian": "Albania",
    "Kosovan": "Kosovo", "Bosnian": "Bosnia and Herzegovina", "Montenegrin": "Montenegro", "Moldovan": "Moldova",
    "Armenian": "Armenia", "Azerbaijani": "Azerbaijan", "Georgian": "Georgia", "Haitian": "Haiti",
    "Guatemalan": "Guatemala", "Nicaraguan": "Nicaragua", "Panamanian": "Panama", "Paraguayan": "Paraguay",
    "Uruguayan": "Uruguay", "Costa Rican": "Costa Rica", "Dominican": "Dominican Republic", "Cuban": "Cuba",
    "Puerto Rican": "Puerto Rico", "Korean": "South Korea", "South Korean": "South Korea", "North Korean": "North Korea",
    "Luxembourgish": "Luxembourg", "Maltese": "Malta", "Cypriot": "Cyprus", "Slovak": "Slovakia", "Slovenian": "Slovenia",
    "Bulgarian": "Bulgaria", "Belarusian": "Belarus", "Kazakh": "Kazakhstan", "Uzbek": "Uzbekistan",
    "Trinidadian": "Trinidad and Tobago", "Sierra Leonean": "Sierra Leone", "Liberian": "Liberia", "Malian": "Mali",
    "Nigerien": "Niger", "Bahraini": "Bahrain", "Qatari": "Qatar", "Kuwaiti": "Kuwait", "Omani": "Oman",
    "Yemeni": "Yemen", "Libyan": "Libya", "Tunisian": "Tunisia", "Algerian": "Algeria", "Taiwanese": "Taiwan",
}
_ISO3 = {
    "USA": "United States", "GBR": "United Kingdom", "NGA": "Nigeria", "DEU": "Germany", "ESP": "Spain",
    "FRA": "France", "ITA": "Italy", "BGD": "Bangladesh", "IRL": "Ireland", "POL": "Poland", "IND": "India",
    "CAN": "Canada", "AUS": "Australia", "NLD": "Netherlands", "KEN": "Kenya", "AUT": "Austria", "PAK": "Pakistan",
    "BRA": "Brazil", "MEX": "Mexico", "PRT": "Portugal", "SWE": "Sweden", "NOR": "Norway", "DNK": "Denmark",
    "FIN": "Finland", "CHE": "Switzerland", "BEL": "Belgium", "ZAF": "South Africa", "GHA": "Ghana",
    "ARE": "United Arab Emirates", "CHN": "China", "JPN": "Japan", "RUS": "Russia", "TUR": "Turkey",
    "SGP": "Singapore", "NZL": "New Zealand", "ARG": "Argentina", "COL": "Colombia", "PER": "Peru",
    "CHL": "Chile", "VEN": "Venezuela", "EGY": "Egypt", "MAR": "Morocco", "UKR": "Ukraine", "ROU": "Romania",
    "GRC": "Greece", "HUN": "Hungary", "CZE": "Czech Republic", "SRB": "Serbia", "HRV": "Croatia",
    "JAM": "Jamaica", "PHL": "Philippines", "MYS": "Malaysia", "THA": "Thailand", "VNM": "Vietnam",
    "IDN": "Indonesia", "LKA": "Sri Lanka", "NPL": "Nepal", "AFG": "Afghanistan", "IRN": "Iran", "IRQ": "Iraq",
    "SYR": "Syria", "LBN": "Lebanon", "JOR": "Jordan", "SAU": "Saudi Arabia", "ISR": "Israel", "SOM": "Somalia",
    "ETH": "Ethiopia", "UGA": "Uganda", "TZA": "Tanzania", "ZWE": "Zimbabwe", "CMR": "Cameroon", "SEN": "Senegal",
    "SDN": "Sudan", "ERI": "Eritrea", "KOR": "South Korea", "PRK": "North Korea", "HKG": "Hong Kong", "MAC": "Macao",
    "CIV": "Cote D'Ivoire", "LUX": "Luxembourg", "ISL": "Iceland", "EST": "Estonia", "LVA": "Latvia",
    "LTU": "Lithuania", "ALB": "Albania", "QAT": "Qatar", "KWT": "Kuwait", "BHR": "Bahrain", "OMN": "Oman",
}
NOT_COUNTRIES = {"international", "europe", "global", "worldwide", "africa", "asia", "unknown", "n a", "na"}

_CANON = {fold(c): c for c in COUNTRIES}
_CANON.update({fold(k): v for k, v in _ALIASES.items()})
# Longest first so "South Korea" wins over "Korea", "Northern Ireland" over "Ireland".
_FREE_TEXT_KEYS = sorted((k for k in _CANON if len(k) > 2), key=len, reverse=True)
_FREE_RX = re.compile(r"\b(" + "|".join(re.escape(k) for k in _FREE_TEXT_KEYS) + r")\b")


def country(value: str) -> str | None:
    """Canonical country for a field value ('NG', 'Nigerian', 'Lagos, Nigeria [Business]')."""
    if not value:
        return None
    v = re.sub(r"\[[^\]]*\]", "", value).strip()
    last = v.split(",")[-1].strip()
    for cand in (last, v):
        f = fold(cand)
        if f in NOT_COUNTRIES or not f:
            continue
        if f in _CANON:
            return _CANON[f]
        if len(cand) == 2 and cand.upper() in _ALIASES:
            return _ALIASES[cand.upper()]
        if cand.upper() in _ISO3:  # whole-value only: 'CAN' in free text is a word, not Canada
            return _ISO3[cand.upper()]
    found = countries_in(last)
    return found[0] if found else None


def countries_in(text: str) -> list[str]:
    """Countries mentioned in free text (names and demonyms), in order of appearance."""
    out = []
    for m in _FREE_RX.finditer(fold(text)):
        c = _CANON[m.group(1)]
        if c not in out:
            out.append(c)
    return out


# ---------------------------------------------------------------- names (principle 8)

PLACEHOLDERS = {"na", "n a", "test", "unknown", "xxx", "none", "null", "nil", "tbc", "tba", "unk", "nn", "x", "xx"}


class NameRules:
    def __init__(self, variant_groups: list[list[str]], legal_forms: list[str]):
        self.groups_of: dict[str, set[int]] = {}
        for i, g in enumerate(variant_groups):
            for n in g:
                self.groups_of.setdefault(fold(n), set()).add(i)
        self.legal_forms = {fold(x) for x in legal_forms}

    def parts(self, name: str, company: bool = False) -> list[str]:
        out = []
        for p in fold(name).split():
            if len(p) < 2 or p.isdigit() or p in PLACEHOLDERS or any(ch.isdigit() for ch in p):
                continue
            if company and p in self.legal_forms:
                continue
            if p not in out:
                out.append(p)
        return out

    def part_match(self, a: str, b: str) -> bool:
        if a == b:
            return True
        ga, gb = self.groups_of.get(a), self.groups_of.get(b)
        if ga and gb and ga & gb:
            return True
        n = min(len(a), len(b))
        if n < 4:
            return False
        return _lev(a, b, 2 if n >= 7 else 1) <= (2 if n >= 7 else 1)

    def agree(self, name_a: str, name_b: str, company: bool = False) -> bool:
        return self.agree_parts(self.parts(name_a, company), self.parts(name_b, company))

    def agree_parts(self, pa: list[str], pb: list[str]) -> bool:
        if not pa or not pb:
            return False
        short, long_ = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
        matched, covered_short = _max_match(short, long_, self)
        return matched >= 2 or covered_short == len(short)


def _units(parts: list[str]) -> list[tuple[str, frozenset[int]]]:
    """Single parts plus adjacent pairs written together (Abdul Salam ~ Abdulsalam)."""
    u = [(p, frozenset([i])) for i, p in enumerate(parts)]
    u += [(parts[i] + parts[i + 1], frozenset([i, i + 1])) for i in range(len(parts) - 1)]
    return u


def _max_match(short: list[str], long_: list[str], nr: NameRules) -> tuple[int, int]:
    """Maximum one-to-one matching between name units. Returns (#matches, #short parts covered).

    A unit is one part or two adjacent parts written together; each match counts once.
    Names are short (<10 parts), so exhaustive search is cheap and exact.
    """
    us, ul = _units(short), _units(long_)
    edges = [(i, j) for i, (a, _) in enumerate(us) for j, (b, _) in enumerate(ul)
             if (len(us[i][1]) == 1 or len(ul[j][1]) == 1) and nr.part_match(a, b)]
    best = (0, 0)

    def rec(k, used_s, used_l, n, cov):
        nonlocal best
        if (n, cov) > best:
            best = (n, cov)
        for e in range(k, len(edges)):
            i, j = edges[e]
            si, lj = us[i][1], ul[j][1]
            if si & used_s or lj & used_l:
                continue
            rec(e + 1, used_s | si, used_l | lj, n + 1, cov + len(si))

    rec(0, frozenset(), frozenset(), 0, 0)
    return best


@lru_cache(maxsize=200_000)
def _lev(a: str, b: str, cap: int) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        lo = i
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            lo = min(lo, cur[j])
        if lo > cap:
            return cap + 1
        prev = cur
    return prev[-1]
