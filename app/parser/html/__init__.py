"""Strict parsers for the official legacy portal's rendered HTML."""

from app.parser.html.legacy import parse_legacy_listing, parse_lot_detail

__all__ = ["parse_legacy_listing", "parse_lot_detail"]
