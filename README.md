# DLsite MCP Server — Public Metadata, Reviews & Search

A compact, read-only [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
server for researching public DLsite listings with AI assistants. It retrieves
work metadata, regional prices, ratings, creators, public review bodies, keyword
search results, and circle, brand, or publisher profiles. Run it locally over
stdio or self-host it over Streamable HTTP.

No DLsite login is accepted or stored. Purchased works, downloads, wishlists,
cart actions, and DLsite Play are deliberately out of scope. This project is not
affiliated with DLsite or EISYS, Inc.

## Use cases

- Look up a work by a supported product ID or DLsite URL.
- Summarize public metadata, price, rating, genres, creators, and sample links.
- Traverse every public review page with signed continuation cursors.
- Search a DLsite section and compare localized public results.
- Resolve public circle, brand, and publisher profiles for catalog research.

## Supported data and locales

| Capability | Coverage |
| --- | --- |
| Work lookup | Public summary, detailed metadata, or reviews; batches of supported IDs are accepted. |
| Search | `maniax`, `home`, `books`, `soft`, `pro`, and `appx`. |
| Direct work sections | Search sections plus `comic`/comipo IDs such as `BJ370220`. |
| Maker lookup | Public circle (`RG`), brand (`BG`), and publisher (`VG`) profiles. |
| Locales | `ja_JP`, `en_US`, `ko_KR`, `zh_CN`, and `zh_TW`; availability depends on DLsite. |
| Account access | None. The server cannot read purchases, downloads, wishlists, carts, or DLsite Play. |

## Deployment options

| Environment | Status | Best for |
| --- | --- | --- |
| Local stdio | Supported | The simplest setup for a desktop MCP client. |
| Docker on a PC or home server | Supported | A private, always-on endpoint behind your HTTPS reverse proxy or tunnel. |
| Raspberry Pi 4 Model B (2 GB RAM) | Tested hardware only | Uses the same Docker/home-server path. This records the test machine, not a recommendation, minimum requirement, or performance guarantee. |
| GCP Cloud Run | Manual; not project-verified | A constrained single-instance remote endpoint. |
| Cloudflare Workers | Not supported | This CPython/ASGI application is not Workers-native. |
| Cloudflare Tunnel | Possible ingress only | The server still runs on your PC or home server; Tunnel does not run the MCP server. |

Remote deployments make outbound public requests to DLsite. Hosting-provider
egress can be rate-limited or blocked independently of this project, so verify
the intended region and follow DLsite's terms before relying on a cloud host.

## Local stdio

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are recommended.

```sh
git clone https://github.com/BK927/dlsite-mcp.git
cd dlsite-mcp
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
        "/absolute/path/to/dlsite-mcp",
        "python",
        "-m",
        "dlsite_mcp.server"
      ]
    }
  }
}
```

On Windows, use an absolute path such as `C:\\path\\to\\dlsite-mcp`.

## Docker and home-server deployment

The included image runs as a non-root user and serves Streamable HTTP on port
8080. Keep the container bound to loopback and terminate HTTPS in a reverse proxy
or private tunnel unless you have designed an equivalent network boundary.

1. Copy `.env.example` to `.env` and set at least:

   ```text
   MCP_TRANSPORT=http
   HOST=0.0.0.0
   PORT=8080
   MCP_ACCESS_TOKEN=<at least 32 random characters>
   DLSITE_CURSOR_SECRET=<a different stable random secret>
   PUBLIC_BASE_URL=https://mcp.example.com
   ```

2. Build and start the container:

   ```sh
   docker build -t dlsite-mcp .
   docker run -d \
     --name dlsite-mcp \
     --restart unless-stopped \
     --env-file .env \
     -p 127.0.0.1:8080:8080 \
     dlsite-mcp
   ```

3. Check the local health endpoint, then proxy the public HTTPS origin to port
   8080 without buffering or truncating streaming responses:

   ```sh
   curl http://127.0.0.1:8080/healthz
   ```

The MCP endpoint is `/mcp`; `/healthz` does not require MCP credentials. Never
commit `.env`, expose the container's plain HTTP port directly to the internet,
or enable `MCP_ALLOW_UNAUTHENTICATED` on a public endpoint.

**Test-hardware note:** this home-server path was tested on a Raspberry Pi 4
Model B with 2 GB RAM. That is only the hardware used for testing; it is **not**
a recommendation, a minimum requirement, or a performance guarantee.

### ChatGPT OAuth

For a personal ChatGPT custom MCP connection, set `MCP_OAUTH_ENABLED=true` and
configure `PUBLIC_BASE_URL`, `MCP_OAUTH_LOGIN_SECRET`, and
`MCP_OAUTH_SIGNING_SECRET` with distinct random values of at least 32 characters.
Use the private login secret only on this server's authorization page. The OAuth
client allowlist accepts only ChatGPT's published stable and connector-specific
client IDs.

### Shared passkey login

Personal home-server deployments may use [shared passkey login](docs/PASSKEY_LOGIN.md)
for MCP connection approval while keeping DLsite's existing OAuth tokens and scopes.

## GCP Cloud Run


Cloud Run can host the supplied HTTP container because it listens on `PORT=8080`
and the MCP transport is stateless. The current Dockerfile does **not** install
the optional Firestore dependency, however, so its OAuth authorization codes are
held in one process's memory. Use one warm instance with this image:

```sh
gcloud builds submit \
  --tag REGION-docker.pkg.dev/PROJECT/REPOSITORY/dlsite-mcp:1.1.1

gcloud run deploy dlsite-mcp \
  --image REGION-docker.pkg.dev/PROJECT/REPOSITORY/dlsite-mcp:1.1.1 \
  --region REGION \
  --port 8080 \
  --min-instances 1 \
  --max-instances 1 \
  --set-env-vars MCP_TRANSPORT=http \
  --set-secrets MCP_ACCESS_TOKEN=dlsite-mcp-access-token:latest,DLSITE_CURSOR_SECRET=dlsite-mcp-cursor-secret:latest \
  --allow-unauthenticated
```

Create those Secret Manager secrets before deploying. `--allow-unauthenticated`
only lets the request reach the container; the MCP gateway still requires its
bearer token or OAuth. Never combine public Cloud Run ingress with
`MCP_ALLOW_UNAUTHENTICATED=true`.

After Cloud Run reports the stable HTTPS service URL, set `PUBLIC_BASE_URL` to
that exact origin. If you enable ChatGPT OAuth, also attach the login and signing
secrets through Secret Manager. A container replacement can invalidate an
outstanding in-memory authorization code; repeat the connection flow if needed.
Multi-instance OAuth requires a custom image that actually installs the `gcp`
extra and `MCP_OAUTH_STORE=firestore`; the provided image is not
multi-instance-ready. Always set a stable `DLSITE_CURSOR_SECRET` so continuation
cursors survive process replacement.

## Cloudflare: Workers versus Tunnel

Cloudflare Workers cannot directly run this repository. The application expects
normal CPython, Uvicorn/ASGI, and packages with native runtime requirements, and
it has no Wrangler Worker entry point. Cloudflare Containers are also untested
and are not a supported deployment target.

A Cloudflare Tunnel may instead publish the `/mcp` and OAuth routes from a
Docker container that continues to run on your own PC or home server. Cloudflare
is only the HTTPS ingress in that arrangement. Preserve the exact public origin,
Host header, OAuth discovery paths, and authentication controls, and validate the
complete connection before treating it as production-ready.

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

## Protocol behavior and data quality

Use `dlsite_work_get` with `view="reviews"` for review bodies. A response may
return fewer items than `limit` to keep each review intact inside the MCP byte
budget; follow `page.next_cursor` until `data.complete` is true. There is no
corpus-size cap, so works with tens of thousands of reviews remain traversable.
The cursor also records the last review ID and the initial total to reduce
duplicates or gaps if new reviews arrive during a long traversal. Review text
defaults to 1,200 characters per item and can be raised to 4,000. Cursors expire
after 24 hours by default; `DLSITE_CURSOR_TTL_SECONDS` can extend this to 7 days.

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

All three tools advertise a typed `outputSchema` for their existing
`structuredContent`. It describes the response envelope, pagination, source
metadata, and common work/search/maker fields. Provider extension fields remain
available; fields inside data records can be absent when the byte budget compacts
them. `isError` responses retain their separate error contract. The context audit
includes these schemas within a 10,500-byte tool-list / 4,400-byte per-tool limit.

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

```sh
uv run ruff check .
uv run pytest -q
uv run python scripts/audit_context.py
```

Live provider calls are not required by the test suite. DLsite HTML and public
endpoints can change without notice; provider failures use stable MCP error
codes and never expose request headers or secrets.

## FAQ

### Does this require a DLsite account?

No. It only reads public catalog and review data and does not accept DLsite
credentials.

### Can it download purchased works or access my wishlist?

No. Purchases, downloads, wishlists, carts, login, and DLsite Play are outside
the server's scope.

### Is this an official DLsite API?

No. It is an independent MCP server that reads public DLsite data. Page and
endpoint changes can temporarily affect provider availability.

### Can I deploy it on Cloud Run or Cloudflare Workers?

The supplied container can be run as a constrained single-instance Cloud Run
service as described above. Direct Cloudflare Workers deployment is not
supported; Cloudflare Tunnel is only an optional path to a server running
elsewhere.

## Attribution and license

`dlsite-async` is Copyright (c) 2022 byeonhyeok and distributed under the MIT
License. This project depends on the published package and does not include its
login or DLsite Play modules in the MCP surface.

See `THIRD_PARTY_NOTICES.md` for transport-pattern attribution and service
notices. The project code is available under the [MIT License](LICENSE).
