import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description="Manzil AI runtime")
    parser.add_argument("role", choices=["web", "worker"])
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.role == "web":
        import uvicorn

        uvicorn.run("app.web:app", host="0.0.0.0", port=8000)
    else:
        from app.worker import run

        run()


if __name__ == "__main__":
    main()