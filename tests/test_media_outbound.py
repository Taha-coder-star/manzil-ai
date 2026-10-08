"""Task 4.2 acceptance tests: outbound images through the wa_media_id cache (D14).

The WhatsApp client and the bucket are fakes (tests.test_media); the Graph upload
call is an httpx.MockTransport. Database tests use the manzil_app login (RLS on).
All data is fictional.
"""

import time
import uuid

import httpx
import psycopg
import pytest

from app import media, whatsapp
from app.db import tenant_transaction
from app.provision_agency import create_agency
from tests.test_inventory import active_property, insert
from tests.test_media import (  # noqa: F401 - md is a fixture
    CUSTOMER,
    PNID,
    STAFF,
    TOKEN,
    FakeChannel,
    FakeStorage,
    image_message,
    md,
    photo,
)


def stored_photo(md, media_id="111", wamid="wamid.P1"):  # noqa: F811
    md.channel.files[media_id] = photo()
    md.deliver(image_message(wamid, media_id))
    md.drain()
    [(photo_id, conversation_id)] = md.query(
        "SELECT id, conversation_id FROM inbound_media WHERE message_id = "
        "(SELECT id FROM message WHERE provider_message_id = %s)", (wamid,)
    )
    return photo_id, conversation_id


def send(md, photo_id, conversation_id, key):  # noqa: F811
    return media.send_image(
        md.conn, md.channel, md.storage, md.agency, conversation_id, STAFF,
        ("inbound_media", photo_id), key, caption="Fictional photo (DEMO)",
    )


def cache(md, photo_id):  # noqa: F811
    return md.query(
        "SELECT wa_upload_id, extract(epoch FROM wa_upload_expires_at - now()) / 86400 "
        "FROM inbound_media WHERE id = %s", (photo_id,)
    )[0]


def outbound_rows(md):  # noqa: F811
    return md.query(
        "SELECT dedupe_key, status, last_error_code, media_source, media_source_id "
        "FROM outbound_message WHERE dedupe_key LIKE 'k%%' ORDER BY created_at"
    )


def test_same_image_twice_uses_the_cached_id(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)

    first = send(md, photo_id, conversation_id, "k1")
    second = send(md, photo_id, conversation_id, "k2")

    assert (first.status, first.via, second.status, second.via) == ("sent", "id", "sent", "id")
    assert len(md.channel.uploads) == 1
    [wa_key] = [k for k in md.storage.objects if k.endswith("/wa.jpg")]
    assert md.channel.uploads[0] == md.storage.objects[wa_key][0]
    ids = [p["image"]["id"] for p in md.channel.sent if p["type"] == "image"]
    assert ids == ["9000000000000001"] * 2
    cached_id, days_left = cache(md, photo_id)
    assert cached_id == "9000000000000001" and 29.9 < float(days_left) <= 30
    assert [r[3:] for r in outbound_rows(md)] == [("inbound_media", photo_id)] * 2


def test_near_expiry_id_is_uploaded_again(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)
    send(md, photo_id, conversation_id, "k1")
    with tenant_transaction(md.conn, md.agency):
        md.conn.execute("UPDATE inbound_media SET wa_upload_expires_at = now() + interval '4 days'")
    result = send(md, photo_id, conversation_id, "k2")
    assert result.via == "id" and len(md.channel.uploads) == 2
    assert md.channel.sent[-1]["image"]["id"] == "9000000000000002"


def test_forced_invalid_id_falls_back_to_the_link(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)
    media.force_invalid_cached_id(md.conn, md.agency, photo_id)
    md.channel.refuse_ids = {media.INVALID_MEDIA_ID}

    result = send(md, photo_id, conversation_id, "k1")

    assert (result.status, result.via) == ("sent", "link")
    assert md.channel.uploads == []
    link = md.channel.sent[-1]["image"]["link"]
    assert link.startswith("https://demo-bucket.s3.amazonaws.com/tenant/") and "/wa.jpg?" in link
    assert md.channel.sent[-1]["image"]["caption"] == "Fictional photo (DEMO)"
    assert [r[:3] for r in outbound_rows(md)] == [
        ("k1", "failed", "http_400_meta_131009"), ("k1:link", "sent", None),
    ]
    assert cache(md, photo_id) == (None, None)
    # The cache heals: the next send uploads a fresh ID.
    assert send(md, photo_id, conversation_id, "k2").via == "id"
    assert len(md.channel.uploads) == 1


def test_upload_failure_sends_by_link(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)
    md.channel.upload_errors = [whatsapp.PermanentMediaError("upload_http_400")]
    result = send(md, photo_id, conversation_id, "k1")
    assert (result.status, result.via) == ("sent", "link")
    assert cache(md, photo_id) == (None, None)
    assert [r[0] for r in outbound_rows(md)] == ["k1:link"]


