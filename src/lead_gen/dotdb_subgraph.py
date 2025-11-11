"""DotDB subgraph for fetching and validating domains from dotdb API."""
from typing import Annotated, Dict, List, Optional, Any
import logging

from pydantic import BaseModel, Field
import asyncio
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from typing_extensions import TypedDict
import aiohttp
import tldextract

from lead_gen.clients.dotdb_client import DotDBClient
from lead_gen.clients.jina_client import JinaClient
from lead_gen.configuration import LeadGenConfiguration
from open_deep_research.configuration import Configuration
from open_deep_research.deep_researcher import configurable_model
from open_deep_research.utils import (
    get_api_key_for_model,
    get_base_url_for_model,
    get_model_provider_for_model,
    normalize_model_name,
)
from lead_gen.classify_prompts import DOTDB_KEYWORD_GEN_PROMPT


class DotDBState(TypedDict):
    """State for DotDB subgraph."""
    domain_name: str  # Input domain (e.g., "covertcameras.com")
    classification_output: Optional[str]
    generated_keywords: Annotated[List[str], lambda x, y: y]
    dotdb_domains: Annotated[List[str], lambda x, y: y]  # Domains fetched from dotdb
    jina_results: Annotated[List[Dict[str, Any]], lambda x, y: y]  # Jina API results for domains
    active_domains: Annotated[List[str], lambda x, y: y]  # Domains with successful Jina responses
    leads: Annotated[List[Dict[str, Any]], lambda x, y: y]  # Structured leads


# Note: We do not validate the lead payload here; the LLM is expected to
# produce the correct structure. Downstream will map/convert as needed.


EXTRACTOR = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=None)
logger = logging.getLogger(__name__)

def extract_sld(domain: str) -> str:
    """Extract SLD (keyword) robustly using tldextract (no disk/network)."""
    ext = EXTRACTOR(domain)
    return ext.domain.lower()


async def generate_dotdb_keywords(state: DotDBState, config: Optional[RunnableConfig] = None) -> Dict:
    """Generate DotDB search keywords from the domain using LLM as per strict prompt."""
    domain_name = state.get("domain_name", "")
    if not domain_name:
        return {"generated_keywords": []}

    cfg = Configuration.from_runnable_config(config) if config else Configuration()
    model = (
        configurable_model
        .with_retry(stop_after_attempt=cfg.max_structured_output_retries)
        .with_config({
            "model": normalize_model_name(cfg.research_model),
            "model_provider": get_model_provider_for_model(cfg.research_model),
            "base_url": get_base_url_for_model(cfg.research_model),
            "max_tokens": cfg.research_model_max_tokens,
            "api_key": get_api_key_for_model(cfg.research_model, config),
            "tags": ["langsmith:nostream"],
        })
    )

    sld = extract_sld(domain_name)
    parts = [p for p in sld.replace('-', ' ').split() if p]
    root_a = parts[0] if parts else sld
    root_b = parts[1] if len(parts) > 1 else sld
    prompt = DOTDB_KEYWORD_GEN_PROMPT.format(
        domain_name=domain_name,
        root_example_a=root_a,
        root_example_b=root_b,
        adjacent_example_1=f"{root_a} {root_b[:-1]+'er' if len(root_b)>3 else root_b}",
        adjacent_example_2=f"{root_a[:2]} {root_b}" if len(root_a) > 2 else f"{root_b} {root_a}"
    )

    from langchain_core.messages import HumanMessage
    result = await model.ainvoke([HumanMessage(content=prompt)])
    text = (result.content or "")

    # Prefer machine-readable JSON_TOP_TIER line if present
    keywords: List[str] = []
    json_line = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("JSON_TOP_TIER:"):
            json_line = line
            break
    if json_line:
        try:
            json_part = json_line.split(":", 1)[1].strip()
            import json as _json
            parsed = _json.loads(json_part)
            if isinstance(parsed, list):
                keywords = [str(x) for x in parsed if str(x).strip()]
        except Exception:
            keywords = []

    # Fallback: parse ONLY the Top Tier bullet section if JSON not found
    if not keywords:
        in_top_tier = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not in_top_tier:
                if line.lower().startswith("🏆 top tier") or line.lower().startswith("top tier"):
                    in_top_tier = True
                continue
            if line.startswith("* "):
                item = line[2:].strip()
                if "(" in item and ")" in item:
                    item = item.split("(", 1)[0].strip()
                if item:
                    keywords.append(item)
                continue
            break

    # Fallback to SLD if model didn't yield any Top Tier bullets
    if not keywords and sld:
        keywords = [sld]

    # Build variants: include original phrase; if it has spaces, also include hyphenated version
    # and a concatenated version without spaces or hyphens
    variants: List[str] = []
    seen: set[str] = set()
    for phrase in keywords:
        base = phrase.strip().lower()
        if not base:
            continue
        # If single token (no spaces), include as-is
        if " " not in base and base not in seen:
            variants.append(base)
            seen.add(base)
        # For multi-word phrases, exclude spaced version; include hyphenated
        if " " in base:
            hyphenated = base.replace(" ", "-")
            if hyphenated and hyphenated not in seen:
                variants.append(hyphenated)
                seen.add(hyphenated)
        # Always include compact (no spaces/hyphens)
        compact = base.replace(" ", "").replace("-", "")
        if compact and compact not in seen:
            variants.append(compact)
            seen.add(compact)

    logger.info("[dotdb] generate_dotdb_keywords: top_tier=%d, variants=%d", len(keywords), len(variants))
    return {"generated_keywords": variants[:80]}


