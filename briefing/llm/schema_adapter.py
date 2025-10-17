"""Minimal schema adapter for LLM providers."""

def to_gemini(schema: dict) -> dict:
    """Convert JSON Schema to Gemini response_schema format."""
    TYPE_MAP = {
        "object": "OBJECT",
        "string": "STRING",
        "array": "ARRAY",
        "number": "NUMBER",
        "integer": "INTEGER",
        "boolean": "BOOLEAN"
    }
    
    def convert(node):
        if not isinstance(node, dict):
            return node
            
        result = {}
        
        # Convert type
        if "type" in node:
            result["type"] = TYPE_MAP.get(node["type"], node["type"])
        
        # Handle properties with ordering
        if "properties" in node:
            result["properties"] = {
                k: convert(v) for k, v in node["properties"].items()
            }
            result["propertyOrdering"] = list(node["properties"].keys())
        
        # Handle array items
        if "items" in node:
            result["items"] = convert(node["items"])

        # Copy constraints (extend as needed for compatibility)
        for key in [
            "required",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
            "format",
            "enum",
            "const",
            "pattern",
        ]:
            if key in node:
                result[key] = node[key]

        # Logical/conditional keywords (best-effort passthrough)
        if "allOf" in node and isinstance(node["allOf"], list):
            result["allOf"] = [convert(x) for x in node["allOf"]]
        if "anyOf" in node and isinstance(node["anyOf"], list):
            result["anyOf"] = [convert(x) for x in node["anyOf"]]
        if "oneOf" in node and isinstance(node["oneOf"], list):
            result["oneOf"] = [convert(x) for x in node["oneOf"]]
        if "not" in node and isinstance(node["not"], dict):
            result["not"] = convert(node["not"])
        if "if" in node and isinstance(node["if"], dict):
            result["if"] = convert(node["if"])
        if "then" in node and isinstance(node["then"], dict):
            result["then"] = convert(node["then"])
        if "else" in node and isinstance(node["else"], dict):
            result["else"] = convert(node["else"])
        
        # Note: additionalProperties not supported in Gemini API
        # Removed to fix compatibility with latest Gemini API
            
        return result
    
    # Remove $schema and convert
    clean = {k: v for k, v in schema.items() if k != "$schema"}
    return convert(clean)

def to_openai(schema: dict) -> dict:
    """Prepare schema for OpenAI (just remove $schema)."""
    return {k: v for k, v in schema.items() if k != "$schema"}
