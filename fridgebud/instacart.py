"""Turn the shopping list into an Instacart link (Instacart Developer Platform).

One API call creates a shopping-list page on Instacart and returns its link. The person
taps it, picks a store, and checks out on Instacart. FridgeBud never sees their account,
cart or payment. Needs a key from the Instacart Developer Platform dashboard:

    INSTACART_API_KEY   development or production key
    INSTACART_API_URL   default https://connect.instacart.com (use the development host with a development key)

Instacart reviews an integration before issuing a production key, and asks that the link
is labelled "Shop on Instacart" or "Shop ingredients".
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .capture import Transport, _http_post

DEFAULT_URL = "https://connect.instacart.com"
PATH = "/idp/v1/products/products_link"


def _http_get(url: str, headers: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise RuntimeError(f"Instacart error {err.code}: {err.read().decode(errors='replace')[:300]}") from err


class Instacart:
    def __init__(self, api_key: str | None = None, transport: Transport | None = None, base_url: str | None = None,
                 getter=None):
        self.api_key = api_key or os.environ.get("INSTACART_API_KEY", "")
        self.base_url = (base_url or os.environ.get("INSTACART_API_URL", DEFAULT_URL)).rstrip("/")
        self.transport = transport or (lambda url, headers, body: _http_post(url, headers, body, timeout=30))
        self.getter = getter or (lambda url, headers: _http_get(url, headers))
        if not self.api_key and transport is None:
            raise RuntimeError("Set INSTACART_API_KEY to create Instacart links.")

    def shopping_link(self, names: list[str], title: str = "FridgeBud shopping list", partner_url: str = "") -> str | None:
        """A link to an Instacart page with these items, or None if Instacart didn't return one."""
        if not names:
            return None
        body = {"title": title, "link_type": "shopping_list",
                "line_items": [{"name": n, "quantity": 1, "unit": "each"} for n in names[:60]]}
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json", "Content-Type": "application/json"}
        if partner_url:
            body["landing_page_configuration"] = {"partner_linkback_url": partner_url}
        body["expires_in"] = 30
        reply = self.transport(self.base_url + PATH, headers, json.dumps(body).encode())
        url = str(reply.get("products_link_url") or "")
        return url if url.startswith("https://") else None

    def nearby_retailers(self, postal_code: str, country: str) -> list[str]:
        """Names of the stores Instacart delivers from near this postal code (US or CA), in Instacart's order."""
        query = urllib.parse.urlencode({"postal_code": postal_code, "country_code": country})
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
        reply = self.getter(f"{self.base_url}/idp/v1/retailers?{query}", headers)
        return [str(r.get("name")) for r in reply.get("retailers") or [] if r.get("name")][:12]
