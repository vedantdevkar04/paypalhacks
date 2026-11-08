"""One PayPal REST client. Gets the access token, refreshes it before it expires, retries once on 401."""

import os
import time
from typing import Callable

import httpx

BASE_URLS = {"SANDBOX": "https://api-m.sandbox.paypal.com", "PRODUCTION": "https://api-m.paypal.com"}


class PayPalError(Exception):
    def __init__(self, status: int, body: dict | str):
        self.status = status
        self.body = body
        name = (body.get("name") or body.get("error")) if isinstance(body, dict) else ""
        detail = (body.get("message") or body.get("error_description")) if isinstance(body, dict) else str(body)[:200]
        super().__init__(f"PayPal {status} {name}: {detail}")


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        environment: str = "SANDBOX",
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if environment != "SANDBOX":
            raise ValueError("HiClose runs in the PayPal sandbox only (hard rule 10)")
        self._auth = (client_id, client_secret)
        self._http = httpx.Client(base_url=BASE_URLS[environment], timeout=20, transport=transport)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_env(cls) -> "PayPalClient":
        return cls(
            os.environ["PAYPAL_CLIENT_ID"],
            os.environ["PAYPAL_CLIENT_SECRET"],
            os.environ.get("PAYPAL_ENVIRONMENT", "SANDBOX"),
        )

    def _access_token(self, force: bool = False) -> str:
        if force or self._token is None or self._clock() >= self._expires_at:
            response = self._http.post("/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=self._auth)
            if response.status_code != 200:
                raise PayPalError(response.status_code, _body(response))
            data = response.json()
            self._token = data["access_token"]
            self._expires_at = self._clock() + max(60, int(data.get("expires_in", 3600)) - 300)
        return self._token

    def request(self, method: str, path: str, json: dict | None = None, request_id: str | None = None) -> dict:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._access_token(force=attempt == 1)}"}
            if request_id:
                headers["PayPal-Request-Id"] = request_id
            body = json if json is not None or method == "GET" else {}
            response = self._http.request(method, path, json=body, headers=headers)
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code >= 400:
                raise PayPalError(response.status_code, _body(response))
            return response.json() if response.content else {}
        raise PayPalError(401, "unauthorized after refreshing the token")


def _body(response: httpx.Response) -> dict | str:
    try:
        return response.json()
    except ValueError:
        return response.text
