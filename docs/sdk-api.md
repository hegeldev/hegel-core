# Hegel SDK API Specification

This document is the complete specification for implementing Hegel SDKs. It
covers the public API, protocol, design guidance, and implementation checklist.

## Overview

A Hegel SDK enables property-based testing from any language by communicating
with the Hegel server via Unix socket. The SDK provides:

1. **Generator types** that produce random values according to constraints
2. **Combinators** for composing generators into complex structures
3. **Socket communication** to request values from Hypothesis's generation engine
4. **Schema composition** for efficient single-request generation

### Core Principles

- **Schema composition preferred**: When possible, compose schemas and make a
  single socket request for the entire structure.
- **Compositional fallback**: When schemas are unavailable (after `map`/`filter`
  on non-basic generators, or `flat_map`), generate structurally with multiple
  requests.
- **Idiomatic APIs**: Match the target language's conventions for generics,
  builders, and error handling.
- **Minimal overhead**: Reuse connections, batch requests via schema composition.

## Core Concepts

### Generators

A **generator** is the central abstraction in Hegel. A generator of type `T`
can produce random values of that type through its `generate()` method.
Generators are called inside a Hegel test body, which is run many times by
the test engine.

Every generator has an optional **schema** — a JSON/CBOR dictionary that
describes the values it produces. When a generator has a schema, it can
send a single request to the Hegel server to generate a value.
When a generator has no schema, it falls back to **compositional generation**:
making multiple requests to the server, wrapped in spans for structure tracking.

Schema-based generation is preferred because:
1. It reduces round-trips between the SDK and the server.
2. It gives the server (Hypothesis) a complete picture of the data structure,
   enabling better shrinking.

### Basic Generators

A **basic generator** is a generator that always has a schema and optionally
applies a client-side transform to the server-generated value. The key insight
is that `map()` on a basic generator preserves the schema by composing the
transform function, rather than losing it.

A basic generator consists of:
- A **raw schema**: the JSON/CBOR schema sent to the server.
- An optional **transform**: a function `Any -> T` applied to the raw value
  returned by the server. When the transform is absent (identity), the server
  value is used directly.

The `generate()` method of a basic generator:
1. Sends the raw schema to the server.
2. Receives a raw value.
3. If a transform is present, applies it to the raw value.
4. Returns the result.

The `schema()` method always returns the raw schema.

**In Python**, `BasicGenerator` is a subclass of `Generator` (is-a relationship).

**In Rust**, `BasicGenerator<T>` is a separate struct with a `schema` field
and an `Option<Arc<dyn Fn(Value) -> T>>` transform. The `Generate<T>` trait
defines `as_basic() -> Option<BasicGenerator<T>>` (default: `None`).
Combinators inspect `as_basic()` to determine whether schema-based generation
is possible and to compose transforms.

**In C++**, `BasicGenerator<T>` is a separate class with a JSON schema and
`std::optional<std::function<T(nlohmann::json&)>>` transform. `IGenerator<T>`
defines `as_basic()` returning `std::optional<BasicGenerator<T>>` (default:
`std::nullopt`). Combinators inspect `as_basic()` to compose schemas and
transforms.

### When is a generator basic?

A generator is basic (i.e. has a schema and optional transform) in these cases:

