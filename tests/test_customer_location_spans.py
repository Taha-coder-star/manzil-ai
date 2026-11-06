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
