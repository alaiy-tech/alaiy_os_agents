# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
The `listing_channels` seam: how a marketplace tells this agent what it needs.

There is ONE listing agent per site and it knows nothing about any marketplace.
What Amazon wants from a listing and what Shopify wants are facts about those
channels, so they arrive from the app that owns the channel — its connector —
through a list hook:

    listing_channels = ["alaiy_os_connector_amazon_sp_api.listing_channel.channel"]

Each entry is a dotted path to a no-argument callable returning one adapter:

    {
      "channel": "amazon",              # the id the model passes back to every tool
      "label": "Amazon",                # what a person calls it
      "identifier_label": "seller SKU", # what its products are keyed by, in prose
      "source_doctype":   "Amazon Product Listing",
      "enriched_doctype": "Amazon Enriched Listing",
      "spec":     {...},                # the fields and rules, handed to the model
      "input_options": [...],           # per-request toggles the desk renders
      "handlers": {
        "get_product":          "dotted.path",   # required — fn(product)
        "save_listing":         "dotted.path",   # required — fn(product, listing)
        "validate":             "dotted.path",   # optional — fn(listing) -> [defect, ...]
        "get_reference_values": "dotted.path",   # optional — fn()
        "prepare_images":       "dotted.path",   # optional — fn(product, translate, white_bg, generate, image_urls)
        "health":               "dotted.path",   # optional — fn(product); see below
      },
    }

Every handler is called with keyword arguments only, under the names above, so an
adapter is free to give its own function whatever signature suits it as long as
the wrapper it registers accepts those keywords. That is the whole of the
adaptation: core never learns that Amazon calls its identifier `sku` and Shopify
calls it `item_code`.

## Why `spec` is data and not the agent's output schema

An `OS Agent Registry` row holds one `output_schema`, and the two channels on a
commerce bench share only six of their fields. Merging them into a superset is
what loses the rules that matter — Amazon's 120-150 character title, its exactly
five bullets, its ~250-byte keyword budget — because a field that is optional in
the schema reads as optional to the model. Splitting into one row per channel
buys strictness at the price of a second agent, which is the thing this module
exists to remove.

So the schema on the row is the floor every channel shares, and `spec` is handed
over at runtime by `get_channel_spec` (see tools.py) as an ordinary tool result.
The model is *told* what this channel needs instead of inferring it, one round
trip costs what a merged schema would have cost in accuracy, and a third channel
is a new adapter rather than a new row.

`validate` is where the strictness actually lives. It is Python, so it can say
things JSON Schema cannot — a byte budget, a banned-term list, "no keyword that
already appears in the title" — and its defects come back through `_run_tools`
as an `is_error` tool result, which the model reads and fixes. That is the same
correction loop a permission error or a bad argument already takes.

## Fail closed

A source that raises leaves the agent with NO channels rather than the ones that
happened to load. Degrading open here would mean enriching a product against
whichever channel answered first, writing that to a live catalogue — a silently
wrong listing is worse than a run that refuses. Same discipline, and the same
reasoning, as `chat/tools.py`'s tenant sources.

