# src/lead_gen/agent.py

from typing import Annotated, List, Optional, Dict, Any

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
from pydantic import BaseModel, Field

from lead_gen.classify_prompts import classification_and_buyers_prompt, CLASSIFICATION_GUIDE, leadgen_supervisor_prompt
from lead_gen.dotdb_subgraph import dotdb_subgraph


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


class Lead(BaseModel):
    website: str = Field(..., description="Canonical website or domain of the lead")
    detailed_summary: str = Field(..., description="Detailed, actionable summary of why this is a fit")
    rationale: str = Field(..., description="Short justification tying back to classification/buyer tiers")
    tier: Optional[str] = Field(None, description="Buyer or classification tier for this lead")
    meta_data: Optional[Dict[str, Any]] = Field(
        default=None, description="Optional metadata such as contact hints, geo, size"
    )


class LeadList(BaseModel):
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


async def get_leads(state: LeadGenState, _config: Optional[RunnableConfig] = None):
    """Return merged leads from both supervisor and dotdb workflows.

    Args:
        state: Current LeadGenState containing leads from both workflows
        config: Runtime configuration (unused, kept for compatibility)

    Returns:
        Dictionary containing the merged leads
    """
    # Get leads from state (merged by override_reducer from both workflows)
    leads = state.get("leads", [])

    # Convert dict leads to Lead objects if needed
    # (assuming supervisor may return Lead objects and dotdb returns dicts)
    processed_leads = []
    for lead in leads:
        if isinstance(lead, dict):
            # Convert dict to Lead object
            processed_leads.append(Lead(**lead))
        elif isinstance(lead, Lead):
            processed_leads.append(lead)
        else:
            # Keep as is if already in correct format
            processed_leads.append(lead)

    return {"leads": processed_leads}


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
            )
        )
    return {"leads": parsed}


# Build the LeadGen graph with parallel workflows
# Flow: classify → (supervisor || dotdb) → merge → get_leads
leadgen_builder = StateGraph(LeadGenState, input=LeadGenInputState, config_schema=Configuration)

# Nodes
leadgen_builder.add_node("classify_and_seed_supervisor", classify_and_seed_supervisor)
leadgen_builder.add_node("research_supervisor", supervisor_subgraph)  # supervisor workflow
leadgen_builder.add_node("dotdb_generate_leads", dotdb_generate_leads)  # dotdb+jina→leads
leadgen_builder.add_node("get_leads", get_leads)  # merge and return leads
# final_report_generation is intentionally disabled for LeadGen flow

# Edges - run dotdb and supervisor in parallel
leadgen_builder.add_edge(START, "classify_and_seed_supervisor")
# Both workflows start from classify_and_seed_supervisor
leadgen_builder.add_edge("classify_and_seed_supervisor", "research_supervisor")  # supervisor path
leadgen_builder.add_edge("classify_and_seed_supervisor", "dotdb_generate_leads")  # dotdb path (parallel)
# Both workflows converge at get_leads
leadgen_builder.add_edge("research_supervisor", "get_leads")
leadgen_builder.add_edge("dotdb_generate_leads", "get_leads")
leadgen_builder.add_edge("get_leads", END)

# Compiled graph
leadgen_researcher = leadgen_builder.compile()
