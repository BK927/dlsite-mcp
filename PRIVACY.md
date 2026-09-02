# Privacy

DLsite MCP sends only the public work ID, maker ID, search query, selected
DLsite section, and locale needed for a request to public DLsite endpoints.
It does not accept DLsite credentials and does not access account data.

The process keeps a bounded in-memory cache. Cache entries expire and are not
persisted. OAuth access keys, bearer tokens, authorization headers, and cursor
signing secrets must not be logged.
