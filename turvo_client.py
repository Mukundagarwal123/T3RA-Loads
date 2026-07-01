import logging
import time
from typing import Any, Dict

import requests

import config

logger = logging.getLogger(__name__)

_TOKEN_CACHE = {"access_token": None, "expires_at": 0.0}


def get_access_token() -> str:
    now = time.time()
    if _TOKEN_CACHE["access_token"] and now < (_TOKEN_CACHE["expires_at"] - 60):
        return _TOKEN_CACHE["access_token"]

    url = f"{config.TURVO_BASE_URL}/v1/oauth/token"
    headers = {"x-api-key": config.TURVO_API_KEY, "Content-Type": "application/json"}
    payload = {
        "grant_type": "password",
        "client_id": config.TURVO_CLIENT_ID,
        "client_secret": config.TURVO_CLIENT_SECRET,
        "username": config.TURVO_USERNAME,
        "password": config.TURVO_PASSWORD,
        "scope": "read+trust+write",
        "type": "business",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()

    data = resp.json()
    token = data["access_token"]
    expires_in = data.get("expires_in", 3600)

    _TOKEN_CACHE["access_token"] = token
    _TOKEN_CACHE["expires_at"] = now + float(expires_in)
    logger.info("Turvo token refreshed")
    return token


def fetch_shipment_details(shipment_id: int) -> Dict[str, Any]:
    token = get_access_token()
    url = f"{config.TURVO_BASE_URL}/v1/shipments/{shipment_id}"
    headers = {
        "x-api-key": config.TURVO_API_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_carrier_details(carrier_id: int) -> Dict[str, Any]:
    token = get_access_token()
    url = f"{config.TURVO_BASE_URL}/v1/carriers/{carrier_id}"
    headers = {
        "x-api-key": config.TURVO_API_KEY,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()
