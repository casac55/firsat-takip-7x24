from __future__ import annotations

import json
from typing import Any
from bs4 import BeautifulSoup

from src.filter import parse_price_tl


def extract_json_ld_products(html: str) -> list[dict[str, Any]]:
    """Sayfadaki tüm JSON-LD bloklarından ürün/teklif kayıtlarını çıkarır."""
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        _walk_json_ld(data, results)
    return results


def _walk_json_ld(node: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_json_ld(item, out)
        return
    if not isinstance(node, dict):
        return

    node_type = str(node.get("@type", "")).lower()
    if node_type == "itemlist" and isinstance(node.get("itemListElement"), list):
        for el in node["itemListElement"]:
            if isinstance(el, dict):
                _walk_json_ld(el.get("item", el), out)
        return

    if node_type == "product" or ("offers" in node and "name" in node):
        title = str(node.get("name", "")).strip()
        product_url = str(node.get("url", "")).strip()
        image_url = _extract_ld_image(node.get("image"))
        offers = node.get("offers")
        offer_list = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])

        for off in offer_list:
            if not isinstance(off, dict):
                continue
            # AggregateOffer içinde nested offers olabilir
            if isinstance(off.get("offers"), list):
                for sub in off["offers"]:
                    _append_offer(title, product_url, image_url, sub, out)
            else:
                _append_offer(title, product_url, image_url, off, out)

    if "@graph" in node and isinstance(node["@graph"], list):
        _walk_json_ld(node["@graph"], out)


def _extract_ld_image(img: Any) -> str:
    if isinstance(img, list) and img:
        img = img[0]
    if isinstance(img, dict):
        img = img.get("url") or img.get("contentUrl") or ""
    if isinstance(img, str):
        u = img.strip()
        if u.startswith("//"):
            return "https:" + u
        if u.startswith("http"):
            return u
    return ""


def _append_offer(
    title: str, default_url: str, image_url: str, off: dict[str, Any], out: list[dict[str, Any]]
) -> None:
    if not isinstance(off, dict):
        return
    price = parse_price_tl(off.get("price") or off.get("lowPrice"))
    if price is None:
        return
    high_price = parse_price_tl(off.get("highPrice"))
    seller_obj = off.get("seller")
    seller_name = ""
    if isinstance(seller_obj, dict):
        seller_name = str(seller_obj.get("name", "")).strip()
    elif isinstance(seller_obj, str):
        seller_name = seller_obj.strip()

    availability = str(off.get("availability", "")).lower()
    in_stock = "outofstock" not in availability

    out.append(
        {
            "title": title,
            "price_tl": price,
            "high_price_tl": high_price,
            "url": str(off.get("url") or default_url or "").strip(),
            "image_url": image_url,
            "seller": seller_name,
            "in_stock": in_stock,
        }
    )
