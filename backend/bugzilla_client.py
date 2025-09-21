# backend/bugzilla_client.py
import bugzilla
from urllib.parse import urlparse, parse_qs, urlencode
import requests

# Import the centralized settings
from .config import settings

class BugzillaClient:
    """A client to interact with the Bugzilla API."""

    def __init__(self, url: str, api_key: str = None):
        self.url = url
        self.api_key = api_key
        # We can initialize the official client if we need it for other operations
        # For now, we are using direct requests for more control.
        # self.client = bugzilla.Bugzilla(url, api_key=api_key)

    def get_bugs_data(self, bug_ids: list, include_fields: list):
        """
        Fetches details for a list of bug IDs.
        Uses a direct requests call for performance.
        """
        if not bug_ids:
            return {"bugs": []}

        # Ensure default fields are always there if needed, but for now, we use what's passed.
        params = {
            "id": ",".join(map(str, bug_ids)),
            "include_fields": ",".join(include_fields),
        }
        if self.api_key:
            params['api_key'] = self.api_key

        try:
            response = requests.get(f"{self.url}/rest/bug", params=params)
            response.raise_for_status()  # Raises an exception for 4xx/5xx errors
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error fetching bug data: {e}")
            return {"bugs": [], "error": str(e)}

    def search_bugs(self, query_url: str):
        """
        Takes a full Bugzilla search URL, extracts its parameters,
        and executes the search via the REST API to get bug IDs.
        """
        try:
            # 1. Parse the user-provided URL to extract its query parameters
            parsed_url = urlparse(query_url)
            query_params = parse_qs(parsed_url.query, keep_blank_values=True)

            if not query_params:
                return {"error": "No valid search parameters found in the query URL."}

            # 2. Prepare a new, clean set of parameters for the REST API call
            api_params = {}
            for key, value in query_params.items():
                # parse_qs returns a list for each value, we take the first one.
                if value:
                    api_params[key] = value[0]

            # 3. Force the necessary parameters for a clean API response
            # We only need the ID and summary for the test result.
            api_params['include_fields'] = 'id,summary'

            if self.api_key:
                api_params['api_key'] = self.api_key

            # 4. Construct the correct API endpoint URL
            api_endpoint = f"{self.url}/rest/bug"

            # 5. Execute the request
            response = requests.get(api_endpoint, params=api_params)
            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            # This will catch network errors or 4xx/5xx responses
            error_message = f"Failed to execute search. Status: {e.response.status_code if e.response else 'N/A'}."
            try:
                # Try to get a more specific error from Bugzilla's JSON response
                error_detail = e.response.json().get('message')
                if error_detail:
                    error_message = error_detail
            except:
                # If the response isn't JSON (e.g., HTML error page), use the raw text.
                if e.response is not None:
                    error_message = f"Received non-JSON response from server: {e.response.text[:200]}"

            return {"error": error_message}
        except Exception as e:
            # Catch any other parsing errors
            return {"error": f"An unexpected error occurred: {str(e)}"}


# --- Singleton Instance ---
# This creates a single client instance for the whole application, using the config.
client = BugzillaClient(url=settings.bugzilla_url, api_key=settings.bugzilla_api_key)