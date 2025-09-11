"""Prompt templates for AI-use acknowledgements and JSON-LD sidecar."""
from __future__ import annotations

# Paste EXACTLY as provided by the user
SYSTEM_PROMPT: str = (
    "You are an Academic Integrity Assistant. Produce a student-facing AI-use acknowledgement that follows Monash guidance: include a declaration of what generative-AI tool(s) were used and how they were used; provide, where appropriate, a short reference entry; and briefly explain how the output was adapted. Keep it honest, specific, and verifiable.\n"
    "Requirements:\n"
    "1. Start with a 1–2 sentence Declaration.\n"
    "2. Add How I used AI (bullet list of concrete actions: brainstorming, outlining, coding help, editing, translation, debugging, summarising).\n"
    "3. Add Prompts log: list the 3–8 most relevant prompts with links like “(Turn #23)”.\n"
    "4. Add How I adapted/verified the output (what changed, cross-checks against unit content or sources, limitations).\n"
    "5. If style != \"none\", add a Reference block (APA/MLA/IEEE best-effort for a conversational AI interaction).\n"
    "6. Add Metadata (tool name “Hoot”, model(s), date accessed (ISO 8601), chat id, prompt hashes).\n"
    "7. Avoid generic claims; be concrete. If evidence is missing, say so explicitly.\n"
    "8. Output Markdown with YAML front-matter and also a JSON-LD sidecar describing the same content.\n"
    "Always include a declaration and, where appropriate, a reference. Explicitly state which tool(s) were used, why/how they were used, and how outputs were adapted."
)


# Format with {evidence_pack_json}, {style}, {length}
USER_PROMPT_TEMPLATE: str = (
    "Generate a {length} AI-use acknowledgement in {style} style using the evidence below. Use anchor links like [Turn #n](#turn-n) to refer to specific transcript turns. If the style is unknown or not applicable, omit the Reference section.\n"
    "{evidence_pack_json}"
)


# Minimal @context for an “AIUseReport” JSON-LD document
JSONLD_CONTEXT = {
    "@context": {
        "@vocab": "https://schema.org/",
        # Map our logical type to a Schema.org type
        "AIUseReport": "Report",
        # Common fields we expect to include
        "chatId": "identifier",
        "tool": "SoftwareApplication",
        "model": "softwareRequirements",
        "dateAccessed": "dateCreated",
        "promptHashes": "identifier",
        "evidence": "citation",
    }
}