| Generator | Basic? | Schema | Transform |
|-----------|--------|--------|-----------|
| `integers(...)` | Always | `{"type": "integer", ...}` | None (identity) |
| `floats(...)` | Always | `{"type": "number", ...}` | None |
| `booleans(...)` | Always | `{"type": "boolean", ...}` | None |
| `text(...)` | Always | `{"type": "string", ...}` | None |
| `binary(...)` | Always | `{"type": "binary", ...}` | None |
| `just(value)` | Always | `{"const": <value>}` or `{"const": null}` | Returns the constant value, ignoring server input |
| `sampled_from(values)` | Always | `{"type": "integer", "min_value": 0, "max_value": len-1}` | Returns `values[index]` |
| `basic.map(f)` | Always | Same schema as `basic` | `f` composed with existing transform |
| `nonbasic.map(f)` | Never | — | — |
| `gen.flat_map(f)` | Never | — | — |
| `gen.filter(pred)` | Never | — | — |
| `lists(elements)` | If `elements` is basic | `{"type": "list", "elements": ..., ...}` | Applies element transform to each item (if any) |
| `tuples(e1, e2, ...)` | If ALL elements are basic | `{"type": "tuple", "elements": [...]}` | Applies each element's transform (if any) |
| `dicts(keys, values)` | If both are basic | `{"type": "dict", "keys": ..., "values": ..., ...}` | Applies key/value transforms, converts pairs to dict |
| `one_of(g1, g2, ...)` | If ALL branches are basic | `{"one_of": [...]}` | See below |
| `optional(element)` | If `element` is basic | Via `one_of(just(None), element)` | Via one_of |
| Format generators (emails, urls, etc.) | Always | `{"type": "email"}` etc. | None |
| `from_regex(pattern)` | Always | `{"type": "regex", ...}` | None |

### Non-basic fallback

When a generator is not basic, it uses compositional generation:
1. Opens a span with an appropriate label.
2. Makes individual `generate` requests for each sub-value.
3. Closes the span.

This produces correct values but with more round-trips and potentially
worse shrinking.

## Generators

### `integers`

Generate integer values.

**Parameters:**
- `min_value` (optional): Minimum value (inclusive). Default depends on the
  language: in Python, unbounded; in Rust/C++, the type's minimum.
- `max_value` (optional): Maximum value (inclusive). Default depends on the
  language: in Python, unbounded; in Rust/C++, the type's maximum.

**Schema:**
```json
{"type": "integer", "min_value": <min>, "max_value": <max>}
```

`min_value` and `max_value` are omitted when no bound is specified (Python only;
typed languages always include bounds derived from the integer type).

**Basic:** Always. No transform.

### `floats`

Generate floating-point numbers.

**Parameters:**
- `min_value` (optional): Minimum value.
- `max_value` (optional): Maximum value.
- `allow_nan` (bool): Whether NaN can be generated. Default: `false` in Python,
  `true` in Rust/C++.
- `allow_infinity` (bool): Whether infinity can be generated. Default: `false`
  in Python, `true` in Rust/C++.
- `exclude_min` (bool): Exclude the minimum from the range. Default: `false`.
- `exclude_max` (bool): Exclude the maximum from the range. Default: `false`.
- `width` (int): Bit width (32 or 64). Default: 64 in Python, inferred from
  type in Rust/C++.

**Schema:**
```json
{
  "type": "number",
  "min_value": <min>,
  "max_value": <max>,
  "exclude_min": <bool>,
  "exclude_max": <bool>,
  "allow_nan": <bool>,
  "allow_infinity": <bool>,
  "width": <int>
}
```

**Basic:** Always. No transform.

### `booleans`

Generate boolean values.

**Parameters:**
- `p` (float, optional): Probability of `true`. Default: 0.5. (Python only;
  Rust and C++ currently use uniform probability.)

**Schema:**
```json
{"type": "boolean", "p": <float>}
```

or simply `{"type": "boolean"}` when `p` is not specified.

**Basic:** Always. No transform.

### `text`

Generate Unicode text strings.

**Parameters:**
- `min_size` (int): Minimum string length. Default: 0.
- `max_size` (int, optional): Maximum string length. Default: no limit.

**Schema:**
```json
{"type": "string", "min_size": <int>, "max_size": <int>}
```

`max_size` is omitted when unspecified.

**Basic:** Always. No transform.

### `binary`

Generate binary data. The server returns CBOR byte strings directly;
SDKs receive raw bytes with no additional decoding needed.

**Parameters:**
- `min_size` (int): Minimum size in bytes. Default: 0.
- `max_size` (int, optional): Maximum size in bytes. Default: no limit.

**Schema:**
```json
{"type": "binary", "min_size": <int>, "max_size": <int>}
```

