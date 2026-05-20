"""Prompt loader for v1 mustache templates."""
import os
from typing import Optional

import pystache

PROMPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_renderer = pystache.Renderer(escape=lambda u: u)


def load_prompt(template_name: str, context: Optional[dict] = None) -> str:
    """Read `<template_name>.mustache` from this package and render it with context.

    When `context` is None the raw template text is returned, which lets callers
    pass it on to a downstream renderer (e.g. BedrockModel.render_prompt_template).
    """
    template_path = os.path.join(PROMPTS_DIR, f"{template_name}.mustache")
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()
    if context:
        return _renderer.render(template, context)
    return template
