import os

from cogs._gas_config import get_gas_config


def _parse_snowflake(name: str, value: str) -> int:
    value = value.strip()
    if not value.isascii() or not value.isdecimal():
        raise RuntimeError(f"{name} must be a positive Discord ID")

    try:
        snowflake = int(value)
    except ValueError as error:
        raise RuntimeError(
            f"{name} must be a positive Discord ID"
        ) from error
    if not 0 < snowflake < 2**64:
        raise RuntimeError(f"{name} must be a positive Discord ID")
    return snowflake


def required_snowflake(name: str) -> int:
    configured_value = get_gas_config(name)
    if not isinstance(configured_value, str):
        raise RuntimeError(f"{name} must contain exactly one Discord ID")
    value = configured_value
    return _parse_snowflake(name, value)


def required_bootstrap_owner_id() -> int:
    value = os.getenv("GAS_BOOTSTRAP_OWNER_ID")
    if value is None or not value.strip():
        raise RuntimeError(
            "Missing required GAS_BOOTSTRAP_OWNER_ID in the .env file"
        )
    return _parse_snowflake("GAS_BOOTSTRAP_OWNER_ID", value)


def required_snowflake_list(name: str) -> tuple[int, ...]:
    configured_value = get_gas_config(name)
    entries = (
        configured_value
        if isinstance(configured_value, tuple)
        else (configured_value,)
    )
    return tuple(_parse_snowflake(name, entry) for entry in entries)
