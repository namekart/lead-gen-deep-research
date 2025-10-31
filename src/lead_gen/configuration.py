import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

class LeadGenConfiguration(BaseModel):
    """Configuration for LeadGen project, e.g. scraping URL."""
    scraper_url: str = Field(default_factory=lambda: os.getenv("SCRAPER_URL", "http://localhost:3000/api/"))
