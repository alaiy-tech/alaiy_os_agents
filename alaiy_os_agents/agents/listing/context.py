# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Deterministic prefetch for a listing run.

`get_channel_spec`, `get_reference_values`, `get_listing_health` and
`prepare_images` cost the model nothing to decide — every run calls all four,
in the same order, with arguments that are just the product/channel it was
already given. That is not the model exercising judgement, it is the model
paying an LLM round trip to fetch data the workflow already knows it needs.
`get_product` is the one exception: its photos are the model's actual
evidence, and skipping it would mean writing a listing nobody looked at.

So a caller that already knows the product before the run starts (the admin
API's `enrich_product`, `bulk.run_chunk`) resolves the other four in plain
Python and drops the result on the payload as `_context`, keyed by the exact
tool_id each one replaces. `_context` is a convention the engine itself
enforces (see `executor._strip_prefetched_tools`): any tool named there is
removed from what the model is offered this run, not merely asked to skip —
so this is a guarantee, not a request the model can ignore. A caller that
does not have the product until the model reads it out of free text (Ask
Alaiy's chat dispatch) passes no `_context`, and the agent falls back to
calling the tools itself exactly as it always did.

Nothing here is a new source of truth. Every value in `_context` is the exact
tool result `tools.py` would have returned for the same call; this only moves
*when* that call happens, from a model-initiated turn to a Python call before
the run exists.
"""

import frappe

from alaiy_os_agents.agents.listing import channels, tools

#: tool_id -> handler, for the fixed lookups a prefetch can answer. Keyed by
#: tool_id (not a paraphrase of it) because that is exactly what the engine's
#: `_context` convention matches against to strip a tool from the run.
PREFETCHABLE = {
	"get_channel_spec": lambda product, channel: tools.get_channel_spec(channel=channel, product=product),
	"get_reference_values": lambda product, channel: tools.get_reference_values(
		channel=channel, product=product
	),
	"get_listing_health": lambda product, channel: tools.get_listing_health(product, channel=channel),
}


def build_context(product, channel=None, prepare_images=False):
	"""The four fixed lookups for one product, or None if the product does not
	resolve to a channel right now.

	None covers exactly the cases `get_channel_spec` itself would refuse on: no
	channel connector installed, an identifier on no channel and no catalogue
	row, an identifier on more than one channel, or a catalogue product nobody
	has registered yet. Every one of those has its own next move (register,
	disambiguate, install a connector) that belongs to the model's ordinary
	tool-error correction loop, not to a prefetch that has no way to ask a
	clarifying question — so this simply declines and the run proceeds exactly
	as it would have with no `_context` at all.
	"""
	try:
		adapter = channels.resolve(product, channel)
	except Exception:
		return None

	resolved = adapter["channel"]
	context = {tool_id: fn(product, resolved) for tool_id, fn in PREFETCHABLE.items()}
	context["prepare_images"] = tools.prepare_images(
		product=product, channel=resolved, prepare_images=prepare_images
	)
	return context


def augment_payload(payload):
	"""`payload`, with `_context` added when its `product` resolves to a channel.

	Never raises: a caller building a payload must not fail the whole run over
	an optimisation. A prefetch that errors just means `_context` is left off,
	and the model calls the tools itself — the same run it would have been
	without this module.
	"""
	payload = dict(payload or {})
	product = payload.get("product")
	if not product:
		return payload

	try:
		context = build_context(
			product, payload.get("channel"), bool(payload.get("prepare_images"))
		)
	except Exception:
		frappe.log_error(title=f"Listing context prefetch failed for {product}")
		context = None

	if context is not None:
		payload["_context"] = context
	return payload
