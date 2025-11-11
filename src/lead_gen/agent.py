# src/lead_gen/agent.py

from typing import Annotated, List, Optional, Dict, Any, Union
from urllib.parse import urlparse

import tldextract

from langchain_core.messages import HumanMessage, SystemMessage, MessageLikeRepresentation
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END

from open_deep_research.configuration import Configuration
from open_deep_research.deep_researcher import (
    supervisor_subgraph,
    configurable_model,
)
from open_deep_research.utils import (
    get_api_key_for_model,
    get_base_url_for_model,
    get_model_provider_for_model,
    get_today_str,
    normalize_model_name,
)
from open_deep_research.state import override_reducer
from typing_extensions import TypedDict
from pydantic import BaseModel, Field, ConfigDict

from lead_gen.classify_prompts import classification_and_buyers_prompt, CLASSIFICATION_GUIDE, leadgen_supervisor_prompt
from lead_gen.dotdb_subgraph import dotdb_subgraph


# Initialize tldextract without disk cache or network requests
_EXTRACTOR = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=None)


class LeadGenInputState(TypedDict):
    """User-provided inputs for LeadGen flow."""
    domain_name: str


class LeadGenState(TypedDict, total=False):
    """Typed state for LeadGen flow."""
    # Inputs
    domain_name: str
    # Intermediate
    classification_output: str
    # Supervisor context
    supervisor_messages: Annotated[list[MessageLikeRepresentation], override_reducer]
    research_brief: str
    # Research artifacts
    notes: Annotated[list[str], override_reducer]
    # Final structured leads
    leads: Annotated[list["Lead"], override_reducer]


class LeadMetaData(BaseModel):
    model_config = ConfigDict(extra='forbid')
    domain: Optional[str] = None
    title: Optional[str] = None
    signals: Optional[str] = None  # Changed from Dict to str to avoid Azure schema issues
    geo: Optional[str] = None
    contact: Optional[str] = None  # Changed from Union to just str to avoid Azure schema issues

class Lead(BaseModel):
    model_config = ConfigDict(extra='forbid')

    website: str = Field(..., description="Canonical website or domain of the lead")
    detailed_summary: str = Field(..., description="Detailed, actionable summary of why this is a fit")
    rationale: str = Field(..., description="Short justification tying back to classification/buyer tiers")
    tier: Optional[str] = Field(None, description="Buyer or classification tier for this lead")
    meta_data: Optional[LeadMetaData] = Field(  # <-- changed from Dict[str, Any]
        default=None, description="Optional metadata such as contact hints, geo, size"
    )
    email_template: Optional[str] = Field(
        default=None,
        description="Personalized email template with variables: {{first_name}}, {{last_name}}, {{phone_number}}, {{company_name}}, {{website}}, {{location}}, {{linkedin_profile}}, {{company_url}}"
    )


class LeadList(BaseModel):
    model_config = ConfigDict(extra='forbid')  # ensure additionalProperties: false at root
    leads: List[Lead] = Field(..., description="List of extracted leads from web search results")


async def classify_and_seed_supervisor(state: LeadGenState, config: RunnableConfig):
    """Classify domain, generate buyer personas, and seed supervisor in one step."""
    cfg = Configuration.from_runnable_config(config)
    domain_name = state.get("domain_name") or ""
    classification_guide = CLASSIFICATION_GUIDE

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

    # Step 1: Run classification and buyer personas prompt
    prompt = classification_and_buyers_prompt.format(
        classification_guide=classification_guide,
        domain_name=domain_name,
    )
    result = await model.ainvoke([HumanMessage(content=prompt)])
    classification_output = result.content

    # Step 2: Create supervisor context using customized prompt
    supervisor_system_prompt = leadgen_supervisor_prompt.format(
        date=get_today_str(),
        max_concurrent_research_units=cfg.max_concurrent_research_units,
        max_researcher_iterations=cfg.max_researcher_iterations
    )

    return {
        "classification_output": classification_output,
        "research_brief": classification_output,  # Use classification output as research brief
        "supervisor_messages": {
            "type": "override",
            "value": [
                SystemMessage(content=supervisor_system_prompt),  # LeadGen-specific supervisor prompt
                HumanMessage(content=classification_output),      # Classification output as human message
            ],
        },
    }


