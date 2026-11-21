# My screening engine

I built a rule-based solution that follows each principle of the bank's identity SOP (`POLICY.md`). It only clears a hit when a fact contradicts it, because one unsafe clear costs as much as 20 needless reviews. On the dev set it gets all 50 practice labels right with no unsafe clears.

Run it with `system/run <data folder> <output file>`. It needs only the Python 3 standard library.

## Architecture

```
system/run <data> <out>
        │
   load        read every file; a missing field is recorded as unknown
        │
   customer    alert link → candidate records → merge into one person profile
        │
   hit         hit fields → first paragraph of notes → web search results
        │
   compare     fact contradiction   → DIFFERENT_ENTITY
               name + DoB agree     → SAME_ENTITY
               otherwise            → REVIEW
        │
   output      decision, code, record, reason, evidence
```

I always run every SOP contradiction (type, deceased, too young, date of birth, name, location), so if two fire, the reason will include both.

## How I find the customer and the listed person

For the customer, I follow the alert's link (account, login or email). If it leads to several people (e.g. a joint account), I use the name and date of birth to pick between them. Without a link, I search for records that fit the screened details. I merge records that share a phone and date of birth, or a document number on an approved check. The latest approved ID check, in the KYC table or in a note, gives the legal name and date of birth. Rejected, failed or expired checks are ignored.

For the listed person, I look for a date of birth in the `dobs` field, then the first paragraph of the profile ("aged 45 in March 2019" means born 1973 or 1974), then web search results. Most search results are about namesakes, so I only use one if it describes the same kind of listing in a country tied to the hit.

## Representing evidence and ensuring reproducibility

Every fact carries the IDs of the records it came from, for example `Fact(value=1968-03-05, sources=("NOTE-14E25A",))`. The evidence for a hit is the sources of the facts that decided it. The reason is built from the same facts, so it can't mention anything the engine didn't use. If a fact is there but I can't read it, I don't clear, and the reason says why. Running `python system/run.py <data> <out> --trace <file>` shows every fact and rule for each hit.

There are no model calls or randomness, and records are processed in a fixed sorted order. I checked that the output bytes match across repeated runs, on Windows and Linux, and under different Python hash seeds. If a record can't be processed, the hit becomes `REVIEW`.

## Model, prompts and cost of a run

I don't use a model at runtime. I used Grok (`grok-4.20-0309-non-reasoning`) offline in `audit/` to re-read and reword the dev text and check what my rules missed. It found 10 reading gaps, such as "b. 1973" not being read as a birth date, and I fixed each one as a rule with a test. The prompts are in `audit/extract.py`, `audit/stage2.py` and `audit/stage3.py`.

A run takes about 20 seconds and costs nothing, because it makes no API calls. The offline audit cost roughly $5.

## Three cases from the dev set

**Clear mismatch: H-FC22E2, `DOB_MISMATCH`.** The customer's approved passport check (KYC-04520F) says Joseph Gallardo, born 2002. The hit gives no birth date itself, but search result SR-65C752 describes "Bartolo Diego Gallardo, 39, a politically exposed person in Peru, in September 2012" (so born in 1972 or 1973), which matches the hit's listing and country.

**Match: H-582410, `NAME_DOB_LOCATION_MATCH`.** The alert's account is joint, and the screened details pick CUS-377745. Their KYC note gives Elvira Velasco Morillo, born 05/03/1968, Spain. The profile says she was 30 in November 1998, so born 1967 or 1968. Name, birth year and country all agree.

**Left for review: H-FA9F83, `INSUFFICIENT_DATA`.** The customer's check (KYC-80B445) gives Diana Cirstian, Moldova. The hit is Diana Cristian, a Moldovan judge. The names agree under the SOP's spelling rule and the country matches, but the hit gives no date of birth, and the SOP needs one to confirm a person.

## Limits

I send 229 of 819 dev hits (28%) to review, because I only clear when I can read the contradiction. I don't read dates of death from profile text, only from `deceased_date`. I also don't read country codes such as "US" in search results. In a rare case that could hide an agreeing birth date and let a country difference clear a hit, but reading short codes in free text creates false matches of its own.
