from typing import Optional
import aiohttp
from lead_gen.configuration import LeadGenConfiguration

class ScraperClient:
    """Client for scraping company information from the scraper API."""
    def __init__(self, config: LeadGenConfiguration):
        self.base_url = config.scraper_url.rstrip("/")

    async def get_company_info(self, company_domain: str) -> Optional[dict]:
        """Get company information from the scraper API."""
        url = f"{self.base_url}/company/tracxn"
        payload = {"companyDomain": company_domain}
        headers = {"Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        return None
                    response = await resp.json()
                    if response.get("success") and "data" in response:
                        return response["data"]
                    return None
        except Exception:
            return None
