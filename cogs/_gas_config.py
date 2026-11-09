import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import aiohttp


logger = logging.getLogger(__name__)
ENVIRONMENT_KEY_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CONFIG_VALUES: dict[str, str | list[str]] = {}


@dataclass(frozen=True)
class GasConfigResult:
    source: Literal["remote", "cache", "unavailable"]
    notification: str | None = None


def _normalize_config(payload: object) -> dict[str, str | list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("GAS response must be a JSON object")
    if not payload:
        raise ValueError("GAS configuration must not be empty")

    normalized: dict[str, str | list[str]] = {}
    for name, value in payload.items():
        if not isinstance(name, str) or not ENVIRONMENT_KEY_PATTERN.fullmatch(name):
            raise ValueError("GAS configuration keys must be valid environment names")
        if isinstance(value, str):
            values = [item.strip() for item in value.split(",")]
            if not all(values):
                raise ValueError(f"{name} contains an empty value")
            normalized[name] = values if len(values) > 1 else values[0]
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(item, str) and item.strip() for item in value)
        ):
            normalized[name] = [item.strip() for item in value]
        else:
            raise ValueError(f"{name} must be a string or a non-empty string array")
    return normalized


def get_gas_config(name: str) -> str | tuple[str, ...]:
    try:
        value = CONFIG_VALUES[name]
    except KeyError as error:
        raise RuntimeError(f"Missing required {name} in GAS configuration") from error
    return tuple(value) if isinstance(value, list) else value


def _apply_config(config: dict[str, str | list[str]]) -> None:
    CONFIG_VALUES.clear()
    CONFIG_VALUES.update(config)


async def load_gas_config(base_dir: Path) -> GasConfigResult:
    cache_path = base_dir / "data" / "gas_config.json"
    gas_url = os.getenv("GAS_CONFIG_URL", "").strip()

    if gas_url:
        try:
            parsed_url = urlsplit(gas_url)
            if parsed_url.scheme != "https" or not parsed_url.hostname:
                raise ValueError("GAS_CONFIG_URL 必須是 HTTPS 網址")

            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(gas_url) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
            config = _normalize_config(payload)

            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = cache_path.with_suffix(".tmp")
                temporary_path.write_text(
                    json.dumps(config, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary_path.replace(cache_path)
            except OSError:
                logger.exception("Unable to save the successful GAS config cache")
                _apply_config(config)
                return GasConfigResult(
                    source="remote",
                    notification=(
                        "GAS 設定已成功載入，但無法儲存本機快取；"
                        "請檢查 Bot 的 data 資料夾權限。"
                    ),
                )
            _apply_config(config)
            logger.info("Loaded Discord settings from Google Apps Script")
            return GasConfigResult(source="remote")
        except ValueError as error:
            logger.warning(
                "Invalid GAS configuration (%s); "
                "trying the last-known-good cache",
                error,
            )
        except aiohttp.ClientResponseError as error:
            logger.warning(
                "GAS endpoint returned HTTP %s; trying the last-known-good cache",
                error.status,
            )
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            logger.warning(
                "GAS request failed (%s); trying the last-known-good cache",
                type(error).__name__,
            )
    else:
        logger.warning(
            "GAS_CONFIG_URL is not configured; trying the last-known-good cache"
        )

    try:
        cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
        cached_config = _normalize_config(cached_payload)
    except (OSError, UnicodeError) as error:
        logger.error(
            "No valid GAS config cache is available (%s)",
            type(error).__name__,
        )
        return GasConfigResult(
            source="unavailable",
            notification=(
                "無法取得有效的 GAS 設定，且沒有可用的上次成功快取；"
                "Bot 已停止載入功能。請檢查 GAS_CONFIG_URL、GAS 試算表，"
                "或通知管理員。"
            ),
        )
    except (json.JSONDecodeError, ValueError) as error:
        logger.error("GAS config cache is invalid: %s", error)
        return GasConfigResult(
            source="unavailable",
            notification=(
                "無法取得有效的 GAS 設定，且沒有可用的上次成功快取；"
                "Bot 已停止載入功能。請檢查 GAS_CONFIG_URL、GAS 試算表，"
                "或通知管理員。"
            ),
        )

    _apply_config(cached_config)
    logger.warning("Loaded Discord settings from the last-known-good GAS cache")
    return GasConfigResult(
        source="cache",
        notification=(
            "目前無法取得最新的 GAS 設定，Bot 已改用上次成功的本機快取。"
            "請確認 GAS 與試算表狀態。"
        ),
    )