**Basic:** Always. No transform needed in any SDK — CBOR byte strings
are deserialized directly to the language's native byte type (`bytes` in
Python, `Vec<u8>` in Rust, `json::binary_t` in C++).

### `just`

Generate a constant value.

**Parameters:**
- `value`: The constant value to always return.

**Schema:** `{"const": null}` (Python) or `{"const": <value>}` (Rust/C++
for serializable types).

In Python, the schema always uses `null` as the const value (the server
generates a null, and the client-side transform returns the actual constant).
This allows `just` to work with non-JSON-serializable values.

In Rust, `just` requires `Serialize` and uses `{"const": <serialized_value>}`.
There is also `just_any` for non-serializable types, which has no schema
and is not basic.

In C++, `just` uses `{"const": <value>}` for primitive types (bool, int,
float, string) and falls back to a non-basic function-backed generator for
other types.

**Basic:** Always (for serializable/primitive types). Transform: `_ -> value`
(ignores server input).

### `sampled_from`

Sample uniformly from a fixed list of values.

**Parameters:**
- `values`: A non-empty list of values.

**Schema:**
```json
{"type": "integer", "min_value": 0, "max_value": <len - 1>}
```

The server generates an index; the client-side transform returns
`values[index]`.

**Basic:** Always. Transform: `index -> values[index]`.

Note: In Rust, `sampled_from` (owned) uses the index approach for
CBOR-primitive types and is non-basic for complex types.
`sampled_from_slice` (borrowed) uses a `{"sampled_from": [values...]}`
schema for all types. In C++, `sampled_from` uses `{"sampled_from": [...]}`
for primitive types and the index approach for complex types. The index
approach is the canonical one used by the reference Python implementation.

### `from_regex`

Generate strings matching a regular expression pattern.

**Parameters:**
- `pattern` (string): The regex pattern.
- `fullmatch` (bool): Whether the entire string must match. Default: `false`.

**Schema:**
```json
{"type": "regex", "pattern": <string>, "fullmatch": <bool>}
```

**Basic:** Always. No transform.

### Format Generators

These generators produce formatted strings.

| Generator | Schema |
|-----------|--------|
| `emails()` | `{"type": "email"}` |
| `urls()` | `{"type": "url"}` |
| `domains()` | `{"type": "domain", "max_length": <int>}` |
| `ip_addresses()` | `{"type": "ipv4"}`, `{"type": "ipv6"}`, or `{"one_of": [{"type": "ipv4"}, {"type": "ipv6"}]}` |
| `dates()` | `{"type": "date"}` |
| `times()` | `{"type": "time"}` |
| `datetimes()` | `{"type": "datetime"}` |

**Basic:** Always. No transform.

## Combinators

### `map`

Transform the output of a generator with a function.

```
gen.map(f) -> Generator
```

**On a basic generator:** Returns a new basic generator with the same raw
schema and a composed transform. If the original transform was `t`, the new
transform is `x -> f(t(x))`. If there was no transform, the new transform
is `f`.

**On a non-basic generator:** Returns a non-basic generator that wraps the
source generator and applies `f` to each generated value.
Generation is wrapped in a span with label `MAPPED`.

This is the key innovation of basic generators: `map()` preserves the
schema, allowing optimized single-request generation even after
transformations.

### `flat_map`

Dependent generation where the output of one generator determines the next.

```
gen.flat_map(f) -> Generator
```

where `f` takes a value and returns a generator.

**Always non-basic.** The resulting generator has no schema because the
second generator depends on a runtime value. Generation is wrapped in a
span with label `FLAT_MAP`.

### `filter`

Filter generated values by a predicate.

```
gen.filter(predicate) -> Generator
```

**Always non-basic.** The resulting generator has no schema because
predicates cannot be expressed in the schema language.

Tries up to 3 times to generate a value satisfying the predicate. If all
3 attempts fail, calls `assume(false)` to reject the test case.

Each attempt is wrapped in a span with label `FILTER`. Successful attempts
close the span normally; failed attempts close with `discard=true`.

