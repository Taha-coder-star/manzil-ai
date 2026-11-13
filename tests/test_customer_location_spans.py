"""D2-02 semantic location boundaries; no provider calls or database."""
import pytest

from app import customer_tools, demo_dataset, location

G = location.Gazetteer.from_dataset(demo_dataset.load())


@pytest.mark.parametrize('span', [
    'office on rent in Karachi', 'around 5 lakh per month',
    'house for sale in North Karachi', 'shop for rent in Clifton',
])
def test_unresolvable_requirement_clauses_need_extraction_repair(span):
    assert customer_tools.misbucketed_location(span, G)


@pytest.mark.parametrize('span', [
    'Karachi', 'in Karachi', 'DHA Phase 6', 'DHA City Karachi',
    'North Karachi', 'Block 6', 'p6', 'Clifon', '5 lakh road',
    'office on rent in Lahore',
])
def test_places_ambiguity_unsupported_names_and_other_cities_keep_existing_policy(span):
    assert not customer_tools.misbucketed_location(span, G)


def test_a_real_catalog_alias_is_not_rejected_because_it_contains_intent_words():
    span = 'office on rent in Karachi'
    gz = location.Gazetteer(G.places.values(), [*G.aliases, location.Alias('khi', span)])
    assert not customer_tools.misbucketed_location(span, gz)


@pytest.mark.parametrize('span,text,previous', [
    ('Bahria Town','Bahria Town Phase 8 mein ghar',()),
    ('Bahria','Bahria Town Karachi Phase 8 mein ghar',()),
    ('DHA','DHA mein phase 8 ghar',()),
    ('Defence','Defence Phase VIII mein ghar',()),
    ('Gulshan','Gulshan Block H mein flat',()),
    ('Bahria Town','Bahria Town Phase 8 mein ghar',('Bahria Town Phase 8',)),
    ('Phase 8','Bahria Town Phase 8 mein ghar',('Bahria Town Phase 8',)),
])
def test_a_qualified_place_cannot_be_shortened_during_extraction_repair(span,text,previous):
    assert customer_tools.incomplete_location_span(span,text,previous)


@pytest.mark.parametrize('span,text,previous', [
    ('Bahria Town','Bahria Town mein ghar',()),
    ('DHA Phase 8','DHA Phase 8 mein ghar',()),
    ('Phase 8','Phase 8 mein ghar',()),
    ('p6','p6 mein ghar',()),
    ('DHA Phase 6','DHA Phase 6 aur Phase 8 mein ghar',()),
    ('Phase 8','DHA Phase 6 aur Phase 8 mein ghar',('DHA Phase 6',)),
    ('Defence Phase 8','Bahria Town Phase 8 ya Defence Phase 8',('Bahria Town Phase 8',)),
])
def test_complete_places_bare_qualifiers_and_explicit_alternatives_keep_existing_resolution(span,text,previous):
    assert not customer_tools.incomplete_location_span(span,text,previous)
