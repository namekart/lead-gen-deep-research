from typing import Any, Dict, Optional, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# Import the compiled LeadGen graph from your existing code
from lead_gen.agent import leadgen_researcher
from lead_gen.clients.dotdb_client import DotDBClient
from lead_gen.configuration import LeadGenConfiguration

app = FastAPI(title="LeadGen API", version="1.0.0")

class LeadGenRequest(BaseModel):
    domain_name: str = Field(..., description="Domain to research, e.g., covertcameras.com")
    configurable: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional overrides for Configuration fields",
    )

class LeadGenResponse(BaseModel):
    leads: list[dict] = Field(default_factory=list)

class DotDBRequest(BaseModel):
    keywords: List[str] = Field(..., description="List of keywords to search for, e.g., ['covertcamera', 'marketingguru']")

class DotDBSingleRequest(BaseModel):
    keyword: str = Field(..., description="Single keyword to search for, e.g., 'covertcamera'")

@app.post("/leadgen/run", response_model=LeadGenResponse)
async def run_leadgen(req: LeadGenRequest):
    config = {}
    if req.configurable:
        config["configurable"] = req.configurable

    final_state = await leadgen_researcher.ainvoke({"domain_name": req.domain_name}, config)
    return LeadGenResponse(leads=final_state.get("leads", []))

@app.post("/dotdb/getleads")
async def get_dotdb_leads(req: DotDBRequest) -> Dict[str, List[str]]:
    """Extract active domains from dotdb API for given keywords.

    Returns a dictionary mapping keywords to their lists of active domains.
    """
    config = LeadGenConfiguration()
    client = DotDBClient(config.dotdb_url)

    try:
        domains = await client.get_active_domains(
            keywords=req.keywords,
            site_status="active",
            count_sorting=1
        )
        return domains
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@app.post("/dotdb/getleads/single")
async def get_dotdb_leads_single(req: DotDBSingleRequest) -> List[str]:
    """Extract active domains from dotdb API for a single keyword.

    Returns a list of active domains for the given keyword.
    """
    config = LeadGenConfiguration()
    client = DotDBClient(config.dotdb_url)

    try:
        domains_dict = await client.get_active_domains(
            keywords=[req.keyword],
            site_status="active",
            count_sorting=1
        )
        # Return the domains for the single keyword
        return domains_dict.get(req.keyword, [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