def normalize_website(website: str) -> str:
    """Normalize website URL for deduplication using tldextract.

    Extracts the registered domain (domain + suffix) from URLs, handling:
    - Standard TLDs (com, org, io, etc.)
    - Two-part TLDs (co.uk, com.au, etc.)
    - Complex TLDs (parliament.uk, etc.)
    - Subdomains (www, api, etc.)
    - Protocols (http://, https://)
    - Paths and query parameters

    Args:
        website: Website URL or domain string

    Returns:
        Normalized domain string (registered_domain in lowercase)

    Examples:
        "https://www.example.com/path" -> "example.com"
        "http://api.example.co.uk" -> "example.co.uk"
        "www.test.io" -> "test.io"
    """
    if not website:
        return ""

    # Remove whitespace
    website = website.strip()

    # Extract domain components using tldextract
    extracted = _EXTRACTOR(website)

    # Build registered domain (domain + suffix)
    # If no suffix, just use domain (for localhost, IP addresses, etc.)
    if extracted.suffix:
        registered_domain = f"{extracted.domain}.{extracted.suffix}".lower()
    elif extracted.domain:
        registered_domain = extracted.domain.lower()
    else:
        # Fallback: try to extract from URL if tldextract fails
        try:
            if not website.startswith(("http://", "https://")):
                website = f"https://{website}"
            parsed = urlparse(website)
            domain = parsed.netloc or parsed.path.split("/")[0]
            registered_domain = domain.lower().strip("/")
        except Exception:
            registered_domain = website.lower()

    return registered_domain


async def dedupe_leads(state: LeadGenState, _config: Optional[RunnableConfig] = None):
    """Deduplicate leads based on normalized website URLs.

    Uses a dictionary-based approach for O(1) lookup and replacement.
    When duplicates are found, keeps the lead with more information.

    Args:
        state: Current LeadGenState containing leads from both workflows
        config: Runtime configuration (unused, kept for compatibility)

    Returns:
        Dictionary containing deduplicated leads
    """
    # Get leads from state (merged by override_reducer from both workflows)
    leads = state.get("leads", [])

    # Convert dict leads to Lead objects if needed
    processed_leads = []
    for lead in leads:
        if isinstance(lead, dict):
            processed_leads.append(Lead(**lead))
        elif isinstance(lead, Lead):
            processed_leads.append(lead)
        else:
            # Keep as is if already in correct format
            processed_leads.append(lead)

    # Deduplicate based on normalized website using dict for efficient lookup
    # Key: normalized domain, Value: (index in deduplicated list, Lead object)
    seen_domains: Dict[str, tuple[int, Lead]] = {}
    deduplicated: List[Lead] = []

    for lead in processed_leads:
        normalized = normalize_website(lead.website)

        if not normalized:
            # Keep leads with empty/missing websites
            deduplicated.append(lead)
            continue

        if normalized not in seen_domains:
            # First occurrence of this domain
            index = len(deduplicated)
            deduplicated.append(lead)
            seen_domains[normalized] = (index, lead)
        else:
            # Duplicate domain found - keep the one with more information
            existing_index, existing_lead = seen_domains[normalized]
            # Prefer lead with longer detailed_summary or more metadata
            should_replace = (
                len(lead.detailed_summary) > len(existing_lead.detailed_summary)
                or (lead.meta_data and not existing_lead.meta_data)
            )

            if should_replace:
                # Replace existing with better lead
                deduplicated[existing_index] = lead
                seen_domains[normalized] = (existing_index, lead)

    return {
        "leads": {
            "type": "override",
            "value": deduplicated,
        }
    }


