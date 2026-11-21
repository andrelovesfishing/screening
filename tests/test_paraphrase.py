"""Stress test: wording the dev set never used. The unseen set has 'free text worded differently'.
Each case is (text, expected fact). Run: python -m unittest discover tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "system"))

from screening.data import Store  # noqa: E402,F401
from screening.facts import _age_in, _dated, hit_profile, parse_kyc_note  # noqa: E402
from screening.text import countries_in, country, parse_date  # noqa: E402

DATES = [
    ("03.05.1970", (1970, 5, 3)), ("3rd May 1970", (1970, 5, 3)), ("May 3rd, 1970", (1970, 5, 3)),
    ("1970/05/03", (1970, 5, 3)), ("3-May-1970", (1970, 5, 3)), ("03 May, 1970", (1970, 5, 3)),
    ("3 Sept 1970", (1970, 9, 3)), ("Sep. 3, 1970", (1970, 9, 3)), ("1970-5-3", (1970, 5, 3)),
    ("the 3rd of May 1970", (1970, 5, 3)), ("May 1970", (1970, 5, None)), ("1970", (1970, None, None)),
    ("19700503", (1970, 5, 3)), ("3 mai 1970", (1970, None, None)),  # unknown month word: year only, never invented
]

KYC_NOTES = [  # (text, created_at, name, (y, m, d), country)
    ("Passport verified for JOHN SMITH, d.o.b. 03.05.1970, issued by the United Kingdom.", "2024-01-02",
     "JOHN SMITH", (1970, 5, 3), "United Kingdom"),
    ("Identity check passed (Onfido, 2024-01-02): ADA OBI, date of birth 1981-07-09, Nigerian national ID.",
     "2024-01-02", "ADA OBI", (1981, 7, 9), "Nigeria"),
    ("On 2 January 2024 Veriff approved MARIA GARCIA LOPEZ's Spanish passport; she was born on 9 July 1981.",
     "2024-01-02", "MARIA GARCIA LOPEZ", (1981, 7, 9), "Spain"),
    ("Verification complete for LI WEI. Birthdate: July 9, 1981. Document: Singapore passport.", "2024-01-02",
     "LI WEI", (1981, 7, 9), "Singapore"),
    ("Sumsub confirmed ID for O'NEILL-BRADY SEAN (DOB 09/07/1981) using an Irish driving licence on 2024-01-02.",
     "2024-01-02", "O'NEILL-BRADY SEAN", (1981, 7, 9), "Ireland"),
]

PROFILE_FIRST = [  # (first paragraph, expected possible birth years)
    ("Jan Kowalski was born in 1966.", (1966,)),
    ("Born on 30 June 1969, Christian Zaum is a German national.", (1969,)),
    ("Ahmed Bello (born 1952) is a Nigerian politician.", (1952,)),
    ("He was 52 at the time of his March 2020 arrest.", (1967, 1968)),
    ("In March 2020, aged 52, he was arrested.", (1967, 1968)),
    ("When charged in March 2020 he was fifty-two.", None),  # words: must not invent a number
    ("Marta Ruiz, 52, was charged in March 2020.", (1967, 1968)),
    ("The 52-year-old was charged in March 2020.", (1967, 1968)),
    ("At the time of the March 2020 indictment, Ruiz was aged 52.", (1967, 1968)),
    ("In March 2020 Ruiz, then 52, was charged with fraud.", (1967, 1968)),
    ("Ruiz turned 52 shortly before being charged in March 2020.", (1967, 1968)),
]

SNIPPETS = [  # (snippet, age, (year, month))
    ("In May 2019, 33-year-old Marcos Costa, a politically exposed person in Brazil, was profiled.", 33, (2019, 5)),
    ("Marcos Costa (33), a politically exposed person in Brazil, appeared in a May 2019 report.", 33, (2019, 5)),
    ("A May 2019 article described Marcos Costa, aged thirty-three, as a politically exposed person.", None, (2019, 5)),
    ("Marcos Costa, who turned 33 that year, was named in adverse media in Brazil in May 2019.", 33, (2019, 5)),
    ("Brazil, May 2019: Marcos Costa, 33, a sanctioned individual.", 33, (2019, 5)),
    ("Marcos Costa, age 33, was the subject of enforcement action in Brazil (report of 12 May 2019).", 33, (2019, 5)),
]

COUNTRY_VALUES = [
    ("Republic of Ireland", "Ireland"), ("Côte d'Ivoire", "Cote D'Ivoire"), ("Viet Nam", "Vietnam"),
    ("Korea, Republic of", "South Korea"), ("U.S.A.", "United States"), ("UAE", "United Arab Emirates"),
    ("Kingdom of Saudi Arabia", "Saudi Arabia"), ("Russian Federation", "Russia"), ("Türkiye", "Turkey"),
    ("Czechia", "Czech Republic"), ("The Netherlands", "Netherlands"), ("Islamic Republic of Iran", "Iran"),
    ("PK", "Pakistan"), ("NGA", "Nigeria"), ("GBR", "United Kingdom"), ("Hong Kong SAR", "Hong Kong"),
    ("Federal Republic of Germany", "Germany"), ("People's Republic of China", "China"),
]

RESIDENCE_NOTES = [
    ("Customer spent 2015–2018 living in Ghana before moving here.", "Ghana"),
    ("Mentioned a previous address in Lagos, Nigeria (2012-2016).", "Nigeria"),
    ("Client was resident in the Republic of Ireland for several years.", "Ireland"),
    ("Says they grew up in Côte d'Ivoire and later studied in France.", "Cote D'Ivoire"),
]


class Paraphrases(unittest.TestCase):
    def test_dates(self):
        for s, want in DATES:
            d = parse_date(s)
            got = (d.year, d.month, d.day) if d else None
            with self.subTest(s=s):
                self.assertEqual(got, want)

    def test_kyc_notes(self):
        for text, created, name, dob, ctry in KYC_NOTES:
            _, n, d, c, _ = parse_kyc_note({"note_id": "N", "created_at": created, "text": text})
            with self.subTest(text=text):
                self.assertEqual(n, name)
                self.assertEqual((d.year, d.month, d.day) if d else None, dob)
                self.assertEqual(c, ctry)

    def test_profile_first_paragraph(self):
        for text, years in PROFILE_FIRST:
            hit = {"hit_id": "H-000000", "entity_type": "Individual", "name": "X Y", "profile_notes": text}
            hp = hit_profile(_EmptyStore(), hit)
            got = hp.dobs[0].value.years if hp.dobs else None
            with self.subTest(text=text):
                self.assertEqual(got, years)

    def test_snippets(self):
        for text, age, ym in SNIPPETS:
            at = _dated(text)
            with self.subTest(text=text):
                self.assertEqual(_age_in(text, loose=True), age)
                self.assertEqual((at.year, at.month), ym)

    def test_country_values(self):
        for v, want in COUNTRY_VALUES:
            with self.subTest(v=v):
                self.assertEqual(country(v), want)

    def test_residence_notes(self):
        for text, want in RESIDENCE_NOTES:
            with self.subTest(text=text):
                self.assertIn(want, countries_in(text))


class _EmptyStore:
    search_by_query = {}


if __name__ == "__main__":
    unittest.main()
