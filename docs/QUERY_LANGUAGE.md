# Query Language

ScopiEngine has two ways to ask a question: **ScopiQL**, a compact string
syntax built for typing at a terminal or pasting into a log-search box, and
the **JSON DSL**, a deliberate subset of Elasticsearch's query DSL for
programmatic callers. Both compile to the exact same query AST
(`scopiengine.query.ast`) through the same field-resolution code
(`scopiengine.query.compiler`) — so the shorter syntax is never a
second-class citizen. See the [parity tests](#scopiql--dsl-parity) for the
guarantee this makes concrete.

- [ScopiQL grammar](#scopiql-grammar)
- [Field resolution](#field-resolution-how-a-value-becomes-a-term)
- [Pipeline stages](#pipeline-stages)
- [Worked examples](#worked-examples)
- [JSON DSL compatibility matrix](#json-dsl-compatibility-matrix)
- [Aggregations](#aggregations)
- [`hits.total` and known limitations](#hitstotal-and-known-limitations)

## ScopiQL grammar

```
query        := or_expr ( '|' stage )*
or_expr      := and_expr ( OR and_expr )*
and_expr     := not_expr ( AND? not_expr )*      # AND is implicit between adjacent terms
not_expr     := (NOT | '!') not_expr | primary
primary      := '(' or_expr ')'
              | field ':' '(' or_expr ')'         # values inside share the field
              | field ':' '[' value TO value ']'  # inclusive range
              | field ':' ('>=' | '>' | '<=' | '<') value   # one-sided range
              | field ':' STRING                   # phrase
              | field ':' WORD                     # term, or prefix if WORD ends in '*'
              | '_exists_' ':' field
              | STRING                              # bare phrase, across every text field
              | WORD                                # bare term, across every text field

stage        := 'sort' sort_field (',' sort_field)*
              | 'limit' INTEGER
              | 'fields' field (',' field)*
              | 'stats' 'count' '(' ')' 'by' field
sort_field   := '-'? field                          # leading '-' means descending
```

`AND`/`OR`/`NOT` are recognised in uppercase only (matching the Lucene/ES
`query_string` convention — a field genuinely named `and` still works fine,
since these only act as operators between two expressions, never as a bare
value); `&&`/`||`/`!` are exact equivalents. Two expressions with no operator
between them are an implicit `AND`, so `level:ERROR service:auth` means the
same thing as `level:ERROR AND service:auth`.

A quoted string can escape `"` and `\` with a backslash (`\"`, `\\`).

## Field resolution: how a value becomes a term

Every `field:value` is resolved against the index's mapping before it becomes
part of the query AST:

| Mapped type | What happens |
|---|---|
| `text` | The value is analyzed with the field's search analyzer (falling back to its index-time one). Zero resulting tokens matches nothing; one token becomes a `Term`; more than one becomes an OR of `Term`s (see [`compiler.term_query`](../src/scopiengine/query/compiler.py)). |
| `keyword` | Used verbatim, case-sensitive, no analysis. |
| `long` / `double` | Coerced to a number, then order-preserving-encoded (`scopiengine.mapping.encoding`) so a `Range` bound compares correctly. |
| `boolean` | `"true"`/`"1"` and `"false"`/`"0"` (case-insensitive) coerce to `True`/`False`. |
| `date` | Parsed as ISO-8601 (`scopiengine.mapping.encoding.parse_iso8601_millis`) and encoded the same way as `long`. A bare date like `2026-08-01` is midnight UTC that day. |
| **not mapped** | **Not a parse error.** The query still parses and executes, but matches nothing — every leaf AST node looks the field up by id first and returns no matches when it isn't mapped (`scopiengine.index.searcher._execute`). This is deliberate: a typo'd field name degrades to "no results," the same as any other query with nothing to match, rather than surfacing as a 400. |

`message:conn*` (a `Prefix`) analyzes the text before the `*` for a `text`
field and uses only its *first* resulting token — a multi-word prefix on a
`text` field is out of scope for 1.0. A `keyword` field's prefix is used
verbatim, unanalyzed. Prefix and range queries are unsupported on `boolean`
fields and on `text` fields for range (`UnsupportedFeatureError`, naming the
field and its type).

### `now`, relative to a caller-supplied instant

`now`, `now-1h`, `now+30m` and friends (units `s`/`m`/`h`/`d`/`w`) resolve
against "now" at parse time. The REST API and CLI use the real wall clock;
tests pass a fixed instant (`scopiengine.query.scopiql.parse(..., now=...)`)
so relative-time queries are deterministic.

## Pipeline stages

A ScopiQL query can end in one or more `| stage`s, left to right:

| Stage | Effect |
|---|---|
| `sort <field>` / `sort -<field>` | Reorder by `field` (`-` for descending) instead of relevance. `_score` means "relevance," explicitly. Comma-separates for a multi-key sort. |
| `limit N` | Caps the number of hits returned (overrides the request's `size`). |
| `fields a,b,c` | Projects `_source` down to just those field paths, instead of returning the whole document. |
| `stats count() by <field>` | Buckets every match by `field`'s value, counted — ScopiQL's only aggregation, equivalent to the DSL's `{"terms": {"field": ...}}`. Appears in the response under `aggregations.stats`. |

## Worked examples

```
level:ERROR AND service:auth
status:>=500 AND NOT path:/health
message:"connection refused"                  # phrase
message:conn*                                 # prefix
level:(ERROR OR WARN OR FATAL)
@timestamp:[2026-08-01 TO 2026-08-16]         # inclusive range
@timestamp:>now-1h                            # relative time
_exists_:trace_id
"payment failed"                              # bare phrase across all text fields
level:ERROR AND service:auth | sort -@timestamp | limit 20
service:auth | fields service,status,message | limit 50
status:>=500 | stats count() by service
```

Every one of these is covered directly in `tests/unit/test_scopiql.py`,
asserted against the exact AST it must produce.

### Parse errors name the position and what was expected

A malformed query is never just "invalid syntax":

```
$ scopi search logs 'level:'
error [invalid_query]: ScopiQL parse error at position 6: expected a value,
'(', '[', or a comparison operator, found 'end of input'
```

The underlying `InvalidQueryError.detail` carries the same information
structurally — `{"position": 6, "expected": "...", "found": null}` — for a
caller that wants to point at the offending character programmatically (an
editor integration, for instance).

## JSON DSL compatibility matrix

POST a JSON body to `_search` (or use `?q=` for ScopiQL against the same
endpoint) to use the DSL instead. Every clause and top-level option not
listed as supported below raises `UnsupportedFeatureError` (HTTP 400) naming
the exact key — a misspelled or unimplemented option is never silently
ignored, so a query can never quietly return the wrong results.

### Leaf queries

| Clause | Supported | Notes / use instead |
|---|---|---|
| `match_all` | ✅ | |
| `match` | ✅ | Shorthand `{"field": "value"}` or extended `{"field": {"query": "value", "boost": 2}}`. Compiles through the same field resolution as ScopiQL's bare `field:value`. |
| `match_phrase` | ✅ | |
| `term` | ✅ | |
| `terms` | ✅ | Compiles to an OR of `term`. |
| `prefix` | ✅ | Same first-token-only limitation on `text` fields as ScopiQL. |
| `range` | ✅ | `gte`/`gt`/`lte`/`lt`, plus `boost`. |
| `exists` | ✅ | |
| `bool` | ✅ | `must`/`should`/`must_not`/`filter`, plus `boost`. A single clause need not be wrapped in a list. |
| `match_bool_prefix`, `multi_match`, `query_string`, `simple_query_string`, `wildcard`, `regexp`, `fuzzy`, `span_*`, `nested`, `has_child`/`has_parent`, `geo_*`, `script`, `function_score`, `dis_max`, `constant_score`, `intervals` | ❌ | Use `bool` + `term`/`match`/`range`/`prefix` to express the same intent, or ScopiQL. `constant_score` — use `bool.filter` (filter clauses already contribute no score). |

### Top-level options

| Option | Supported | Notes |
|---|---|---|
| `query` | ✅ | Defaults to `match_all` if omitted. |
| `from` / `size` | ✅ | |
| `sort` | ✅ | String shorthand (`"field"`, `"-field"`), `{"field": "asc"|"desc"}`, or `{"field": {"order": "asc"|"desc"}}`. A genuine index-wide sort, bounded by `max_sort_candidates` — see [below](#sort-is-a-real-index-wide-sort). |
| `_source` | ✅ | `true`/`false`, a field name, or a list of field names. |
| `aggs` / `aggregations` | ✅ (partial) | Only `terms` (see [Aggregations](#aggregations)). Specifying both `aggs` and `aggregations` in the same request is rejected. |
| `min_score`, `track_total_hits`, `explain`, `highlight`, `script_fields`, `post_filter`, `collapse`, `search_after`, `pit`, `runtime_mappings`, `stored_fields`, `docvalue_fields`, `rescore`, `suggest`, `_name`/`indices_boost` | ❌ | Not implemented for 1.0. `track_total_hits` in particular is unnecessary here — `hits.total.value` is always the true, uncapped match count (see below), never an approximation you'd need to opt into. |

## Aggregations

Both languages support exactly one aggregation shape: bucket-by-field,
counted — ScopiQL's `stats count() by <field>` and the DSL's
`{"aggs": {"<name>": {"terms": {"field": "...", "size": 10}}}}`. The response
shape matches Elasticsearch's `terms` aggregation:

```json
{
  "aggregations": {
    "by_service": {
      "doc_count_error_upper_bound": 0,
      "sum_other_doc_count": 3,
      "buckets": [
        {"key": "auth", "doc_count": 12},
        {"key": "billing", "doc_count": 7}
      ]
    }
  }
}
```

`doc_count_error_upper_bound` is always `0`: every match is actually counted
(`scopiengine.index.searcher.iter_matches` streams every match, not a
sample), never approximated the way a sharded engine sometimes must. Metric
aggregations (`avg`, `sum`, `min`, `max`, `cardinality`, `percentiles`, ...),
`date_histogram`, and nested/pipeline aggregations are not implemented —
`aggs`/`aggregations` naming anything but `terms` raises
`UnsupportedFeatureError` naming the aggregation.

## `hits.total` and known limitations

**`hits.total.value` is always the true number of matching documents**, not
the page length and never approximated — `scopiengine.index.searcher.search`
counts every match while it streams past to fill the bounded top-N heap, so
the count costs nothing beyond what ranking already does. There is no
`track_total_hits` knob because there is nothing it would need to opt into.
This stays true even when a field sort has to truncate (below) — `total`
never reflects the cap.

### `sort` is a real index-wide sort

`level:ERROR | sort -@timestamp | limit 20` — "the 20 most recent errors" —
is the headline use case this engine exists to serve, and it means what it
says: the *genuinely* 20 most recent matching documents, not the 20
highest-relevance matches reordered among themselves. For a filter-shaped
query like `level:ERROR`, BM25 relevance is close to meaningless (most
matches score near-identically), so a page-local "rank by relevance first,
then sort that page" would silently return an arbitrary 20 — correctly
ordered, but the wrong 20 entirely. ScopiQL and the DSL both avoid that: a
sort naming any real field (anything other than a plain, descending
`_score`) streams every match, extracts each candidate's sort key from its
stored source, and keeps a bounded record of the best `from_ + size`
candidates seen — so the page returned is the true index-wide top-N, and
memory scales with the page size, never with how many documents match. A
pure `_score` sort (the default, when there is no `sort` at all) needs none
of this — relevance is already computed while matching, so it keeps the
original single-pass path.

That per-query candidate scan is bounded by
[`max_sort_candidates`](INSTALL.md) (default `10000`, configurable via
`SCOPI_MAX_SORT_CANDIDATES`) — past that many matches, a field sort is no
longer guaranteed to be the exact index-wide top-N. When it has to stop
early, the response says so rather than returning a truncated result that
looks identical to a complete one:

```json
{
  "scopi": {
    "sort_truncated": true,
    "max_sort_candidates": 10000
  }
}
```

`hits.total.value` still reports the true, complete match count in this
case — counting continues past the cap, it is only the per-candidate sort
key lookup that stops — and a warning naming `max_sort_candidates` is logged
server-side. If a query's field sort routinely needs to consider more than
`max_sort_candidates` matches to find the true top-N, narrow it with a
filter first (a `@timestamp` range, a tighter `level`/`service` match)
rather than relying solely on raising the setting.

Multiple sort keys (`sort -status,service` / `"sort": ["-status", "service"]`)
break ties left-to-right; a document missing a given sort field always sorts
after every document that has it, in both ascending and descending order.

Known, deliberate limitations for 1.0:

- **A `text`/`keyword` prefix on `text` fields only uses the first analyzed
  token.**
- **Field resolution against an unmapped field never errors** (see the table
  above) — this is a deliberate design choice, not an oversight, but it does
  mean a genuine typo in a field name looks identical to "no results" rather
  than a 400.

## ScopiQL / DSL parity

`tests/integration/test_api.py::test_scopiql_and_dsl_parity` runs a table of
equivalent `(ScopiQL, DSL)` pairs against the same index and asserts they
return identical hit ids **and** identical scores, in order — not just the
same document set. That test is the actual guarantee behind everything
above; treat it as the source of truth if this document and the code ever
disagree.
