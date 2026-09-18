# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The listing agent's tools. Every one of them is channel-agnostic.

Each tool resolves the channel — from the `channel` the model passes, or from the
product identifier itself (see `channels.resolve`) — and hands off to that
channel's handler. Nothing here knows what a bullet point is, what Amazon
suppresses a listing for, or which doctype holds a Shopify variant.

## Why every tool takes `channel` as an argument

The alternative is resolving once per run and stashing it on `frappe.local`. That
reads tidier and is wrong here: `run_now` executes inside a chat worker that is
already handling one session, `_dispatch_tools` has no run-scoped teardown, and a
value left behind by a failed turn would silently steer the next one. Passing it
costs the model a copied string — the same thing the prompt already asks of it for
`prepare_images`'s toggle — and makes every call independently correct.

`channel` stays optional on all of them because on most benches an identifier
exists on exactly one channel, and asking a seller which marketplace their own SKU
is on is asking for something the system can work out.
"""

import base64
import os

import frappe

from alaiy_os_agents.agents.listing import channels

#: Some product-photo CDNs refuse a request with no browser-like User-Agent, so
#: an external image is always fetched here rather than handed to the provider as
#: a bare URL for it to fetch. Carried over from the channel packs, where the
#: refusal was observed against a real supplier CDN.
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AlaiyOS-Listing/1.0)"}

MEDIA_TYPES = {
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".png": "image/png",
	".gif": "image/gif",
	".webp": "image/webp",
}


# ── what this channel wants ───────────────────────────────────────────────────


def get_channel_spec(channel=None, product=None):
	"""The fields and rules this channel expects, for the model to write against.

	This is the tool that makes one agent enough for every marketplace. The row's
	`output_schema` carries only what every channel shares; everything a channel
	asks for beyond that — Amazon's five bullets and byte-budgeted keywords,
	Shopify's SEO fields and variant options — arrives here, at runtime, as an
	ordinary tool result.

	Returning it rather than merging it into the schema is deliberate. A merged
	superset makes every channel's own fields optional, and a field the schema
	says is optional is a field the model treats as optional; a schema per channel
	means a registry row per channel, which is the second agent this app exists to
	remove. A tool result costs one round trip and keeps both properties.

	`fields` is a JSON-Schema fragment of the channel's own properties, `rules` is
	prose. Both come from the adapter verbatim — core has no opinion about either.
	"""
	adapter = channels.resolve(product, channel) if (product or channel) else None
	if adapter is None:
		# No product and no channel: the model is orienting rather than working on
		# something. Tell it what exists so its next call can be specific.
		frappe.throw(
			"Say which channel you are writing for. Available: "
			f"{', '.join(sorted(channels.labels()))}."
		)

	spec = adapter.get("spec") or {}
	return {
		"channel": adapter["channel"],
		"label": adapter.get("label") or adapter["channel"],
		"identifier_label": adapter.get("identifier_label") or "product identifier",
		"fields": spec.get("fields") or {},
		"rules": spec.get("rules") or "",
		# Named so the model can say "saved to the Amazon Enriched Listing" without
		# being told the doctype separately, and so a `needs_review` note can point
		# a human at the right place.
		"review_doctype": adapter.get("enriched_doctype"),
		# What this channel can actually do, so the model does not call a step that
		# does not exist and then report it as a failure.
		"has_image_step": bool((adapter.get("handlers") or {}).get("prepare_images")),
		"has_health_check": bool((adapter.get("handlers") or {}).get("health")),
		"can_register": bool((adapter.get("handlers") or {}).get("register")),
	}


# ── reading the product ───────────────────────────────────────────────────────


def get_product(product, channel=None):
	"""This product as its channel holds it, photos included."""
	adapter = channels.resolve(product, channel)
	return channels.require(adapter, "get_product")(product=product)


def get_reference_values(channel=None, product=None):
	"""Vocabulary already in use on this channel, so output stays consistent.

	A channel that publishes no reusable vocabulary is not an error — it gets an
	empty answer and a note, which is something the model can carry on from.
	"""
	adapter = channels.resolve(product, channel) if (product or channel) else None
	if adapter is None:
		frappe.throw(
			"Say which channel you are writing for. Available: "
			f"{', '.join(sorted(channels.labels()))}."
		)
	fn = channels.handler(adapter, "get_reference_values")
	if fn is None:
		return {
			"values": {},
			"note": f"The {adapter.get('label')} channel publishes no reference vocabulary.",
		}
	return fn()


def get_listing_health(product, channel=None):
	"""What the channel says is wrong with this listing right now.

	The point of a separate tool, when `get_product` already returns a listing's
	issues alongside everything else: as one field among twenty, the issues read as
	background. Asked for on their own, they are the subject — which is what a
	"why is this suppressed" question needs, and what lets the agent report *which*
	problem it fixed rather than silently rewriting the copy.

	A channel with no issue feed says so plainly. Amazon reports suppression
	reasons; Shopify has no equivalent, and inventing a verdict for it would be
	worse than admitting there is nothing to read.
	"""
	adapter = channels.resolve(product, channel)
	fn = channels.handler(adapter, "health")
	if fn is None:
		return {
			"channel": adapter["channel"],
			"supported": False,
			"issues": [],
			"note": (
				f"{adapter.get('label')} does not report listing issues back to Alaiy OS, "
				"so there is nothing to diagnose here. Leave the diagnosis empty."
			),
		}
	result = fn(product=product) or {}
	result.setdefault("channel", adapter["channel"])
	result.setdefault("supported", True)
	return result


# ── looking at a photo ────────────────────────────────────────────────────────


def view_image(image_url):
	"""Fetch an image URL and hand it back as something the model can actually see.

	Needed whenever the only evidence for a product is a URL rather than an
	identifier — a string is not evidence, and a listing written from one is
	invention.
	"""
	return {
		"_content_blocks": [
			{"type": "text", "text": f"Reference image ({image_url}):"},
			_image_block(image_url),
		]
	}


def _image_block(image_url):
	"""A base64 vision block for an external image URL."""
	import requests

	resp = requests.get(image_url, timeout=30, headers=FETCH_HEADERS)
	resp.raise_for_status()
	mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
	if not mime or not mime.startswith("image/"):
		mime = MEDIA_TYPES.get(os.path.splitext(image_url or "")[1].lower()) or "image/jpeg"
	return {
		"type": "image",
		"source": {
			"type": "base64",
			"media_type": mime,
			"data": base64.b64encode(resp.content).decode("ascii"),
		},
	}


# ── the image step ────────────────────────────────────────────────────────────


def prepare_images(product=None, channel=None, prepare_images=False, image_urls=None):
	"""Run this channel's image step, if it has one and it was switched on.

	Whether anything happens is the channel's decision and not the model's — the
	same rule both channel packs already state in their own tool descriptions. The
	toggle is relayed verbatim from the run input, because image work costs money
	per photo and nothing but an explicit opt-in should start it.

	The parameter is named for the input option it carries (`prepare_images`, the
	fieldname the desk surfaces render) rather than something tidier like
	`enabled`. That is what makes "copy it verbatim from the input" an instruction
	the model can follow literally instead of a mapping it has to infer.
	"""
	adapter = channels.resolve(product, channel) if (product or channel) else None
	if adapter is None:
		frappe.throw(
			"Say which channel you are writing for. Available: "
			f"{', '.join(sorted(channels.labels()))}."
		)

	fn = channels.handler(adapter, "prepare_images")
	if fn is None:
		return {
			"images": [],
			"note": (
				f"The {adapter.get('label')} channel has no image step. Set images to [] "
				"— this is expected, not a failure."
			),
		}
	return fn(product=product, enabled=bool(prepare_images), image_urls=image_urls or None)


# ── putting a product on a channel ────────────────────────────────────────────


def register_product(product, channel=None):
	"""Give a catalogue product a listing record on a channel, so it can be enriched.

	The gap this closes: supplier connectors fill the catalogue with products, and
	a sales channel keys everything to its own listing record. A product sourced
	from a supplier therefore has nothing for an enrichment to be written onto, and
	`get_channel_spec` refuses — correctly, but as a dead end. This is the hop.

	**Local only. Nothing is sent to the channel.** It creates the record that
	makes the product enrichable, in whatever "not live yet" state that channel
	uses. Publishing remains a separate, deliberate act on an enrichment a person
	has reviewed, and nothing here brings it closer to happening.

	Idempotent by contract: a product that already has a record gets that record
	back, unchanged. Registering never edits an existing listing — what the channel
	already holds beats anything the catalogue can offer.
	"""
	if not channel:
		# Not resolved from the product: the whole reason to be in this function is
		# that the product resolves to nothing yet. Where exactly one channel can
		# register, that is not a choice worth asking about; where several can, it
		# is the user's to make and not ours to guess.
		options = channels.registrable()
		if not options:
			frappe.throw(
				"No channel on this site can register a product. It has to be listed "
				"on a channel by other means before a listing can be written."
			)
		if len(options) > 1:
			frappe.throw(
				"Say which channel to register it on: " + ", ".join(sorted(options)) + "."
			)
		channel = next(iter(options))

	adapter = channels.get(channel)
	return channels.require(adapter, "register")(product=product)


# ── writing it down ───────────────────────────────────────────────────────────


def save_listing(product, listing, channel=None):
	"""Validate the finished listing against the channel's rules, then persist it.

	**This is where the channel's strictness actually lives.** The agent's own
	`output_schema` is the floor every channel shares, and JSON Schema could not
	carry the rest of it anyway — it has no way to say "under 250 bytes in total",
	"none of these sixty words", or "no keyword that already appears in the title".
	The adapter's `validate` is Python and says all three.

	A defect is raised rather than returned, so `_dispatch_tools` marks the result
	`is_error` and the model reads it and fixes the listing — the same correction
	loop a denied permission or a bad argument already takes. Nothing is written
	until it passes, so a rejected listing leaves no half-saved row behind.
	"""
	adapter = channels.resolve(product, channel)

	# Mechanical, non-content fixes the channel knows how to make on the
	# model's behalf -- see the adapter's own `normalize` for what qualifies
	# and why. A channel with none just hands `listing` back unchanged.
	normalize = channels.handler(adapter, "normalize")
	if normalize is not None:
		listing = normalize(listing=listing)

	validate = channels.handler(adapter, "validate")
	if validate is not None:
		defects = validate(listing=listing) or []
		if defects:
			frappe.throw(
				f"This listing does not meet {adapter.get('label')}'s requirements "
				"and was NOT saved. Fix these and call save_listing again:\n- "
				+ "\n- ".join(defects)
			)

	return channels.require(adapter, "save_listing")(product=product, listing=listing)
