"""The FPL side of the wire: the HTTP client, the login that gives it a token, and the
session it belongs to. `rotowire_scraper` is the one other source - predicted lineups -
and lives here for the same reason.

Knows nothing about the warehouse. The engine reads through this; nothing here reads
the engine.
"""
