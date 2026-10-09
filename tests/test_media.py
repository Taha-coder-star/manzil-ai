"""Task 4.1 acceptance tests: inbound media (C5, D14).

Image processing runs on generated fictional images; the WhatsApp Graph API is
an httpx.MockTransport and the bucket an in-memory fake. Database tests use the
restricted manzil_app login (RLS on).
"""

import hashlib
import io
import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import psycopg
import pytest
from PIL import Image
from psycopg import sql

from app import media, migrate, webhook, whatsapp
from app.db import tenant_transaction
from app.handlers import make_handlers
from app.ingest import store_event
from app.provision_agency import create_agency
from app.worker import Worker
from tests.test_inventory import insert
from tests.test_inventory_d11 import schema_at_006_with_rows

PNID = "100000000000001"
STAFF = "923000000009"
CUSTOMER = "923000000001"
TOKEN = "test-token-not-real"
MEDIA_HOST = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1"
PUBLIC_BASE = "https://demo-bucket.s3.us-east-1.amazonaws.com"

GPS_IFD = 0x8825
ORIENTATION = 0x0112


def photo(size=(400, 300), *, fmt="JPEG", mode="RGB", exif=True, orientation=6):
    """A fictional photo carrying camera EXIF, GPS and a rotation tag."""
    image = Image.new(mode, size, (200, 120, 40, 128)[: len(mode)] if mode != "L" else 128)
    out = io.BytesIO()
    options = {}
    if exif:
        tags = Image.Exif()
        tags[0x010F] = "Fictional Camera"  # Make
        tags[ORIENTATION] = orientation
        tags.get_ifd(GPS_IFD).update({1: "N", 2: (24.0, 48.0, 0.0), 3: "E", 4: (67.0, 2.0, 0.0)})
        options["exif"] = tags.tobytes()
    image.save(out, fmt, **options)
    return out.getvalue()


def opened(data):
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


# --- image processing -------------------------------------------------------------


def test_jpeg_gives_three_clean_images():
    source = photo((400, 300))
    assert media.has_metadata(source)

    result = media.process_image(source, "image/jpeg")

    assert result.mime_type == "image/jpeg"
    # Orientation 6 rotates: the stored images are upright.
    assert (result.width, result.height) == (300, 400)
    assert opened(result.original).size == (300, 400)
    assert result.original_ext == "jpg" and result.original_type == "image/jpeg"
    for encoded in (result.original, result.wa, result.card):
        assert not media.has_metadata(encoded)
        assert b"Fictional Camera" not in encoded
    wa = opened(result.wa)
    assert wa.format == "JPEG" and wa.mode == "RGB" and wa.size == (300, 400)
    card = opened(result.card)
    assert card.format == "JPEG" and card.size == media.CARD_SIZE


def test_large_photo_is_downscaled_per_derivative():
    result = media.process_image(photo((4000, 3000), exif=False))
    assert max(opened(result.original).size) == media.ORIGINAL_MAX_EDGE
    assert max(opened(result.wa).size) == media.WA_MAX_EDGE
    assert len(result.wa) <= media.WA_MAX_BYTES
    assert opened(result.card).size == media.CARD_SIZE


def test_wa_derivative_is_forced_under_the_cap(monkeypatch):
    monkeypatch.setattr(media, "WA_MAX_BYTES", 3_000)
    noise = Image.effect_noise((1600, 1200), 100).convert("RGB")
    out = io.BytesIO()
    noise.save(out, "JPEG", quality=95)
    result = media.process_image(out.getvalue())
    assert len(result.wa) <= 3_000


def test_png_with_alpha_keeps_png_original_and_flattens_derivatives():
    result = media.process_image(photo(fmt="PNG", mode="RGBA", exif=False), "image/png")
    assert result.mime_type == "image/png"
    assert result.original_ext == "png" and opened(result.original).format == "PNG"
    assert opened(result.wa).mode == "RGB"
    assert not any(media.has_metadata(b) for b in (result.original, result.wa, result.card))


