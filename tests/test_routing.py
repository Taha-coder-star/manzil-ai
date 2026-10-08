import uuid

import pytest

from app import webhook
from app.routing import EnvSenderResolver, RoutingConfigError, parse_sender_agencies

AGENCY_A, AGENCY_B = uuid.uuid4(), uuid.uuid4()


def test_parses_sender_map():
    raw = f" 111={AGENCY_A} , 222={AGENCY_B}"

    assert parse_sender_agencies(raw) == {"111": AGENCY_A, "222": AGENCY_B}


@pytest.mark.parametrize("raw", [
    None, "", "111", f"={AGENCY_A}", "111=", "111=not-a-uuid",
    f"111={AGENCY_A},111={AGENCY_B}", f"111={AGENCY_A},",
])
def test_rejects_bad_sender_map(raw):
    with pytest.raises(RoutingConfigError):
        parse_sender_agencies(raw)


def test_resolver_has_no_default_agency():
    resolver = EnvSenderResolver({"111": AGENCY_A})

    assert resolver.resolve("111") == AGENCY_A
    assert resolver.resolve("999") is None


def test_extract_skips_unsupported_and_incomplete_items():
    envelope = {"entry": [
        {"changes": [
            {"field": "account_update", "value": {}},
            {"field": "messages", "value": {"messages": [{"id": "wamid.X"}]}},
            {"field": "messages", "value": {
                "metadata": {"phone_number_id": "111"},
                "messages": [{"type": "text"}, {"id": "wamid.OK", "from": "92300"}],
                "statuses": [{"id": "wamid.S"}, {"id": "wamid.S", "status": "read"}],
            }},
        ]},
        {"changes": "not-a-list"},
        "not-an-entry",
    ]}

    events, skipped = webhook.extract_events(envelope)

    assert [(e.event_key, e.job_type) for e in events] == [
        ("wa:msg:wamid.OK", "process_message"),
        ("wa:status:wamid.S:read", "reconcile_status"),
    ]
    assert skipped == 4
    assert events[0].payload["contact"] is None
