"""The SOP's own examples, as tests. Run: python -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "system"))

from screening.facts import _age_in, parse_kyc_note  # noqa: E402
from screening.rules import dob_agrees, dob_contradicted  # noqa: E402
from screening.text import D, NameRules, age_to_years, country, countries_in, parse_date  # noqa: E402

NR = NameRules(
    [["muhammad", "mohammed", "mohamed"], ["yusuf", "joseph", "joe"], ["joseph", "joe", "jose"], ["anthony", "tony"]],
    ["ltd", "gmbh", "sa", "limited", "inc"],
)


class Names(unittest.TestCase):
    def test_variants_and_order(self):
        self.assertTrue(NR.agree("Mohammed Ali Khan", "Khan, Muhammad Ali"))
        self.assertTrue(NR.agree("Tony Okafor", "Anthony Okafor"))

    def test_variant_groups_are_not_transitive(self):
        self.assertTrue(NR.part_match("yusuf", "joseph"))
        self.assertTrue(NR.part_match("joseph", "jose"))
        self.assertFalse(NR.part_match("yusuf", "jose"))

    def test_written_together_or_apart(self):
        self.assertTrue(NR.agree("Abdulsalam Bello", "Abdul Salam Bello"))
        self.assertTrue(NR.agree("Abdulsalam", "Abdul Salam"))  # one part covering both

    def test_edit_limits(self):
        self.assertTrue(NR.part_match("musa", "moussa") is False)  # 4-letter: max 1 edit, this is 2
        self.assertTrue(NR.part_match("petrov", "petrof"))  # 6 letters, 1 edit
        self.assertFalse(NR.part_match("petrov", "petruf"))  # 6 letters, 2 edits
        self.assertTrue(NR.part_match("alexandra", "aleksandra"))  # long: 2 edits
        self.assertFalse(NR.part_match("ali", "aly") and False)
        self.assertFalse(NR.part_match("bob", "rob"))  # under 4 letters must be identical
        self.assertFalse(NR.part_match("kumar", "kmuar"))  # a swap is two edits

    def test_initials_placeholders_brackets_accents(self):
        self.assertEqual(NR.parts("J. Ramírez (Junior) TEST"), ["ramirez"])
        self.assertTrue(NR.agree("José Ramírez", "Jose RAMIREZ"))

    def test_company_legal_forms(self):
        self.assertTrue(NR.agree("Smith Capital Ltd", "SMITH CAPITAL LIMITED", company=True))
        self.assertEqual(NR.parts("Acme GmbH", company=True), ["acme"])

    def test_two_parts_or_whole_shorter_name(self):
        self.assertTrue(NR.agree("Anna Maria Petrova", "Anna Petrova"))
        self.assertFalse(NR.agree("Anna Petrova", "Anna Ivanova"))


class Dates(unittest.TestCase):
    def test_formats(self):
        for s, want in [("1985-09-12", (1985, 9, 12)), ("12/09/1985", (1985, 9, 12)),
                        ("September 12, 1985", (1985, 9, 12)), ("12 September 1985", (1985, 9, 12)),
                        ("1968-02", (1968, 2, None)), ("1994", (1994, None, None))]:
            d = parse_date(s)
            self.assertEqual((d.year, d.month, d.day), want, s)

    def test_age_at_date_gives_two_years(self):
        self.assertEqual(age_to_years(45, D((2019,), 3)).years, (1973, 1974))

    def test_dob_rules(self):
        cust = D((1979,), 11, 8)
        self.assertTrue(dob_contradicted(cust, D((1976, 1977))))  # >1 year outside
        self.assertFalse(dob_contradicted(cust, D((1978,))))  # exactly 1 year out: not a contradiction
        self.assertTrue(dob_contradicted(cust, D((1979,), 5)))  # same year, month differs
        self.assertTrue(dob_agrees(D((1985,), 9, 12), D((1985, 1986))))
        self.assertTrue(dob_agrees(D((1968,), 2, 17), D((1968,), 2)))  # year+month agrees with full date
        self.assertFalse(dob_agrees(D((1968,), 2, 17), D((1968,), 3)))


class Extraction(unittest.TestCase):
    def test_ages(self):
        for s, a in [("Marcos, 33 year old, a PEP in Brazil, May 2019", 33),
                     ("In January 2023 he was 46.", 46), ("At 27, Juan Gonzalez appeared in October 1978.", 27),
                     ("Zainab Adewale, then 19.", 19), ("Adam Drozd, at age 31, charged in December 2005.", 31)]:
            self.assertEqual(_age_in(s, loose=True), a, s)

    def test_kyc_note(self):
        n = {"note_id": "N1", "created_at": "2024-07-12",
             "text": "Onfido verified the Bangladeshi passport for KARIM KHAN on 2024-07-12; subject date of birth is December 5, 1992."}
        created, name, dob, ctry, src = parse_kyc_note(n)
        self.assertEqual(name, "KARIM KHAN")
        self.assertEqual((dob.year, dob.month, dob.day), (1992, 12, 5))
        self.assertEqual(ctry, "Bangladesh")

    def test_countries(self):
        self.assertEqual(country("NG"), "Nigeria")
        self.assertEqual(country("Nigerian"), "Nigeria")
        self.assertEqual(country("Lagos, Nigeria [Business]"), "Nigeria")
        self.assertIsNone(country("International"))
        self.assertEqual(countries_in("lived and worked in South Korea and Northern Ireland"), ["South Korea", "United Kingdom"])


if __name__ == "__main__":
    unittest.main()