def test_replays_never_send_twice(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)
    send(md, photo_id, conversation_id, "k1")
    assert send(md, photo_id, conversation_id, "k1").status == "sent"
    media.force_invalid_cached_id(md.conn, md.agency, photo_id)
    md.channel.refuse_ids = {media.INVALID_MEDIA_ID}
    send(md, photo_id, conversation_id, "k2")
    sent = len(md.channel.sent)
    assert send(md, photo_id, conversation_id, "k2").status == "sent"  # replays the link row
    assert len(md.channel.sent) == sent and len(md.channel.uploads) == 1


def test_outside_window_sends_nothing_and_no_link(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)
    with tenant_transaction(md.conn, md.agency):
        md.conn.execute(
            "UPDATE conversation SET last_customer_message_at = now() - interval '2 days'"
        )
    sent = len(md.channel.sent)
    result = send(md, photo_id, conversation_id, "k1")
    assert (result.status, result.via) == ("failed", None)
    assert send(md, photo_id, conversation_id, "k1").status == "failed"
    assert [r[:3] for r in outbound_rows(md)] == [("k1", "failed", "outside_window")]
    assert len(md.channel.sent) == sent


def test_control_gate_cancels_without_fallback(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)
    with tenant_transaction(md.conn, md.agency):
        md.conn.execute("UPDATE conversation SET mode = 'human'")
    result = send(md, photo_id, conversation_id, "k1")
    assert result.status == "cancelled"
    assert [r[:2] for r in outbound_rows(md)] == [("k1", "cancelled")]


def failed_status(wamid, code):
    return {"id": wamid, "status": "failed", "timestamp": str(int(time.time())),
            "recipient_id": STAFF, "errors": [{"code": code, "title": "fictional"}]}


def deliver_status(md, status):  # noqa: F811
    from app import webhook
    from app.ingest import store_event

    envelope = {"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": PNID}, "statuses": [status],
    }}]}]}
    events, _ = webhook.extract_events(envelope)
    with tenant_transaction(md.conn, md.agency):
        for event in events:
            store_event(md.conn, md.agency, event)


def test_failed_delivery_of_an_id_resends_once_by_link(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)
    first = send(md, photo_id, conversation_id, "k1")
    assert first.via == "id"

    deliver_status(md, failed_status(first.provider_message_id, 131053))
    md.drain()

    assert cache(md, photo_id) == (None, None)
    assert [r[:2] for r in outbound_rows(md)] == [("k1", "sent"), ("k1:link", "sent")]
    assert "link" in md.channel.sent[-1]["image"]
    assert md.query(
        "SELECT status FROM job WHERE job_type = %s", (media.FALLBACK_JOB_TYPE,)
    ) == [("succeeded",)]

    # A failed link send is not retried again.
    link_wamid = md.query(
        "SELECT provider_message_id FROM outbound_message WHERE dedupe_key = 'k1:link'"
    )[0][0]
    deliver_status(md, failed_status(link_wamid, 131053))
    md.drain()
    assert len(md.query(
        "SELECT 1 FROM job WHERE job_type = %s", (media.FALLBACK_JOB_TYPE,)
    )) == 1


def test_duplicate_photo_shares_the_stored_copy_cache(md):  # noqa: F811
    photo_id, conversation_id = stored_photo(md)
    md.channel.files["222"] = md.channel.files["111"]
    duplicate_id, _ = stored_photo(md, "222", "wamid.P2")
    md.channel.files["222"] = md.channel.files["111"]
    send(md, photo_id, conversation_id, "k1")
    result = send(md, duplicate_id, conversation_id, "k2")
    assert result.via == "id" and len(md.channel.uploads) == 1
    assert cache(md, duplicate_id) == (None, None)  # the stored copy holds the cache


def text(wamid, body, sender=STAFF):
    return {"from": sender, "id": wamid, "timestamp": str(int(time.time())),
            "type": "text", "text": {"body": body}}


def test_demo_photo_command(md):  # noqa: F811
    stored_photo(md)
    md.channel.sent.clear()

    md.deliver(text("wamid.T1", "#demo photo"))
    md.drain()
    md.deliver(text("wamid.T2", "#demo photo"))
    md.drain()
    assert [p["image"].get("id") for p in md.channel.sent] == ["9000000000000001"] * 2
    assert len(md.channel.uploads) == 1

    md.channel.refuse_ids = {media.INVALID_MEDIA_ID}
    md.deliver(text("wamid.T3", "#demo photo badid"))
    md.drain()
    assert "link" in md.channel.sent[-1]["image"]
    assert md.channel.sent[-1]["image"]["caption"] == "Your latest photo (DEMO) badid"

    md.deliver(text("wamid.T4", "#demo photo", sender=CUSTOMER))
    md.drain()
    assert md.channel.sent[-1] == whatsapp.text("Photo demo is for staff numbers only.")


