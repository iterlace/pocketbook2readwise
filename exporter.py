import os
import re
import asyncio
import datetime as dt
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import aiohttp
import pydantic

LINE_BREAK_SYMBOLS = ["\xa0", "\u2029"]
PAGE_REGEX = re.compile(r".*page=(\d*).*", re.I)


class Highlight(pydantic.BaseModel):
    # reverse reference
    book: "Book"

    id_: Optional[str] = pydantic.Field(alias="id")
    created_at: dt.datetime
    quote: str
    note: Optional[str]
    page: Optional[int]


class BookCover(pydantic.BaseModel):
    width: int
    height: int
    path: pydantic.HttpUrl


class BookMetadata(pydantic.BaseModel):
    authors: Optional[str]
    cover: Optional[List[BookCover]]


class Book(pydantic.BaseModel):
    id: str
    path: str
    title: str
    fast_hash: str
    metadata: BookMetadata


Highlight.update_forward_refs()


class Pocketbook:
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/109.0"
    root = "https://cloud.pocketbook.digital/api/v1.0/"

    def __init__(self):
        self.session = aiohttp.ClientSession()
        self.session.headers["User-Agent"] = self.ua
        self.is_authenticated = False

    async def auth(self):
        provider = os.environ.get("POCKETBOOK_LOGIN_PROVIDER")
        if provider is None:
            raise EnvironmentError(f"POCKETBOOK_LOGIN_PROVIDER is missing!")

        login_data_raw = os.environ.get("POCKETBOOK_LOGIN_DATA")
        if login_data_raw is None:
            raise EnvironmentError(f"POCKETBOOK_LOGIN_DATA is missing!")

        request_data = {}
        for record in login_data_raw.split("&"):
            k, v = record.split("=")
            request_data[k] = v

        url = self.url(f"auth/login/{provider}")
        async with self.session.post(url=url, data=request_data) as response:
            assert response.status == 200
            data = await response.json()

            self.session.headers["Authorization"] = f"{data['token_type']} {data['access_token']}"  # fmt: skip
            self.is_authenticated = True

    def url(self, path: str) -> str:
        if path.startswith("/"):
            path = path[1:]
        return urljoin(self.root, path)

    async def get_all_books(self) -> List[Book]:
        async with self.session.get(self.url("books?limit=10000")) as response:
            assert response.status == 200
            items = (await response.json())["items"]

        books = pydantic.parse_obj_as(List[Book], items)
        return books

    async def get_book_highlights(self, book: Book) -> Any:
        url = self.url(f"notes?fast_hash={book.fast_hash}")
        async with self.session.get(url) as response:
            assert response.status == 200
            data = await response.json()
            assert isinstance(data, list)

        futures = []
        for highlight_raw in data:
            type_ = highlight_raw.get("type", None)
            if not type_:
                continue
            if type_ in ("bookmark",):
                continue
            if type_ not in ("highlight", "note"):
                breakpoint()

            futures.append(self.get_highlight(book, highlight_raw["uuid"]))

        highlights = await asyncio.gather(*futures)
        return highlights

    async def get_highlight(self, book: Book, highlight_id: str) -> Highlight:
        url = self.url(f"notes/{highlight_id}?fast_hash={book.fast_hash}")
        async with self.session.get(url=url) as response:
            assert response.status == 200
            data = await response.json()

        # Parse quote
        quote: str = data["quotation"]["text"]
        for symbol in LINE_BREAK_SYMBOLS:
            quote = quote.replace(symbol, "\n")
        quote = quote.strip()

        # Parse note
        note: Optional[str] = None
        if data["type"]["value"] == "note":
            note = data["note"]["text"]
            for symbol in LINE_BREAK_SYMBOLS:
                note = note.replace(symbol, "\n")
            note = note.strip()

        # Parse page
        page = None
        if begin := data["quotation"].get("begin", None):
            if match := PAGE_REGEX.match(begin).group(1):
                if isinstance(match, str) and match.isnumeric():
                    page = int(match)

        created_at = dt.datetime.fromtimestamp(data["mark"]["created"])

        fallback_id = None
        if anchor := data["mark"].get("anchor", None):
            fallback_id = f"{book.id}_{anchor}"
        id_ = data.get("orig_id", data.get("uuid", fallback_id))
        return Highlight(
            book=book, id=id_, quote=quote, note=note, page=page, created_at=created_at
        )


def serialize_highlight(highlight: Highlight) -> Dict[str, Any]:
    result = {
        "text": highlight.quote,
        "title": highlight.book.title,
        "author": highlight.book.metadata.authors,
        "source_type": "pocketbook",
        "category": "books",
        "location_type": "page",
        "location": highlight.page,
        "note": highlight.note,
        "highlighted_at": highlight.created_at.isoformat(),
        "highlight_url": highlight.id_,
    }

    if highlight.book.metadata.cover:
        cover = max(highlight.book.metadata.cover, key=lambda c: c.height * c.width)
        result["image_url"] = cover.path

    return result


async def gather_all_highlights() -> Any:
    pb_client = Pocketbook()
    await pb_client.auth()
    books = await pb_client.get_all_books()

    all_highlights = []

    futures = [pb_client.get_book_highlights(book) for book in books]
    results = await asyncio.gather(*futures)
    for group in results:
        all_highlights.extend(group)
    return all_highlights


async def push_to_readwise(highlights: List[Dict[str, Any]]) -> Any:
    async with aiohttp.ClientSession() as session:
        token = os.environ["READWISE_TOKEN"]
        async with session.post(
            "https://readwise.io/api/v2/highlights/",
            headers={"Authorization": f"Token {token}"},
            json={"highlights": highlights},
        ) as response:
            return await response.json()


def handler(pd: Any = None):
    loop = asyncio.new_event_loop()
    all_highlights = loop.run_until_complete(gather_all_highlights())
    serialized = [serialize_highlight(h) for h in all_highlights]

    return loop.run_until_complete(push_to_readwise(serialized))


if __name__ == "__main__":
    handler()
