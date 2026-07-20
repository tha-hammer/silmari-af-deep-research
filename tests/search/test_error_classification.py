import asyncio

import aiohttp
import pytest
from hypothesis import given
from hypothesis import strategies as st
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from skills.search.errors import Transience, classify_search_error


def _http_error(status: int) -> aiohttp.ClientResponseError:
    url = URL("https://provider.example/search")
    headers = CIMultiDictProxy(CIMultiDict())
    request_info = aiohttp.RequestInfo(url, "GET", headers, url)
    return aiohttp.ClientResponseError(request_info, (), status=status)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (408, Transience.TRANSIENT),
        (429, Transience.TRANSIENT),
        (500, Transience.TRANSIENT),
        (501, Transience.TRANSIENT),
        (502, Transience.TRANSIENT),
        (503, Transience.TRANSIENT),
        (504, Transience.TRANSIENT),
        (599, Transience.TRANSIENT),
        (401, Transience.NON_RECOVERABLE),
        (402, Transience.NON_RECOVERABLE),
        (403, Transience.NON_RECOVERABLE),
    ],
)
def test_http_status_classification(status: int, expected: Transience) -> None:
    assert classify_search_error(_http_error(status)) is expected


@pytest.mark.parametrize(
    "error",
    [
        ValueError("JINA_API_KEY environment variable is required"),
        RuntimeError("No search providers available"),
        Exception("unexpected provider failure"),
    ],
)
def test_non_recoverable_errors_fail_closed(error: BaseException) -> None:
    assert classify_search_error(error) is Transience.NON_RECOVERABLE


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        asyncio.TimeoutError(),
        aiohttp.ClientConnectionError("connection reset"),
    ],
)
def test_transport_errors_are_transient(error: BaseException) -> None:
    assert classify_search_error(error) is Transience.TRANSIENT


@given(status=st.integers(min_value=400, max_value=599))
def test_every_http_error_maps_to_one_transience(status: int) -> None:
    classification = classify_search_error(_http_error(status))
    expected = (
        Transience.TRANSIENT
        if status in {408, 429} or 500 <= status <= 599
        else Transience.NON_RECOVERABLE
    )

    assert classification is expected
