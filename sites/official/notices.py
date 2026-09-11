# 공식 공지·업데이트 HTML을 조회하고 게시물 목록으로 변환합니다.
import re

from bs4 import BeautifulSoup

from .models import Notice

BASE_URL = "https://mabinogimobile.nexon.com"
NOTICE_URL = f"{BASE_URL}/News/Notice"
UPDATE_URL = f"{BASE_URL}/News/Update"


def parse_posts(html: str, category: str) -> list[Notice]:
    soup = BeautifulSoup(html, "html.parser")

    if category == "공지":
        pattern = re.compile(r"^/News/Notice/\d+", re.IGNORECASE)
    else:
        pattern = re.compile(r"^/News/Update/\d+", re.IGNORECASE)

    posts = []
    found_urls = set()

    for link in soup.find_all("a", href=True):
        href = link.get("href", "")
        title = link.get_text(" ", strip=True)

        if not pattern.match(href) or not title:
            continue

        full_url = BASE_URL + href if href.startswith("/") else href

        if full_url in found_urls:
            continue

        found_urls.add(full_url)
        posts.append(
            {
                "category": category,
                "title": title,
                "url": full_url,
            }
        )

    return posts


class OfficialNotices:
    def __init__(self, http):
        self.http = http

    async def get_posts(self, page_url, category):
        html = await self.http.get_text(page_url, headers={"User-Agent": "Mozilla/5.0"})
        return parse_posts(html, category)

    async def fetch_all(self):
        notices = await self.get_posts(NOTICE_URL, "공지")
        updates = await self.get_posts(UPDATE_URL, "업데이트")
        return notices, updates
