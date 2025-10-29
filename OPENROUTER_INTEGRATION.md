# OpenRouter Integration Guide

## Overview
OpenRouter support is now fully integrated into the LeadGen research system. You can use OpenRouter models alongside native providers (Google Gemini, Anthropic Claude, OpenAI) by simply using the `openrouter:` prefix in your model names.

## How It Works
The integration automatically:
- Detects `openrouter:` prefix in model names
- Routes through OpenAI-compatible API (provider: `openai`)
- Sets base URL to `https://openrouter.ai/api/v1`
- Strips the prefix for the actual API call (e.g., `openrouter:openai/gpt-4o-mini` → `openai/gpt-4o-mini`)
- Uses `OPENROUTER_API_KEY` for authentication

## Environment Setup

### Required Environment Variables
```bash
# OpenRouter API Key
OPENROUTER_API_KEY=sk-or-v1-...

# Tavily for web search
TAVILY_API_KEY=tvly-...

# Optional: LangSmith for tracing
LANGSMITH_API_KEY=lsv2_pt_...
```

### In LangGraph Studio
Add these in the "API Keys" panel:
- `OPENROUTER_API_KEY`
- `TAVILY_API_KEY`
- `LANGSMITH_API_KEY` (optional)

## Model Configuration

### Recommended Setup (Tool-Calling Compatible)
For research/supervisor steps that need tool-calling, use OpenAI-family models via OpenRouter:

```bash
# Research Model (MUST support tool-calling)
RESEARCH_MODEL=openrouter:openai/gpt-4o-mini

# Other models (no tool-calling needed, can use any provider)
SUMMARIZATION_MODEL=google_genai:gemini-2.0-flash
COMPRESSION_MODEL=google_genai:gemini-2.0-flash
FINAL_REPORT_MODEL=google_genai:gemini-2.0-flash
```

### All-OpenRouter Setup
```bash
RESEARCH_MODEL=openrouter:openai/gpt-4o-mini
SUMMARIZATION_MODEL=openrouter:openai/gpt-4o-mini
COMPRESSION_MODEL=openrouter:openai/gpt-4o-mini
FINAL_REPORT_MODEL=openrouter:openai/gpt-4o-mini
```

### Available OpenRouter Models (Examples)
**OpenAI-family (Tool-calling compatible):**
- `openrouter:openai/gpt-4o-mini`
- `openrouter:openai/gpt-4o`
- `openrouter:openai/gpt-4-turbo`

**Anthropic (Tool-calling compatible):**
- `openrouter:anthropic/claude-3.5-sonnet`
- `openrouter:anthropic/claude-3-opus`
- `openrouter:anthropic/claude-3-haiku`

**Others (May not support tool-calling):**
- `openrouter:google/gemini-2.0-flash-preview-09-2025`
- `openrouter:meta-llama/llama-3.1-405b-instruct`
- `openrouter:mistralai/mistral-large`

⚠️ **Important:** For `RESEARCH_MODEL`, always use a tool-calling compatible model (OpenAI-family or Anthropic routes recommended).

## Native Providers (Still Supported)
You can continue using native providers without OpenRouter:

```bash
# Google Gemini
RESEARCH_MODEL=google_genai:gemini-2.0-flash

# Anthropic Claude
RESEARCH_MODEL=anthropic:claude-3-5-sonnet-20241022

# OpenAI (direct, not through OpenRouter)
RESEARCH_MODEL=openai:gpt-4o-mini
```

**Required API Keys for Native Providers:**
- `GOOGLE_API_KEY` for `google_genai:*`
- `ANTHROPIC_API_KEY` for `anthropic:*`
- `OPENAI_API_KEY` for `openai:*`

## Mixed Provider Example
```bash
# Research via OpenRouter (tool-calling works)
RESEARCH_MODEL=openrouter:openai/gpt-4o-mini

# Summarization via native Google (fast, cheap)
SUMMARIZATION_MODEL=google_genai:gemini-2.0-flash

# Compression via native Anthropic (high quality)
COMPRESSION_MODEL=anthropic:claude-3-5-sonnet-20241022

# Final report via native OpenAI (balanced)
FINAL_REPORT_MODEL=openai:gpt-4o
```

## Verification Steps
After setting up:

1. **Start the server:**
   ```bash
   langgraph dev
   ```

2. **In LangGraph Studio:**
   - Set your model names in config
   - Add API keys in "API Keys" panel
   - Run a LeadGen job with a domain name

3. **Check for tool-calling:**
   - After "research_supervisor" node
   - Last AI message should have `tool_calls` (ConductResearch)
   - You should see non-empty `raw_notes` and `notes` in state
   - `generate_leads` should produce leads

4. **Common Issues:**
   - **No leads generated:** RESEARCH_MODEL doesn't support tool-calling → switch to OpenAI-family route
   - **401 errors:** OPENROUTER_API_KEY not set or invalid
   - **400 "invalid model":** Model route doesn't exist on OpenRouter or not accessible to your account

## Cost Optimization Tips
- Use `openrouter:openai/gpt-4o-mini` for research (cheap, tool-capable)
- Use `google_genai:gemini-2.0-flash` for summarization/compression (cheapest)
- Reserve expensive models for final reports or classification if needed

## Technical Details
The integration uses these helper functions in `src/open_deep_research/utils.py`:

- `get_model_provider_for_model()`: Maps `openrouter:*` → `"openai"`
- `get_base_url_for_model()`: Returns `https://openrouter.ai/api/v1` for OpenRouter models
- `normalize_model_name()`: Strips `openrouter:` prefix for API calls
- `get_api_key_for_model()`: Returns `OPENROUTER_API_KEY` for OpenRouter models

All model configuration calls automatically use these helpers to support OpenRouter transparently.

## Support
For issues or questions:
- OpenRouter docs: https://openrouter.ai/docs
- Check model availability: https://openrouter.ai/models
- Verify your API key has credits and model access

