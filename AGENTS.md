# AGENTS.md

## 1. Core Directives (Communication & Output)
- **Strict Conciseness:** Answer questions directly in the first sentence. Do not include conversational filler, greetings, or sign-offs (e.g., "Sure, I can help with that," "Let me know if you need anything else").
- **Code-First Output:** When asked for code changes keep explanations to a maximum of 2–3 bullet points *only* if architectural decisions require clarification.
- **No Unnecessary Narratives:** Do not narrate your thought process or summarize what you just did unless a command fails or an edge case is discovered.
- **Formatting:** Use simple bulleted lists or compact tables for multi-item responses. Keep paragraphs under 3 sentences.

## 2. Python Standards
- **Version & Typing:** Target modern Python (3.11+). Use built-in type annotations (`list[str]`, `dict[str, Any]`, `X | None`) on all new function signatures.
- **Docstrings & Comments:** Do not add docstrings to self-explanatory functions or private helpers. Only write inline comments to explain *why* non-obvious logic exists, never *what* the code is doing.
- **Error Handling:** Let exceptions bubble up naturally unless specific recovery logic is required. Avoid broad `try/except Exception:` blocks.
- **Tooling:** Assume `ruff` handles formatting and linting, and `pytest` handles testing. Do not manually tweak line lengths unless asked.

## 3. Execution & Verification Workflow
1. **Read First:** Inspect the target file(s) before proposing changes.
2. **Minimal Diff:** Apply the smallest possible change that satisfies the prompt.
3. **Targeted Testing:** When verifying changes, run `pytest <path/to/specific_test.py>` rather than the entire test suite unless requested.
4. **Stop Condition:** Once the specific task passes verification, stop immediately. Do not suggest next steps, follow-up features, or additional optimizations.