# Design

## Context

See proposal.md for motivation. The facts from the current code that shape the approach:

- `routing._lines` is a line-oriented tokenizer: it yields the tokens of each line, which of them were quoted,
  and the brace depth *before* the line. It already ignores comments and heredoc bodies, refuses an
  unterminated quote, and refuses an unbalanced block.
- `_Inspection.scan` walks every line and checks every token with `_addresses`, which strips a scheme, a
  trailing port, and a path. It has no notion of a token's position, so a directive argument is checked
  exactly like a site address.
- `_Inspection.scan` also returns `import` arguments, which `RouteManager._scan_hostnames` follows. Imports
  are currently collected at any depth.
- `_Inspection.own_import_lines` finds the managed import; it is independent of the address scan and is not
  changed by this design.
- `render_route` writes exactly one form: `domain {` / `reverse_proxy 127.0.0.1:<port>` / `}`.
- Every module the server runs must be stdlib-only; `routing.py` already is.

## Goals / Non-Goals

**Goals:**

- Compare the registered hostname only against tokens Caddy reads as a site address.
- Keep every conflict the current scan finds for a genuine site address, including a one-line block, an
  address list spread over lines, a quoted address, a scheme, a port, a trailing dot, and a wildcard.
- Keep the refusal for a configuration whose addresses cannot be determined.
- Change nothing about the managed import, the route file format, or the transaction.

**Non-Goals:**

- Detecting a `host` request matcher as a claim on a hostname. It is not a site address, and Caddy still
  refuses a genuine duplicate site address at validation time.
- Inspecting the body of a snippet or named-route definition as if it were a site block. Snippets are
  documented as sets of directives; treating their bodies as address lists is the bug this change removes.
- Replacing the Caddyfile parser. The scan is a conservative reading of the grammar, and Caddy validation
  remains the backstop.

## Decisions

**An address is a token on a depth-0 line, before the opening brace.** Caddy's `addresses()` reads tokens at
the top level until it sees `{`; everything after the brace, and everything inside a block, is a directive or
a subdirective. The tokenizer already reports the depth before each line, so a site block header is the only
depth-0 line that can declare addresses. This is the whole fix: the position decides, not the token's shape.

**Use the tokenizer's quoted flag instead of comparing two token lists.** `_lines` currently yields the tokens
and a second list holding only the unquoted ones. Reconstructing which token was quoted from two lists is
ambiguous when a quoted token equals the following unquoted one, so the tokenizer now yields a parallel
`quoted` flag per token. The three call sites in this module are updated; the public behaviour of the
tokenizer is unchanged.

**Stop an address line at its opening brace, and refuse a brace inside an address.** A one-line site block
(`example.com { respond ok }`) declares `example.com` and then directives. A token containing `{` or `}` in
address position is an environment-variable placeholder (`{$DOMAIN}`) or a missing space (`example.com{`),
neither of which can be classified; it is refused as uninspectable. This replaces the old blanket check on
every depth-0 token, which refused a placeholder in a directive argument too.

**Treat a top-level address line without an opening brace as a single brace-less site.** Caddy allows the
braces to be omitted when the Caddyfile holds exactly one site block: the first top-level line is the address
list and the rest of the file is its body. A line that ends with a comma continues the address list, which is
how Caddy allows the list to span lines. So the scanner tracks three states per file: reading addresses,
reading a continued address list, and reading a brace-less body (where nothing is an address). Without this,
the brace-less form would still be read as one address per token, which is the reported bug.

**Follow an import only where a site block can appear.** A top-level `import` substitutes tokens at the top
level, so its file can declare sites and is followed as today. An `import` inside a block substitutes
directives into that block and can never declare a site address, so following it would scan a directive
fragment as a Caddyfile - the same false conflict in another form. Snippet names are already skipped by
`_scan_hostnames`.

**A snippet or named-route definition is not a site.** `(name) {` and `&(name) {` at the top level are
definition names, not addresses. Their bodies are not scanned for addresses, because a snippet body is a set
of directives. A site block hidden inside a snippet and imported at the top level is therefore not inspected
as an address; Caddy still refuses a genuine duplicate address, and the scan never wrote such a form.

**Keep the wildcard overlap check on the same token set.** It has the same bug - a wildcard upstream such as
`reverse_proxy https://*.example.com` was read as a wildcard site - and the same fix applies.

## Risks / Trade-offs

- [A site declared inside a snippet imported at the top level is no longer inspected as an address] -> The
  documentation describes snippets as directive sets; Caddy validation still refuses a genuine duplicate
  address, and the existing import follower already skips snippet names. The scan's unique value (catching
  spellings Caddy accepts, such as a different port) is unaffected for every form Shiplord or an operator
  writes as a site block.
- [A `host` matcher inside another site no longer blocks the hostname] -> It is not a site address and cannot
  take over the site; Caddy refuses a real duplicate address. This is the behaviour the issue asks for.
- [The brace-less state machine could stop scanning too early] -> It only starts when a top-level address line
  ends without a brace and without a trailing comma, which is exactly where Caddy ends its address list; the
  rules are taken from Caddy's own `addresses()` parser and are covered by unit tests.
- [A tokenizer change touches the managed-import detection] -> `own_import_lines` compares the token list and
  the quoted flags instead of two token lists, and the existing import tests (duplicate, nested, quoted) cover
  it.

## Migration Plan

No migration. The route file format, the managed import, the transaction, the error kinds, and the exit codes
are unchanged. Rollback is reverting `routing.py`.

## Open Questions

None. The real-Caddy evidence was run before the pull request: `tests/linux/run.sh caddy` passed 7/7,
including the new upstream-and-header case, which failed against the previous `routing.py`.
