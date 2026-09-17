# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Every default agent Alaiy OS ships, and how they reach `OS Agent Registry`.

One app holds them all. `agents/listing/` is the first; the next is a sibling
directory beside it, and nothing here or in hooks.py has to learn its name —
discovery walks `agents/` and takes whatever declares itself.

    agents/
      listing/
        meta.py      build_agent_meta() -> the manifest
        channels.py  its own seams
        tools.py     its handlers
        prompts/     its prompt
        schemas/     its output schema

## An agent is a package that exposes `build_agent_meta`

That is the whole contract. `sync()` imports each subpackage of `agents/`, calls
that function, and upserts what comes back. A directory without it is skipped
rather than treated as broken, so a shared helpers package can live there too.

A package may expose `build_agent_metas` instead — plural, returning a list — when
how many agents it describes is not known until it runs on a site. `agents/connector`
is the one that does: it builds one agent per installed connector, so a bench with
three connectors gets three rows out of that single package and a bare bench gets
none. Everything downstream is identical; only the count varies.

Deliberately not a hook. A hook is for apps that do not know about each other,
and these all ship together in this one — a registration list would be a second
place to edit and a second thing to forget. The directory IS the list.

## Ships installed, starts disabled

**Every agent here is registered with `is_enabled = 0` on first insert.** The
code is on every bench that has this app, but nothing runs until someone turns it
on in the Agents settings screen, and `is_enabled` is what the rest of Alaiy OS
already reads: `chat/skills.py` filters the `/` catalogue on it and `chat/agents.py`
runs only what that catalogue offers. So enabling an agent is what makes Ask Alaiy
able to use it, and disabling it is a complete off switch — no separate wiring, and
nothing here to keep in step with the chat.

Off by default because these are not free. An agent is a model, its tools, and
whatever they touch — spend, and in some cases writes — and a bench should not
acquire any of that by upgrading an app.

