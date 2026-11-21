"""SOP clauses checked on hand-built people and hits. Run: python -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "system"))

from screening.facts import Fact, HitProfile, Person  # noqa: E402
from screening.rules import decide  # noqa: E402
from screening.text import NameRules, parse_date  # noqa: E402

NR = NameRules([], ["ltd"])
ALERT = {"screened_at": "2026-11-14"}


def person(name="Amina Bello Okafor", dob="1980-05-02", countries=("Nigeria",)):
    return Person(
        record_ids=["CUS-1"], entity_type="individual",
        names=[Fact(name, ("KYC-1",))], verified_name=Fact(name, ("KYC-1",)),
        dob=Fact(parse_date(dob), ("KYC-1",)) if dob else None,
        countries=[Fact(c, ("CUS-1",)) for c in countries],
    )


def hit(names=("Okafor, Amina Bello",), dobs=("1980",), countries=("Nigeria",)):
    return HitProfile(
        hit_id="H-1", entity_type="individual", names=list(names),
        dobs=[Fact(parse_date(d), ("H-1",)) for d in dobs], dob_origin="dobs field",
        countries=[Fact(c, ("H-1",)) for c in countries],
        deceased=None, earliest_report=None, listing_phrase="a sanctioned individual",
    )


class CountryDifference(unittest.TestCase):
    def test_name_and_dob_agree_countries_differ_is_review(self):
        # Principle 6: "A country difference does not clear a hit whose name and date of birth agree:
        # that hit is REVIEW." It is not a confirmation either, because the countries contradict.
        o = decide(person(), hit(countries=("Pakistan",)), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("REVIEW", "INSUFFICIENT_DATA"))
        self.assertIn("Pakistan", o.reason)

    def test_name_and_dob_agree_hit_gives_no_country_confirms(self):
        o = decide(person(), hit(countries=()), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("SAME_ENTITY", "NAME_DOB_MATCH"))

    def test_all_three_agree_confirms(self):
        o = decide(person(), hit(), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("SAME_ENTITY", "NAME_DOB_LOCATION_MATCH"))

    def test_country_difference_without_dob_agreement_clears(self):
        o = decide(person(), hit(dobs=(), countries=("Pakistan",)), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("DIFFERENT_ENTITY", "LOCATION_MISMATCH"))


class ReasonText(unittest.TestCase):
    def test_review_reason_says_what_is_missing(self):
        o = decide(person(), hit(dobs=()), ALERT, NR)
        self.assertEqual(o.decision, "REVIEW")
        self.assertIn("the hit gives no date of birth", o.reason)

    def test_same_input_same_words(self):
        a = decide(person(), hit(countries=("Pakistan", "Iran")), ALERT, NR).reason
        b = decide(person(), hit(countries=("Iran", "Pakistan")), ALERT, NR).reason
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
