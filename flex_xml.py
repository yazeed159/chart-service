"""
flex_xml.py
Minimal XML -> dict converter for IBKR's Flex Web Service responses, tuned
to match n8n's built-in "xml" node's default output shape (xml2js with
mergeAttrs: true, explicitArray: false) -- which is the shape "Extract &
Match Trades1"'s original JS code expects (reads trade.symbol,
trade.dateTime, etc. directly, not trade.$.symbol).

IBKR's Flex XML is attribute-only for the elements that matter here
(<TradeConfirm symbol="..." dateTime="..." .../>, all self-closing), so
this converter doesn't need to handle mixed attribute+text-content nodes.

Shape produced, matching xml2js:
  - Each element becomes a dict of {attr_name: value, child_tag: value_or_list}
  - A child tag that appears once -> a dict (or string if it has no
    attrs/children, just text)
  - A child tag that appears more than once under the same parent -> a list
  - The whole document is wrapped as {root_tag: {...}}, same as xml2js's
    default (this is what lets "Report Ready?1" check $json.FlexQueryResponse).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any


def _local_tag(tag: str) -> str:
    # Strip any {namespace} prefix ElementTree adds -- IBKR's Flex XML has
    # none in practice, but this keeps it safe if that ever changes.
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _element_to_value(el: ET.Element) -> Any:
    children = list(el)
    text = (el.text or "").strip()

    if not children and not el.attrib:
        # Plain text leaf (e.g. <Status>Success</Status>)
        return text

    node: dict[str, Any] = dict(el.attrib)
    for child in children:
        tag = _local_tag(child.tag)
        value = _element_to_value(child)
        if tag in node:
            existing = node[tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                node[tag] = [existing, value]
        else:
            node[tag] = value

    if not node and text:
        return text
    return node


def parse_flex_xml(raw: bytes | str) -> dict:
    """Parses a raw Flex Web Service XML response (SendRequest's,
    GetStatement's, or an error FlexStatementResponse) into a dict shaped
    like n8n's xml node output: {root_tag: {...}}."""
    root = ET.fromstring(raw)
    return {_local_tag(root.tag): _element_to_value(root)}


def as_list(value) -> list:
    """IBKR (and xml2js) collapse a single child to a bare dict instead of
    a 1-item list -- every caller that iterates FlexStatement /
    TradeConfirm etc. needs this normalization, same as the original JS's
    `Array.isArray(x) ? x : [x]` pattern."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