async def fetch_dotdb_domains(state: DotDBState, config: Optional[RunnableConfig] = None) -> Dict:
    """Fetch domains from dotdb API in a single bulk request for all generated keywords."""
    gen_keywords = state.get("generated_keywords") or []
    if not gen_keywords:
        return {"dotdb_domains": []}

    config_obj = LeadGenConfiguration()
    client = DotDBClient(config_obj.dotdb_url)

    try:
        # Single bulk call for all keywords
        domains_by_kw = await client.get_active_domains(
            keywords=gen_keywords,
            site_status="active",
        )
        # Flatten and dedupe
        all_domains: List[str] = []
        for kw, items in domains_by_kw.items():
            for d in items:
                if d not in all_domains:
                    all_domains.append(d)
        # Exact SLD filter: keep only domains whose SLD exactly matches a generated keyword
        allowed_slds = set((kw or "").strip().lower() for kw in gen_keywords if (kw or "").strip())
        filtered_domains: List[str] = []
        for d in all_domains:
            try:
                sld = extract_sld(d)
            except Exception:
                continue
            if sld in allowed_slds:
                filtered_domains.append(d)

        logger.info(
            "[dotdb] fetch_dotdb_domains: total=%d, filtered_exact_sld=%d, keywords=%d",
            len(all_domains), len(filtered_domains), len(gen_keywords)
        )
        return {"dotdb_domains": filtered_domains}
    except (RuntimeError, ValueError, aiohttp.ClientError):
        logger.exception("[dotdb] fetch_dotdb_domains: bulk call failed")
        return {"dotdb_domains": []}


async def check_jina_api(state: DotDBState, config: Optional[RunnableConfig] = None) -> Dict:
    dotdb_domains = state.get("dotdb_domains", [])

    if not dotdb_domains:
        logger.warning("[dotdb] check_jina_api: no dotdb_domains to process")
        return {
            "jina_results": [],
            "active_domains": []
        }

    config_obj = LeadGenConfiguration()
    client = JinaClient(api_key=config_obj.jina_api_key)

    concurrency_limit = 10
    semaphore = asyncio.Semaphore(concurrency_limit)
    logger.info("[dotdb] check_jina_api: start, domains=%d, concurrency=%d", len(dotdb_domains), concurrency_limit)

    async def fetch_one(domain: str) -> Dict[str, Any]:
        try:
            async with semaphore:
                response = await client.fetch_site_info(domain)
            if response and JinaClient.is_success_response(response):
                data = response.get("data", [])
                if data:
                    first_item = data[0]
                    logger.debug("[dotdb] jina success for %s (title=%s)", domain, first_item.get("title"))
                    return {
                        "domain": domain,
                        "title": first_item.get("title"),
                        "url": first_item.get("url"),
                        "content": first_item.get("content"),
                        "description": first_item.get("description"),
                        "success": True,
                    }
            error_msg = JinaClient.get_error_message(response) if response else "No response"
            logger.warning("[dotdb] jina failure for %s: %s", domain, error_msg)
            return {"domain": domain, "success": False, "error": error_msg}
        except (RuntimeError, ValueError, aiohttp.ClientError) as e:
            logger.warning("[dotdb] jina exception for %s: %s", domain, str(e))
            return {"domain": domain, "success": False, "error": f"Exception: {str(e)}"}

    unique_domains = list(dict.fromkeys(dotdb_domains))
    tasks = [fetch_one(domain) for domain in unique_domains]
    results = await asyncio.gather(*tasks)

    logger.info("[dotdb] check_jina_api: results=%d", len(results))
    jina_results = results
    active_domains = [r["domain"] for r in results if r.get("success")]
    logger.info("[dotdb] check_jina_api: active_domains=%d", len(active_domains))

    return {
        "jina_results": jina_results,
        "active_domains": active_domains
    }


