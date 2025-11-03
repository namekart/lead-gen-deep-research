from typing import List, Dict, Any
import aiohttp


class DotDBClient:
    """Client for interacting with the dotdb API to extract prospect leads."""

    def __init__(self, base_url: str):
        """
        Initialize the DotDB client.

        Args:
            base_url: Base URL of the dotdb API (e.g., "https://amp2-1.grayriver-ffcf7337.westus.azurecontainerapps.io")
        """
        self.base_url = base_url.rstrip("/")

    async def get_active_domains(
        self,
        keywords: List[str],
        site_status: str = "active",
        count_sorting: int = 1
    ) -> Dict[str, List[str]]:
        """
        Fetch leads for keywords and extract all active domains grouped by keyword.

        Args:
            keywords: List of keywords to search for (e.g., ["covertcamera", "marketingguru"])
            site_status: Site status filter (default: "active")
            count_sorting: Sorting parameter (default: 1)

        Returns:
            Dictionary mapping keywords to their lists of active domains
            (e.g., {"covertcamera": ["covertcameraclothing.com", ...], ...})
        """
        url = f"{self.base_url}/dotdb/getleads/bulk"
        params = {
            "site_status": site_status,
            "count_sorting": count_sorting
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=keywords,
                    headers=headers,
                    params=params,
                    timeout=30
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise RuntimeError(f"dotdb API error {resp.status}: {error_text}")

                    response_data = await resp.json()
                    return self._extract_active_domains(response_data)
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Failed to connect to dotdb API: {str(e)}") from e

    def _extract_active_domains(self, response_data: Dict[str, Any]) -> Dict[str, List[str]]:
        """
        Extract all active domains from the dotdb API response, grouped by keyword.

        Args:
            response_data: The JSON response from dotdb API

        Returns:
            Dictionary mapping keywords to their lists of active domains
        """
        result = {}

        # Iterate through each keyword's results
        for keyword, keyword_data in response_data.items():
            active_domains: List[str] = []
            # Some APIs may return null/None for keywords with no data
            if not isinstance(keyword_data, dict):
                result[str(keyword)] = active_domains
                continue
            matches = keyword_data.get("matches") or []

            # Iterate through each match
            for match in matches:
                name = (match.get("name") or "").strip()
                if not name:
                    continue
                site_status_info = match.get("site_status") or {}
                active_suffixes = site_status_info.get("active_suffixes") or []

                # Combine name with each active suffix to form domains
                for suffix in active_suffixes:
                    # Remove leading dot if present (some APIs might not include it)
                    clean_suffix = suffix.lstrip(".")
                    if clean_suffix:
                        domain = f"{name}.{clean_suffix}"
                    else:
                        domain = name
                    active_domains.append(domain)

            result[str(keyword)] = active_domains

        return result

