"""Domain validation strategies for checking if domains are actually active."""

from typing import List, Dict
import aiohttp
import asyncio
import socket
import ssl
from datetime import datetime


class DomainValidator:
    """Validates domains to check if they are actually active and accessible."""

    def __init__(self, timeout: int = 5, max_concurrent: int = 20):
        """
        Initialize the domain validator.

        Args:
            timeout: Timeout in seconds for each check
            max_concurrent: Maximum concurrent domain checks
        """
        self.timeout = timeout
        self.max_concurrent = max_concurrent

    async def validate_domains(self, domains: List[str]) -> Dict[str, Dict[str, bool]]:
        """
        Validate multiple domains in parallel using multiple strategies.

        Args:
            domains: List of domains to validate

        Returns:
            Dictionary mapping domain to validation results
            {
                "example.com": {
                    "dns_resolves": True,
                    "http_reachable": True,
                    "https_reachable": True,
                    "ssl_valid": True,
                    "is_active": True  # Overall status
                }
            }
        """
        # Use semaphore to limit concurrent requests
        semaphore = asyncio.Semaphore(self.max_concurrent)

        validation_tasks = [
            self._validate_single_domain(domain, semaphore)
            for domain in domains
        ]

        results = await asyncio.gather(*validation_tasks, return_exceptions=True)

        # Convert results to dictionary format
        validation_results = {}
        for domain, result in zip(domains, results):
            if isinstance(result, Exception):
                validation_results[domain] = {
                    "dns_resolves": False,
                    "http_reachable": False,
                    "https_reachable": False,
                    "ssl_valid": False,
                    "is_active": False,
                    "error": str(result)
                }
            else:
                validation_results[domain] = result

        return validation_results

    async def _validate_single_domain(self, domain: str, semaphore: asyncio.Semaphore) -> Dict[str, bool]:
        """Validate a single domain using multiple strategies."""
        async with semaphore:
            # Remove protocol if present
            domain = domain.replace("http://", "").replace("https://", "").split("/")[0]

            results = {
                "dns_resolves": False,
                "http_reachable": False,
                "https_reachable": False,
                "ssl_valid": False,
                "is_active": False
            }

            # Strategy 1: DNS Resolution
            dns_resolves = await self._check_dns(domain)
            results["dns_resolves"] = dns_resolves

            if not dns_resolves:
                return results

            # Strategy 2: HTTP Reachability
            http_reachable = await self._check_http(domain)
            results["http_reachable"] = http_reachable

            # Strategy 3: HTTPS Reachability (more reliable indicator)
            https_reachable, ssl_valid = await self._check_https(domain)
            results["https_reachable"] = https_reachable
            results["ssl_valid"] = ssl_valid

            # Determine overall active status
            # Consider active if HTTPS works or HTTP works with DNS resolution
            results["is_active"] = https_reachable or (http_reachable and dns_resolves)

            return results

    async def _check_dns(self, domain: str) -> bool:
        """Check if domain resolves to an IP address."""
        try:
            loop = asyncio.get_event_loop()
            # Use getaddrinfo in executor to avoid blocking
            ip_address = await loop.run_in_executor(
                None,
                lambda: socket.gethostbyname(domain)
            )
            return bool(ip_address)
        except (socket.gaierror, socket.herror, OSError):
            return False

    async def _check_http(self, domain: str) -> bool:
        """Check if domain is reachable via HTTP."""
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"http://{domain}"
                async with session.get(
                    url,
                    allow_redirects=True,
                    ssl=False
                ) as response:
                    # Consider 2xx, 3xx, and 4xx as "active" (server responds)
                    # 5xx might indicate server issues, but still "active"
                    return response.status < 500
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return False
        except Exception:
            return False

    async def _check_https(self, domain: str) -> tuple[bool, bool]:
        """Check if domain is reachable via HTTPS and has valid SSL.

        Returns:
            Tuple of (reachable, ssl_valid)
        """
        reachable = False
        ssl_valid = False

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                url = f"https://{domain}"
                async with session.get(
                    url,
                    allow_redirects=True,
                    ssl=False  # We'll check SSL separately
                ) as response:
                    reachable = response.status < 500
                    ssl_valid = True  # If we got a response, SSL worked
        except aiohttp.ClientSSLError:
            # Domain exists but SSL is invalid
            reachable = await self._check_http(domain)
            ssl_valid = False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            reachable = False
            ssl_valid = False

        # Additional SSL certificate check
        if reachable and not ssl_valid:
            ssl_valid = await self._check_ssl_certificate(domain)

        return reachable, ssl_valid

    async def _check_ssl_certificate(self, domain: str) -> bool:
        """Check if domain has a valid SSL certificate."""
        try:
            context = ssl.create_default_context()
            loop = asyncio.get_event_loop()

            def check_ssl():
                with socket.create_connection((domain, 443), timeout=self.timeout) as sock:
                    with context.wrap_socket(sock, server_hostname=domain) as ssock:
                        cert = ssock.getpeercert()
                        # Check if certificate is not expired
                        if cert:
                            # Parse certificate expiry
                            not_after = cert.get("notAfter")
                            if not_after:
                                expiry_date = datetime.strptime(
                                    not_after, "%b %d %H:%M:%S %Y %Z"
                                )
                                return datetime.now() < expiry_date
                        return True

            return await loop.run_in_executor(None, check_ssl)
        except (OSError, socket.error, ssl.SSLError):
            return False
        except Exception:
            return False

    def filter_active_domains(
        self,
        validation_results: Dict[str, Dict[str, bool]]
    ) -> List[str]:
        """
        Filter domains based on validation results.

        Args:
            validation_results: Results from validate_domains()

        Returns:
            List of domains that are considered active
        """
        active_domains = []
        for domain, results in validation_results.items():
            if results.get("is_active", False):
                active_domains.append(domain)
        return active_domains