`health` is the only optional capability with a reason worth recording. Amazon
reports why it suppressed a listing (`suppression_reasons`); Shopify has no
equivalent feed at all. So a channel that cannot answer "what is wrong with this
listing" declares no handler, and the diagnosis section of the output stays
empty rather than being invented.
"""

import frappe

from alaiy_os.engine.executor import ToolStop

HOOK = "listing_channels"

#: Handlers an adapter cannot omit. Without `get_product` there is nothing to
#: enrich from; without `save_listing` there is nowhere to put the result. The
#: rest are genuinely optional — a channel with no image pipeline and no issue
#: feed is a complete channel, just a smaller one.
REQUIRED_HANDLERS = ("get_product", "save_listing")


class ChannelError(frappe.ValidationError):
	"""A channel could not be loaded or resolved. Never swallowed."""


class NoChannel(ChannelError, ToolStop):
	"""There is no channel to write for, and nothing this run can do about it.

	Most of what goes wrong here is the model's to fix, and stays an ordinary
	`ChannelError` for that reason: a channel id that does not exist comes back
	naming the ones that do, a catalogue product that is on no channel comes back
	naming `register_product`, an identifier on two channels comes back asking
	which. Each of those is a tool error the model reads and acts on, and the run
	carries on — which is the whole correction loop and worth keeping.

	These two are different. No connector installed means there is no channel on
	this site at all; an identifier that matches no listing and no catalogue row
	means there is nothing to write about. Neither has a next tool call, and being
	`ToolStop` ends the run at the tool rather than handing the model a dead end
	while its output schema still demands a listing — which is what produced a
	"listing" titled "Product not found" instead of an answer.
	"""


#: `get` and `resolve` both reach it, so it is written once. Phrased as the whole
#: answer rather than a diagnostic: it is relayed verbatim to whoever asked, by
#: whichever surface ran the agent.
NO_CHANNEL = (
	"There is no way I can generate a listing for this, because no sales channel "
	"connector is installed and enabled on this site — there is nothing to write a "
	"listing for and nowhere to save one. Install and enable a channel connector "
	"first."
)


def sources():
	"""Every registered adapter, keyed by channel id.

	Raises `ChannelError` if any source fails — see **Fail closed** above. The
	log names the entry so a broken connector is one search away.
	"""
	adapters = {}
	for entry in frappe.get_hooks(HOOK) or []:
		try:
			adapter = frappe.get_attr(entry)()
		except Exception:
			frappe.log_error(title=f"Listing channel source {entry} failed")
			raise ChannelError(
				"A listing channel could not be loaded, so no channel is available. "
				"See the Error Log."
			)
		_check(entry, adapter)
		adapters[adapter["channel"]] = adapter
	return adapters


def _check(entry, adapter):
	"""Refuse a malformed adapter at load, not mid-run.

	A missing handler surfaces here as one clear message naming the connector,
	rather than as an AttributeError six tool calls into a turn that has already
	cost money.
	"""
	if not isinstance(adapter, dict) or not adapter.get("channel"):
		raise ChannelError(f"Listing channel source {entry} returned no channel id.")
	handlers = adapter.get("handlers") or {}
	missing = [name for name in REQUIRED_HANDLERS if not handlers.get(name)]
	if missing:
		raise ChannelError(
			f"Listing channel '{adapter['channel']}' ({entry}) declares no "
			f"{', '.join(missing)} handler."
		)


def get(channel):
	"""One adapter by channel id, or throw naming what is available."""
	adapters = sources()
	if not adapters:
		raise NoChannel(NO_CHANNEL)
	if channel not in adapters:
		frappe.throw(
			f"There is no '{channel}' channel on this site. Available: "
			f"{', '.join(sorted(adapters))}.",
			exc=ChannelError,
		)
	return adapters[channel]


def resolve(product, channel=None):
	"""The adapter this product belongs to.

	An explicit `channel` wins and is simply checked. Without one the identifier
	is looked up in every adapter's `source_doctype`: exactly one hit resolves,
	and anything else throws a message that says what to do about it.

	This is what lets `/listing ABC-123` work. A seller thinks in SKUs, not in
	marketplaces, and on most benches an identifier exists on exactly one channel
	— so asking for the channel every time would be asking for something the
	system already knows. When it genuinely does not know (the same code listed
	on two channels) the ambiguity is real and the user is the only one who can
	settle it, so it is put to them rather than guessed.
	"""
	if channel:
		return get(channel)

	adapters = sources()
	if not adapters:
		raise NoChannel(NO_CHANNEL)

	hits = [
		adapter
		for adapter in adapters.values()
		if adapter.get("source_doctype")
		and frappe.db.exists(adapter["source_doctype"], product)
	]

	if len(hits) == 1:
		return hits[0]
	if not hits:
		message, error = _not_listed(product, adapters)
		raise error(message)
	frappe.throw(
		f"'{product}' is listed on more than one channel "
		f"({', '.join(sorted(a['channel'] for a in hits))}). Say which one to "
		"write for.",
		exc=ChannelError,
	)


def registrable(adapters=None):
	"""The channels that can put a catalogue product on themselves, by id.

	A supplier connector fills the catalogue with ERPNext Items; a sales channel
	keys everything to its own listing record. `register` is the hop between them,
	and it is optional because not every channel has one to offer.
	"""
	adapters = sources() if adapters is None else adapters
	return {
		channel: adapter
		for channel, adapter in adapters.items()
		if (adapter.get("handlers") or {}).get("register")
	}


def _not_listed(product, adapters):
	"""Why this identifier resolved to nothing — and what to do about it.

	Two very different situations wear the same error. A typo is a dead end. A
	real catalogue product that has simply never been put on a channel is one step
	from working, and saying so is the difference between a run that stops and a
	run that continues: an agent told only "not listed" reports a dead end, and the
	person who asked is left to work out that registration is a thing.

	Returns `(message, error class)`, because that difference is also the
	difference between a tool error and the end of the run. Only the registrable
	case has a next move, so only it stays a `ChannelError` the model can act on;
	the other two are `NoChannel` and stop the run with no output. Deciding it here
	rather than at the raise keeps one place that knows which situation this is.
	"""
	known = frappe.db.exists("Item", product)
	can_register = registrable(adapters)

	if known and can_register:
		return (
			f"'{product}' is a product in this catalogue but is not on any sales "
			f"channel yet, so there is nothing to write a listing onto. Put it on "
			f"one first with register_product — {', '.join(sorted(can_register))} "
			f"can do that — then carry on."
		), ChannelError
	if known:
		return (
			f"'{product}' is a product in this catalogue but is not on any sales "
			f"channel, and no channel here can register it. It has to be listed on "
			"a channel before a listing can be written for it."
		), NoChannel
	return (
		f"There is no way I can generate a listing for '{product}': it is not "
		f"listed on any channel on this site ({', '.join(sorted(adapters))}), and "
		"is not a product in this catalogue either. Check the identifier — it has "
		"to be the code alone, not a product name."
	), NoChannel


def handler(adapter, name):
	"""One of an adapter's handlers as a callable, or None if it declares none.

	Resolved per call rather than at adapter-build time: describing the channels
	must not import every connector's client into a turn that never calls one.
	The same reasoning as `chat/tools.py:_pack_tool`.
	"""
	path = (adapter.get("handlers") or {}).get(name)
	if not path:
		return None
	return frappe.get_attr(path)


def require(adapter, name):
	"""`handler()`, but a channel that cannot do this says so in words.

	The message is written for the model, because that is who reads it: it is
	returned as a tool error mid-run, and "this channel has no image step" is
	something it can carry into `notes` and keep going with.
	"""
	fn = handler(adapter, name)
	if fn is None:
		frappe.throw(
			f"The {adapter['label']} channel has no {name.replace('_', ' ')} step. "
			"Continue without it and say so in your notes.",
			exc=ChannelError,
		)
	return fn


def labels():
	"""`{channel: label}` for every registered channel — for prompts and errors."""
	return {channel: adapter.get("label") or channel for channel, adapter in sources().items()}