def test_webp_is_accepted():
    result = media.process_image(photo(fmt="WEBP", exif=False), "image/webp")
    assert result.mime_type == "image/webp" and result.original_ext == "jpg"


@pytest.mark.parametrize(("data", "declared", "code"), [
    (b"%PDF-1.7 fictional", None, "unsupported_type"),
    (b"GIF89a" + b"\0" * 32, None, "unsupported_type"),
    (photo(exif=False), "image/png", "mime_mismatch"),
    (photo(exif=False)[:200], "image/jpeg", "corrupt_image"),
    (b"\xff\xd8\xff" + b"\0" * 64, None, "corrupt_image"),
])
def test_bad_files_are_rejected(data, declared, code):
    with pytest.raises(media.MediaRejected) as caught:
        media.process_image(data, declared)
    assert caught.value.code == code


def test_pixel_bombs_are_rejected_before_decoding(monkeypatch):
    monkeypatch.setattr(media, "MAX_PIXELS", 100 * 100)
    with pytest.raises(media.MediaRejected) as caught:
        media.process_image(photo((200, 200), exif=False))
    assert caught.value.code == "too_many_pixels"


def test_object_keys_and_public_base():
    agency, item, token = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    keys = media.object_keys(agency, item, token, "jpg")
    assert keys == {
        "original": f"tenant/{agency}/inbound/{item}/original.jpg",
        "wa": f"tenant/{agency}/inbound/{item}/wa.jpg",
        "card": f"card/{agency}/{token}.jpg",
    }
    aws = SimpleNamespace(s3_bucket="b", s3_region="us-east-1", s3_endpoint_url=None,
                          media_public_base_url=None)
    assert media.public_base_url(aws) == "https://b.s3.us-east-1.amazonaws.com"
    other = SimpleNamespace(s3_bucket="b", s3_region="x", s3_endpoint_url="https://minio.local",
                            media_public_base_url=None)
    assert media.public_base_url(other) is None
    cdn = SimpleNamespace(s3_bucket="b", s3_region="x", s3_endpoint_url=None,
                          media_public_base_url="https://cdn.example/")
    assert media.public_base_url(cdn) == "https://cdn.example"


def test_stageable_messages():
    assert media.is_stageable("image", None)
    assert media.is_stageable("document", "image/jpeg")
    assert not media.is_stageable("document", "application/pdf")
    assert not media.is_stageable("audio", "audio/ogg")
    assert not media.is_stageable("sticker", "image/webp")


# --- Graph API media download -----------------------------------------------------


