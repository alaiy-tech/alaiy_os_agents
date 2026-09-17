# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""One agent per connector, built from the connector's export.

What is worth testing here is the split: the connector supplies what only it
knows, and everything else — model, prompt, turn budget, reply shape — comes from
this app and is the same for every connector. A regression would look like a
connector setting its own model again, or a bench acquiring an agent it has no
connector for.

The other property is that one broken connector cannot take the rest down. That
is a deliberate difference from `agents/listing`'s channel seam, which fails
closed, and the reason is in `meta.exports()`.

None of this needs a site: the hook is patched and nothing is written.
"""

import unittest
from unittest.mock import patch

from alaiy_os_agents.agents.connector import meta

HOOKS = "frappe.get_hooks"
ATTR = "frappe.get_attr"
LOG = "frappe.log_error"

DECLARED = {
	"properties": {"marketplace": {"type": "string", "description": "Which marketplace."}},
	"required": ["marketplace"],
}

EXPORT = {
	"agent_id": "amazon_sp_api",
	"label": "Amazon (SP-API)",
	"description": "Answers questions about this seller's Amazon listings and sales.",
	"input_schema": DECLARED,
	"tools": [
		{
			"tool_id": "list_listings",
			"description": "A page of the Amazon listing register.",
			"handler": "alaiy_os_connector_amazon_sp_api.api.list_listings",
			"parameters_schema": {"type": "object", "properties": {}},
			"connector": "amazon_sp_api",
			"required_permissions": [{"doctype": "Amazon Product Listing", "ptype": "read"}],
		}
	],
}


class TestInputSchema(unittest.TestCase):
	"""`task` plus whatever the connector declares, in one flat object."""

	def test_merges_declared_properties(self):
		schema = meta.input_schema(EXPORT)
		self.assertEqual(set(schema["properties"]), {"task", "marketplace"})
		self.assertEqual(schema["required"], ["task", "marketplace"])

	def test_task_alone_is_a_complete_agent(self):
		schema = meta.input_schema({})
		self.assertEqual(list(schema["properties"]), ["task"])
		self.assertEqual(schema["required"], ["task"])

	def test_task_is_never_required_twice(self):
		# A connector restating `task` is redundant, not an error — and a duplicate
		# in `required` is what jsonschema reports as a schema fault rather than an
		# argument fault, which reaches the model as an error it cannot act on.
		schema = meta.input_schema(
			{"input_schema": {"properties": {"task": {"type": "string"}}, "required": ["task"]}}
		)
		self.assertEqual(schema["required"], ["task"])


class TestBuild(unittest.TestCase):
	"""The connector owns its questions; this app owns the agent."""

	def setUp(self):
		self.meta = meta.build(EXPORT)

	def test_engine_settings_come_from_this_app(self):
		self.assertEqual(self.meta["model"], meta.MODEL)
		self.assertEqual(self.meta["max_turns"], meta.MAX_TURNS)
		self.assertEqual(self.meta["output_format"], "JSON")
		self.assertEqual(self.meta["output_schema"], meta.OUTPUT_SCHEMA)

	def test_channel_knowledge_comes_from_the_connector(self):
		self.assertEqual(self.meta["agent_id"], EXPORT["agent_id"])
		self.assertEqual(self.meta["agent_name"], EXPORT["label"])
		self.assertEqual(self.meta["description"], EXPORT["description"])
		self.assertEqual(self.meta["tools"], EXPORT["tools"])

	def test_prompt_names_its_own_channel(self):
		# The one thing every connector agent must get right, because the turn that
		# aggregates several of them is holding them all at once. An
		# un-interpolated template would leave every agent answering about
		# "{{channel}}".
		prompt = self.meta["system_prompt"]
		self.assertNotIn("{{channel}}", prompt)
		self.assertIn(EXPORT["label"], prompt)

	def test_reachable_from_chat_and_off_the_flat_surface(self):
		# One flag does both: `chat/skills.py` offers an agent with `chat_skill = 1`
		# to `/` and to `run_agent`, and `chat/tools.py` lends out the tools of
		# agents with `chat_skill = 0` only.
		self.assertEqual(self.meta["chat_skill"], 1)
		self.assertEqual(self.meta["skill_slug"], EXPORT["agent_id"])

	def test_starts_enabled(self):
		# Unlike the agents this app ships. A connector agent exists because someone
		# installed and configured that connector; asking them to choose it again is
		# not a safety property. See registry.py's `enabled_on_insert`.
		self.assertIs(self.meta["enabled_on_insert"], True)


class TestExports(unittest.TestCase):
	"""Reading the hook, and what a broken connector costs."""

	def _run(self, entries, attrs):
		with (
			patch(HOOKS, return_value=entries),
			patch(ATTR, side_effect=lambda path: attrs[path]),
			patch(LOG) as logged,
		):
			return meta.build_agent_metas(), logged

	def test_one_agent_per_connector(self):
		built, _ = self._run(["a.export", "b.export"], {
			"a.export": lambda: EXPORT,
			"b.export": lambda: dict(EXPORT, agent_id="shopify", label="Shopify"),
		})
		self.assertEqual([m["agent_id"] for m in built], ["amazon_sp_api", "shopify"])

	def test_a_broken_connector_does_not_take_the_others_down(self):
		def boom():
			raise RuntimeError("connector half-installed")

		built, logged = self._run(["bad.export", "good.export"], {
			"bad.export": boom,
			"good.export": lambda: EXPORT,
		})
		self.assertEqual([m["agent_id"] for m in built], ["amazon_sp_api"])
		# Skipped, not swallowed: the operator has a record of the agent that is
		# missing and why.
		self.assertEqual(logged.call_count, 1)

	def test_a_bench_with_no_connectors_registers_nothing(self):
		built, _ = self._run([], {})
		self.assertEqual(built, [])
