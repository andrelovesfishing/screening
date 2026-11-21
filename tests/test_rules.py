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


class Countries(unittest.TestCase):
    # Principle 6: "Codes and nationalities are countries; territories count as their own country."
    def test_every_iso_code_reads(self):
        from screening.text import _ISO_ROWS, country
        for a2, a3, name in _ISO_ROWS:
            if a2 != "NA":  # "NA" is a placeholder far more often than Namibia
                self.assertEqual(country(a2), name, a2)
            self.assertEqual(country(a3), name, a3)

    def test_codes_seen_unread_on_dev(self):
        from screening.text import country
        self.assertEqual([country(c) for c in ("RS", "PE", "RO", "CI", "ID")],
                         ["Serbia", "Peru", "Romania", "Cote D'Ivoire", "Indonesia"])

    def test_territories_are_their_own_country(self):
        from screening.text import country
        self.assertEqual(country("JE"), "Jersey")
        self.assertEqual(country("Puerto Rico"), "Puerto Rico")

    def test_codes_are_not_read_in_free_text(self):
        from screening.text import countries_in
        self.assertEqual(countries_in("We can help in the per diem case"), [])


class SilentGuards(unittest.TestCase):
    """Guards fire 0 times on dev; each stops a clear that rests on something we could not read."""

    def test_unread_customer_location_blocks_location_clear(self):
        p = person()
        p.unread_locations = ["NOTE-1 mentions where they live but names no country we read"]
        o = decide(p, hit(dobs=(), countries=("Pakistan",)), ALERT, NR)
        self.assertEqual(o.decision, "REVIEW")
        self.assertIn("could not be read", o.reason)

    def test_unread_hit_dob_blocks_dob_clear(self):
        h = hit(dobs=("1950",))
        self.assertEqual(decide(person(), h, ALERT, NR).code, "DOB_MISMATCH")
        h.unread_dobs = ["dobs value 'circa fifties' not read"]
        self.assertNotEqual(decide(person(), h, ALERT, NR).decision, "DIFFERENT_ENTITY")

    def test_unknown_entity_type_does_not_clear_on_type(self):
        h = hit()
        h.entity_type = "vessel"
        self.assertNotEqual(decide(person(), h, ALERT, NR).code, "ENTITY_TYPE_MISMATCH")

    def test_address_names_its_country_anywhere(self):
        from screening.facts import hit_profile

        class _S:
            search_by_query = {}
        hp = hit_profile(_S(), {"hit_id": "H-2", "entity_type": "Individual", "name": "A B",
                                "addresses": ["Nigeria, Lagos"], "dobs": []})
        self.assertEqual([c.value for c in hp.countries], ["Nigeria"])
        hp = hit_profile(_S(), {"hit_id": "H-3", "entity_type": "Individual", "name": "A B",
                                "addresses": ["12 Marina Road, Lagos"], "dobs": []})
        self.assertEqual(hp.countries, [])
        self.assertEqual(hp.unparsed_countries, ["12 Marina Road, Lagos"])


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
