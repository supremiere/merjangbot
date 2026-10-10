import asyncio
from unittest.mock import AsyncMock

import pytest

from discord_bot.presenters.rune_stats import build_rune_stats_embed
from sites.erinndata.models import RUNE_CLASSES
from sites.erinndata.parser import parse_page
from sites.erinndata.runes import RuneStatsService


def bar(name, rate):
    return f'<div class="bar"><b>{name}</b><i class="mt">신화</i><em>{rate}%</em></div>'


def current_page():
    cards = []
    for name in RUNE_CLASSES:
        cards.append(f'''<article class="job v3"><header><div><h3>{name}</h3></div></header>
          <div class="summary">장신구 가짜 요약 100%</div>
          <details><div class="bg" data-part="장신구">
            {bar('상위 룬', 80)}{bar('중간 룬', 79.9)}{bar('경계 룬', 40)}
            {bar('하위 룬', 10)}{bar('제외 룬', 9.9)}</div>
          <div class="bg" data-part="방어구">
            <div class="bst">각성(밤의 축복) <small>택1</small></div>{bar('각성 룬', 55.8)}
            <div class="bst">침식</div>{bar('침식 룬', 46.7)}
            <div class="bst">그 외 <small>20% 이상</small></div>{bar('일반 룬', 81.1)}
            <div class="bst">신화 <small>10% 이상</small></div>{bar('신화 룬', 12.1)}</div>
          <div class="bg" data-part="무기">{bar('무기 룬', 78.7)}</div>
          <div class="bg" data-part="엠블럼">{bar('엠블럼 룬', 69.9)}</div></details>
          <div class="sts"><div class="st" data-mod="156"><div class="st-h"><b>조합 A</b></div>
            <div class="st-s">세공 강타 › 보조</div><div class="st-u">조합 메모</div></div></div>
          <div class="cbs"><div class="cb"><div class="cw">10월 오토</div>
            <div class="cc"><span class="sk"><b>1</b></span> → <span class="sk"><b>2</b></span></div>
            <div class="cm">운용 메모</div></div></div></article>''')
    return '<p>10.10 갱신 채용률 10.05</p>' + ''.join(cards)


def test_current_cards_read_detailed_rates_and_keep_class_boundaries():
    data = parse_page(current_page())
    assert set(data) == set(RUNE_CLASSES)
    row = data['기사']
    assert row['data_date'] == '10.05'
    assert row['accessory'] == {
        '80': [{'name': '상위 룬', 'rate': '80%'}],
        '40': [{'name': '중간 룬', 'rate': '79.9%'}, {'name': '경계 룬', 'rate': '40%'}],
        '10': [{'name': '하위 룬', 'rate': '10%'}],
    }
    assert row['defense']['각성'] == [{'name': '각성 룬', 'rate': '55.8%'}]
    assert row['defense']['신화'] == [{'name': '신화 룬', 'rate': '12.1%'}]
    assert row['weapon'] == [{'name': '무기 룬', 'rate': '78.7%'}]
    assert row['emblem'] == [{'name': '엠블럼 룬', 'rate': '69.9%'}]
    assert row['combos'][0] == {'combo': '조합 A', 'upgrade': '156', 'engraving': '강타 › 보조', 'note': '조합 메모'}
    assert row['operations'][0]['combo'] == '1 → 2'
    embed = build_rune_stats_embed(row)
    assert '신화 룬' in next(f.value for f in embed.fields if '방어구' in f.name)


def test_legacy_grouped_page_still_parses():
    html = '<p>2026.09.07 기준</p>' + ''.join(
        f'<h3>{name}</h3><p>장신구</p><p>80% 이상</p><p>구속 91.2%</p>'
        '<p>방어구</p><p>각성</p><p>서약 55.8%</p><p>무기</p><p>잔영 78.7%</p>'
        '<p>엠블럼</p><p>천칭 69.9%</p><p>실전 세팅</p>' for name in RUNE_CLASSES
    )
    row = parse_page(html)['기사']
    assert row['data_date'] == '2026-09-07'
    assert row['accessory']['80'] == [{'name': '구속', 'rate': '91.2%'}]
    assert row['defense']['각성'] == [{'name': '서약', 'rate': '55.8%'}]


def test_invalid_current_page_preserves_previous_cache():
    class Repository:
        saved = None

        def save(self, data):
            self.saved = data

    repository = Repository()
    http = type('Http', (), {})()
    http.get_text = AsyncMock(return_value=current_page().replace('<em>', '<span>').replace('</em>', '</span>'))
    service = RuneStatsService(http, repository)
    previous = {'기사': {'class_name': '기사'}}
    service.cache = previous
    with pytest.raises(RuntimeError, match='핵심 파싱 검증 실패'):
        asyncio.run(service.refresh())
    assert service.cache is previous
    assert repository.saved is None
