VERSION = 1
MODEL = "gpt-4o-mini-2024-07-18"
MAX_INPUT_CHARS = 32_000

PROMPT = """You are organizing a personal knowledge base.

Given an article's extracted text, return a JSON object with exactly these keys:

  "title":   a short factual title (3-12 words; no quotes; no trailing period).
  "summary": a 1-3 sentence neutral summary of what the item is about.
  "tags":    a list of 1-8 short lowercase tag strings (single words or 2-3-word
             phrases; kebab-case allowed). Tags describe topics, not item type.

Output strict JSON. Do not wrap in code fences or commentary.
"""
