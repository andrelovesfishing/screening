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


class OtherPeoplesNotes(unittest.TestCase):
    # Principle 2: "Never use another customer's records." A KYC note reached through a shared email
    # whose holder shares no name part with the customer is about someone else.
    def test_unrelated_note_is_ignored(self):
        from screening.facts import CustomerResolver

        class _S:
            applicants_by_customer, kyc_by_applicant = {}, {}
            customers = {"CUS-1": {"record_id": "CUS-1", "full_name": "Hadiza Adewale", "email": "x@y.z",
                                   "entity_type": "Individual", "dob": "1990-01-01", "residence_country": "NG"}}
            notes_by_email = {"x@y.z": [{"note_id": "NOTE-1", "channel": "kyc", "created_at": "2023-06-14",
                                         "text": "On 2023-06-14, Onfido validated the passport for EMEKA NWOSU, born 16 October 1995."}]}
        p = CustomerResolver(_S(), NR).profile(["CUS-1"], {"customer_name": "Hadiza Adewale"})
        self.assertIsNone(p.verified_name)
        self.assertEqual(p.dob.sources, ("CUS-1",))

    def test_related_note_is_used(self):
        from screening.facts import CustomerResolver

        class _S:
            applicants_by_customer, kyc_by_applicant = {}, {}
            customers = {"CUS-1": {"record_id": "CUS-1", "full_name": "Davit Sargsyan", "email": "x@y.z",
                                   "entity_type": "Individual"}}
            notes_by_email = {"x@y.z": [{"note_id": "NOTE-1", "channel": "kyc", "created_at": "2023-06-14",
                                         "text": "On 2023-06-14, Onfido validated the passport for DANIEL SARGSYAN, born 16 October 1995."}]}
        p = CustomerResolver(_S(), NR).profile(["CUS-1"], {"customer_name": "Davit Sargsyan"})
        self.assertEqual(p.verified_name.value, "DANIEL SARGSYAN")


def company(name="Starling Logistics Ltd", inc="2015-03-01", countries=("United Kingdom",)):
    p = person(name, inc, countries)
    p.entity_type = "Company"
    return p


def company_hit(names=("Starling Logistics Limited",), inc=(), countries=("United Kingdom",), report=None):
    h = hit(names, inc, countries)
    h.entity_type = "Company"
    h.earliest_report = Fact(parse_date(report), ("H-1",)) if report else None
    return h


