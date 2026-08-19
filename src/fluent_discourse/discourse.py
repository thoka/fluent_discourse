from typing import Any
import requests
from json.decoder import JSONDecodeError
from simplejson.errors import JSONDecodeError as SimpleJSONDecodeError  
from .errors import *
import time
import logging
import os
from copy import deepcopy

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
logger.addHandler(logging.StreamHandler())
logger.setLevel(logging.INFO)

# Transient server-side errors: retried with exponential backoff instead of
# failing the whole run. 500 is deliberately excluded - it usually signals a
# genuine application error on the Discourse side, not a temporary condition.
RETRYABLE_STATUS_CODES = {502, 503, 504}

class DiscourseApiPath:
    def __init__(self, discourse, path, logger=logger):
        self.discourse = discourse
        self._path = path
        self._logger = logger

    def _(self, name):
        # Add name to path
        return DiscourseApiPath(
            self.discourse,
            self._path + [str(name)],
        )
    
    def get(self, data=None):
        # Make a get request
        url = self._make_url()
        return self.discourse._request("GET", url, params=data)
    
    def get_all(self, data=None):
        """
        Get all results from a paginated API endpoint. 
        Uses the `offset` and `page` parameters to fetch all results.
        Limit is guessed from the first page response.
        If the first page does not contain exactly one list, it will raise an AssertionError.
        """

        # get first page
        if data is None:
            data = {}

        # TODO: respect pagination parameters
        data["offset"] = 0
        data["page"] = 0  

        first_page = self.get(data=data)

        keys_with_lists = [key for key, value in first_page.items() if isinstance(value, list)]

        assert len(keys_with_lists) == 1, "Expected exactly one key with a list value in the first page response" 

        key_to_results = keys_with_lists[0]

        res = first_page[key_to_results]
        batch_size = len(res)
       
        while True:
            # Get next chunk of results
            data["offset"] = len(res)
            data["page"] += 1           
            next_page = self.get(data=data)

            if not next_page or not isinstance(next_page, dict):
                break

            if key_to_results not in next_page:
                break

            next_results = next_page[key_to_results]
            if not next_results:
                break

            res.extend(next_results)

            if len(next_results) < batch_size:
                # If the next page has fewer results than the batch size, we assume it's the last page
                break

        first_page[key_to_results] = res

        return first_page
    

    def post(self, data=None):
        # Make a post request
        url = self._make_url()
        return self.discourse._request("POST", url, data=data)

    def put(self, data=None):
        # Make a put request
        url = self._make_url()
        return self.discourse._request("PUT", url, data=data)

    def delete(self, data=None):
        # Make a delete request
        url = self._make_url()
        return self.discourse._request("DELETE", url, data=data)

    def _make_url(self):
        # Build the request url from cache segments
        endpoint = "/".join(self._path)
        # strip forward slash from e.g. '.json' or '.rss' segments if passed
        endpoint = endpoint.replace("/.", ".")

        if "." not in endpoint: # add json as default format
            endpoint += ".json"

        url = f"{self.discourse._base_url}/{endpoint}"
        return url

    def __getattr__(self, name):
        """
        Calling self.attribute_name adds "attribute_name" to self._cache

        Only works for strings
        """
        if name == "json":
            return self._(f".{name}")
        return self._(name)

    def __getitem__(self, name):
        """
        Calling self[attribute_name] adds "attribute_name" to self._cache

        Primarily for integers
        """
        return self._(name)

class Cache:
    def __init__(self):
        self.groups = {}
        self.categories = {}
        self.users = {}

