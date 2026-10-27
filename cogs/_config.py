import os


def _parse_snowflake(name: str, value: str) -> int:
    value = value.strip()
    if not value.isascii() or not value.isdecimal():
        raise RuntimeError(f"{name} must be a positive Discord ID in .env")

    try:
        snowflake = int(value)
    except ValueError as error:
        raise RuntimeError(
            f"{name} must be a positive Discord ID in .env"
        ) from error
    if not 0 < snowflake < 2**64:
        raise RuntimeError(f"{name} must be a positive Discord ID in .env")
    return snowflake


def required_snowflake(name: str) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required {name} in the .env file")
    return _parse_snowflake(name, value)


def required_snowflake_list(name: str) -> tuple[int, ...]:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required {name} in the .env file")

    entries = value.split(",")
    if any(not entry.strip() for entry in entries):
        raise RuntimeError(
            f"{name} must be a comma-separated list of Discord IDs in .env"
        )
    return tuple(_parse_snowflake(name, entry) for entry in entries)
