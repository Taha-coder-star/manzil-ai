import logging
import signal
import threading

logger = logging.getLogger(__name__)


def run():
    stop = threading.Event()

    def request_shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    logger.info("Worker started; queue processing is not configured yet.")
    stop.wait()
    logger.info("Worker stopped cleanly.")