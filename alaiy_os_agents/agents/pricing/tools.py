# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The pricing agent's own tools: reading a product's own price off the core
catalogue, and persisting a finished comparison.

Both read and write against plain `Item`/`Item Price` — core ERPNext
doctypes every Alaiy OS site has — and nothing from `alaiy_os_thesolist` or
any other seller app, so this agent installs and runs on any bench, same as
`agents/listing`.
"""

import frappe
from frappe.utils import flt, now_datetime

RESULT_DOCTYPE = "Price Comparison Result"

#: Kept short: this is a courtesy (a clean link beats a redirect wrapper), not
#: a verification step, so a slow or unresponsive retailer must not stall the
#: save — see _resolve_url.
RESOLVE_URL_TIMEOUT = 4


def _resolve_url(url):
	"""Best-effort: the real page a citation URL leads to, not the URL itself.

	`search_competitor_listings`'s citations are often a search provider's own
	redirect wrapper (e.g. a Vertex AI `grounding-api-redirect` link) rather
	than the retailer's own address — fine for a model to cite, useless for an
	admin to click. A HEAD request costs milliseconds and no page body, which
	is the point: this agent does not fetch pages (see meta.py's module
	docstring), and resolving a redirect is not fetching one.

	Anything that goes wrong — blocked, slow, a HEAD the server refuses —
	falls back to the original URL rather than failing the save over a
	cosmetic nicety.
	"""
	import requests

	try:
		resp = requests.head(url, timeout=RESOLVE_URL_TIMEOUT, allow_redirects=True)
		return resp.url or url
	except Exception:
		return url


def _resolve_urls(urls):
	"""Resolve every citation URL concurrently instead of one HEAD at a time.

	`save_comparison` is the run's final, blocking action — an admin's
	"Compare Pricing" click sits on the wall clock this adds. Run
	sequentially, N competitor prices cost up to N * RESOLVE_URL_TIMEOUT;
	a thread pool bounds it to one timeout's worth regardless of N.
	"""
	if not urls:
		return {}
	from concurrent.futures import ThreadPoolExecutor

	with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as pool:
		resolved = list(pool.map(_resolve_url, urls))
	return dict(zip(urls, resolved))


def get_product(product):
	"""This item's own selling price, brand and name — the seed for a search query."""
	if not frappe.db.exists("Item", product):
		frappe.throw(f"No product '{product}' found in the catalogue.")

	item = frappe.db.get_value(
		"Item", product, ["item_code", "item_name", "brand", "description"], as_dict=True
	)

	price_row = frappe.db.get_value(
		"Item Price",
		{"item_code": product, "price_list": "Standard Selling"},
		["price_list_rate", "currency"],
		as_dict=True,
	)

	return {
		"item_code": item.item_code,
		"item_name": item.item_name,
		"brand": item.brand,
		"description": item.description,
		"solist_price": flt(price_row.price_list_rate) if price_row else None,
		"currency": (price_row.currency if price_row else None) or frappe.db.get_default("currency"),
	}


def save_comparison(product, competitor_prices=None):
	"""Upsert this product's comparison result.

	Upserts by item code (autoname `field:item_code`), so a re-run replaces
	the previous comparison rather than piling up history — same discipline
	as the listing agent's `save_listing`.
	"""
	if not frappe.db.exists("Item", product):
		frappe.throw(f"No product '{product}' found in the catalogue.")

	competitor_prices = competitor_prices or []
	# A row with no URL and no price is not a competitor price. Dropped rather
	# than raised: the model may pass a malformed row from a flaky turn, and
	# losing one row is better than losing the whole comparison.
	usable = [
		row for row in competitor_prices if (row or {}).get("url") and (row or {}).get("price") is not None
	]

	prices = get_product(product)

	from alaiy_os.engine.context import get_agent_context

	run = get_agent_context().get("run")

	if frappe.db.exists(RESULT_DOCTYPE, product):
		doc = frappe.get_doc(RESULT_DOCTYPE, product)
	else:
		doc = frappe.new_doc(RESULT_DOCTYPE)
		doc.item_code = product

	doc.solist_price = prices["solist_price"]
	doc.currency = prices["currency"]
	doc.run = run
	doc.compared_at = now_datetime()
	resolved_urls = _resolve_urls([row["url"] for row in usable])

	doc.set("competitor_prices", [])
	for row in usable:
		doc.append(
			"competitor_prices",
			{
				"source_name": row.get("source_name") or row["url"],
				"url": resolved_urls[row["url"]],
				"price": flt(row["price"]),
				"currency": row.get("currency"),
				"captured_at": now_datetime(),
			},
		)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"item_code": doc.item_code,
		"solist_price": doc.solist_price,
		"competitor_prices": len(doc.competitor_prices),
	}
