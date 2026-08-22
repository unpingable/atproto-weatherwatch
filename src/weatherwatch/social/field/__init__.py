"""Social Weather — an instrument for the changing conditions of a public field.

A weather service reports conditions. It does not decide who is responsible
for the storm. This package answers *what changed in the interaction field*
and is built so that it cannot answer *who caused it*.

The distinction is not a disclaimer bolted on at the end; it is why the data
model looks the way it does. Every quantity here is computed from
weatherwatch's minute counters, whose output alphabet is finite and
identity-free by construction, so there is no actor, no target and no text
anywhere in the lineage. A question about a person is not refused at the API
-- it is unanswerable from what was kept.

Order of work, deliberately:

    1. climatology   what is the normal weather?
    2. observation   what were conditions in this window?
    3. candidates    which windows sit outside their own climatology?
    4. (not built)   detectors

Starting at 3 without 1 is how "unusual" gets defined by whoever picked the
threshold. `climatology.py` exists so "unusual" has a denominator first, and
so the instrument can say plainly when its own history is too short to
support the claim.
"""

FIELD_SCHEMA_VERSION = 1
