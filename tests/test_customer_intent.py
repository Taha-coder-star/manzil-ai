"""Greeting alone and fresh-search commands (app.customer_intent): pure, no database."""

import pytest

from app.customer_intent import (
    CLEAR_OLD,
    HELLO,
    NEW_SEARCH,
    SALAM,
    fresh_command,
    greeting_kind,
)


@pytest.mark.parametrize("text, kind", [
    ("salam", SALAM), ("Salam!", SALAM), ("Assalam o alaikum", SALAM),
    ("assalamualaikum", SALAM), ("AOA", SALAM), ("Salam bhai", SALAM), ("السلام علیکم", SALAM),
    ("hi", HELLO), ("Hello!", HELLO), ("hey", HELLO), ("good morning", HELLO),
    ("hi, salam", SALAM),
])
def test_a_greeting_alone_is_a_greeting(text, kind):
    assert greeting_kind(text) == kind


@pytest.mark.parametrize("text", [
    "", "   ", "shukriya", "thanks", "ok", "o", "bhai",
    "salam, DHA mein flat chahiye", "hi 3 bed flat", "hello i want a house",
    "salam mera area dha phase 6 hai", "good morning 8 crore",
])
def test_a_greeting_with_a_request_or_no_greeting_goes_on_to_the_model(text):
    assert greeting_kind(text) is None


@pytest.mark.parametrize("text, kind, standalone", [
    ("nayi search", NEW_SEARCH, True),
    ("Nayi search please", NEW_SEARCH, True),
    ("mujhe nayi search karni hai", NEW_SEARCH, True),
    ("new search", NEW_SEARCH, True),
    ("salam nayi search", NEW_SEARCH, True),
    ("Nayi search: DHA 6 mein flat chahiye", NEW_SEARCH, False),
    ("purani requirements hata do", CLEAR_OLD, True),
    ("purani requirements hata do ji", CLEAR_OLD, True),
    ("please clear old requirements", CLEAR_OLD, True),
    ("pichli search bhool jao", CLEAR_OLD, True),
    ("old filters delete karo", CLEAR_OLD, True),
    ("reset", CLEAR_OLD, True),
    ("purani requirements hata do, DHA 6 mein flat", CLEAR_OLD, False),
])
def test_a_fresh_search_command_is_found_and_marked_standalone_or_not(text, kind, standalone):
    assert fresh_command(text) == (kind, standalone)


@pytest.mark.parametrize("text", [
    "mera area dha phase 6", "rent par ghar chahiye", "8 crore tak", "pichli baar 500 gaz tha",
    "nayi listing dikhao", "purana ghar", "salam",
])
def test_ordinary_requests_are_not_commands(text):
    assert fresh_command(text) is None