class Graph:
    """A scripted Graph API: media lookups hand out download URLs."""

    def __init__(self, data, *, mime="image/jpeg", expire=0, info_status=200,
                 download_status=200, sha=None, host=MEDIA_HOST, file_size=None):
        self.data, self.mime, self.expire = data, mime, expire
        self.info_status, self.download_status = info_status, download_status
        self.sha = sha if sha is not None else hashlib.sha256(data).hexdigest()
        self.host, self.file_size = host, file_size
        self.info_calls = self.downloads = 0

    def __call__(self, request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if request.url.host == "graph.facebook.com":
            self.info_calls += 1
            assert request.url.path == "/v25.0/987654321"
            assert request.url.params["phone_number_id"] == PNID
            if self.info_status != 200:
                return httpx.Response(self.info_status, json={"error": {"code": 100}})
            return httpx.Response(200, json={
                "url": self.host, "mime_type": self.mime, "sha256": self.sha,
                "file_size": self.file_size or len(self.data), "id": "987654321",
            })
        self.downloads += 1
        if self.expire:
            self.expire -= 1
            return httpx.Response(404)
        if self.download_status != 200:
            return httpx.Response(self.download_status)
        return httpx.Response(200, content=self.data)


def client_for(graph):
    return whatsapp.WhatsAppClient(
        PNID, TOKEN, "v25.0", http=httpx.Client(transport=httpx.MockTransport(graph))
    )


def test_fetch_media_downloads_and_verifies():
    data = photo()
    graph = Graph(data)
    assert client_for(graph).fetch_media("987654321", 1_000_000) == (data, "image/jpeg")
    assert (graph.info_calls, graph.downloads) == (1, 1)


def test_expired_url_is_requeried_once():
    graph = Graph(photo(), expire=1)
    data, _ = client_for(graph).fetch_media("987654321", 1_000_000)
    assert data and (graph.info_calls, graph.downloads) == (2, 2)

    graph = Graph(photo(), expire=2)
    with pytest.raises(whatsapp.RetryableMediaError) as caught:
        client_for(graph).fetch_media("987654321", 1_000_000)
    assert caught.value.code == "url_expired"


@pytest.mark.parametrize(("graph", "error", "code"), [
    (Graph(b"x" * 100, file_size=10_000), whatsapp.PermanentMediaError, "too_large"),
    (Graph(b"x" * 2_000), whatsapp.PermanentMediaError, "too_large"),
    (Graph(b"x", info_status=404), whatsapp.PermanentMediaError, "info_http_404_meta_100"),
    (Graph(b"x", info_status=500), whatsapp.RetryableMediaError, "info_http_500_meta_100"),
    (Graph(b"x", download_status=503), whatsapp.RetryableMediaError, "download_http_503"),
    (Graph(b"x", sha="0" * 64), whatsapp.RetryableMediaError, "sha256_mismatch"),
    (Graph(b"x", host="https://evil.example/x"), whatsapp.PermanentMediaError,
     "untrusted_media_host"),
    (Graph(b"x", host="http://lookaside.fbsbx.com/x"), whatsapp.PermanentMediaError,
     "untrusted_media_host"),
])
def test_fetch_media_failures(graph, error, code):
    with pytest.raises(error) as caught:
        client_for(graph).fetch_media("987654321", 1_000)
    assert caught.value.code == code


def test_invalid_media_id_is_never_requested():
    graph = Graph(b"x")
    with pytest.raises(whatsapp.PermanentMediaError):
        client_for(graph).fetch_media("../me", 1_000)
    assert graph.info_calls == 0


# --- end to end through the worker -----------------------------------------------


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.fail = 0

    def put(self, key, data, content_type, cache_control=None):
        if self.fail:
            self.fail -= 1
            raise ConnectionError("fictional outage")
        self.objects[key] = (data, content_type, cache_control)
        self.puts.append(key)

    def delete(self, key):
        self.objects.pop(key, None)

    def get(self, key):
        return self.objects[key][0]

    def presigned_url(self, key, seconds):
        return f"https://demo-bucket.s3.amazonaws.com/{key}?X-Amz-Expires={seconds}"


class FakeChannel:
    """Records sends and uploads; inbound media come from a dict of media ID -> bytes.
    Image IDs in refuse_ids are rejected at send time, like an invalid ID."""

    def __init__(self):
        self.files = {}
        self.media_errors = []
        self.sent = []
        self.uploads = []
        self.upload_errors = []
        self.refuse_ids = set()

    def send(self, to, payload, *, reply_to=None):
        image = payload.get("image") or {}
        if image.get("id") in self.refuse_ids:
            raise whatsapp.PermanentSendError("http_400_meta_131009")
        self.sent.append(payload)
        return f"wamid.OUT{len(self.sent)}"

    def upload_media(self, data, mime_type, filename="image.jpg"):
        if self.upload_errors:
            raise self.upload_errors.pop(0)
        self.uploads.append(data)
        return f"9{len(self.uploads):015d}"

    def mark_read(self, wamid, *, typing=False):
        pass

    def fetch_media(self, media_id, max_bytes):
        if self.media_errors:
            raise self.media_errors.pop(0)
        return self.files[media_id], "image/jpeg"

    def close(self):
        pass


@pytest.fixture
def md(app_role_db):
    _, app_url, _ = app_role_db
    conn = psycopg.connect(app_url, autocommit=True)
    agency = create_agency(conn, "Demo Realty (DEMO)", "demo", PNID)
    with tenant_transaction(conn, agency):
        conn.execute(
            "INSERT INTO staff (agency_id, name, whatsapp_number, role) VALUES "
            "(current_agency_id(), 'Demo agent', %s, 'agent'), "
            "(current_agency_id(), 'Demo broker', '923000000008', 'broker')",
            ("+" + STAFF,),
        )
    channel, storage = FakeChannel(), FakeStorage()
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket="demo-bucket",
        s3_region="us-east-1", s3_endpoint_url=None, media_public_base_url=None,
        site_address="demo.example",
    )
    handlers = make_handlers(
        settings, client_factory=lambda pnid: channel, storage=storage,
        image_link=lambda: "https://demo.example/x.jpg",
    )
    assert media.JOB_TYPE in handlers
    worker = Worker(app_url, [agency], handlers, threading.Event(), backoff=lambda n: 0)

    def deliver(*messages):
        envelope = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": PNID}, "messages": list(messages),
        }}]}]}
        events, _ = webhook.extract_events(envelope)
        with tenant_transaction(conn, agency):
            for event in events:
                store_event(conn, agency, event)

    def drain():
        while worker.run_once():
            pass

    def query(sql, params=()):
        with tenant_transaction(conn, agency):
            return conn.execute(sql, params).fetchall()

    yield SimpleNamespace(conn=conn, agency=agency, channel=channel, storage=storage,
                          deliver=deliver, drain=drain, query=query, worker=worker)
    worker.close()
    conn.close()


