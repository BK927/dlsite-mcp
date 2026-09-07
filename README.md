# DLsite MCP

Compact, read-only MCP server for public DLsite metadata. It uses
[`dlsite-async`](https://github.com/bhrevol/dlsite-async) for work and maker
lookups and small public adapters for keyword search and reviews.

No DLsite login is accepted or stored. Purchased works, downloads, wishlists,
cart actions, and DLsite Play are deliberately out of scope.

## Public tools

| Tool | Purpose |
| --- | --- |
| `dlsite_work_get` | Read summary/details or public review bodies for a product ID/URL; reviews use signed continuation cursors. |
| `dlsite_search` | Search one DLsite section with bounded results and signed continuation cursors. |
| `dlsite_maker_get` | Resolve a public circle, brand, or publisher profile. |

The static tool list does not depend on provider availability. Detailed option
help lives in `dlsite://catalog` and `dlsite://schema/{operation}` so idle
context stays small. Successes use a stable `structuredContent` envelope;
human-readable `content` is only one line. Publisher-controlled fields are
listed under `meta.untrusted_fields`.

Use `dlsite_work_get` with `view="reviews"` for review bodies. A response may
return fewer items than `limit` to keep each review intact inside the MCP byte
budget; follow `page.next_cursor` until `data.complete` is true. There is no
corpus-size cap, so works with tens of thousands of reviews remain traversable.
The cursor also records the last review ID and the initial total to reduce
duplicates or gaps if new reviews arrive during a long traversal. Review text
defaults to 1,200 characters per item and can be raised to 4,000. Cursors expire
after 24 hours by default; `DLSITE_CURSOR_TTL_SECONDS` can extend this to 7 days.

Search supports `maniax`, `home`, `books`, `soft`, `pro`, and `appx`. Direct
work lookup also supports `comic`/comipo product IDs such as `BJ370220`;
comipo's separate client-rendered search is not exposed. Supported
metadata/price locales are `ja_JP`, `en_US`, `ko_KR`, `zh_CN`, and `zh_TW`.
Availability of translated metadata is determined by DLsite.

Work and maker references must be a single ID or an absolute `dlsite.com` URL
for that entity. Use an array for multiple works; a string containing several
IDs is rejected. Work batches retain all-or-error semantics: an upstream
failure includes the failing `details.product_id`, and successfully fetched
records can still be reused from the cache on a subsequent request.

Detailed work metadata is parsed in the requested language, including localized
headings and dates. `description` is DLsite's meta-description summary with its
site promotion removed, not the entire product page; `description_source`
identifies this source. Unrecognized rows, failed field extraction, and absent
detail tables produce `meta.warnings`. Summary view intentionally omits HTML-only
detail fields. A null field does not by itself mean that parsing failed.

When fields fail to parse or row headings are unrecognized, details also include
`metadata_diagnostics.failed_fields` (canonical field names) and
`metadata_diagnostics.unrecognized_row_labels` (untrusted source headings).
The latter is limited to eight distinct labels of 80 characters each;
`unrecognized_row_count` and `labels_truncated` indicate the full row count and
whether labels were shortened or omitted. Unknown rows have no assumed field
mapping: for example, DLsite's `Miscellaneous`/`기타` row is reported explicitly.

Search applies DLsite's native category/audience filters, rather than relying on
the storefront URL prefix. `data.site` is the requested section;
`data.applied_filters` records the filters and `data.result_sites` records the
storefronts of the returned items. For example, an adult storefront can include
all-ages products for the same audience, and `books` is the adult comic section.
Cross-storefront results are called out in `meta.warnings`. Search locales can
also affect DLsite's language eligibility rules, not just displayed labels.

Some localized search pages do not expose review counts. In that case the value
remains null and a warning explains the source limitation; work lookup's
`public_metrics.review_count` can supply the count separately. Search cursors
created before the category-filter fix are rejected: restart without a cursor.

## Local stdio

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are recommended.

```powershell
uv sync --extra dev
uv run python -m dlsite_mcp.server
```

Example MCP configuration:

```json
{
  "mcpServers": {
    "dlsite-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "C:\\path\\to\\dlsite-mcp",
        "python",
        "-m",
        "dlsite_mcp.server"
      ]
    }
  }
}
```

## Streamable HTTP and ChatGPT OAuth

Copy `.env.example`, set secrets, and run with `MCP_TRANSPORT=http`. HTTP mode
serves `/mcp` and `/healthz`. It requires a static bearer token unless
`MCP_ALLOW_UNAUTHENTICATED=true`. For ChatGPT custom MCP registration, enable
the included personal OAuth flow and use the same private value as
`MCP_OAUTH_LOGIN_SECRET` on the authorization page.

```text
MCP_TRANSPORT=http
MCP_ACCESS_TOKEN=<at least 32 random characters>
PUBLIC_BASE_URL=https://your-host.example
MCP_OAUTH_ENABLED=true
MCP_OAUTH_LOGIN_SECRET=<at least 32 random characters>
MCP_OAUTH_SIGNING_SECRET=<different random secret, at least 32 characters>
```

The OAuth client allowlist accepts only ChatGPT's published stable and
connector-specific client IDs. On a multi-instance deployment, select the
optional Firestore authorization-code store.

## Plugin packaging

The repository root is the authoritative Codex plugin source. To install a
local-development wrapper or a deployed HTTPS endpoint without registering two
copies of the same tools:

```powershell
./scripts/sync-codex-plugin.ps1 -Profile local
./scripts/sync-codex-plugin.ps1 -Profile cloud -Url https://host.example/mcp
```

The cloud profile reads its bearer token from `DLSITE_MCP_ACCESS_TOKEN`. Start a
new Codex task after synchronization so the new tool registry is loaded.

## Development

```powershell
uv run ruff check .
uv run pytest -q
uv run python scripts/audit_context.py
```

Live provider calls are not required by the test suite. DLsite HTML and public
endpoints can change without notice; provider failures use stable MCP error
codes and never expose request headers or secrets.

## Attribution

`dlsite-async` is Copyright (c) 2021 byeonhyeok and distributed under the MIT
License. This project depends on the published package and does not include its
login or DLsite Play modules in the MCP surface.

See `THIRD_PARTY_NOTICES.md` for transport-pattern attribution and service
notices. This project is not affiliated with DLsite or EISYS, Inc.