def test_demo_photo_without_a_photo(md):  # noqa: F811
    md.deliver(text("wamid.T1", "#demo photo"))
    md.drain()
    assert md.channel.sent == [whatsapp.text("No stored photo yet: send a photo first.")]


@pytest.mark.parametrize(("body", "expected"), [
    ("#demo photo", ""), ("#DEMO Photo BadID", "badid"), ("#demo photo x", None),
    ("#demo image", None), ("demo photo", None),
])
def test_photo_command_parsing(body, expected):
    from app import echo

    assert echo.photo_command({"type": "text", "text": {"body": body}}) == expected


def test_property_media_is_a_cache_source(app_role_db):
    admin_url, app_url, _ = app_role_db
    with psycopg.connect(admin_url, autocommit=True) as admin:
        karachi = insert(admin, "location", level="city", name="Karachi")
    conn = psycopg.connect(app_url, autocommit=True)
    try:
        agency = create_agency(conn, "Demo Realty (DEMO)", "demo", PNID)
        with tenant_transaction(conn, agency):
            pid = insert(conn, "property", **active_property(agency, karachi))
            item = insert(conn, "property_media", agency_id=agency, property_id=pid,
                          original_key="demo/o.jpg", wa_image_key="demo/wa.jpg", sort_order=0)
            conversation = insert(conn, "conversation", agency_id=agency,
                                  customer_wa_id=CUSTOMER)
            conn.execute("UPDATE conversation SET last_customer_message_at = now()")
        channel, storage = FakeChannel(), FakeStorage()
        storage.put("demo/wa.jpg", photo(), "image/jpeg")
        for key in ("k1", "k2"):
            result = media.send_image(conn, channel, storage, agency, conversation, CUSTOMER,
                                      ("property_media", item), key)
            assert (result.status, result.via) == ("sent", "id")
        assert len(channel.uploads) == 1
        with tenant_transaction(conn, agency):
            assert conn.execute(
                "SELECT wa_media_id IS NOT NULL, exif_stripped FROM property_media"
            ).fetchone() == (True, False)
    finally:
        conn.close()


def test_unknown_or_unusable_sources_are_refused(md):  # noqa: F811
    with pytest.raises(ValueError):
        media.send_image(md.conn, md.channel, md.storage, md.agency, uuid.uuid4(), STAFF,
                         ("staff", uuid.uuid4()), "k1")
    md.worker.handlers.pop(media.JOB_TYPE)
    md.deliver(image_message("wamid.P9", "999"))
    md.drain()
    [(pending_id, conversation_id)] = md.query("SELECT id, conversation_id FROM inbound_media")
    with pytest.raises(LookupError):
        send(md, pending_id, conversation_id, "k1")


# --- Graph upload -----------------------------------------------------------------


def upload_client(handler):
    return whatsapp.WhatsAppClient(
        PNID, TOKEN, "v25.0", http=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_upload_media_posts_multipart():
    seen = {}

    def handler(request):
        seen["path"], seen["auth"] = request.url.path, request.headers["authorization"]
        seen["body"] = request.read()
        return httpx.Response(200, json={"id": "1234567890"})

    assert upload_client(handler).upload_media(b"\xff\xd8\xfffictional", "image/jpeg") == \
        "1234567890"
    assert seen["path"] == f"/v25.0/{PNID}/media" and seen["auth"] == f"Bearer {TOKEN}"
    body = seen["body"]
    assert b'name="messaging_product"' in body and b"whatsapp" in body
    assert b'name="type"' in body and b"image/jpeg" in body
    assert b'name="file"; filename="image.jpg"' in body and b"\xff\xd8\xfffictional" in body


@pytest.mark.parametrize(("response", "error"), [
    (httpx.Response(400, json={"error": {"code": 100}}), whatsapp.PermanentMediaError),
    (httpx.Response(500), whatsapp.RetryableMediaError),
    (httpx.Response(200, json={}), whatsapp.RetryableMediaError),
])
def test_upload_media_errors(response, error):
    with pytest.raises(error):
        upload_client(lambda request: response).upload_media(b"x", "image/jpeg")


def test_migration_009_columns(app_role_db):
    admin_url, _, schema = app_role_db
    with psycopg.connect(admin_url) as admin:
        constraints = {r[0] for r in admin.execute(
            "SELECT conname FROM pg_constraint WHERE connamespace = %s::regnamespace",
            (schema,),
        )}
    assert {"inbound_media_wa_upload_pair_check", "outbound_message_media_source_pair_check",
            "outbound_message_media_source_check"} <= constraints
