# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Re-export of the shared web-search tool — see `agents/_shared/websearch.py`
for the implementation. Kept as a module here so `meta.py`'s `_WEBSEARCH`
handler path (`alaiy_os_agents.agents.pricing.websearch.*`) resolves, the same
convention `agents/listing/websearch.py` follows.

Only `search_competitor_listings` — this agent has no page-opening tool, see
`meta.py`'s module docstring for why.
"""

from alaiy_os_agents.agents._shared.websearch import search_competitor_listings  # noqa: F401
