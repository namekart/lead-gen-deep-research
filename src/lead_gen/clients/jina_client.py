"""Client for interacting with Jina AI API to fetch website information."""

from typing import Optional, Dict, Any
import aiohttp
import os
from dotenv import load_dotenv
import tldextract

load_dotenv()


# Initialize a non-blocking extractor: no disk cache, no network
EXTRACTOR = tldextract.TLDExtract(cache_dir=None, suffix_list_urls=None)

def extract_sld_from_domain(domain: str) -> str:
    """
    Extract the second-level domain (SLD) from a domain name using tldextract.

    Uses the robust tldextract library which properly handles:
    - Standard TLDs (com, org, io, etc.)
    - Two-part TLDs (co.uk, com.au, etc.)
    - Complex TLDs (parliament.uk, etc.)
    - Subdomains

    Examples:
        "covertcameravehicles.com" -> "covertcameravehicles"
        "www.marketingguru.io" -> "marketingguru"
        "subdomain.example.co.uk" -> "example"
        "https://www.example.com/path" -> "example"

    Args:
        domain: Domain name to extract SLD from (may include protocol, www, path)

    Returns:
        Extracted SLD (second-level domain)
    """
    # Extract domain using pre-configured extractor (no disk/network side-effects)
    extracted = EXTRACTOR(domain)

    # Return the domain part (SLD)
    return extracted.domain


class JinaClient:
    """Client for interacting with the Jina AI API."""

    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://s.jina.ai"):
        """
        Initialize the Jina client.

        Args:
            api_key: Jina API key (defaults to JINA_API_KEY env var)
            base_url: Base URL of the Jina API (default: "https://s.jina.ai")
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.getenv("JINA_API_KEY", "")

    async def fetch_site_info(self, domain: str) -> Optional[Dict[str, Any]]:
        """
        Fetch website information from Jina AI for a given domain.

        This method:
        1. Extracts the SLD from the domain
        2. Makes a request to Jina AI API
        3. Returns the response (handles both success and error responses)

        Args:
            domain: Domain name (e.g., "covertcameravehicles.com")

        Returns:
            Dictionary containing the Jina AI response (success or error format), or None on network error
        """
        # Extract SLD from domain
        sld = extract_sld_from_domain(domain)

        if not sld:
            return None

        # Build the URL
        url = f"{self.base_url}/?q={sld}"

        # Prepare headers
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-Engine": "direct",
            "X-Site": domain
        }

        if not self.api_key:
            raise ValueError("Jina API key is required. Set JINA_API_KEY environment variable or pass api_key parameter.")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=30) as resp:
                    response_data = await resp.json()

                    # Jina API returns JSON even for errors (422, etc.)
                    # So we return the response regardless of status code
                    # Caller can check response["code"] to determine success/failure
                    return response_data
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Failed to connect to Jina API: {str(e)}") from e

    @staticmethod
    def is_success_response(response: Dict[str, Any]) -> bool:
        """
        Check if the Jina API response indicates success.

        Args:
            response: Jina API response dictionary

        Returns:
            True if response is successful (code 200), False otherwise
        """
        return response.get("code") == 200 and response.get("status") == 20000

    @staticmethod
    def get_error_message(response: Dict[str, Any]) -> Optional[str]:
        """
        Extract error message from a failed Jina API response.

        Args:
            response: Jina API response dictionary

        Returns:
            Error message if response indicates failure, None otherwise
        """
        if JinaClient.is_success_response(response):
            return None

        return response.get("readableMessage") or response.get("message")

