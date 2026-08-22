# Web security controls

## State-changing request origin policy

Status: implemented in repository/local/CI runtime. Production enablement remains
configuration/inventory work; this document does not claim the live origin value.

Every `POST`, `PUT`, `PATCH` and `DELETE` request is checked centrally before route
dispatch. The request must contain an HTTP(S) `Origin` header or, when `Origin` is
absent, an HTTP(S) `Referer`. An invalid, opaque (`null`), missing or disallowed
source receives HTTP 403 before authentication or mutation logic runs. API failures
contain only a fixed error; no accepted origins, hosts or request values are echoed.

Production must configure the exact public origin or origins:

```env
ARCHIVEDB_ALLOWED_ORIGINS=https://library.example.com
```

Multiple origins are comma-separated. Values must be origins only: HTTP(S) scheme,
host and optional non-default port, without credentials, path, query or fragment.
Invalid configuration fails startup. When the setting is empty, the compatibility
mode compares only the source hostname to the request hostname; this supports local
development and an unreconciled tunnel configuration but is not the intended final
production setting.

Same-origin browser forms and `fetch` requests already send `Origin`. Operational
HTTP clients and smoke tests must set it explicitly. This control complements
`SameSite=Lax`, `HttpOnly` and production `Secure` session cookies; it does not relax
authentication or authorization.

Logout is POST-only. GET requests no longer mutate the session, and both tracked UI
logout controls submit same-origin forms.

## Verification

Pure tests cover normalization, exact allowlisting, missing/opaque sources, hostile
lookalike domains, referer fallback and local hostname mode. Real legacy/SQLite
runtime parity rejects both missing and attacker origins and exercises every normal
workflow with an explicit allowed origin.

## EPUB chapter HTML

Status: implemented in repository/local/CI runtime. This changes the tracked reader
path; production enablement still follows the inventory/reconciliation rollout and
is not claimed here.

Untrusted chapter markup is parsed with Python's `HTMLParser` and reconstructed from
explicit tag, attribute and URL-scheme allowlists. The reader never returns the
original markup directly. Script/style/form/embedded content, SVG and MathML
subtrees, comments, processing instructions, event handlers, inline style and
unknown attributes are removed. `href`, `src` and `cite` allow relative references,
fragments and only `http`, `https` or `mailto` schemes; control-character,
whitespace and character-reference scheme obfuscation is rejected. Existing reader
asset URLs remain relative and allowed.

Malformed active subtrees fail closed: content after an unclosed active tag is not
reintroduced. Forbidden HTML void elements are discarded without swallowing safe
following content. Output is balanced and attribute values/text are escaped. This
is intentionally a content-security boundary, so EPUB inline styles and foreign
namespaces are not preserved.

Unit tests cover normal semantic EPUB markup, reader asset URLs, malformed markup,
dangerous/obfuscated schemes, duplicate attributes, active foreign namespaces and
forbidden void elements. The real legacy/SQLite reader parity workflow also serves
a malicious seeded chapter and asserts the response contains safe content but none
of the executable constructs.

## Upload ownership and stored-path confinement

Status: implemented for repository/local/CI runtime. Package sessions/jobs already
enforced owner identity; this completes the review of the remaining novel-upload
asset surfaces. Production enablement remains part of the reconciled runtime rollout.

- upload records receive a server-generated identifier and the authenticated email
  is stored as owner; clients cannot choose either value;
- pending cover assets return 404 unless requested by that owner or an admin;
- approved covers remain available to authenticated library readers;
- approval and rejection remain admin-only;
- package session mutation/download and job status/cancellation remain owner-only;
- EPUB, cover and extracted-folder paths loaded from mutable metadata are resolved
  through symlinks and must remain within their configured storage root;
- local downloads use the same component-aware confinement rule, eliminating
  sibling-prefix acceptance such as `structured_output_evil`.

Path validation fails closed on empty, malformed, absolute escape, `..`, sibling
prefix, cross-drive and symlink escape values. Invalid metadata is not followed,
served or deleted. This does not rewrite metadata or move user files.

Pure tests cover normal descendants, root handling, traversal, absolute children,
sibling-prefix paths and symlink escapes where the platform permits them. The real
metadata workflow uploads a pending cover as a normal user, verifies owner/admin
access and unrelated-user denial, approves it, then verifies authenticated library
access and complete legacy/SQLite parity.

## Remaining Phase 11 work

- tighten CSP after inline CSS/JS is split;
- inventory-gate origin-network restrictions, secret ownership/rotation and admin
  audit retention.
