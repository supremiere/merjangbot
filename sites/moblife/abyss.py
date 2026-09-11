# 어비스 API 조회·복호화와 출현 주기·현재 상태·알림 시점을 계산합니다.
import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .models import AbyssRecord

KST = timezone(timedelta(hours=9))
ABYSS_CYCLE = timedelta(hours=36, minutes=15)
ABYSS_ALERT_MINUTES = (60, 30, 10, 1)


def split_eab_payload(payload):
    if len(payload) < 12:
        raise ValueError("EAB payload가 너무 짧습니다.")

    tail_length = int(payload[1:3])
    iv_padding = int(payload[3:4])
    data_padding = int(payload[0:1])

    iv_base64 = payload[4:14][::-1] + payload[-tail_length:] + ("=" * iv_padding)

    encrypted_base64 = payload[14:-tail_length] + ("=" * data_padding)

    iv = base64.b64decode(iv_base64)
    encrypted = base64.b64decode(encrypted_base64)

    return iv, encrypted


def decrypt_eab_payload(payload, key):
    iv, encrypted = split_eab_payload(payload)
    key_bytes = key.encode("utf-8")

    decryptor = Cipher(
        algorithms.AES(key_bytes),
        modes.CBC(iv),
    ).decryptor()

    padded_plain = decryptor.update(encrypted) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded_plain) + unpadder.finalize()

    return json.loads(plain.decode("utf-8"))


def parse_iso_datetime(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def select_latest_abyss_record(records):
    valid = []

    for record in records:
        if not isinstance(record, dict):
            continue

        start = record.get("start_datetime")
        if not start:
            continue

        try:
            start_dt = parse_iso_datetime(start)
        except Exception:
            continue

        valid.append((start_dt, record))

    if not valid:
        return None

    valid.sort(key=lambda item: item[0], reverse=True)
    return valid[0][1]


async def discover_moblife_key(client):
    """모비라이프 공개 프론트 JS에서 현재 EAB 복호화 키를 찾는다."""
    html = await client.http.get_text(client.base_url + "/")
    soup = BeautifulSoup(html, "html.parser")

    script_urls = []
    for tag in soup.find_all("script", src=True):
        script_urls.append(urljoin(client.base_url + "/", tag["src"]))

    for script_url in script_urls:
        try:
            text = await client.http.get_text(script_url)
        except Exception:
            continue

        if "DH:()=>" not in text:
            continue

        # DH:()=>d 형태에서 변수명 d를 얻는다.
        var_match = re.search(r"DH:\(\)=>([A-Za-z_$][A-Za-z0-9_$]*)", text)
        if not var_match:
            continue

        variable = var_match.group(1)
        start = max(0, var_match.start() - 1000)
        end = min(len(text), var_match.start() + 5000)
        nearby = text[start:end]

        key_match = re.search(
            rf"\b{re.escape(variable)}\s*=\s*\"([^\"]{{16,64}})\"",
            nearby,
        )

        if key_match:
            return key_match.group(1)

    raise RuntimeError("모비라이프 어비스 복호화 키를 찾지 못했습니다.")


class AbyssService:
    def __init__(self, client):
        self.client = client
        self.key = client.eab_key
        self.anchor: AbyssRecord | None = None
        self._refresh_lock = asyncio.Lock()

    async def get_records(self, force_key_refresh=False):
        if force_key_refresh:
            try:
                self.key = await discover_moblife_key(self.client)
            except Exception:
                self.key = self.client.eab_key
        data = await self.client.get("/d/api/v1/eab")
        payload = data.get("payload")
        if not payload:
            raise RuntimeError("모비라이프 EAB 응답에 payload가 없습니다.")
        try:
            records = decrypt_eab_payload(payload, self.key)
        except Exception as error:
            if not force_key_refresh:
                return await self.get_records(force_key_refresh=True)
            raise RuntimeError("어비스 데이터 복호화 실패") from error
        if not isinstance(records, list):
            raise RuntimeError("어비스 데이터 형식이 예상과 다릅니다.")
        return records

    async def refresh(self):
        async with self._refresh_lock:
            latest = select_latest_abyss_record(await self.get_records())
            if latest is None:
                raise RuntimeError("사용 가능한 어비스 출현 데이터가 없습니다.")
            self.anchor = latest

    def get_next_abyss_spawn(self, now_utc):
        if not self.anchor:
            return None, False

        start_text = self.anchor.get("start_datetime")
        if not start_text:
            return None, False

        spawn = parse_iso_datetime(start_text)
        is_estimated = bool(self.anchor.get("is_post_maintenance_estimate", False))

        # 점검 후 추정값은 사이트에서 갱신될 때까지 그 1회 예상시간만 사용
        if is_estimated:
            if spawn <= now_utc:
                return None, True
            return spawn, True

        # 일반 데이터는 모비라이프와 동일하게 36시간 15분 주기로 다음 시간을 계산
        if spawn <= now_utc:
            elapsed = now_utc - spawn
            cycles = int(elapsed.total_seconds() // ABYSS_CYCLE.total_seconds()) + 1
            spawn = spawn + (ABYSS_CYCLE * cycles)

        return spawn, False

    def get_abyss_status(self, now_utc):
        """현재 출현 중인지, 아니면 다음 출현 시각이 언제인지 계산한다."""
        if not self.anchor:
            return None, False, False

        start_text = self.anchor.get("start_datetime")
        if not start_text:
            return None, False, False

        spawn = parse_iso_datetime(start_text)
        estimated = bool(self.anchor.get("is_post_maintenance_estimate", False))

        # 점검 후 예상값은 아직 오지 않은 1회 예상 시각만 표시
        if estimated:
            if spawn <= now_utc:
                return None, False, True
            return spawn, False, True

        active_duration = timedelta(minutes=15)

        # 기준 시각에서 36시간 15분 단위로 현재/다음 회차를 찾는다.
        while spawn + active_duration <= now_utc:
            spawn += ABYSS_CYCLE

        is_active = spawn <= now_utc < spawn + active_duration
        return spawn, is_active, False

    def due_alerts(self, now_utc):
        spawn, estimated = self.get_next_abyss_spawn(now_utc)
        if spawn is None:
            return []
        remaining = (spawn - now_utc).total_seconds()
        return [
            (spawn, minutes, estimated)
            for minutes in ABYSS_ALERT_MINUTES
            if minutes * 60 - 59 <= remaining <= minutes * 60
        ]