def image_message(wamid, media_id, sender=STAFF, kind="image", mime="image/jpeg"):
    return {
        "from": sender, "id": wamid, "timestamp": str(int(time.time())), "type": kind,
        kind: {"id": media_id, "mime_type": mime, "sha256": "ignored"},
    }


def media_rows(md):
    return md.query(
        "SELECT status, error_code, mime_type, original_key, wa_image_key, card_image_key, "
        "public_url_card, exif_stripped, duplicate_of_id, card_token::text, id::text "
        "FROM inbound_media ORDER BY created_at"
    )


def fetch_jobs(md):
    return md.query(
        "SELECT status, last_error_code FROM job WHERE job_type = %s ORDER BY created_at",
        (media.JOB_TYPE,),
    )


def test_staff_photo_becomes_three_clean_objects(md):
    md.channel.files["111"] = photo()
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()

    [row] = media_rows(md)
    status, error, mime, original, wa, card, url, stripped, dup, token, item = row
    assert (status, error, mime, stripped, dup) == ("stored", None, "image/jpeg", True, None)
    assert original == f"tenant/{md.agency}/inbound/{item}/original.jpg"
    assert wa == f"tenant/{md.agency}/inbound/{item}/wa.jpg"
    assert card == f"card/{md.agency}/{token}.jpg"
    assert url == f"{PUBLIC_BASE}/{card}"
    assert sorted(md.storage.puts) == sorted([original, wa, card])
    assert md.storage.objects[card][1:] == ("image/jpeg", media.CARD_CACHE_CONTROL)
    assert md.storage.objects[original][2] is None  # private objects are not cacheable
    for data, _, _ in md.storage.objects.values():
        assert not media.has_metadata(data)
    assert fetch_jobs(md) == [("succeeded", None)]
    assert md.query("SELECT content_sha256 FROM inbound_media")[0][0] == hashlib.sha256(
        md.channel.files["111"]).hexdigest()


def test_photo_sent_as_document_is_staged(md):
    md.channel.files["112"] = photo()
    md.deliver(image_message("wamid.D1", "112", kind="document"),
               image_message("wamid.D2", "113", kind="document", mime="application/pdf"))
    md.drain()
    assert [r[0] for r in media_rows(md)] == ["stored"]


