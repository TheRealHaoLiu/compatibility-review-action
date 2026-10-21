"""compatibility-review: an upgrade-compatibility note for pull requests.

The model answers factual questions about a change (prompt.md, schema.json); a fixed rule
(derive.py) turns the answers into Red, Yellow, or Green. Repository facts computed by tooling
(enrich.py) are appended to the diff before the model reads it.
"""

__version__ = "0.1.0"
PROMPT_VERSION = "v9"
RULE_VERSION = "v9"