### `lists` / `vecs` / `vectors`

Generate lists (or vectors) of elements.

**Parameters:**
- `elements`: A generator for list elements.
- `min_size` (int): Minimum list length. Default: 0.
- `max_size` (int, optional): Maximum list length. Default: no limit.

**When elements is basic:**

Returns a basic generator.

Schema:
```json
{"type": "list", "elements": <element_raw_schema>, "min_size": <int>, "max_size": <int>}
```

Transform: If the element generator has a transform `t`, the list transform
applies `t` to each element of the raw list. If there is no element transform,
there is no list transform (identity).

**When elements is not basic:**

Falls back to compositional generation using a server-managed collection
(in Python and Rust) or a generated length (in C++). The collection protocol
uses `new_collection` / `collection_more` / `collection_reject` commands.
Generation is wrapped in a span with label `LIST`.

### `tuples`

Generate tuples (fixed-length heterogeneous sequences).

**Parameters:**
- `elements`: Two or more generators, one per tuple position.

**When ALL elements are basic:**

Returns a basic generator.

Schema:
```json
{"type": "tuple", "elements": [<schema1>, <schema2>, ...]}
```

Transform: If any element has a non-identity transform, the tuple transform
applies each element's transform to the corresponding position. If all
elements have identity transforms, there is no tuple transform.

**When any element is not basic:**

Falls back to compositional generation: generates each element individually.
Wrapped in a span with label `TUPLE`.

### `dicts` / `hashmaps` / `dictionaries`

Generate dictionaries (maps).

**Parameters:**
- `keys`: Generator for keys.
- `values`: Generator for values.
- `min_size` (int): Minimum number of entries. Default: 0.
- `max_size` (int, optional): Maximum number of entries.

**When both keys and values are basic:**

Returns a basic generator.

Schema:
```json
{"type": "dict", "keys": <key_schema>, "values": <value_schema>, "min_size": <int>, "max_size": <int>}
```

The server returns a list of `[key, value]` pairs. The transform applies
key and value transforms to each pair, then converts to a dictionary.
Even when both key and value transforms are identity, a transform is
needed to convert the list of pairs into a dictionary.

**When either is not basic:**

Falls back to compositional generation. Wrapped in a span with label `MAP`.

### `one_of`

Choose from one of several generators.

**Parameters:**
- `generators`: Two or more generators.

**When ALL generators are basic with no transforms (identity):**

Returns a basic generator with a simple one_of schema.

Schema:
```json
{"one_of": [<schema1>, <schema2>, ...]}
```

No transform needed — the server directly generates a value from one of the
schemas.

**When ALL generators are basic but some have transforms:**

Returns a basic generator using **tagged tuples**. Each branch becomes a
tagged tuple `[tag, value]` where `tag` is a constant integer identifying
the branch.

Schema:
```json
{
  "one_of": [
    {"type": "tuple", "elements": [{"const": 0}, <schema1>]},
    {"type": "tuple", "elements": [{"const": 1}, <schema2>]},
    ...
  ]
}
```

Transform: Reads the tag to determine which branch was selected, then applies
that branch's transform to the value.

**When any generator is not basic:**

Falls back to compositional generation: generates an index, then delegates
to the selected generator. Wrapped in a span with label `ONE_OF`.

### `optional` / `optional_`

Generate an optional value (either a value or null/None/nullopt).

```
optional(element)
```

Implemented as `one_of(just(null), element)`. Inherits the basicness rules
from `one_of`.

### `builds`

Construct objects from generated field values.

```
builds(type, generators...)
```

Used in static languages to construct typed objects from individually
generated fields. See "Object/Struct Generation" under Design Considerations
for patterns.

### Dynamic Language Only: `fixed_dictionaries`

```
fixed_dictionaries({"key": gen, ...})
```

Generate a dictionary with fixed keys and generated values. Essential for
dynamic languages to generate struct-like data without actual struct types.

## Span Labels

