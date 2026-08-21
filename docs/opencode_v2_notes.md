# OpenCode V2 (`opencode2`) — auth & compatibility notes

> Source: external model briefing (2026-08-21). Captured here for future
> integration work. NOT yet integrated into Pact's `OpenCodeServerBackend`.

## Why V1 backend got `401` against `opencode2 serve`

V2 changed the auth scheme. Key facts:

- V2 **always requires a password**. If not set via `OPENCODE_PASSWORD`
  (new) or `OPENCODE_SERVER_PASSWORD` (legacy), it generates a random
  32-byte password and prints it to console on startup.
- Username is hardcoded to: `opencode`
- Scheme: **HTTP Basic Auth**
  `Authorization: Basic base64("opencode:<password>")`
- A dummy `opencode:opencode` does NOT work — the real server password
  is required.

### Set a stable password for local testing
```powershell
$env:OPENCODE_PASSWORD="test123"
opencode2 serve --port 4097
```
Both `OPENCODE_PASSWORD` and legacy `OPENCODE_SERVER_PASSWORD` are honoured.

### Verify manually
```powershell
$token = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes("opencode:test123"))
curl.exe -i -H "Authorization: Basic $token" http://127.0.0.1:4097/openapi.json
```
Should return non-401.

### Undocumented: query-param token
V2 middleware also accepts `?auth_token=<base64(username:password)>`,
e.g. `http://127.0.0.1:4097/openapi.json?auth_token=b3BlbmNvZGU6dGVzdDEyMw==`
(base64 of `opencode:test123`). Requires the backend to append this query
param to every request — not currently done by Pact.

## No `--no-auth` / `--insecure` flag in V2
`opencode2 serve` only supports: `--hostname`, `--port`, `--service`,
`--stdio`. Empty `OPENCODE_PASSWORD=""` does NOT disable auth (regenerates).

## Reverse-proxy workaround (localhost only)
Run V2, then a local proxy (e.g. Caddy) on 4096 that injects the
Authorization header, so the V1 backend sees an unauthenticated endpoint:

```caddy
127.0.0.1:4096 {
    reverse_proxy 127.0.0.1:4097 {
        header_up Authorization "Basic b3BlbmNvZGU6dGVzdDEyMw=="
    }
}
```
Point Pact backend at `http://127.0.0.1:4096`.

## BIGGER CAVEAT — API incompatibility
Auth is NOT the only difference. V2 is a **completely new server/API stack**:
- V1 backend expects the V1 server API.
- V2 server provides the V2 API.
After fixing 401, expect further mismatches: `404`, unknown endpoints,
different JSON schema, different event/SSE protocol.

**Conclusion:** a reverse proxy solves ONLY authentication, not V1↔V2 API
compatibility. Before pursuing V2 as a drop-in backend, compare the V1
(1.18.20) and V2 OpenAPI specs to assess compatibility realism.

## Related fix (V1, DONE experimentally 2026-08-21)
V1 `opencode serve` hard-caps output at ~32k tokens (ignores our
`max_completion_tokens`). Workaround:
```bash
export OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=1048576
```
Experiment on chapter 0026 (opencode-go/muse, whole-chapter): with the env
var, generation reached output 14543 + reasoning 26971 = 41514 tokens,
`finish_reason=stop` (no 32k cap). See backend fix in
`opencode_server_lifecycle.py` (managed serve now sets this env).
