import logging

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 3


def check_database(database_url):
    if not database_url:
        return "not_configured"

    import psycopg

    try:
        with psycopg.connect(database_url, connect_timeout=TIMEOUT_SECONDS) as conn:
            conn.execute("SELECT 1").fetchone()
    except psycopg.Error as exc:
        # Log the type only: the message can contain the connection string.
        logger.warning("Database health check failed: %s", type(exc).__name__)
        return "unreachable"
    return "ok"


def check_bucket(bucket, region, endpoint_url):
    if not bucket:
        return "not_configured"

    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            config=Config(
                connect_timeout=TIMEOUT_SECONDS,
                read_timeout=TIMEOUT_SECONDS,
                retries={"max_attempts": 1},
            ),
        )
        client.head_bucket(Bucket=bucket)
    except (BotoCoreError, ClientError) as exc:
        logger.warning("Bucket health check failed: %s", type(exc).__name__)
        return "unreachable"
    return "ok"
