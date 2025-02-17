"""REST client handling, including GitLabStream base class."""

from __future__ import annotations

import copy
import urllib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union, cast
from urllib.parse import parse_qs, urlparse

import requests
from dateutil.parser import parse
from singer_sdk.authenticators import APIKeyAuthenticator
from singer_sdk.helpers.jsonpath import extract_jsonpath
from singer_sdk.streams import GraphQLStream, RESTStream

API_TOKEN_KEY = "Private-Token"
API_TOKEN_SETTING_NAME = "private_token"

DEFAULT_REST_API_URL = "https://gitlab.com/api/v4"
DEFAULT_GRAPHQL_API_URL = "https://gitlab.com/api"

SCHEMAS_DIR = Path(__file__).parent / Path("./schemas")


class GitLabStream(RESTStream):
    """GitLab stream class."""

    records_jsonpath = "$[*]"
    next_page_token_jsonpath = "$.X-Next-Page"
    extra_url_params: Optional[dict] = None
    bookmark_param_name = "since"
    _LOG_REQUEST_METRIC_URLS = True  # Okay to print in logs
    # sensitive_request_path = False  # TODO: Update SDK to accept this instead.

    @property
    def url_base(self) -> str:
        """Return the API URL root, configurable via tap settings.

        If no path is provided, the base URL will be appended with `/api/v4`.
        E.g. 'https://gitlab.com' would become 'https://gitlab.com/api/v4'

        Note: trailing slashes ('/') are scrubbed prior to comparison, so that
        'https://gitlab.com` is equivalent to 'https://gitlab.com/' and
        'https://gitlab.com/api/v4' is equivalent to 'https://gitlab.com/api/v4/'.
        """
        # Remove trailing '/' from url base.
        result = self.config.get("api_url", DEFAULT_REST_API_URL).rstrip("/")

        # If path part is not provided, append the v4 endpoint as default:
        # For example 'https://gitlab.com' => 'https://gitlab.com/api/v4'
        if not urlparse(result).path:
            result += "/api/v4"
        return result

    @property
    def schema_filename(self) -> str:
        """Return the filename for the stream's schema."""
        return f"{self.name}.json"

    @property
    def schema_filepath(self) -> Optional[Path]:
        """Return the filepath for the stream's schema."""
        return SCHEMAS_DIR / self.schema_filename

    @property
    def authenticator(self) -> APIKeyAuthenticator:
        """Return a new authenticator object."""
        return APIKeyAuthenticator.create_for_stream(
            self,
            key=API_TOKEN_KEY,
            value=self.config[API_TOKEN_SETTING_NAME],
            location="header",
        )

    @property
    def http_headers(self) -> dict:
        """Return the http headers needed."""
        headers = {}
        if "user_agent" in self.config:
            headers["User-Agent"] = self.config.get("user_agent")
        return headers

    def get_url_params(
        self, context: Optional[dict], next_page_token: Optional[Any]
    ) -> Dict[str, Any]:
        """Return a dictionary of values to be used in URL parameterization."""
        # If the class has extra default params, start with those:
        # TODO: SDK Bug: without copy(), this will leak params across classes/objects.
        params: dict = copy.copy(self.extra_url_params or {})

        if next_page_token:
            params["page"] = next_page_token
        if self.replication_key:
            params["sort"] = "asc"
            params["order_by"] = self.replication_key
            if self.is_timestamp_replication_key:
                params[self.bookmark_param_name] = self.get_starting_timestamp(context)

        return params

    def get_next_page_token(
        self, response: requests.Response, previous_token: Optional[Any]
    ) -> Optional[Any]:
        """Return token for identifying next page or None if not applicable."""
        return response.headers.get("X-Next-Page", None)

    @staticmethod
    def _url_encode(val: Union[str, datetime, bool, int, List[str]]) -> str:
        """Encode the val argument as url-compatible string."""
        return urllib.parse.quote_plus(str(val))

    def get_url(self, context: Optional[dict]) -> str:
        """Get stream entity URL."""
        url = "".join([self.url_base, self.path or ""])
        vals = copy.copy(dict(self.config))
        vals.update(context or {})
        for key, val in vals.items():
            search_text = "".join(["{", key, "}"])
            if search_text in url:
                url = url.replace(search_text, self._url_encode(val))
                if "{project_path}" in search_text:
                    self.logger.debug(
                        f"Found project arg. URL is {url} after parsing "
                        f"input val '{val}' to '{self._url_encode(val)}'."
                    )

        return url

    def post_process(self, row: dict, context: Optional[dict] = None) -> Optional[dict]:
        """Post process records."""
        result = super().post_process(row, context)
        del row
        if result is None:
            return None

        assert context is not None  # Tell linter that context is non-null

        for key, val in context.items():
            if key in self.schema.get("properties", {}) and key not in result:
                result[key] = val

        return result


