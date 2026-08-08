"""Nellis Auction deal-finding and valuation engine.

This package analyzes public auction listings and computes a defensible
walk-away max bid. It deliberately does NOT place bids: Nellis' Terms of
Service prohibit automated bidding, and their server-side proxy bidding plus
30-second bid extensions mean automated placement would produce outcomes
identical to entering a max bid by hand. The edge here is valuation, not speed.
"""

__version__ = "0.1.0"