`is_enabled` is written **only when the row is first created**. After that it is
the operator's, and a migrate must never quietly re-disable an agent someone
turned on, nor re-enable one they turned off — the same reason `_RUNTIME_FIELDS`
has always excluded it from the reconcile.
"""

import importlib
import json
import pkgutil

import frappe

#: Fields written from a manifest on every reconcile. `is_enabled` is the
#: operator's after the first insert — see the module docstring.
_RUNTIME_FIELDS = {"is_enabled"}

#: Manifest keys the desk surfaces read, which are not registry fields.
#:
#: `writes` and `enabled_on_insert` are read here rather than surfaced: the first
#: by `_tool_row`, the second by `_upsert`. Both are listed so `doc.set` is never
#: called with them — a manifest key with no field behind it sets a stray attribute
#: on the Document, which saves without complaint and is then invisible.
_NON_REGISTRY_FIELDS = {
	"agent_id",
	"tools",
	"input_options",
	"override_app",
	"writes",
	"enabled_on_insert",
}

#: Dict-valued manifest keys that land on Code fields. Without the dump they are
#: stored as a Python repr, which `json.loads` then refuses — silently, and only
#: visible later as a skill that rejects every argument you give it.
_JSON_FIELDS = ("output_schema", "input_schema")


def agents():
	"""Every agent package in this app, as `{agent_id: manifest}`.

	Import failures are not swallowed. A broken agent is a bug in this app, and
	the migrate that would have installed it is exactly where it should surface —
	unlike a tenant hook, where failing closed protects a site from someone else's
	mistake.
	"""
	from alaiy_os_agents import agents as package

	found = {}
	for module in pkgutil.iter_modules(package.__path__):
		if not module.ispkg:
			continue
		meta_module = f"{package.__name__}.{module.name}.meta"
		try:
			meta_py = importlib.import_module(meta_module)
		except ModuleNotFoundError:
			# Not an agent — a shared helpers package, say. Skipping is right, and
			# quiet: this is the documented way to have one.
			continue
		# Plural first: a package describing a variable number of agents exposes
		# only `build_agent_metas`, and one describing exactly one exposes only
		# `build_agent_meta`. See the module docstring.
		build_many = getattr(meta_py, "build_agent_metas", None)
		build_one = getattr(meta_py, "build_agent_meta", None)
		if build_many:
			metas = build_many()
		elif build_one:
			metas = [build_one()]
		else:
			continue
		for meta in metas:
			found[meta["agent_id"]] = meta
	return found


def sync():
	"""Upsert every agent in this app. Idempotent; called on install and migrate."""
	if not frappe.db.exists("DocType", "OS Agent Registry"):
		# alaiy_os may not be migrated yet on a fresh bench. Our own next migrate
		# catches it.
		return

	for meta in agents().values():
		_upsert(meta)
	frappe.db.commit()


def _upsert(meta):
	agent_id = meta["agent_id"]
	existing = frappe.db.exists("OS Agent Registry", agent_id)

	if existing:
		doc = frappe.get_doc("OS Agent Registry", agent_id)
	else:
		doc = frappe.new_doc("OS Agent Registry")
		doc.agent_id = agent_id
		# The one moment `is_enabled` is ours. The DocType's own default is 1,
		# which is right for a connector's pack — installed because someone chose
		# that connector — and wrong for an agent that arrived with an app upgrade.
		#
		# A manifest says which it is. `agents/connector` builds exactly the first
		# case and asks for it: those agents exist only because a connector was
		# installed and configured, so starting them off would mean a bench that
		# chose a connector then has to choose it again.
		doc.is_enabled = 1 if meta.get("enabled_on_insert") else 0

	for key, value in meta.items():
		if key in _NON_REGISTRY_FIELDS or key in _RUNTIME_FIELDS:
			continue
		if key in _JSON_FIELDS and isinstance(value, dict):
			value = json.dumps(value, indent=1)
		doc.set(key, value)

	doc.set("tools", [_tool_row(meta, tool) for tool in meta.get("tools", [])])

	# save() inserts when new. The OS Agent Tool child controller validates every
	# handler path here, so a broken tool fails at migrate rather than mid-run.
	doc.save(ignore_permissions=True)


def _tool_row(meta, tool):
	return {
		"tool_id": tool["tool_id"],
		"description": tool["description"],
		"handler": tool["handler"],
		"parameters_schema": (
			json.dumps(tool["parameters_schema"], indent=1)
			if isinstance(tool.get("parameters_schema"), dict)
			else tool.get("parameters_schema")
		),
		"connector": tool.get("connector"),
		# Marks which tools change state. Nothing reads this any more: it existed to
		# keep writers off Ask Alaiy's directly-callable tool surface, and that
		# surface is gone — every tool is now reached only inside a run that applied
		# its agent's own rules, which is the property `effect` was approximating.
		#
		# Still written, because it is the honest record of what a tool does and the
		# thing any future gate would read. A manifest that stopped declaring it
		# would have to rediscover it.
		"effect": "write" if tool["tool_id"] in set(meta.get("writes") or ()) else "read",
		# What the tool must be able to read for its handler to return real data.
		# Carried through rather than dropped because two gates gate on it and both
		# read it off the row: `engine/factory.py` refuses a run whose user is
		# missing any of it, and `api/agent_settings.py` refuses the switch. A tool
		# that declared its requirements in a manifest and arrived here without them
		# is an undeclared tool — silently ungated, and reported to the operator as
		# though nobody had described it.
		"required_permissions": (
			json.dumps(tool["required_permissions"], indent=1)
			if isinstance(tool.get("required_permissions"), (list, dict))
			else tool.get("required_permissions")
		),
	}


def unregister():
	"""Remove every agent this app registered. Called on uninstall.

	`force=True` because each past `OS Agent Run` links to its agent, so the
	default link check refuses the delete and the uninstall dies the moment an
	agent has ever run. The runs keep their agent id as recorded history; they
	simply stop pointing at a live row.
	"""
	for agent_id in agents():
		if frappe.db.exists("OS Agent Registry", agent_id):
			frappe.delete_doc("OS Agent Registry", agent_id, force=True, ignore_permissions=True)
	frappe.db.commit()
