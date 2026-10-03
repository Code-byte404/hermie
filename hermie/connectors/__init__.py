"""Data connectors: read-only access to the user's business data (App Store Connect, Apple Ads, ...).

A connector only fetches. Hermie's rules (business flag, offline sandbox, no cloud, data room, logs) live in
guard.py and apply to every connector the same way."""
from .base import Connector, ConnectorContext, ConnectorResult, ConnectorTool, Status

__all__ = ["Connector", "ConnectorContext", "ConnectorResult", "ConnectorTool", "Status"]