Spans are used to group related generation calls, helping the test engine
understand data structure for effective shrinking. Each span type has a
numeric label:

| Label | Value | Used By |
|-------|-------|---------|
| `LIST` | 1 | List/vector compositional generation |
| `LIST_ELEMENT` | 2 | Individual list elements |
| `SET` | 3 | Set compositional generation |
| `SET_ELEMENT` | 4 | Individual set elements |
| `MAP` | 5 | Dictionary compositional generation |
| `MAP_ENTRY` | 6 | Individual dict entries |
| `TUPLE` | 7 | Tuple compositional generation |
| `ONE_OF` | 8 | One-of compositional generation |
| `OPTIONAL` | 9 | Optional compositional generation |
| `FIXED_DICT` | 10 | Fixed-key dict compositional generation |
| `FLAT_MAP` | 11 | flat_map combinator |
| `FILTER` | 12 | filter combinator |
| `MAPPED` | 13 | map combinator on non-basic generators |
| `SAMPLED_FROM` | 14 | sampled_from compositional fallback |
| `ENUM_VARIANT` | 15 | Rust derive macro enum variant generation |

## Test Control Functions

### `assume(condition)`

Reject the current test case if `condition` is false. This tells the engine
that this particular combination of inputs is not interesting, without
counting it as a test failure.

### `note(message)`

Print a message, but only on the final (shrunk) run. Useful for debugging
counterexamples.

### `target(value, label)`

Guide the test engine toward higher values of a numeric metric. The engine
will try to maximize the target value, which can help find edge cases.

## Protocol

SDKs communicate with the Hegel server (hegeld) via a binary protocol over
Unix domain sockets.

### Connection Lifecycle

1. SDK creates a socket path.
2. SDK spawns hegeld with that socket path.
3. hegeld binds to the socket and listens.
4. SDK connects to hegeld's socket as a client.
5. A single persistent connection is maintained per program run.
6. Multiple tests run over the same connection.

### Packet Format

Each packet has a 20-byte header:
- Magic: `0x4845474C` ("HEGL")
- CRC32 checksum of the payload
- Channel ID
- Message ID (with reply bit at `1 << 31`)
- Payload length

Followed by a CBOR-encoded payload and a terminator byte (`0x0A`).

### Channel Multiplexing

- Channel 0: Control channel (handshake, `run_test`)
- Odd-numbered channels: Created by the SDK for test communication

### Key Commands

| Command | Direction | Description |
|---------|-----------|-------------|
| `run_test` | SDK -> Server | Start a property test |
| `generate` | SDK -> Server | Generate a value from a schema |
| `start_span` | SDK -> Server | Begin a span group |
| `stop_span` | SDK -> Server | End a span group |
| `mark_complete` | SDK -> Server | Report test case outcome |
| `new_collection` | SDK -> Server | Create a server-managed collection |
| `collection_more` | SDK -> Server | Ask if more elements needed |
| `collection_reject` | SDK -> Server | Reject last element |
| `target` | SDK -> Server | Report optimization target |

### Events

| Event | Direction | Description |
|-------|-----------|-------------|
| `test_case` | Server -> SDK | Run a test case |
| `test_done` | Server -> SDK | All test cases complete, results available |

### Important Notes

- **Request ID matching**: Always verify response ID matches request ID.
- **additionalProperties**: Server automatically adds `"additionalProperties": false` to object schemas — SDKs should NOT add this.

## Error Handling

### The `assume()` Function

Every SDK must implement `assume(condition)`:

- If condition is true: returns normally.
- If condition is false: throws an exception (C++) or panics with a special
  marker (Rust). The test runner catches this and reports the test case as
  INVALID to the server via `mark_complete`.

**Purpose:** Signal that the current test case is invalid (not a failure).
Hegel will try different inputs.

### Error Categories

| Error Type | Action |
|------------|--------|
| JSON parse error | `assume(false)` |
| Server returns error | `assume(false)` |
| Filter exhaustion | `assume(false)` |

### Filter Implementation