class ProjectBasedStream(GitLabStream):
    """Base class for streams that are keys based on project ID."""

    state_partitioning_keys = ["project_id"]


class NoSinceProjectBasedStream(ProjectBasedStream):
    """Base class for streams lacking a "since" query parameter.

    It includes logic to emulate a "since" query parameter, for
    instance for notes streams (eg. issue notes, MR notes...).
    """

    def get_next_page_token(
        self, response: requests.Response, previous_token: Optional[Any]
    ) -> Optional[Any]:
        """Emulate a "since" parameter for streams that do not support it.

        Return a token for identifying next page or None if no more pages.
        """
        # extract the cutoff time from request parameters
        request_parameters = parse_qs(str(urlparse(response.request.url).query))
        # parse_qs interprets "+" as a space, revert this to keep an aware datetime
        try:
            cutoff = (
                request_parameters[self.bookmark_param_name][0].replace(" ", "+")
                if self.bookmark_param_name in request_parameters
                else None
            )
        except IndexError:
            cutoff = None

        if cutoff is not None:
            # get result items from response
            resp_json = response.json()
            results = resp_json
            if len(results) == 0:
                # gitlab API sometimes returns empty lists, no need to paginate further
                return None
            # if we receive items past the cutoff timestamp, we can stop paginating
            if parse(results[-1][self.replication_key]) < parse(cutoff):
                return None

        return super().get_next_page_token(response, previous_token)


class GroupBasedStream(GitLabStream):
    """Base class for streams that are keys based on group ID."""

    state_partitioning_keys = ["group_id"]

    @property
    def partitions(self) -> List[dict]:
        """Return a list of partition key dicts (if applicable), otherwise None."""
        if "{group_id}" in self.path:
            if "groups" not in self.config:
                raise ValueError(
                    f"Missing `groups` setting which is required for the "
                    f"'{self.name}' stream."
                )

            return [
                {"group_id": id} for id in cast(list, self.config["groups"].split(" "))
            ]

        raise ValueError(
            "Could not detect partition type for Gitlab stream "
            f"'{self.name}' ({self.path}). "
            "Expected a URL path containing '{project_path}' or '{group_id}'. "
        )


class GitlabGraphQLStream(GraphQLStream, GitLabStream):
    """Base class for graphql streams."""

    @property
    def url_base(self) -> str:
        """Return the base url for graphql streams."""
        base_url = self.config.get("graphql_api_url_base", DEFAULT_GRAPHQL_API_URL)
        return f"{base_url}/graphql"

    # the jsonpath under which to fetch the list of records from the graphql response
    records_jsonpath: str = "$.data.[*]"  # type: ignore

    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        """Parse the response and return an iterator of result rows.

        .. _requests.Response:
            https://docs.python-requests.org/en/latest/api/#requests.Response
        """
        resp_json = response.json()
        yield from extract_jsonpath(self.records_jsonpath, input=resp_json)
