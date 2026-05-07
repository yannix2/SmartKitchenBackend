from urllib import response

import requests
from app.core.config import settings


def _base_url() -> str:
    return "https://test-api.uber.com" if settings.UBER_ENV == "sandbox" else "https://api.uber.com"
def get_uber_token() -> dict:
    url = (
        "https://sandbox-login.uber.com/oauth/v2/token"
        if settings.UBER_ENV == "sandbox"
        else "https://auth.uber.com/oauth/v2/token"
    )
    response = requests.post(
        url,
        data={
            "client_id": settings.UBER_CLIENT_ID,
            "client_secret": settings.UBER_CLIENT_SECRET,
            "grant_type": "client_credentials",
            "scope": "eats.store eats.store.orders.read eats.order eats.report",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        return response.json()
    except ValueError:
        return {"error": "invalid_response", "text": response.text, "status": response.status_code}


def get_stores(token: str) -> dict:
    response = requests.get(
        f"{_base_url()}/v1/eats/stores",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        return response.json()
    except ValueError:
        return {"error": "invalid_response", "text": response.text, "status": response.status_code}


def get_store_status(token: str, store_id: str) -> dict:
    response = requests.get(
        f"{_base_url()}/v1/eats/store/{store_id}/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        return response.json()
    except ValueError:
        return {"error": "invalid_response", "text": response.text, "status": response.status_code}


def get_store_by_id(token: str, store_id: str) -> dict:
    response = requests.get(
        f"{_base_url()}/v1/eats/stores/{store_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        return response.json()
    except ValueError:
        return {"error": "invalid_response", "text": response.text, "status": response.status_code}


def get_order(token: str, order_id: str) -> dict:
    response = requests.get(
        f"{_base_url()}/v2/eats/order/{order_id}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept-Encoding": "gzip"
                 },
    )
    try:
        print(response.status_code)
        print(response.text)
        return response.json()
    except ValueError:
        return {"error": "invalid_response", "text": response.text, "status": response.status_code}

def get_order_details(token: str, order_id: str, expand: str | None = "carts,deliveries,payment") -> dict:
    """
    GET /v1/delivery/order/{order_id} — full MerchantOrder.
    `expand` is a comma-separated list of: carts, deliveries, payment.
    Includes carts (line items + totals) which is what we need to compute order amounts.
    """
    params = {"expand": expand} if expand else None
    response = requests.get(
        f"{_base_url()}/v1/delivery/order/{order_id}",
        headers={"Authorization": f"Bearer {token}", "Accept-Encoding": "gzip"},
        params=params,
    )
    try:
        return response.json()
    except ValueError:
        return {"error": "invalid_response", "text": response.text, "status": response.status_code}


def get_report_status(token: str, workflow_id: str) -> dict:
    response = requests.get(
        f"{_base_url()}/v1/eats/report/{workflow_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        return response.json()
    except ValueError:
        return {"error": "invalid_response", "text": response.text, "status": response.status_code}


def create_report(token: str, store_uuids: list, start_date: str, end_date: str, report_type: str) -> dict:
    response = requests.post(
        f"{_base_url()}/v1/eats/report",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "report_type": report_type,
            "store_uuids": store_uuids,
            "start_date": start_date,
            "end_date": end_date,
        },
    )
    try:
        return response.json()
    except ValueError:
        return {"error": "invalid_response", "text": response.text, "status": response.status_code}
