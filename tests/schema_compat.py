"""What a reply valid under a new JSON Schema can carry that an old one rejects.

An MCP client checks every tool reply against the outputSchema of the
tools/list it fetched, which can be days old, so a reply has to pass the
schemas served before as well as the current one. Covers what Pydantic
generates: $ref into $defs, anyOf, type, enum and const, format and pattern,
bounds, properties and required, items and prefixItems.
"""

from typing import Any

Schema = dict[str, Any]
NOTES = {"title", "description", "default", "examples", "$defs"}
FLOORS = ("minimum", "exclusiveMinimum", "minLength", "minItems")
CEILINGS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems")


def breaks(old: Schema, new: Schema) -> list[str]:
    """One line per spot where `new` lets through what `old` rejects, empty when every value `new` accepts passes `old`."""
    return _breaks(old, new, old.get("$defs", {}), new.get("$defs", {}), "$")


def _breaks(old: Schema, new: Schema, old_defs: Schema, new_defs: Schema, at: str) -> list[str]:
    old, new = _resolved(old, old_defs), _resolved(new, new_defs)
    if not old.keys() - NOTES:
        return []
    if "anyOf" in new:
        return [line for branch in new["anyOf"] for line in _breaks(old, branch, old_defs, new_defs, at)]
    if "anyOf" in old:
        tries = [_breaks(branch, new, old_defs, new_defs, at) for branch in old["anyOf"]]
        return min(tries, key=len)
    found = []
    if "type" in old:
        allowed = _types(old)
        extra = [t for t in _types(new) if t not in allowed and not (t == "integer" and "number" in allowed)]
        if "type" not in new or extra:
            found.append(f"{at}: {'/'.join(extra) or 'any type'} where {'/'.join(sorted(allowed))} was expected")
    if (closed := _values(old)) is not None:
        values = _values(new)
        extra = [v for v in values or [] if v not in closed]
        if values is None or extra:
            found.append(f"{at}: {extra or 'any value'} outside {closed}")
    for key in ("format", "pattern"):
        if key in old and new.get(key) != old[key]:
            found.append(f"{at}: {key} {new.get(key, 'none')} where {old[key]} was expected")
    for key in FLOORS + CEILINGS:
        if key in old and (key not in new or (new[key] < old[key] if key in FLOORS else new[key] > old[key])):
            found.append(f"{at}: {key} {new.get(key, 'none')} looser than {old[key]}")
    found += _object_breaks(old, new, old_defs, new_defs, at)
    if "items" in old:
        found += _breaks(old["items"], new.get("items", {}), old_defs, new_defs, f"{at}[]")
    for i, item in enumerate(old.get("prefixItems", [])):
        found += _breaks(item, (new.get("prefixItems") or [{}] * (i + 1))[i], old_defs, new_defs, f"{at}[{i}]")
    return found


def _object_breaks(old: Schema, new: Schema, old_defs: Schema, new_defs: Schema, at: str) -> list[str]:
    old_props, new_props, required = old.get("properties", {}), new.get("properties", {}), set(new.get("required", []))
    found = [
        f"{at}.{name}: removed" if name not in new_props else f"{at}.{name}: may be missing"
        for name in old.get("required", [])
        if name not in required
    ]
    for name, prop in new_props.items():
        if name in old_props:
            found += _breaks(old_props[name], prop, old_defs, new_defs, f"{at}.{name}")
        elif old.get("additionalProperties") is False:
            found.append(f"{at}.{name}: not allowed")
    return found


def _resolved(schema: Schema, defs: Schema) -> Schema:
    if "$ref" not in schema:
        return schema
    rest = {k: v for k, v in schema.items() if k != "$ref"}
    return {**_resolved(defs[schema["$ref"].removeprefix("#/$defs/")], defs), **rest}


def _types(schema: Schema) -> set[str]:
    kind = schema.get("type", [])
    return set(kind) if isinstance(kind, list) else {kind}


def _values(schema: Schema) -> list[Any] | None:
    return schema["enum"] if "enum" in schema else [schema["const"]] if "const" in schema else None
