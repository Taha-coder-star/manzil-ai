"""Greeting alone and fresh-search commands (app.customer_intent): pure, no database."""

import pytest

from app.customer_intent import (
    CLEAR_OLD,
    HELLO,
    NEW_SEARCH,
    SALAM,
    asks_for_agent,
    fresh_command,
    greeting_kind,
    listing_lookup,
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


@pytest.mark.parametrize("text", [
    "purani requirements mat hata do",          # "do not remove the old requirements"
    "do not clear old requirements",
    "don't clear old requirements",
    "dont reset",
    "nayi search nahi chahiye",                 # "I do not want a new search"
    "nayi search mat karo",
    "purani requirements na hata do",           # "na" is also a polite particle: ambiguous
    "old filters delete mat karna",
    "never reset my search",
    "kya purani requirements hata sakte ho?",   # a question, not an instruction
    "purani requirements hata do?",
    "can you clear old requirements",
    "agar nayi search karoon to kya hoga",      # a condition
    "reset without losing my budget",
])
def test_a_negated_questioning_or_conditional_command_is_not_a_command(text):
    assert fresh_command(text) is None


@pytest.mark.parametrize("text", [
    "nayi search karo", "nai search please", "start over", "purani requirements hata do",
    "pichli search bhool jao", "reset", "clear old requirements",
])
def test_plain_affirmative_commands_still_work(text):
    assert fresh_command(text) is not None


@pytest.mark.parametrize("text", [
    "agent se baat karao", "mujhe visit karni hai", "I want to talk to an agent",
    "documents ke liye agent se baat karni hai", "kisi se baat karwa dein", "call me please",
    "booking karni hai", "broker ka number do",
])
def test_asking_for_a_person_or_a_visit_is_asking_for_the_agent(text):
    assert asks_for_agent(text) is True


@pytest.mark.parametrize("text", [
    "Is ghar ke documents clear hain?", "is the title clear", "registry ho jayegi?",
    "DH6-101 mein gas connection hai?", "", None,
])
def test_a_question_about_the_listing_is_not_asking_for_the_agent(text):
    assert asks_for_agent(text) is False


@pytest.mark.parametrize("text, code", [
    ("DH6-103 dikhao", "DH6-103"), ("dh6-103 dikhao", "DH6-103"), ("DH6-103", "DH6-103"),
    ("show me DH6-103", "DH6-103"), ("DH6-105 dikhao", "DH6-105"),
    ("DH6-103 ki details bhejo", "DH6-103"), ("dikhao DH6-999 please", "DH6-999"),
])
def test_a_listing_code_asked_for_by_itself_is_a_lookup(text, code):
    assert listing_lookup(text) == code


@pytest.mark.parametrize("text", [
    "DH6-101 mein gas connection hai?", "DH6-101 ki aur photos", "DH6-101 ka price kam hoga?",
    "DH6-101 aur DH6-102 dikhao", "mujhe DH6-101 pasand hai, visit karni hai",
    "Ghar chahiye", "", None, "2026-123 dikhao",
])
def test_anything_else_with_a_code_is_left_to_the_model(text):
    assert listing_lookup(text) is None