@pytest.mark.parametrize("sender", [CUSTOMER, "923000000008"])
def test_customer_and_broker_photos_are_not_staged(md, sender):
    md.deliver(image_message("wamid.C1", "114", sender=sender))
    md.drain()
    assert media_rows(md) == [] and fetch_jobs(md) == [] and md.storage.puts == []
    # The message itself is still logged.
    assert md.query("SELECT media_id FROM message WHERE direction = 'inbound'") == [("114",)]


def test_replayed_jobs_stage_and_upload_once(md):
    md.channel.files["111"] = photo()
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()
    md.deliver(image_message("wamid.P1", "111"))  # duplicate webhook
    with tenant_transaction(md.conn, md.agency):  # and a forced replay of both jobs
        md.conn.execute("UPDATE job SET status = 'pending', attempts = 0")
    md.drain()
    assert len(media_rows(md)) == 1 and len(fetch_jobs(md)) == 1
    assert len(md.storage.puts) == 3


def test_same_photo_twice_reuses_the_objects(md):
    md.channel.files["111"] = md.channel.files["222"] = photo()
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()
    md.deliver(image_message("wamid.P2", "222"))
    md.drain()

    first, second = media_rows(md)
    assert first[0] == "stored" and second[0] == "duplicate"
    assert second[8] is not None and str(second[8]) == first[10]
    assert second[3:7] == first[3:7]
    assert len(md.storage.puts) == 3


def test_non_image_is_rejected_without_retry(md):
    md.channel.files["111"] = b"%PDF-1.7 fictional"
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()
    assert [r[:2] for r in media_rows(md)] == [("rejected", "unsupported_type")]
    assert fetch_jobs(md) == [("succeeded", None)] and md.storage.puts == []


def test_gone_media_is_rejected(md):
    md.channel.media_errors = [whatsapp.PermanentMediaError("info_http_404")]
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()
    assert [r[:2] for r in media_rows(md)] == [("rejected", "info_http_404")]


def test_transient_errors_retry_then_store(md):
    md.channel.files["111"] = photo()
    md.channel.media_errors = [whatsapp.RetryableMediaError("url_expired")]
    md.storage.fail = 1
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()
    assert [r[0] for r in media_rows(md)] == ["stored"]
    assert fetch_jobs(md) == [("succeeded", None)]


def test_exhausted_retries_mark_the_photo_failed(md):
    md.channel.media_errors = [whatsapp.RetryableMediaError("download_http_503")] * 3
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()
    assert [r[:2] for r in media_rows(md)] == [("failed", "download_http_503")]
    assert fetch_jobs(md) == [("dead", "RetryableMediaError")]


def test_without_a_bucket_fetch_media_is_not_registered():
    settings = SimpleNamespace(
        meta_access_token="unused", meta_api_version="v25.0", s3_bucket=None,
        s3_region=None, s3_endpoint_url=None, media_public_base_url=None, site_address=None,
    )
    assert media.JOB_TYPE not in make_handlers(settings, client_factory=lambda p: None)


# --- schema: tenancy, privileges, constraints -------------------------------------


def test_inbound_media_is_tenant_isolated_and_append_by_status(md, app_role_db):
    admin_url, _, schema = app_role_db
    md.channel.files["111"] = photo()
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()

    other = create_agency(md.conn, "Other Realty (DEMO)", "other", "100000000000002")
    with tenant_transaction(md.conn, other):
        assert md.conn.execute("SELECT count(*) FROM inbound_media").fetchone()[0] == 0
    assert md.conn.execute("SELECT count(*) FROM inbound_media").fetchone()[0] == 0
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        tenant_transaction(md.conn, md.agency),
    ):
        md.conn.execute("DELETE FROM inbound_media")

    with psycopg.connect(admin_url) as admin:
        assert admin.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = 'inbound_media'", (schema,)
        ).fetchone() == (True, True)
        privileges = {r[0] for r in admin.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'manzil_app' AND table_schema = %s "
            "AND table_name = 'inbound_media'", (schema,)
        )}
        assert privileges == {"SELECT", "INSERT", "UPDATE"}