Filters try up to 3 times, using spans for proper shrinking:

```rust
fn generate(&self) -> T {
    for _ in 0..3 {
        if let Some(value) = discardable_group(labels::FILTER, || {
            let value = self.source.generate();
            if (self.predicate)(&value) {
                Some(value)
            } else {
                None  // Triggers span discard
            }
        }) {
            return value;
        }
    }
    assume(false);
    unreachable!()
}
```

### Rejection Mechanism

Each language uses its idiomatic unwinding mechanism for `assume(false)`:

```cpp
// C++ - throw a special exception caught by hegel()
class HegelReject : public std::exception {
public:
    const char* what() const noexcept override { return "assume failed"; }
};

void assume(bool condition) {
    if (!condition) {
        throw HegelReject();
    }
}
```

```rust
// Rust - panic with special marker caught by catch_unwind
pub fn assume(condition: bool) {
    if !condition {
        panic!("HEGEL_REJECT");
    }
}
```

The test runner catches these and reports status INVALID to the server via
`mark_complete`.

## Design Considerations

### Language-Specific Idioms

Before implementing, understand the target language's patterns for:

| Concept | Questions to Answer |
|---------|---------------------|
| **Generics** | Templates? Type parameters? Protocols? |
| **Higher-order functions** | Closures? Lambdas? Function types? |
| **Option types** | `Option<T>`? `Maybe`? `null`? `nil`? |
| **Object construction** | Constructors? Builders? Factory functions? |
| **Collections** | List vs Vector vs Array naming? Set types? |
| **Error handling** | Exceptions? Result types? Error codes? |

### API Style Patterns

#### Builder Pattern (Rust style)
```rust
integers::<i32>()
    .with_min(0)
    .with_max(100)
    .generate()
```

#### Parameter Structs (C++ style)
```cpp
integers<int>({.min_value = 0, .max_value = 100})
```

#### Keyword Arguments (Python style)
```python
integers(min_value=0, max_value=100)
```

#### Fluent Interface (Java/Kotlin style)
```kotlin
Generators.integers()
    .between(0, 100)
    .generate()
```

### Type Parameter Conventions

**Static languages with bounds inference:**
```rust
integers::<u8>()  // Automatically min=0, max=255
integers::<i32>().with_min(-1000).with_max(1000)  // Override defaults
```

**Dynamic languages:**
```python
integers()  # Arbitrary precision
integers(min_value=-128, max_value=127)  # Explicit bounds
```

### Naming Conventions

| Concept | Examples by Language Style |
|---------|---------------------------|
| Lists | `vectors` (C++), `vecs` (Rust), `lists` (Python), `arrays` (JS) |
| Maps | `dictionaries` (Python), `hashmaps` (Rust), `maps` (Java) |
| Optional | `optional` (C++), `optional` (Rust), `none_or` (Hypothesis) |

### Object/Struct Generation

#### Static Languages

Two patterns for struct generation:

**1. Default Generator (schema from type reflection)**
```cpp
// C++ with reflect-cpp
auto gen = default_generator<Person>();
Person p = gen.generate();
```

**2. Builder Pattern with Field Generators**
```rust
// Rust with derive macro
let gen = PersonGenerator::new()
    .with_name(text().with_max_length(50))
    .with_age(integers::<u32>().with_max(120));
let p: Person = gen.generate();
```

#### Dynamic Languages

Use `fixed_dictionaries`:
```python
person_gen = fixed_dictionaries(
    {"name": text(max_size=50), "age": integers(min_value=0, max_value=120)}
)
```

#### Schema for Objects

**Option 1: Use tuples (recommended for schema composition)**

Generate fields as a tuple and convert to object in generate():

```json
{
  "type": "tuple",
  "elements": [
    {"type": "string", "max_size": 50},
    {"type": "integer", "min_value": 0, "max_value": 120}
  ]
}
```

Then convert the tuple `[name, age]` back to an object `{name: ..., age: ...}`.

**Option 2: Compositional fallback**

