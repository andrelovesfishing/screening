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


def _hp(notes, entity="Individual", dobs=()):
    from screening.facts import hit_profile

    class _S:
        search_by_query = {}
    return hit_profile(_S(), {"hit_id": "H-9", "entity_type": entity, "name": "Ahmed Khan",
                              "profile_notes": notes, "dobs": list(dobs)})


class BirthPhrasings(unittest.TestCase):
    """1a: hit DOB wordings beyond "born" (principle 5: a sentence in the first paragraph)."""

    def test_phrasings_are_read(self):
        for text, years in [
            ("The date of birth of Ahmed Khan is the 17th of July 1939.", (1939,)),
            ("The year of birth for Ahmed Khan is 1952.", (1952,)),
            ("Ahmed Khan came into the world in 1966 in Lagos.", (1966,)),
            ("DOB: 30/06/1969. Listed by OFAC.", (1969,)),
            ("Ahmed Khan (D.O.B. 3 March 1950) was listed.", (1950,)),
            ("Birth date 1971-02-01; nationality Pakistani.", (1971,)),
        ]:
            self.assertEqual([d.value.years for d in _hp(text).dobs], [years], text)

    def test_place_of_birth_is_not_a_date(self):
        self.assertEqual(_hp("Place of birth: Lagos. Listed in 2019.").dobs, [])

    def test_unread_phrasing_triggers_the_guard(self):
        hp = _hp("The date of birth of Ahmed Khan is on file as the seventeenth of July.")
        self.assertEqual(hp.dobs, [])
        self.assertTrue(hp.unread_dobs)


class SlashDates(unittest.TestCase):
    """1b: numeric dates. Day first (UK data); month first when day first is impossible;
    when both readings are possible, a clear that rests on the date needs both readings to give it."""

    def test_month_first_when_day_first_impossible(self):
        self.assertEqual(parse_date("6/20/1955").text(), "1955-06-20")

    def test_ambiguous_keeps_both_readings(self):
        d = parse_date("06/10/1962")
        self.assertEqual([r._plain() for r in d.readings()], ["1962-10-06", "1962-06-10"])
        self.assertEqual(len(parse_date("05/05/1995").readings()), 1)  # both readings are the same date
        self.assertEqual(len(parse_date("1962-06-10").readings()), 1)  # ISO is never ambiguous

    def test_dob_clear_needs_both_readings(self):
        # Customer 06/10/1962 could be 10 June: agrees with a hit born June 1962, so no DOB clear.
        p = person(dob="06/10/1962", countries=())
        self.assertNotEqual(decide(p, hit(dobs=("1962-06",), countries=()), ALERT, NR).code, "DOB_MISMATCH")
        self.assertEqual(decide(p, hit(dobs=("1962-03",), countries=()), ALERT, NR).code, "DOB_MISMATCH")

    def test_temporal_clear_needs_both_readings(self):
        h = hit(dobs=())
        h.earliest_report = Fact(parse_date("2023-07-01"), ("H-1",))
        # 01/12/2007 is 1 Dec 2007 (under 16 on 1 Jul 2023) or 12 Jan 2007 (16 by then): no clear.
        self.assertNotEqual(decide(person(dob="01/12/2007"), h, ALERT, NR).code, "TEMPORAL_IMPOSSIBILITY")


def _profile(note_text, full_name, channel="kyc"):
    from screening.facts import CustomerResolver

    class _S:
        applicants_by_customer, kyc_by_applicant = {}, {}
        customers = {"CUS-1": {"record_id": "CUS-1", "full_name": full_name, "email": "x@y.z",
                               "entity_type": "Individual", "dob": "1983-08-24", "residence_country": "UA"}}
        notes_by_email = {"x@y.z": [{"note_id": "NOTE-1", "channel": channel, "created_at": "2024-11-23",
                                     "text": note_text}]}
    return CustomerResolver(_S(), NR).profile(["CUS-1"], {"customer_name": full_name})


class TerseWording(unittest.TestCase):
    """Stage 3 audit (terse rewording) fixes 2a-2d."""

    def test_2a_holder_is_the_caps_name_that_is_the_customer(self):
        p = _profile("Onfido IDV OK for OLEKSII VOLODYMYROVYCH HRYHORIEV 2024-11-23; nat ID accepted DOB 1975-06-04",
                     "Oleksii Hryhoriev")
        self.assertEqual(p.verified_name.value, "OLEKSII VOLODYMYROVYCH HRYHORIEV")
        self.assertEqual(p.dob.value.text(), "1975-06-04")
        self.assertEqual(p.dob.sources, ("NOTE-1",))

    def test_2a_customer_named_but_no_clean_holder_keeps_note_without_a_name(self):
        p = _profile("IDV OK; Oleksii confirmed, DOB 1975-06-04", "Oleksii Hryhoriev")
        self.assertIsNone(p.verified_name)  # an unknown name can neither clear nor confirm
        self.assertEqual(p.dob.value.text(), "1975-06-04")

    def test_2b_uppercase_codes_in_notes_are_countries(self):
        p = _profile("acct holder res+prof pos NG 2013-2023", "Oleksii Hryhoriev", channel="support")
        self.assertIn("Nigeria", [c.value for c in p.countries])
        p = _profile("Customer says OK, IT team to call back. AM shift.", "Oleksii Hryhoriev", channel="support")
        self.assertEqual(sorted({c.value for c in p.countries}), ["Ukraine"])

    def test_2b_wider_residence_guard(self):
        p = _profile("domiciled in Lagos since 2015", "Oleksii Hryhoriev", channel="support")
        self.assertTrue(p.unread_locations)

    def test_2c_explicit_age_before_a_month_is_still_an_age(self):
        from screening.facts import _age_in
        self.assertEqual(_age_in("MARCOS ROBERTO COSTA age 33 May 2019 a politically exposed person Brazil"), 33)
        self.assertEqual(_age_in("aged 52 March 2020"), 52)
        self.assertIsNone(_age_in("As at 19 November 2025 the details were current."))

    def test_2d_b_dot_is_born(self):
        for text, years in [("Ahmed Khan b. 1973, listed by OFAC.", (1973,)),
                            ("Ahmed Khan (b. 17 Jul 1939) was listed.", (1939,)),
                            ("Ahmed Khan, b. 10-Apr-1960.", (1960,))]:
            self.assertEqual([d.value.years for d in _hp(text).dobs], [years], text)

    def test_3a_age_at_time_label_line(self):
        from screening.facts import _age_in
        self.assertEqual(_age_in("Age at time of questioning: 27"), 27)
        self.assertEqual(_age_in("Age at time: 48"), 48)
        self.assertEqual(_age_in("Age at the time of arrest 31"), 31)
        self.assertIsNone(_age_in("As at 19 November 2025 the details were current."))
        # principle 5: aged 27 in June 2006 -> born 1978 or 1979
        hp = _hp("Name: Ahmed Khan\nAge at time of questioning: 27\nQuestioning date: June 2006")
        self.assertEqual([d.value.years for d in hp.dobs], [(1978, 1979)])