def test_cross_tenant_references_are_rejected(md):
    md.channel.files["111"] = photo()
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()
    [(message_id, conversation_id, staff_id)] = md.query(
        "SELECT message_id, conversation_id, staff_id FROM inbound_media"
    )
    other = create_agency(md.conn, "Other Realty (DEMO)", "other", "100000000000002")
    with (
        pytest.raises(psycopg.errors.ForeignKeyViolation),
        tenant_transaction(md.conn, other),
    ):
        md.conn.execute(
            "INSERT INTO inbound_media (agency_id, conversation_id, message_id, staff_id, "
            "wa_media_id) VALUES (current_agency_id(), %s, %s, %s, '1')",
            (conversation_id, message_id, staff_id),
        )


@pytest.mark.parametrize(("update", "constraint"), [
    ("status = 'stored'", "inbound_media_usable_check"),
    ("status = 'rejected'", "inbound_media_error_check"),
    ("error_code = 'x'", "inbound_media_error_check"),
    ("status = 'duplicate'", "inbound_media_duplicate_check"),
    ("mime_type = 'image/gif'", "inbound_media_mime_type_check"),
    ("public_url_card = 'http://x'", "inbound_media_public_url_card_check"),
    ("duplicate_of_id = id", "inbound_media_check"),
])
def test_inbound_media_constraints(md, update, constraint):
    md.worker.handlers.pop(media.JOB_TYPE)  # stage only; never fetch
    md.deliver(image_message("wamid.P1", "111"))
    md.drain()
    assert [r[0] for r in media_rows(md)] == ["pending"]
    with (
        pytest.raises(psycopg.errors.CheckViolation) as caught,
        tenant_transaction(md.conn, md.agency),
    ):
        md.conn.execute(f"UPDATE inbound_media SET {update}")
    assert caught.value.diag.constraint_name == constraint


def test_property_media_g16_columns(app_role_db):
    admin_url, _, schema = app_role_db
    with psycopg.connect(admin_url) as admin:
        columns = dict(admin.execute(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'property_media'", (schema,)
        ).fetchall())
        constraints = {r[0] for r in admin.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = %s::regclass",
            (f"{schema}.property_media",),
        )}
    assert {"kind", "label", "public_url_card", "wa_media_id", "wa_media_expires_at",
            "exif_stripped", "source_inbound_media_id"} <= set(columns)
    assert columns["kind"] == "'image'::text" and columns["exif_stripped"] == "false"
    assert {"property_media_kind_check", "property_media_label_check",
            "property_media_public_url_card_check", "property_media_wa_media_pair_check",
            "property_media_source_inbound_media_fkey"} <= constraints


def test_upgrade_from_007_keeps_property_media_and_fills_defaults(migration_database):
    """property_media rows written under FORCE RLS survive 008 with its defaults."""
    database_url, schema = migration_database
    with psycopg.connect(database_url) as conn:
        try:
            statement_007, owner, agency, _, pid = schema_at_006_with_rows(conn, schema)
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner)))
            conn.execute(statement_007, prepare=False)
            conn.execute("SET LOCAL ROLE manzil_app")
            item = insert(conn, "property_media", agency_id=agency, property_id=pid,
                          original_key="demo/a.jpg", sort_order=0)

            [(_, _, statement_008)] = [
                m for m in migrate.load_migrations() if m[0] == "008_inbound_media.sql"
            ]
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner)))
            conn.execute(statement_008, prepare=False)

            conn.execute("SET LOCAL ROLE manzil_app")
            assert conn.execute(
                "SELECT id, kind, label, exif_stripped, wa_media_id, public_url_card "
                "FROM property_media"
            ).fetchall() == [(item, "image", None, False, None, None)]
            assert conn.execute("SELECT count(*) FROM inbound_media").fetchone() == (0,)
            with pytest.raises(psycopg.errors.CheckViolation,
                               match="property_media_wa_media_pair_check"):
                conn.execute("UPDATE property_media SET wa_media_id = '1'")
        finally:
            conn.rollback()