When schemas aren't composable, generate each field separately within a
labeled group.

**Note:** The Rust derive macro generates `type: "object"` schemas with
`properties` and `required` fields. This format is planned for future parser
support.

### Connection Threading Model

The Hegel protocol uses a **demand-driven reader** rather than a background
reader thread. When a channel needs a message, it drives the connection's
reader to read from the socket until the needed message arrives (or a timeout
is reached). This avoids the complexity and race conditions of background
threads.

**How it works:**

1. When `Channel.receive_request()` or `Channel.wait_for_response()` is called,
   the channel invokes `Connection.run_reader(until)` where `until` is a
   condition that becomes true when the channel's inbox has a message, the
   channel is closed, or a timeout expires.
2. `run_reader` acquires a reader lock (non-blocking — if another thread holds
   it, the caller polls until the lock is free or `until` is true).
3. While the lock is held, it reads packets from the socket (with short
   timeouts to allow checking the `until` condition) and dispatches them to
   the appropriate channel's inbox.
4. When `until()` returns true, the reader releases the lock and returns.

**Key design points:**

- **No background thread**: There is no dedicated reader thread. Reading
  happens on the calling thread when a channel needs data.
- **Reader lock**: Only one thread reads from the socket at a time. The lock
  is acquired non-blocking — other threads poll until it's available.
- **Short read timeouts**: `read_packet` uses a short socket timeout (e.g.
  100ms) so the reader can periodically check the `until` condition.
- **Close is simple**: Set `running = false`, shutdown the socket, close
  channels. No thread join needed.

**Thread safety for sends:**

- A separate writer lock protects `send_packet` so multiple threads can
  send concurrently without corrupting the socket stream.
- Channel registration also uses the writer lock.

**Thread-local state:**

- Use thread-local storage for the current data channel so that `generate()`,
  `assume()`, etc. work as free functions.
- Use atomic operations for request ID counters.

```rust
thread_local! {
    static CONNECTION: RefCell<Option<ConnectionState>> = RefCell::new(None);
}
static REQUEST_ID: AtomicU64 = AtomicU64::new(0);
```

### UTF-8 and String Lengths

JSON Schema string lengths count **Unicode codepoints**, not bytes:

```json
{"type": "string", "minLength": 1, "maxLength": 10}
```

This means 1-10 codepoints. If your language counts bytes by default (e.g.,
C), you need codepoint-aware string handling.

## Testing Patterns

### Property Test Structure

```rust
fn test_list_reverse() {
    let list = vecs(integers::<i32>())
        .with_min_size(0)
        .with_max_size(100)
        .generate();

    // Property: reversing twice gives original
    let reversed_twice: Vec<i32> = list.iter()
        .rev()
        .rev()
        .cloned()
        .collect();

    assert_eq!(list, reversed_twice);
}
```

### Debugging Output

Use `note()` for debugging output visible during shrinking:

```rust
fn test_sorting() {
    let list = vecs(integers::<i32>()).generate();
    note(&format!("Testing with list: {:?}", list));

    let sorted = sort(&list);
    assert!(is_sorted(&sorted));
}
```

## Implementation Checklist

### Phase 1: Core Infrastructure

- [ ] Socket connection management (demand-driven reader, no background thread)
- [ ] CBOR binary packet serialization (20-byte header + CBOR payload)
- [ ] Request ID counter (atomic)
- [ ] `assume()` function
- [ ] Basic `Generator<T>` type with `generate()` and `as_basic()`

### Phase 2: Primitive Generators

- [ ] `nulls()`
- [ ] `booleans()`
- [ ] `just(value)`
- [ ] `integers()` with min/max
- [ ] `floats()` with min/max and exclusions
- [ ] `text()` with length bounds
- [ ] `binary()` with min/max size (CBOR byte strings)
- [ ] `from_regex(pattern)`

### Phase 3: Format Strings

- [ ] `emails()`
- [ ] `urls()`
- [ ] `domains()`
- [ ] `ip_addresses()` with v4/v6 option
- [ ] `dates()`
- [ ] `times()`
- [ ] `datetimes()`

