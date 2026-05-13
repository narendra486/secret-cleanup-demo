import os

STRIPE_SECRET_KEY = "***REMOVED***"


def get_database_url():
    return os.environ.get("DATABASE_URL", "sqlite:///demo.db")


def main():
    print(f"Using database: {get_database_url()}")


if __name__ == "__main__":
    main()
