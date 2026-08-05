from __future__ import annotations

import json
from typing import Any


def model_dump_compat(model: Any, **kwargs) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    if hasattr(model, "dict"):
        kwargs = dict(kwargs)
        mode = kwargs.pop("mode", None)
        if mode == "json" and hasattr(model, "json"):
            return json.loads(model.json(**kwargs))
        return model.dict(**kwargs)
    raise TypeError(f"Object of type {type(model)!r} does not support dict/model_dump export")


def model_dump_json_compat(model: Any, **kwargs) -> str:
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json(**kwargs)
    if hasattr(model, "json"):
        kwargs = dict(kwargs)
        kwargs.pop("mode", None)
        return model.json(**kwargs)
    raise TypeError(f"Object of type {type(model)!r} does not support json/model_dump_json export")


def model_validate_json_compat(model_cls: Any, payload: str, **kwargs) -> Any:
    if hasattr(model_cls, "model_validate_json"):
        return model_cls.model_validate_json(payload, **kwargs)
    if hasattr(model_cls, "parse_raw"):
        kwargs = dict(kwargs)
        kwargs.pop("strict", None)
        return model_cls.parse_raw(payload, **kwargs)
    raise TypeError(f"Model class {model_cls!r} does not support parse_raw/model_validate_json")