class RareClauses(unittest.TestCase):
    """Principle 6 and 7 paths that are rare on dev."""

    def test_younger_than_16_at_earliest_report_clears(self):
        h = hit(dobs=())
        h.earliest_report = Fact(parse_date("2020-02-18"), ("H-1",))
        o = decide(person(dob="2007-08-21"), h, ALERT, NR)
        self.assertEqual((o.decision, o.code), ("DIFFERENT_ENTITY", "TEMPORAL_IMPOSSIBILITY"))

    def test_exactly_16_at_report_does_not_clear(self):
        h = hit(dobs=())
        h.earliest_report = Fact(parse_date("2023-08-21"), ("H-1",))
        o = decide(person(dob="2007-08-21"), h, ALERT, NR)
        self.assertNotEqual(o.code, "TEMPORAL_IMPOSSIBILITY")

    def test_company_incorporated_after_report_clears(self):
        o = decide(company(inc="2021-05-01"), company_hit(report="2019-09-10"), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("DIFFERENT_ENTITY", "TEMPORAL_IMPOSSIBILITY"))

    def test_company_incorporated_before_report_does_not_clear_on_time(self):
        o = decide(company(inc="2015-03-01"), company_hit(report="2019-09-10"), ALERT, NR)
        self.assertNotEqual(o.code, "TEMPORAL_IMPOSSIBILITY")

    def test_death_before_screening_clears(self):
        h = hit()
        h.deceased = Fact(parse_date("2022-03"), ("H-1",))
        o = decide(person(), h, ALERT, NR)
        self.assertEqual((o.decision, o.code), ("DIFFERENT_ENTITY", "DECEASED"))

    def test_death_after_screening_does_not_clear(self):
        h = hit()
        h.deceased = Fact(parse_date("2026-12-01"), ("H-1",))
        self.assertNotEqual(decide(person(), h, ALERT, NR).code, "DECEASED")

    def test_person_against_company_clears_on_type(self):
        o = decide(person(), company_hit(names=("Okafor Amina Bello Ltd",)), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("DIFFERENT_ENTITY", "ENTITY_TYPE_MISMATCH"))

    def test_company_confirms_on_name_and_country(self):
        # Principle 7: "a company when name and country agree"; legal forms are not name parts.
        o = decide(company(), company_hit(), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("SAME_ENTITY", "NAME_LOCATION_MATCH"))

    def test_company_name_agrees_but_no_shared_country_clears_on_location(self):
        o = decide(company(), company_hit(countries=("Singapore",)), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("DIFFERENT_ENTITY", "LOCATION_MISMATCH"))

    def test_company_incorporation_date_mismatch_clears(self):
        o = decide(company(inc="2015-03-01"), company_hit(inc=("1998",), countries=()), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("DIFFERENT_ENTITY", "DOB_MISMATCH"))

    def test_one_part_name_neither_confirms_nor_clears_on_name(self):
        # Principle 8: fewer than two real parts can be neither confirmed nor cleared on name.
        o = decide(person(name="Amina"), hit(names=("Zainab Yusuf",), countries=()), ALERT, NR)
        self.assertNotEqual(o.code, "NAME_MISMATCH_VERIFIED")
        o = decide(person(name="Amina NA"), hit(names=("Amina",)), ALERT, NR)
        self.assertNotEqual(o.decision, "SAME_ENTITY")

    def test_single_part_hit_alias_can_agree(self):
        o = decide(person(), hit(names=("Okafor",)), ALERT, NR)
        self.assertEqual(o.decision, "SAME_ENTITY")

    def test_dob_one_year_out_does_not_contradict(self):
        o = decide(person(dob="1980-05-02"), hit(dobs=("1981",), countries=()), ALERT, NR)
        self.assertNotEqual(o.code, "DOB_MISMATCH")

    def test_dob_same_year_other_month_contradicts(self):
        o = decide(person(dob="1980-05-02"), hit(dobs=("1980-07",), countries=()), ALERT, NR)
        self.assertEqual(o.code, "DOB_MISMATCH")


class AuditFindings(unittest.TestCase):
    """Fixes from the part B Grok fact audit (checked by hand against POLICY.md)."""

    def test_f1_age_colon_line_is_read(self):
        # H-4A2E8B: "Age: 55" at "December 13, 2017" -> born 1961 or 1962 (principle 5).
        from screening.facts import _age_in
        self.assertEqual(_age_in("information as of December 13, 2017:\nName: Timothy Villagomez\nAge: 55"), 55)

    def test_f1_age_line_gives_hit_dob(self):
        from screening.facts import hit_profile

        class _S:
            search_by_query = {}
        hp = hit_profile(_S(), {"hit_id": "H-4", "entity_type": "Individual", "name": "Timothy Villagomez",
                                "profile_notes": "According to the Federal Bureau of Prisons, information as of "
                                                 "December 13, 2017:\nName: Timothy Villagomez\nAge: 55"})
        self.assertEqual([d.value.years for d in hp.dobs], [(1961, 1962)])

    def test_f2_day_of_a_date_is_not_an_age(self):
        from screening.facts import _age_in
        for s in ("As at 19 November 2025 the details were current.", "at 3rd March 2020 he was listed",
                  "then 21 of May 2019", "He was 12 Nov 2020 arrested"):
            self.assertIsNone(_age_in(s), s)
            self.assertIsNone(_age_in(s, loose=True), s)
        self.assertEqual(_age_in("Zainab Adewale, then 19."), 19)

    def test_f3_company_hit_dates_only_from_dobs_field(self):
        # Principle 5: "For companies, only the dobs field is an incorporation date."
        from screening.facts import hit_profile

        class _S:
            search_by_query = {}
        hp = hit_profile(_S(), {"hit_id": "H-5", "entity_type": "Company", "name": "Acme Holdings Ltd",
                                "profile_notes": "Acme Holdings, born of a 1998 merger, aged 25 in March 2023."})
        self.assertEqual(hp.dobs, [])


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