async def jina_results_to_leads(state: DotDBState, config: Optional[RunnableConfig] = None) -> Dict:
    jina_results = state.get("jina_results", [])
    logger.info("[dotdb] jina_results_to_leads: input_count=%d", len(jina_results))
    if not jina_results:
        return {"leads": []}

    filtered = [r for r in jina_results if r.get("success")]
    logger.info("[dotdb] jina_results_to_leads: filtered_success=%d", len(filtered))

    cfg = Configuration.from_runnable_config(config) if config else Configuration()
    model = (
        configurable_model
        .with_retry(stop_after_attempt=cfg.max_structured_output_retries)
        .with_config({
            "model": normalize_model_name(cfg.research_model),
            "model_provider": get_model_provider_for_model(cfg.research_model),
            "base_url": get_base_url_for_model(cfg.research_model),
            "max_tokens": cfg.research_model_max_tokens,
            "api_key": get_api_key_for_model(cfg.research_model, config),
            "tags": ["langsmith:nostream"],
        })
    )

    from langchain_core.messages import HumanMessage

    cls_out = state.get("classification_output") or ""
    logger.debug("[dotdb] jina_results_to_leads: has_classification_output=%s", bool(cls_out))

    leads: list[Dict[str, Any]] = []
    for it in filtered:
        logger.debug("[dotdb] jina_results_to_leads: processing domain=%s url=%s", it.get("domain"), it.get("url"))
        site_block = (
            f"domain: {it.get('domain','')}\n"
            f"url: {it.get('url','')}\n"
            f"title: {it.get('title','')}\n"
            f"description: {it.get('description','')}\n"
            f"content: {str(it.get('content',''))[:4000]}"
        )
        candidate_url = it.get("url") or (f"https://{it.get('domain','')}" if it.get('domain') else "")
        few_shot = (
            "Examples (follow exactly):\n\n"
            "Good example:\n"
            "{\n"
            "  \"website\": \"https://acme-security.com/\",\n"
            "  \"detailed_summary\": \"Acme Security provides enterprise-grade surveillance systems, including IP cameras, VMS, and integration services for logistics and retail. Their offerings emphasize compliance, 24/7 monitoring, and on-site deployment support.\",\n"
            "  \"rationale\": \"Direct B2B provider of surveillance products/services aligned with category.\",\n"
            "  \"tier\": \"Tier 1\",\n"
            "  \"meta_data\": {\"domain\": \"acme-security.com\", \"title\": \"Acme Security\", \"signals\": {\"active\": true}},\n"
            "  \"email_template\": \"Hi {{first_name}} {{last_name}},\\n\\nI hope this finds you well. I'm reaching out about a premium domain that aligns perfectly with your surveillance and security solutions business.\\n\\nThe domain {{website}} offers:\\n• Industry-specific branding for security providers\\n• Enhanced credibility and SEO\\n• Memorable, professional identity\\n\\nGiven your focus on enterprise surveillance systems, this could be a strategic asset for {{company_name}}.\\n\\nInterested in discussing? Let's connect.\\n\\nBest regards,\\nJohn\\nName.ai LLC | A Namekart Brand\\nWorld's #1 AI Domains Brokerage\\n30 N Gould St Ste R, Sheridan, WY, 82801\\n\\nBook a Meeting: https://cal.com/name-ai\\nTop Assets: Audit.ai | Bank.ai | Market.ai | Match.ai | Soul.ai\\nTransaction Platforms: GoDaddy (DAN) | NameLot (NameSilo)\\n\\nPS: We also offer direct invoicing via Stripe if you wish to pay via Amex, though that requires ID verification.\"\n"
            "}\n\n"
            "REJECT example (domain for sale/parked):\n"
            "Website content: 'THIS DOMAIN NAME IS FOR SALE\\nvoxwire.com\\nSaw.com has successfully helped thousands of buyers acquire the perfect domain name. Interested in voxwire.com? Let's get started.\\nMake an Offer\\nYour offer in USD\\nBuy With Confidence\\nSaw.com has assisted thousands of buyers in securely obtaining their ideal domain...'\n"
            "Result: {}\n"
            "(REJECT because it's a domain-for-sale page, not an operating business)\n\n"
            "REJECT example (parked/non-business):\n"
            "{}\n\n"
        )
        attempt_prompt = (
            "You are a lead qualification analyst. From the following website details, extract a single high-quality B2B lead\n"
            "ONLY if it appears to be an actual operating business.\n\n"
            "**CRITICAL REJECTION CRITERIA - Return {} if ANY of these apply:**\n"
            "1. **Domain-for-sale pages**: Look for indicators like:\n"
            "   - 'THIS DOMAIN NAME IS FOR SALE' or 'DOMAIN FOR SALE' or 'This domain is for sale'\n"
            "   - 'Make an Offer', 'Buy this domain', 'Purchase this domain'\n"
            "   - Domain brokerage services mentioned (Saw.com, Sedo, GoDaddy Auctions, Afternic, etc.)\n"
            "   - 'Buy With Confidence', 'Secure Exchange', 'Powered by [brokerage name]'\n"
            "   - Pricing/offer forms or 'Your offer in USD'\n"
            "   - Pages that are primarily about selling the domain itself, not a business\n"
            "2. **Parked domains**: Generic parking pages, placeholder content, 'Under Construction'\n"
            "3. **Personal blogs**: Personal websites, individual portfolios, non-business content\n"
            "4. **Directories/aggregators**: Business directories, listing sites, content farms\n"
            "5. **Inactive/placeholder**: No real business operations, just placeholder text\n\n"
            "**ACCEPTANCE CRITERIA - Only extract if ALL apply:**\n"
            "- The website represents an actual operating business with products/services\n"
            "- There is substantial business content (not just a landing page)\n"
            "- The business appears to be actively operating (not just a placeholder)\n"
            "- The content is about the business itself, not about selling the domain\n\n"
            "Use title, description, and especially content to decide. If ANY rejection criteria match, return an empty JSON object {}.\n\n"
            "Use the following classification guidance to judge relevance and assign tier appropriately.\n"
            f"CLASSIFICATION GUIDANCE:\n{cls_out}\n\n"
            "Return a JSON object with EXACT keys: website, detailed_summary, rationale, tier, meta_data, email_template.\n"
            f"- website MUST be exactly: {candidate_url}\n"
            "- detailed_summary: 2-4 sentences summarizing offering, target customers, differentiators (grounded in content)\n"
            "- rationale: 1-2 sentences why this is a relevant buyer\n"
            "- tier: 'Tier 1'|'Tier 2'|'Tier 3'\n"
            "- meta_data: object (optional fields: domain, title, signals, geo, contact)\n"
            "- email_template: Generate a SHORT, CONCISE, RELEVANT email (100-150 words max) personalized to this lead's specific business/industry. "
            "Use template variables: {{first_name}}, {{last_name}}, {{phone_number}}, {{company_name}}, {{website}}, {{location}}, {{linkedin_profile}}, {{company_url}}. "
            "These variables should work gracefully even if not populated at runtime. "
            "Include the footer signature: 'Best regards,\\nJohn\\nName.ai LLC | A Namekart Brand\\nWorld\\'s #1 AI Domains Brokerage\\n30 N Gould St Ste R, Sheridan, WY, 82801\\n\\n"
            "Book a Meeting: https://cal.com/name-ai\\nTop Assets: Audit.ai | Bank.ai | Market.ai | Match.ai | Soul.ai\\nTransaction Platforms: GoDaddy (DAN) | NameLot (NameSilo)\\n\\n"
            "PS: We also offer direct invoicing via Stripe if you wish to pay via Amex, though that requires ID verification.'\n\n"
            f"{few_shot}"
            f"Website:\n{site_block[:4000]}\n"
            "Return ONLY the JSON object, with no extra text."
        )
        try:
            llm_text = (await model.ainvoke([HumanMessage(content=attempt_prompt)])).content
            logger.debug("[dotdb] LLM raw output (truncated): %s", (llm_text or "")[:500])
            import json
            item = json.loads(llm_text)
            if not isinstance(item, dict):
                logger.warning("[dotdb] non-dict JSON returned for domain=%s", it.get("domain"))
            elif not item.get("website"):
                logger.warning("[dotdb] missing website in JSON for domain=%s", it.get("domain"))
            else:
                leads.append(item)
                logger.info("[dotdb] lead accepted for domain=%s", it.get("domain"))
        except Exception:
            logger.warning("[dotdb] lead generation failed for domain=%s (non-JSON or parse error)", it.get("domain"))
            continue

    return {"leads": leads}


