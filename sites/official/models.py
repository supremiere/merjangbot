# 공식 홈페이지에서 가져오는 게시물의 공통 데이터 형식을 정의합니다.
from typing import TypedDict


class Notice(TypedDict):
    url: str
    category: str
    title: str
