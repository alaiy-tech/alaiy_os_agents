# Copyright (c) 2026, Alaiy and contributors
# For license information, please see license.txt
"""
Re-export of the shared web-search tools — see `agents/_shared/websearch.py`
for the implementation and the reasoning behind it.

Kept as a module here (rather than pointing every caller at `_shared`
directly) because `meta.py`'s `_WEBSEARCH` handler path and
`alaiy_os_connector_shopify/listing/provenance.py`'s
`from alaiy_os_agents.agents.listing import websearch` both address it at
this path.
"""

from alaiy_os_agents.agents._shared.websearch import (  # noqa: F401
	FETCH_HEADERS,
	MAX_PAGE_CHARS,
	research_record,
	search_competitor_listings,
	view_page,
)