# Build the DotDB subgraph (for internal use)
# Flow: generate_keywords -> fetch_dotdb_domains -> check_jina_api -> jina_results_to_leads -> END
dotdb_builder = StateGraph(DotDBState)

dotdb_builder.add_node("generate_dotdb_keywords", generate_dotdb_keywords)
dotdb_builder.add_node("fetch_dotdb_domains", fetch_dotdb_domains)
dotdb_builder.add_node("check_jina_api", check_jina_api)
dotdb_builder.add_node("jina_results_to_leads", jina_results_to_leads)

dotdb_builder.add_edge(START, "generate_dotdb_keywords")
dotdb_builder.add_edge("generate_dotdb_keywords", "fetch_dotdb_domains")
dotdb_builder.add_edge("fetch_dotdb_domains", "check_jina_api")
dotdb_builder.add_edge("check_jina_api", "jina_results_to_leads")
dotdb_builder.add_edge("jina_results_to_leads", END)

# Compiled subgraph (for internal use in LeadGen workflow)
dotdb_subgraph = dotdb_builder.compile()


# Build standalone DotDB graph (for direct use in LangSmith Studio)
# Same flow as internal
class DotDBInputState(TypedDict):
    domain_name: str


dotdb_standalone_builder = StateGraph(DotDBState, input=DotDBInputState)

