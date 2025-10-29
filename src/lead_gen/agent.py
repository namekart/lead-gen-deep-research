# src/lead_gen/agent.py

from typing import Annotated, List, Optional, Dict

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
    leads: list["Lead"]


class Lead(BaseModel):
    website: str = Field(..., description="Canonical website or domain of the lead")
    detailed_summary: str = Field(..., description="Detailed, actionable summary of why this is a fit")
    rationale: str = Field(..., description="Short justification tying back to classification/buyer tiers")
    meta_data: Optional[Dict[str, str]] = Field(
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


async def generate_leads(state: LeadGenState, config: RunnableConfig):
    """Extract real companies/leads from web search results and structure them."""
    cfg = Configuration.from_runnable_config(config)
    notes_text = "\n".join(state.get("notes", []))
    domain_name = state.get("domain_name", "")

    extraction_model = (
        configurable_model
        .with_structured_output(LeadList)
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

    prompt = (
        "You are extracting real companies/organizations from web search results for domain brokerage.\n"
        f"Domain: {domain_name}\n\n"
        "WEB SEARCH RESULTS (raw data from researchers):\n" + notes_text + "\n\n"
        "Instructions:\n"
        "- Extract ONLY real companies/organizations mentioned in the search results above\n"
        "- Do NOT generate or invent new companies\n"
        "- For each real company found, provide:\n"
        "  * website: Their actual website URL (if mentioned in search results)\n"
        "  * detailed_summary: What the search results say about this company\n"
        "  * rationale: Why this company would be interested in the domain based on search results\n"
        "  * meta_data: Any additional info from search results (location, size, industry, etc.)\n"
        "- Focus on companies that match the classification and buyer tiers\n"
        "- If a company is mentioned multiple times, combine the information\n"
        "- Extract as many real companies as possible from the search results\n"
        "- If search results mention 'Dell, HP, Lenovo' - extract each as separate leads\n"
        "- If search results mention 'Apple Inc. manufactures laptops' - extract Apple as a lead\n"
        "- Only include companies that actually exist and are mentioned in the search results\n"
    )

    result = await extraction_model.ainvoke([HumanMessage(content=prompt)])

    # Guard against invalid output
    if not result or not getattr(result, "leads", None):
        return {"leads": []}

    return {"leads": result.leads}


# Build the LeadGen graph (classify+seed → research → extract → final)
leadgen_builder = StateGraph(LeadGenState, input=LeadGenInputState, config_schema=Configuration)

# Nodes
leadgen_builder.add_node("classify_and_seed_supervisor", classify_and_seed_supervisor)
leadgen_builder.add_node("research_supervisor", supervisor_subgraph)        # reuse existing
leadgen_builder.add_node("generate_leads", generate_leads)                  # extract leads from research
# final_report_generation is intentionally disabled for LeadGen flow

# Edges
leadgen_builder.add_edge(START, "classify_and_seed_supervisor")
leadgen_builder.add_edge("classify_and_seed_supervisor", "research_supervisor")
leadgen_builder.add_edge("research_supervisor", "generate_leads")
leadgen_builder.add_edge("generate_leads", END)

# Compiled graph
leadgen_researcher = leadgen_builder.compile()


