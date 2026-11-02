"""Closed native output syntax; never selects task facts, decisions or evidence."""
from copy import deepcopy


VERSION = "closed-final-or-tool-v1"
_METADATA = {"title", "description", "default", "examples", "$comment"}
_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_ARRAYS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_VALUES = {"items", "additionalProperties", "unevaluatedProperties", "contains",
                  "propertyNames", "not", "if", "then", "else"}


def schema_without_metadata(schema):
    """Remove commentary only at schema positions; preserve property names and enum values."""
    if not isinstance(schema, dict):
        return deepcopy(schema)
    result = {}
    for key, value in schema.items():
        if key in _METADATA:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            value = {name: schema_without_metadata(child) for name, child in value.items()}
        elif key in _SCHEMA_ARRAYS and isinstance(value, list):
            value = [schema_without_metadata(child) for child in value]
        elif key in _SCHEMA_VALUES and isinstance(value, dict):
            value = schema_without_metadata(value)
        else:
            value = deepcopy(value)
        result[key] = value
    return result


def closed_native_response_format(final_schema, tools):
    branches = [{"type": "json_schema", "json_schema": schema_without_metadata(final_schema), "any_order": True}]
    for tool in tools:
        branches.append({
            "type": "tag", "begin": f"<tool_call>\n<function={tool.name}>\n",
            "content": {"type": "json_schema", "json_schema": schema_without_metadata(tool.input_schema),
                        "style": "qwen_xml", "any_order": True},
            "end": "\n</function>\n</tool_call>",
        })
    body = branches[0] if len(branches) == 1 else {"type": "or", "elements": branches}
    return {"type": "structural_tag", "format": {"type": "sequence", "elements": [
        {"type": "regex", "pattern": "[ \\t\\r\\n]*"}, body,
        {"type": "regex", "pattern": "[ \\t\\r\\n]*"},
    ]}}