class NegativeKycNotes(unittest.TestCase):
    """4a, principle 4: "Rejected or expired checks are not evidence", also when written up as a note."""

    def test_rejected_note_is_not_a_check(self):
        p = _profile("Veriff REJECTED the passport check for OLEKSII HRYHORIEV (DOB 1968-02-01) on 2025-06-01: "
                     "document image unreadable.", "Oleksii Hryhoriev")
        self.assertIsNone(p.verified_name)
        self.assertEqual(p.dob.value.text(), "1983-08-24")  # the record's, not the rejected check's
        self.assertFalse(p.unclear_checks)

    def test_expired_and_failed_wordings(self):
        for t in ("Passport from Ukraine for OLEKSII HRYHORIEV, DOB 1968-02-01, had expired; Onfido check closed.",
                  "Sumsub verification failed for OLEKSII HRYHORIEV, born 1968-02-01.",
                  "Persona could not verify OLEKSII HRYHORIEV (DOB 1968-02-01).",
                  "Veriff check for OLEKSII HRYHORIEV, DOB 1968-02-01, was not successful."):
            p = _profile(t, "Oleksii Hryhoriev")
            self.assertEqual(p.dob.value.text(), "1983-08-24", t)

    def test_successful_note_is_still_a_check(self):
        p = _profile("Persona successfully validated the passport from Ukraine for OLEKSII HRYHORIEV "
                     "(DOB 1968-02-01) on 2024-11-23.", "Oleksii Hryhoriev")
        self.assertEqual(p.dob.value.text(), "1968-02-01")

    def test_failed_then_verified_blocks_clears(self):
        # Mixed outcome: we cannot tell which result is the latest, so its facts are not used and
        # no clear may rest on the customer's facts.
        p = _profile("First Onfido attempt failed (glare); re-submitted and verified OLEKSII HRYHORIEV, "
                     "DOB 1968-02-01, on 2024-11-23.", "Oleksii Hryhoriev")
        self.assertTrue(p.unclear_checks)
        pp = person(dob="1950-01-01")
        pp.unclear_checks = ["NOTE-1 records both a failed and a passed check"]
        o = decide(pp, hit(), ALERT, NR)
        self.assertEqual((o.decision, o.code), ("REVIEW", "INSUFFICIENT_DATA"))
        self.assertIn("NOTE-1", o.evidence)
        self.assertEqual(decide(person(dob="1950-01-01"), hit(), ALERT, NR).code, "DOB_MISMATCH")


class UnlinkedSearch(unittest.TestCase):
    """Gap 3, principle 2 + "unknown stays unknown": without a link, a record that leaves a screened
    detail blank still fits on it (a blank DOB, country or city is not a mismatch)."""

    def _resolver(self, **rec):
        from screening.facts import CustomerResolver
        base = {"record_id": "CUS-9", "full_name": "Amina Bello Okafor", "dob": "", "residence_country": "NG",
                "nationality": "NG", "city": "Lagos", "address": "1 Marina Rd, Lagos", "entity_type": "Individual"}
        base.update(rec)

        class _S:
            customers = {"CUS-9": base}
            cust_by_year = {None: ["CUS-9"]}
            cust_by_part = {}
        return CustomerResolver(_S(), NR)

    ALERT = {"customer_name": "Amina Okafor", "dob": "1980-05-02", "country": "Nigeria", "city": "Lagos",
             "entity_type": "Individual"}

    def test_record_without_dob_is_found(self):
        self.assertEqual(self._resolver().search(self.ALERT), ["CUS-9"])

    def test_blank_country_or_city_is_not_a_mismatch(self):
        r = self._resolver(dob="1980", residence_country="", nationality="", city="", address="")
        self.assertTrue(r.fits(r.s.customers["CUS-9"], self.ALERT))

    def test_a_different_country_or_city_still_rules_out(self):
        r = self._resolver(residence_country="GH", nationality="GH")
        self.assertFalse(r.fits(r.s.customers["CUS-9"], self.ALERT))
        r = self._resolver(city="Abuja", address="2 Wuse St, Abuja")
        self.assertFalse(r.fits(r.s.customers["CUS-9"], self.ALERT))


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