async def get_leads(state: LeadGenState, _config: Optional[RunnableConfig] = None):
    """Return final leads from state, ensuring they are deduplicated and properly formatted.

    This node reads the leads from state (which should already be deduplicated by dedupe_leads),
    but performs a final deduplication check to ensure no duplicates exist, especially after
    serialization/deserialization when viewing traces.

    Args:
        state: Current LeadGenState containing leads from both workflows
        config: Runtime configuration (unused, kept for compatibility)

    Returns:
        Dictionary containing the final deduplicated leads
    """
    # Get leads from state (should already be deduplicated, but we'll verify)
    leads = state.get("leads", [])

    # Convert dict leads to Lead objects if needed
    # (handles serialization/deserialization from traces)
    processed_leads = []
    for lead in leads:
        if isinstance(lead, dict):
            # Convert dict to Lead object (from serialized state)
            processed_leads.append(Lead(**lead))
        elif isinstance(lead, Lead):
            processed_leads.append(lead)
        else:
            # Keep as is if already in correct format
            processed_leads.append(lead)

    # Final deduplication pass to ensure no duplicates after serialization/deserialization
    # This is especially important when viewing shared traces where state might be recreated
    seen_domains: Dict[str, tuple[int, Lead]] = {}
    final_leads: List[Lead] = []

    for lead in processed_leads:
        normalized = normalize_website(lead.website)

        if not normalized:
            # Keep leads with empty/missing websites
            final_leads.append(lead)
            continue

        if normalized not in seen_domains:
            # First occurrence of this domain
            index = len(final_leads)
            final_leads.append(lead)
            seen_domains[normalized] = (index, lead)
        else:
            # Duplicate domain found - keep the one with more information
            existing_index, existing_lead = seen_domains[normalized]
            should_replace = (
                len(lead.detailed_summary) > len(existing_lead.detailed_summary)
                or (lead.meta_data and not existing_lead.meta_data)
            )

            if should_replace:
                # Replace existing with better lead
                final_leads[existing_index] = lead
                seen_domains[normalized] = (existing_index, lead)

    # Use override pattern to ensure state is cleanly updated
    return {
        "leads": {
            "type": "override",
            "value": final_leads,
        }
    }


async def dotdb_generate_leads(state: LeadGenState, config: RunnableConfig) -> Dict:
    """Run DotDB (incl. Jina) and return Lead objects directly from subgraph output."""
    domain_name = state.get("domain_name", "")
    dotdb_result = await dotdb_subgraph.ainvoke({
        "domain_name": domain_name,
        "classification_output": state.get("classification_output") or "",
    }, config)
    leads_dicts = dotdb_result.get("leads", [])
    # Convert to Lead models
    parsed: list[Lead] = []
    for item in leads_dicts:
        parsed.append(
            Lead(
                website=item.get("website", ""),
                detailed_summary=item.get("detailed_summary", ""),
                rationale=item.get("rationale", ""),
                tier=item.get("tier"),
                meta_data=item.get("meta_data"),
                email_template=item.get("email_template"),
            )
        )
    return {"leads": parsed}


# Build the LeadGen graph with parallel workflows
# Flow: classify → (supervisor || dotdb) → dedupe → get_leads
leadgen_builder = StateGraph(LeadGenState, input=LeadGenInputState, config_schema=Configuration)

# Nodes
leadgen_builder.add_node("classify_and_seed_supervisor", classify_and_seed_supervisor)
leadgen_builder.add_node("research_supervisor", supervisor_subgraph)  # supervisor workflow
leadgen_builder.add_node("dotdb_generate_leads", dotdb_generate_leads)  # dotdb+jina→leads
leadgen_builder.add_node("dedupe_leads", dedupe_leads)  # deduplicate leads
leadgen_builder.add_node("get_leads", get_leads)  # merge and return leads
# final_report_generation is intentionally disabled for LeadGen flow

# Edges - run dotdb and supervisor in parallel
leadgen_builder.add_edge(START, "classify_and_seed_supervisor")
# Both workflows start from classify_and_seed_supervisor
leadgen_builder.add_edge("classify_and_seed_supervisor", "research_supervisor")  # supervisor path
leadgen_builder.add_edge("classify_and_seed_supervisor", "dotdb_generate_leads")  # dotdb path (parallel)
# Both workflows converge at dedupe_leads
leadgen_builder.add_edge("research_supervisor", "dedupe_leads")
leadgen_builder.add_edge("dotdb_generate_leads", "dedupe_leads")
# Dedupe then goes to get_leads
leadgen_builder.add_edge("dedupe_leads", "get_leads")
leadgen_builder.add_edge("get_leads", END)

# Compiled graph
leadgen_researcher = leadgen_builder.compile()