class Discourse:
    def __init__(
        self, base_url, username, api_key, #path=None,
        raise_for_rate_limit=True, debug=False, timeout= 10,
        max_retries=5, retry_backoff_base=1, retry_backoff_max=60,
    ):
        if base_url[-1] == "/":
            # Remove trailing slash from base_url
            base_url = base_url[:-1]
        self._base_url = base_url
        self._username = username
        self._api_key = api_key
        # self._cache = path or []
        self._raise_for_rate_limit = raise_for_rate_limit
        self._headers = {
            "Content-Type": "application/json",
            "Api-Username": self._username,
            "Api-Key": self._api_key,
        }
        self._debug = debug
        self._timout = timeout
        self._max_retries = max_retries
        self._retry_backoff_base = retry_backoff_base
        self._retry_backoff_max = retry_backoff_max
        self.cache = Cache()
        self.domain = base_url.split("//")[-1].split("/")[0]

    @staticmethod
    def from_env(raise_for_rate_limit=True):
        base_url = os.environ.get("DISCOURSE_URL")
        username = os.environ.get("DISCOURSE_USERNAME")
        api_key = os.environ.get("DISCOURSE_API_KEY")
        return Discourse(
            base_url, username, api_key, raise_for_rate_limit=raise_for_rate_limit
        )

    def _(self, name):
        # Add name to cache, return self
        return DiscourseApiPath(
            self,
            [str(name)],
        )

    def _request(self, method, url, data=None, params=None, attempt=0):

        r = requests.request(
            method, url, json=data, params=params, headers=self._headers, timeout=self._timout
        )

        info = (f"""{method} {url}
DATA:{data}
RESP:{r.status_code}
{r.text}""")

        # print(info)
        #TODO log info

        if r.status_code == 200:
            try:
                return r.json()
            except (SimpleJSONDecodeError, JSONDecodeError) as e:
                # Request succeeded but response body was not valid JSON
                return r.text
        else:
            return self._handle_error(r, method, url, data, params, attempt)

    def _handle_error(self, response, method, url, data, params, attempt=0):
        if response.status_code == 404:
           raise PageNotFoundError(
                f"The requested page was not found, or you do not have permission to access it: {response.url}"
            )
        elif response.status_code == 403:
            raise UnauthorizedError("Invalid credentials")
        elif response.status_code == 429:
            if self._raise_for_rate_limit:
                raise RateLimitError("Rate limit hit")
            else:
                self._wait_for_rate_limit(response, method, url, data, params)
                return self._request(method, url, data, params, attempt)
        elif response.status_code in RETRYABLE_STATUS_CODES:
            if attempt >= self._max_retries:
                raise DiscourseError(
                    f"Discourse server error persisted after {attempt} retries: "
                    f"{response.status_code} - {response.text}"
                )
            self._wait_for_retryable_error(response, attempt)
            return self._request(method, url, data, params, attempt + 1)
        else:
                raise DiscourseError(
                f"Unhandled discourse exception: {response.status_code} - {response.text}"
            )

    def _wait_for_retryable_error(self, response, attempt):
        wait_seconds = min(
            self._retry_backoff_base * (2 ** attempt), self._retry_backoff_max
        )
        logger.warning(
            f"Discourse server error {response.status_code}, retrying in "
            f"{wait_seconds}s (attempt {attempt + 1}/{self._max_retries})"
        )
        time.sleep(wait_seconds)

    def _wait_for_rate_limit(self, response, method, url, data, params):
        # get the number of seconds to wait before retrying, add 1 for 0 errors
        try:
            wait_seconds = int(response.json()["extras"]["wait_seconds"]) + 1
        except:
            wait_seconds = 10
        # add piece to rate limit and then try again
        logger.debug(
            f"Discourse rate limit hit, trying again in {wait_seconds} seconds"
        )
        # sleep for wait_seconds
        time.sleep(wait_seconds)
        return

    def __getattr__(self, name):
        """
        Calling self.attribute_name adds "attribute_name" to self._cache

        Only works for strings
        """
        if name == "json":
            return self._(f".{name}")
        return self._(name)

    def __getitem__(self, name):
        """
        Calling self[attribute_name] adds "attribute_name" to self._cache

        Primarily for integers
        """
        return self._(name)
    

    def __setattr__(self, __name: str, __value: Any) -> None:

        self.__dict__[__name] = __value