dotdb_standalone_builder.add_node("generate_dotdb_keywords", generate_dotdb_keywords)
dotdb_standalone_builder.add_node("fetch_dotdb_domains", fetch_dotdb_domains)
dotdb_standalone_builder.add_node("check_jina_api", check_jina_api)
dotdb_standalone_builder.add_node("jina_results_to_leads", jina_results_to_leads)

dotdb_standalone_builder.add_edge(START, "generate_dotdb_keywords")
dotdb_standalone_builder.add_edge("generate_dotdb_keywords", "fetch_dotdb_domains")
dotdb_standalone_builder.add_edge("fetch_dotdb_domains", "check_jina_api")
dotdb_standalone_builder.add_edge("check_jina_api", "jina_results_to_leads")
dotdb_standalone_builder.add_edge("jina_results_to_leads", END)

# Compiled standalone graph (for LangSmith Studio)
dotdb_standalone = dotdb_standalone_builder.compile()


async def fetch_dotdb_leads(state: Dict, config: RunnableConfig) -> Dict:
    """Run the full DotDB flow and return leads to parent."""
    domain_name = state.get("domain_name", "")

    dotdb_input = {
        "domain_name": domain_name,
        "classification_output": state.get("classification_output") or "",
    }

    result = await dotdb_subgraph.ainvoke(dotdb_input, config)
    return {"leads": result.get("leads", [])}