### Phase 4: Collections

- [ ] `lists(elements)` with min/max size and unique
- [ ] `sets(elements)`
- [ ] `dictionaries(keys, values)` (string keys only)
- [ ] `tuples(generators...)`
- [ ] Schema composition for all collections (via `as_basic()`)
- [ ] Compositional fallback when generators are not basic

### Phase 5: Combinators

- [ ] `sampled_from(elements)`
- [ ] `one_of(generators...)`
- [ ] `optional(generator)`
- [ ] `builds(type, generators...)`
- [ ] `map(f)` on Generator (preserving basicness when source is basic)
- [ ] `flatmap(f)` / `flat_map(f)` on Generator
- [ ] `filter(predicate)` on Generator

### Phase 6: Advanced Features

- [ ] Span support (`start_span`, `stop_span`)
- [ ] Labeled groups for compositional generation
- [ ] Discardable spans for filtering
- [ ] Default generators from types (if language supports reflection)
- [ ] Builder pattern for customizing generators
- [ ] Derive macros / code generation (if applicable)

### Phase 7: Embedded Mode (Optional)

- [ ] Mode detection (external vs embedded)
- [ ] Exception-based assume(false) handling
- [ ] Provided file descriptor support
- [ ] Test runner integration

### Phase 8: Testing & Documentation

- [ ] Unit tests for each generator type
- [ ] Integration test with actual Hegel server
- [ ] Property tests using the SDK itself
- [ ] API documentation
- [ ] Usage examples

## Reference Implementations

- **C++ SDK**: `external/hegel-cpp/`
- **Rust SDK**: `external/hegel-rust/`
- **Go SDK**: Separate repository
- **TypeScript SDK**: Separate repository

The C++ and Rust SDKs implement the full feature set and can serve as
reference for new implementations.

## Language-Specific Notes

### Python

- `BasicGenerator` is a subclass of `Generator` (is-a).
- `isinstance(gen, BasicGenerator)` is used by combinators to check basicness.
- The `_raw_schema` and `_transform` attributes are accessed directly by
  combinators (they are nominally private but used within the module).
- `sampled_from` uses the integer-index approach for all types.
- `just` uses `{"const": null}` schema with a transform that ignores input.

### Rust

- The `Generate<T>` trait defines `generate()` and `as_basic()`.
- `BasicGenerator<T>` has a `schema: Value` and an
  `Option<Arc<dyn Fn(Value) -> T>>` transform.
- `as_basic()` returns `Option<BasicGenerator<T>>` (default: `None`).
  Combinators inspect `as_basic()` to decide whether to compose schemas.
- `map()` on the `Generate` trait returns `Mapped` which implements
  `as_basic()` by checking the source's `as_basic()` and composing via
  `BasicGenerator::map()`. This preserves schemas through `map()`.
- `sampled_from` uses `{"sampled_from": [...]}` for CBOR primitives and
  falls back to index generation for complex types. The Python reference
  uses the index approach universally. SDKs may use either.
- `just` requires `Serialize` and uses `{"const": <value>}`. Non-serializable
  constants use `just_any` (no schema).

### C++

- `Generator<T>` wraps an `IGenerator<T>` via `shared_ptr`.
- `BasicGenerator<T>` has a JSON schema and an
  `std::optional<std::function<T(nlohmann::json&)>>` transform.
- `IGenerator<T>` defines `as_basic()` returning
  `std::optional<BasicGenerator<T>>` (default: `std::nullopt`).
- `BasicBackedGenerator<T>` wraps a `BasicGenerator` directly.
  `FunctionBackedGenerator<T>` optionally holds a `BasicGenerator`.
- `Generator::map()` checks `as_basic()` on the source and, if available,
  composes via `BasicGenerator::map()` to preserve the schema.
- Collection, tuple, and one_of strategies compose transforms through
  `BasicGenerator::from_raw()` which applies the transform (or default
  deserialization) to a raw JSON value.
