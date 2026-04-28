RELEASE_TYPE: minor

This release changes the wire response for `one_of` schemas to include the index of the branch that produced the value:

```
# before: server emits the value directly
value

# after: server emits a 2-element list of (index, value)
[index, value]
```

`index` is the 0-based position of the branch in `generators`. The schema shape itself is unchanged.

This lets client libraries dispatch per-branch transforms directly from the protocol response, replacing a tagged-tuple workaround that each library was implementing on top of the old wire format. The change requires a coordinated update of every client library: older libraries paired with this server (or this version's libraries paired with an older server) will misinterpret `one_of` responses.
