"""
МРК ф17_к3 — выгрузка для дома 4824 (YOUR_TEMPLATE_B_TITLE).

На базе template_b_server.py (шаблон 620889509, «Импорт» 1721521103).
Правила f17 отличаются от f11: I15/I16 — API (не «Импорт»), I19 = статус помещения 4,
I51 = любая приёмка «Не принята», I52 = I51−I54, I53 не используется,
синие — по status_id claims как в template_b_server.
"""
import argparse
import os
import sys
import time
import traceback
from itertools import zip_longest
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import gspread
from gspread.utils import ValueInputOption
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from collections import Counter, defaultdict
import concurrent.futures
import re

from mrk_object_audit import (
    build_flags_for_room,
    build_object_audit_row,
    object_audit_header,
)

# f17: I18/P18 — шаблон; I58/I59/Q32 — «Импорт»

# ID листа «Импорт» в этой книге
IMPORT_SHEET_ID = "1721521103"
# Лист проверки правил parser_template_b (позиции по объектам)
AUDIT_SHEET_ID = 1944447987
# Справочник правил parser_template_b (понятный язык); ежедневная выгрузка отключена
PLAIN_RULES_SHEET_ID = 2002965026
WRITE_PLAIN_RULES_ON_RUN = False


def _deal_contract_type_id(room):
    deal = room.get("deal")
    if not isinstance(deal, dict):
        return None
    v = deal.get("contract_type_id")
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _batch_acells(worksheet, pairs):
    if not pairs:
        return
    data = [{"range": cell, "values": [[val]]} for cell, val in pairs]
    worksheet.batch_update(
        data, raw=False, value_input_option=ValueInputOption.user_entered
    )


def _write_diff_cells(worksheet, pairs):
    """Пишет диффы по одной ячейке — values:batchUpdate с кучей K15/R15… часто не попадает в лист."""
    for cell, val in pairs:
        try:
            worksheet.update_acell(cell, val)
        except Exception as e:
            print(f"  Ошибка записи диффа {cell} = {val!r}: {e}", flush=True)
            raise


def _parse_float_cell(val):
    if val is None or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).strip().replace(",", ".").replace("\xa0", ""))
    except ValueError:
        return 0.0


def _batch_get_cells_float(worksheet, cells):
    """Читает список ячеек; при обрезанном/пустом ответе batch_get не даёт KeyError в ШАГ 5."""

    def _value_from_block(block):
        v = None
        if block is not None and len(block) > 0:
            row0 = block[0]
            if isinstance(row0, (list, tuple)) and len(row0) > 0:
                v = row0[0]
            elif row0 is not None and not isinstance(row0, (list, tuple)):
                v = row0
        return _parse_float_cell(v)

    if not cells:
        return {}
    try:
        raw = worksheet.batch_get(cells)
    except Exception as e:
        print(f"  batch_get не удался ({e}), читаем ячейки по одной", flush=True)
        out = {}
        for cell in cells:
            try:
                out[cell] = _parse_float_cell(worksheet.acell(cell).value)
            except Exception as e2:
                print(f"    {cell}: {e2}", flush=True)
                out[cell] = 0.0
        return out
    if raw is None:
        raw = []
    out = {}
    for cell, block in zip_longest(cells, raw, fillvalue=None):
        out[cell] = _value_from_block(block)
    return out


def get_import_layout_block(ss) -> dict[str, str]:
    """Лист «Импорт»: проф/моб, АПП (I18/P18 — из шаблона, как template_b_server)."""
    import_worksheet = ss.get_worksheet_by_id(int(IMPORT_SHEET_ID))
    mapping = {
        "I58": "B6",
        "I59": "B4",
        "P58": "C6",
        "P59": "C4",
        "Q32": "B8",
        "Q34": "C8",
    }
    out: dict[str, str] = {}
    print("\nШАГ 0.6: I58/I59/P58/P59, Q32/Q34 с листа «Импорт»")
    for target_cell, source_cell in mapping.items():
        try:
            value = import_worksheet.acell(source_cell).value
            out[target_cell] = value if value else "0"
            print(f"  {target_cell} <- {source_cell} (Импорт) = {out[target_cell]}")
        except Exception as e:
            print(f"  Ошибка получения {source_cell}: {e}")
            out[target_cell] = "0"
    return out


def _is_online_sign(deal: dict) -> bool:
    if not isinstance(deal, dict):
        return False
    vals = [str(deal.get("inspection_sign_type") or "").strip().lower(),
            str(deal.get("sign_type") or "").strip().lower()]
    online_marks = {"online", "edo", "edm", "digital", "электронно", "эцп"}
    return any(v in online_marks for v in vals)


# --- Статусы клиентской приёмки (iflat API) ---
INSP_NOT_ACCEPTED = 5
INSP_ACCEPTED_OK = 4
INSP_ACCEPTED_REMARKS = 8
# Клиентская приёмка «в процессе» (взята в работу / идёт приёмка)
INSP_IN_PROGRESS_IDS = {2, 3}

# f17: рекомендовано — статусы API (не «Импорт»)
F17_RECOMEND_STATUSES = {2, 3, 4, 5, 7, 6, 11, 12}
F17_NOT_RECOMEND_STATUSES = {1, 8, 9, 10}
F17_RED_UPS_ROOM_STATUSES = {2, 4}
# f17 I19: записано = статус помещения «Клиентская приёмка: в процессе» (4)
F17_ZAPIS_ROOM_STATUS = 4
# f17: синие claims по status_id (не по названию, как template_b_server)
F17_CLAIMS_INWORK = {1, 2, 8}
F17_CLAIMS_DONE = {9, 3}
MEETING_STATUS_IDS = (INSP_ACCEPTED_OK, INSP_NOT_ACCEPTED, INSP_ACCEPTED_REMARKS)

# Запасной справочник помещений (если API недоступен) — совпадает с GET /rooms/statuses
F17_ROOM_STATUS_FALLBACK: dict[int, str] = {
    1: "Строится",
    2: "Клиентская приемка: Открыта запись",
    3: "Приемка отменена",
    4: "Клиентская приемка: В процессе",
    5: "Клиентская приемка: Замечания",
    6: "Принято",
    7: "Клиентская приемка: Готово к передаче",
    8: "Внутренняя приемка: Готово к передаче",
    9: "Внутренняя приемка: В процессе",
    10: "Внутренняя приемка: Замечания",
    11: "Принято с замечаниями",
    12: "Односторонняя передача",
}


def _iflat_oauth_payload() -> dict:
    return {
        "username": "YOUR_CRM_USERNAME",
        "password": "YOUR_CRM_PASSWORD",
        "account_id": 379,
        "client_id": 2,
        "client_secret": "YOUR_CRM_CLIENT_SECRET",
        "grant_type": "login",
    }


def iflat_request_headers() -> dict:
    r = requests.post(
        "https://YOUR_CRM_API_HOST/api/v1/oauth/token",
        _iflat_oauth_payload(),
        timeout=60,
    )
    r.raise_for_status()
    return {
        "Authorization": f"Bearer {r.json()['access_token']}",
        "Content-Type": "application/json",
    }


def _merge_status_catalog(
    fetched: dict[int, str], fallback: dict[int, str]
) -> dict[int, str]:
    out = dict(fallback)
    out.update({k: v for k, v in fetched.items() if v})
    return out


def _q_iflat(name: str) -> str:
    return f"«{name}»"


def _room_st(room: dict[int, str], sid: int) -> str:
    return _q_iflat(room.get(sid, f"status_id={sid}"))


def _insp_st(insp: dict[int, str], sid: int) -> str:
    return _q_iflat(insp.get(sid, f"status_id={sid}"))


def _claim_st(claim: dict[int, str], sid: int) -> str:
    return _q_iflat(claim.get(sid, f"status_id={sid}"))


def _join_iflat(labels: list[str], sep: str = ", ") -> str:
    return sep.join(labels)


def build_f17_plain_rules(
    room: dict[int, str],
    insp: dict[int, str],
    claim: dict[int, str],
) -> list[tuple[str, str]]:
    """Ячейка | правило; подписи статусов — как в CRM API."""
    rec_ids = sorted(F17_RECOMEND_STATUSES)
    rec_rooms = _join_iflat([_room_st(room, i) for i in rec_ids])
    meeting_insp = _join_iflat(
        [_insp_st(insp, i) for i in MEETING_STATUS_IDS]
    )
    c32_rooms = _join_iflat(
        [_room_st(room, i) for i in (6, 11, 12)]
    )
    green_rooms = _join_iflat([_room_st(room, i) for i in (6, 12)])
    i54_rooms = _join_iflat([_room_st(room, i) for i in (6, 11, 12)])
    claims_done = _join_iflat([_claim_st(claim, i) for i in sorted(F17_CLAIMS_DONE)])
    claims_inwork = _join_iflat(
        [_claim_st(claim, i) for i in sorted(F17_CLAIMS_INWORK)]
    )
    red_ups_rooms = _join_iflat(
        [_room_st(room, i) for i in sorted(F17_RED_UPS_ROOM_STATUSES)]
    )
    f32_insp = _join_iflat(
        [_insp_st(insp, i) for i in (INSP_ACCEPTED_OK, INSP_ACCEPTED_REMARKS)]
    )

    return [
        ("B4", "Подставляем дату запуска скрипта (сегодня), формат дд.мм.гггг."),
        ("I12", "Считаем все квартиры в доме 17_ф3_корпус (CRM house 4824) — любой статус помещения."),
        ("P12", "Считаем все кладовые в этом доме — любой статус помещения."),
        (
            "I13",
            "Считаем квартиры с saleStatuses=FREE в API (непроданные; подпись в UI CRM — «Свободно»).",
        ),
        ("P13", "То же для кладовых (saleStatuses=FREE)."),
        (
            "I15",
            f"Считаем рекомендованные квартиры: проданные, статус помещения из списка "
            f"{rec_rooms}; "
            f"{_room_st(room, 2)} — только если ещё нет клиентской приёмки (type_id=1); "
            f"{_room_st(room, 5)} — только если последняя приёмка {_insp_st(insp, INSP_NOT_ACCEPTED)}; "
            f"{_room_st(room, 7)} не входит; непроданные (FREE) — только {_room_st(room, F17_ZAPIS_ROOM_STATUS)}.",
        ),
        (
            "P15",
            f"Считаем рекомендованные кладовые: статус из списка {rec_rooms} или {_room_st(room, 1)}; "
            f"для {_room_st(room, 2)} и {_room_st(room, 5)} — те же уточнения по приёмкам, что для квартир.",
        ),
        ("I16", "Формула: всего квартир (I12) минус рекомендованные (I15)."),
        ("P16", "Формула: всего кладовых (P12) минус рекомендованные (P15)."),
        (
            "I18",
            "Берём из шаблона листа отчёта при копировании — «сколько отправлено SMS о назначении встречи», квартиры (не из CRM).",
        ),
        ("P18", "То же для кладовых — из шаблона при создании листа."),
        (
            "I19",
            f"Считаем квартиры со статусом помещения {_room_st(room, F17_ZAPIS_ROOM_STATUS)} (status_id=4).",
        ),
        (
            "P19",
            f"Считаем кладовые со статусом помещения {_room_st(room, F17_ZAPIS_ROOM_STATUS)} (status_id=4).",
        ),
        (
            "I20",
            "Формула: отправлено SMS (I18) − записано (I19) − передано по ДДУ (L32) − красные в работе (I52) − красные УПС (I53).",
        ),
        ("P20", "Формула для кладовых: P18 − P19 − L34 − P52 − P53."),
        (
            "H24",
            f"Считаем записи клиентских приёмок (type_id=1) по квартирам за весь период до сегодня "
            f"со статусом приёмки {meeting_insp} (считаются приёмки, не уникальные квартиры).",
        ),
        ("H25", "То же для кладовых."),
        ("H28", "Считаем приёмки по квартирам за сегодня с теми же статусами приёмки, что в H24."),
        ("H29", "То же для кладовых за сегодня."),
        (
            "C32",
            f"Считаем квартиры со статусом помещения {c32_rooms}.",
        ),
        ("C34", "То же для кладовых."),
        (
            "L32",
            "Из квартир C32 оставляем только с договором ДДУ (contract_type_id = 1 в сделке).",
        ),
        ("L34", "Из кладовых C34 — только ДДУ."),
        (
            "V32",
            "Из квартир C32 оставляем только с договором ДКП (contract_type_id = 2).",
        ),
        ("V34", "Из кладовых C34 — только ДКП."),
        ("Q32", "Копируем с листа «Импорт» ячейку B8 — вручную внесённое число АПП по квартирам (не из CRM)."),
        ("Q34", "Копируем с листа «Импорт» ячейку C8 — АПП по кладовым."),
        (
            "I38",
            f"Считаем квартиры со статусом помещения {green_rooms} (зелёные без замечаний).",
        ),
        ("P38", f"Считаем кладовые со статусом помещения {green_rooms}."),
        (
            "I40",
            f"Считаем квартиры со статусом помещения {_room_st(room, 11)} (синие с замечаниями).",
        ),
        ("P40", f"Считаем кладовые со статусом помещения {_room_st(room, 11)}."),
        ("I42", "Всегда 0 — отдельно «синие на проверке УПС» в parser_template_b не считаем."),
        ("P42", "Всегда 0."),
        (
            "I43",
            f"Из синих квартир (I40): на клиентской приёмке есть техзаявка со статусом {claims_done} "
            f"и нет активных заявок со статусом {claims_inwork}.",
        ),
        ("P43", "То же для кладовых."),
        ("I41", "Формула: синие квартиры (I40) − передано клиенту (I43) − на проверке УПС (I42)."),
        ("P41", "Формула для кладовых: P40 − P43 − P42."),
        (
            "I51",
            f"Считаем квартиры, у которых в истории клиентских приёмок хотя бы раз был статус приёмки "
            f"{_insp_st(insp, INSP_NOT_ACCEPTED)} (не обязательно последняя приёмка).",
        ),
        ("P51", "То же для кладовых."),
        (
            "I53",
            f"Из красных: последняя приёмка {_insp_st(insp, INSP_NOT_ACCEPTED)}, статус помещения "
            f"{red_ups_rooms}, на последней приёмке техзаявка {_claim_st(claim, 3)}.",
        ),
        ("P53", "То же для кладовых."),
        (
            "I54",
            f"Квартиры со статусом помещения {i54_rooms}, у которых в истории приёмок когда-либо была "
            f"{_insp_st(insp, INSP_NOT_ACCEPTED)}.",
        ),
        ("P54", "То же для кладовых."),
        ("I52", "Формула: красные (I51) минус устранённые (I54); I53 в эту формулу не вычитаем."),
        ("P52", "Формула: P51 − P54."),
        (
            "I56",
            f"Считаем квартиры со статусом помещения {_room_st(room, 12)} (выставлено ОАПП).",
        ),
        ("P56", f"Считаем кладовые со статусом помещения {_room_st(room, 12)}."),
        ("I58", "Копируем с листа «Импорт» B6 — сколько квартир с профприёмщиком (вручную)."),
        ("I59", "Копируем с листа «Импорт» B4 — вызовы мобильной бригады по квартирам."),
        ("P58", "Копируем с листа «Импорт» C6 — профприёмщик по кладовым."),
        ("P59", "Копируем с листа «Импорт» C4 — моб. бригада по кладовым."),
        (
            "F32",
            f"Считаем приёмки по квартирам за сегодня со статусом приёмки {f32_insp} "
            f"(передано сегодня, legacy-метрика).",
        ),
        ("F34", "То же для кладовых."),
        (
            "K51",
            f"«+за сегодня» по красным квартирам: сегодня последняя приёмка {_insp_st(insp, INSP_NOT_ACCEPTED)}, "
            f"а раньше такого статуса не было (не разница I51 с вчера).",
        ),
        ("R51", "То же для кладовых."),
        ("K15", "Разница: I15 сегодня минус I15 на последнем листе отчёта с более ранней датой в названии."),
        ("R15", "Разница P15 с опорным листом."),
        ("K18", "Разница I18 с опорным листом."),
        ("R18", "Разница P18 с опорным листом."),
        ("K38", "Разница I38 с опорным листом."),
        ("R38", "Разница P38 с опорным листом."),
        ("K40", "Разница I40 с опорным листом."),
        ("R40", "Разница P40 с опорным листом."),
        ("K58", "Разница I58 с опорным листом."),
        ("R58", "Разница P58 с опорным листом."),
        ("K59", "Разница I59 с опорным листом."),
        ("R59", "Разница P59 с опорным листом."),
        ("M13", "Формула в шаблоне: доля непроданных квартир (I13/I12)."),
        ("T13", "Формула: P13/P12."),
        ("M15", "Формула: доля рекомендованных квартир (I15/I12)."),
        ("T15", "Формула: P15/P12."),
        ("H32", "Формула: доля переданных квартир (C32/I12)."),
        ("H34", "Формула: C34/P12."),
        ("R32", "Формула: Q32/L32 — доля АПП к переданным по ДДУ (квартиры)."),
        ("R34", "Формула: Q34/L34 (кладовые)."),
        ("M51", "Формула: доля красных квартир (I51/I12)."),
        ("T51", "Формула: P51/P12."),
        ("M58", "Формула: I58/C32."),
        ("T58", "Формула: P58/C34."),
        ("M59", "Формула: I59/C32."),
        ("T59", "Формула: P59/C34."),
    ]


def _parse_insp_calendar_day(insp: dict) -> date | None:
    for field in ("take_date_end", "take_date_from", "updated_at", "created_at"):
        raw = insp.get(field)
        if not raw:
            continue
        s = str(raw).strip()
        if not s:
            continue
        if "T" in s:
            s = s.split("T", 1)[0]
        elif " " in s and len(s) > 10:
            s = s.split(" ", 1)[0]
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(s[:10], fmt).date()
            except ValueError:
                continue
    return None


def _dedupe_inspections(inspections: list) -> list:
    by_id: dict[int, dict] = {}
    for insp in inspections:
        try:
            iid = int(insp.get("id"))
        except (TypeError, ValueError):
            continue
        by_id[iid] = insp
    return list(by_id.values())


def _sort_inspections_chronologically(inspections: list) -> list:
    def _key(i: dict):
        d = _parse_insp_calendar_day(i)
        return (d or date.min, int(i.get("id") or 0))

    return sorted(_dedupe_inspections(inspections), key=_key)


def _latest_inspection(inspections: list) -> dict | None:
    ordered = _sort_inspections_chronologically(inspections)
    return ordered[-1] if ordered else None


def _insp_status_id(insp: dict) -> int | None:
    try:
        return int(insp.get("status_id"))
    except (TypeError, ValueError):
        return None


def _had_not_accepted_before(inspections: list, before_day: date) -> bool:
    for insp in _dedupe_inspections(inspections):
        d = _parse_insp_calendar_day(insp)
        if d is None or d >= before_day:
            continue
        if _insp_status_id(insp) == INSP_NOT_ACCEPTED:
            return True
    return False


def _count_zapisano(rooms: list, insp_map: dict) -> int:
    """Записано: последняя клиентская приёмка «в процессе», в истории нет «Не принята»."""
    n = 0
    for room in rooms:
        rid = room.get("id")
        insps = insp_map.get(rid, [])
        deduped = _dedupe_inspections(insps)
        if any(_insp_status_id(i) == INSP_NOT_ACCEPTED for i in deduped):
            continue
        latest = _latest_inspection(insps)
        if latest and _insp_status_id(latest) in INSP_IN_PROGRESS_IDS:
            n += 1
    return n


def _count_red_total(rooms: list, insp_map: dict) -> int:
    """Красные: последняя приёмка (после дедупа) = «Не принята»."""
    n = 0
    for room in rooms:
        latest = _latest_inspection(insp_map.get(room.get("id"), []))
        if latest and _insp_status_id(latest) == INSP_NOT_ACCEPTED:
            n += 1
    return n


def _is_red_done(room: dict, insps: list, accepted_statuses: list | set) -> bool:
    """I54: принято {6,11,12} + в истории приёмок была «Не принята»."""
    if room.get("status_id") not in accepted_statuses:
        return False
    return any(
        _insp_status_id(i) == INSP_NOT_ACCEPTED for i in _dedupe_inspections(insps)
    )


def _count_red_done(rooms: list, insp_map: dict, accepted_statuses: list | set) -> int:
    return sum(
        1
        for room in rooms
        if _is_red_done(room, insp_map.get(room.get("id"), []), accepted_statuses)
    )


def _count_red_inwork(
    rooms: list,
    insp_map: dict,
    accepted_statuses: list | set,
    red_ups_room_statuses: set,
    client_insp_ids: frozenset[int] | None = None,
) -> int:
    """I52 (f11): объекты из I51, не I54 и не I53."""
    n = 0
    for room in rooms:
        insps = insp_map.get(room.get("id"), [])
        if not _is_red_pool(insps):
            continue
        if _is_red_done(room, insps, accepted_statuses):
            continue
        if _red_has_claim_ups(room, insps, red_ups_room_statuses, client_insp_ids):
            continue
        n += 1
    return n


def _is_red_f17(insps: list) -> bool:
    """f17 I51: в истории клиентских приёмок есть «Не принята» (любая, не только последняя)."""
    return any(
        _insp_status_id(i) == INSP_NOT_ACCEPTED for i in _dedupe_inspections(insps)
    )


def _count_red_total_f17(rooms: list, insp_map: dict) -> int:
    return sum(
        1
        for room in rooms
        if _is_red_f17(insp_map.get(room.get("id"), []))
    )


def _is_recommended_f17(
    room: dict,
    is_free: bool,
    room_type_id: int,
    insp_map: dict | None = None,
) -> bool:
    """
    I15/P15 (f17, без «Импорт»):
    - Кладовые: status ∈ REC или «Строится» (1).
    - Квартиры: продано (не FREE), status ∈ REC; «Строится»/внутренние — нет;
      status 2 «Открыта запись» — только если ещё нет клиентской приёмки;
      status 5 «Замечания» — только если последняя приёмка тоже «Не принята».
    """
    st = room.get("status_id")
    if room_type_id == 5:
        return st in F17_RECOMEND_STATUSES or st == 1
    if st in F17_NOT_RECOMEND_STATUSES:
        return False
    if is_free:
        return st == F17_ZAPIS_ROOM_STATUS
    if st not in F17_RECOMEND_STATUSES:
        return False
    if st == 7:
        return False
    rid = room.get("id")
    insps = (insp_map or {}).get(rid, insp_map.get(int(rid)) if rid is not None else [])
    if st == 2:
        return not insps
    if st == 5 and insp_map is not None:
        latest = _latest_inspection(insps)
        return bool(latest and _insp_status_id(latest) == INSP_NOT_ACCEPTED)
    return True


def _count_recommended_f17(
    rooms: list, free_ids: set[int], room_type_id: int, insp_map: dict | None = None
) -> int:
    return sum(
        1
        for room in rooms
        if _is_recommended_f17(
            room, room.get("id") in free_ids, room_type_id, insp_map
        )
    )


def _count_zapisano_f17(rooms_by_status: dict) -> int:
    """f17 I19: число объектов со статусом помещения 4 («В процессе»)."""
    return len(rooms_by_status.get(F17_ZAPIS_ROOM_STATUS, []))


def classify_blue_room_f17(room: dict, insps: list, blue_status: int) -> str:
    """f17 синие: claims по status_id (template_b_server), без отдельного I42."""
    if room.get("status_id") != blue_status:
        return ""
    has_inwork = has_done = False
    for insp in insps:
        for claim in insp.get("claims") or []:
            sid = claim.get("status_id")
            if sid in F17_CLAIMS_INWORK:
                has_inwork = True
            elif sid in F17_CLAIMS_DONE:
                has_done = True
    if has_done and not has_inwork:
        return "done"
    return "inwork"


def _room_status_label(room: dict, status_names: dict[int, str]) -> str:
    st = room.get("status") if isinstance(room.get("status"), dict) else {}
    if st.get("name"):
        return str(st["name"]).strip()
    sid = room.get("status_id")
    return status_names.get(sid, f"Статус ID: {sid}")


def _insp_status_label(insp: dict, insp_status_names: dict[int, str]) -> str:
    st = insp.get("status") if isinstance(insp.get("status"), dict) else {}
    if st.get("name"):
        return str(st["name"]).strip()
    sid = _insp_status_id(insp)
    return insp_status_names.get(sid, f"Статус ID: {sid}")


def _contract_label(room: dict) -> str:
    ct = _deal_contract_type_id(room)
    if ct == 1:
        return "ДДУ"
    if ct == 2:
        return "ДКП"
    return ""


def _insp_history_brief(insps: list, insp_status_names: dict[int, str], max_parts: int = 8) -> str:
    ordered = _sort_inspections_chronologically(insps)
    if not ordered:
        return ""
    parts = []
    for i in ordered[-max_parts:]:
        d = _parse_insp_calendar_day(i)
        ds = d.strftime("%d.%m.%Y") if d else "?"
        parts.append(f"{ds}:{_insp_status_label(i, insp_status_names)}")
    return " → ".join(parts)


def _fetch_iflat_status_catalog(api_path: str, headers: dict, label: str) -> dict[int, str]:
    out: dict[int, str] = {}
    try:
        r = requests.get(
            f"https://YOUR_CRM_API_HOST/api/v1{api_path}",
            headers=headers,
            timeout=60,
        )
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            rows = []
        for row in rows:
            try:
                out[int(row["id"])] = str(row.get("name") or "").strip()
            except (TypeError, ValueError, KeyError):
                continue
    except Exception as e:
        print(f"  Не удалось загрузить справочник {label}: {e}", flush=True)
    return out


def fetch_insp_status_names(headers: dict) -> dict[int, str]:
    return _fetch_iflat_status_catalog(
        "/inspections/statuses", headers, "статусов приёмки"
    )


def fetch_room_status_names(headers: dict) -> dict[int, str]:
    return _fetch_iflat_status_catalog("/rooms/statuses", headers, "статусов помещений")


F17_CLAIM_STATUS_FALLBACK: dict[int, str] = {
    1: "Новая",
    2: "В работе",
    3: "Выполнено",
    8: "Возвращено на доработку",
    9: "Закрыто без выполнения",
}


def fetch_claim_status_names(headers: dict) -> dict[int, str]:
    return _fetch_iflat_status_catalog("/claims/statuses", headers, "статусов техзаявок")


def fetch_free_room_ids(headers: dict, house_id: int, room_type_ids: dict[str, int]) -> set[int]:
    free: set[int] = set()
    for key, type_id in room_type_ids.items():
        n0 = len(free)
        page = 1
        while True:
            r = requests.get(
                "https://YOUR_CRM_API_HOST/api/v1/rooms",
                headers=headers,
                params={
                    "houseId": house_id,
                    "saleStatuses": "FREE",
                    "roomType": type_id,
                    "perPage": 100,
                    "page": page,
                },
                timeout=30,
            ).json()
            for room in r.get("data") or []:
                try:
                    free.add(int(room["id"]))
                except (TypeError, ValueError, KeyError):
                    pass
            meta = r.get("meta") or {}
            if page >= meta.get("last_page", 1):
                break
            page += 1
        print(f"  Не продано (FREE) {key}: {len(free) - n0}", flush=True)
    return free


def _yn(flag: bool) -> str:
    return "да" if flag else ""


def _insp_had_status(insps: list, status_id: int) -> bool:
    return any(_insp_status_id(i) == status_id for i in _dedupe_inspections(insps))


def _f_f32_today(insps: list, report_date: date) -> bool:
    for i in _dedupe_inspections(insps):
        if _parse_insp_calendar_day(i) == report_date and _insp_status_id(i) in (
            INSP_ACCEPTED_OK,
            INSP_ACCEPTED_REMARKS,
        ):
            return True
    return False


def _claim_done_on_latest(insps: list, client_insp_ids: frozenset[int] | None) -> bool:
    latest = _latest_inspection(insps)
    if not latest:
        return False
    for _, claim in _claims_on_client_inspections([latest], client_insp_ids):
        if _claim_status_name(claim) == CLAIM_STATUS_RED_UPS:
            return True
        if claim.get("status_id") in F17_CLAIMS_DONE:
            return True
    return False


def _audit_criteria_f17(
    on_type: str,
    flags: dict[str, bool],
    *,
    sid: int | None,
    insps: list,
    latest,
) -> dict[str, str]:
    reasons: dict[str, str] = {}
    cell = "I15" if on_type == "Кв" else "P15"
    if flags.get(cell):
        parts = [f"status_id={sid}"]
        if flags.get("I13" if on_type == "Кв" else "P13"):
            parts.append("FREE")
        else:
            parts.append("продано")
        if sid == 2 and not insps:
            parts.append("st2 без приёмок")
        elif sid == 5:
            ls = _insp_status_id(latest) if latest else None
            parts.append(f"st5, посл.приёмка={ls}")
        reasons[cell] = ", ".join(parts)

    zcell = "I19" if on_type == "Кв" else "P19"
    if flags.get(zcell):
        reasons[zcell] = f"status_id={sid} (нужен 4)"

    for fkey in ("I51", "P51"):
        if flags.get(fkey):
            reasons[fkey] = "любая приёмка status=5 в истории (f17 I51)"

    r52 = "I52" if on_type == "Кв" else "P52"
    if flags.get(r52):
        reasons[r52] = "I51 ∧ не I54 (формула отчёта f17)"

    r53 = "I53" if on_type == "Кв" else "P53"
    if flags.get(r53):
        reasons[r53] = f"посл.приёмка=5 ∧ st∈{{2,4}}={sid} ∧ claim выполнено"

    r54 = "I54" if on_type == "Кв" else "P54"
    if flags.get(r54):
        reasons[r54] = "st∈{6,11,12} ∧ в истории была 5"

    b43 = "I43" if on_type == "Кв" else "P43"
    if flags.get(b43):
        reasons[b43] = "st=11, claim status_id done, нет inwork"

    k51 = "K51" if on_type == "Кв" else "R51"
    if flags.get(k51):
        reasons[k51] = "сегодня посл.=5, раньше 5 не было"

    return reasons


def build_f17_audit_rows(
    *,
    report_date: date,
    today_for_request: str,
    rooms_data: dict,
    insp_map: dict,
    free_room_ids: set[int],
    insp_status_names: dict[int, str],
    status_names: dict[int, str],
    recomend_statuses: list,
    accepted_statuses: list,
    green_statuses: list,
    blue_status: int,
    oapp_status: int,
    client_insp_ids: frozenset[int] | None = None,
) -> list[list]:
    rows: list[list] = [object_audit_header()]
    date_str = today_for_request
    rt_map = {"Кв": 1, "Кл": 5}

    for key, rooms in rooms_data.items():
        rt = rt_map[key]
        for room in rooms:
            rid = room.get("id")
            try:
                rid_i = int(rid)
            except (TypeError, ValueError):
                continue
            insps = insp_map.get(rid_i, insp_map.get(rid, []))
            latest = _latest_inspection(insps)
            sid = room.get("status_id")
            ct = _deal_contract_type_id(room)

            f_free = rid_i in free_room_ids
            f_recom = _is_recommended_f17(room, f_free, rt, insp_map)
            f_zapis = sid == F17_ZAPIS_ROOM_STATUS
            f_c32 = sid in accepted_statuses
            f_ddu = f_c32 and ct == 1
            f_dkp = f_c32 and ct == 2
            f_green = sid in green_statuses
            f_blue = sid == blue_status
            f_oapp = sid == oapp_status

            f_red = _is_red_f17(insps)
            f_red_done = _is_red_done(room, insps, accepted_statuses)
            f_red_ups = _red_has_claim_ups_f17(room, insps, client_insp_ids)
            f_red_pool = bool(latest and _insp_status_id(latest) == INSP_NOT_ACCEPTED)
            f_red_inwork = f_red and not f_red_done

            blue_bucket = classify_blue_room_f17(room, insps, blue_status)
            f_blue_done = blue_bucket == "done"
            f_blue_work = blue_bucket == "inwork"

            f_k51_today = False
            today_insps = [
                i for i in _dedupe_inspections(insps) if _parse_insp_calendar_day(i) == report_date
            ]
            if today_insps:
                latest_today = _latest_inspection(today_insps)
                if (
                    latest_today
                    and _insp_status_id(latest_today) == INSP_NOT_ACCEPTED
                    and not _had_not_accepted_before(insps, report_date)
                ):
                    f_k51_today = True

            flags = build_flags_for_room(
                key,
                in_house=True,
                is_free=f_free,
                is_recommended=f_recom,
                is_zapisano=f_zapis,
                is_c32=f_c32,
                is_ddu=f_ddu,
                is_dkp=f_dkp,
                is_green=f_green,
                is_blue=f_blue,
                is_blue_ups=False,
                is_blue_done=f_blue_done,
                is_blue_inwork=f_blue_work,
                is_red=f_red,
                is_red_done=f_red_done,
                is_red_ups=f_red_ups,
                is_red_inwork=f_red_inwork,
                is_oapp=f_oapp,
                is_k51_today=f_k51_today,
                is_f32_today=_f_f32_today(insps, report_date),
            )

            criteria = _audit_criteria_f17(
                key,
                flags,
                sid=sid,
                insps=insps,
                latest=latest,
            )

            latest_label = ""
            if latest:
                latest_label = (
                    f"id={latest.get('id')}; "
                    f"{_insp_status_label(latest, insp_status_names)}"
                )

            number = (
                room.get("number")
                or room.get("name")
                or (room.get("floor") or {}).get("number")
                if isinstance(room.get("floor"), dict)
                else None
            )
            rows.append(
                build_object_audit_row(
                    date_str=date_str,
                    on_type=key,
                    rid_i=rid_i,
                    number=str(number or ""),
                    room_status_label=_room_status_label(room, status_names),
                    contract_label=_contract_label(room),
                    latest_insp_label=latest_label,
                    insp_history=_insp_history_brief(insps, insp_status_names),
                    flags=flags,
                    diag={
                        "status_id": str(sid) if sid is not None else "",
                        "insp_had_5": _yn(_insp_had_status(insps, INSP_NOT_ACCEPTED)),
                        "insp_latest": latest_label,
                        "red_pool": _yn(f_red_pool),
                        "claim_done_latest": _yn(_claim_done_on_latest(insps, client_insp_ids)),
                    },
                    criteria=criteria,
                )
            )
    return rows


def write_f17_plain_rules_sheet(
    ss,
    plain_rules: list[tuple[str, str]] | None = None,
) -> None:
    """Лист id=PLAIN_RULES_SHEET_ID: Ячейка | правило (подписи статусов из CRM API)."""
    if plain_rules is None:
        headers = iflat_request_headers()
        room = _merge_status_catalog(
            fetch_room_status_names(headers), F17_ROOM_STATUS_FALLBACK
        )
        insp = fetch_insp_status_names(headers)
        claim = _merge_status_catalog(
            fetch_claim_status_names(headers), F17_CLAIM_STATUS_FALLBACK
        )
        plain_rules = build_f17_plain_rules(room, insp, claim)
    ws = ss.get_worksheet_by_id(PLAIN_RULES_SHEET_ID)
    rows: list[list] = [
        [
            "parser_template_b — правила выгрузки МРК, дом 4824 "
            "(названия статусов — как в CRM API /rooms/statuses, /inspections/statuses, /claims/statuses)",
        ],
        ["Ячейка", "Правило"],
    ]
    rows.extend([[cell, rule] for cell, rule in plain_rules])
    needed_rows = len(rows)
    if ws.row_count < needed_rows:
        ws.add_rows(needed_rows - ws.row_count)
    ws.clear()
    ws.update(rows, value_input_option=ValueInputOption.user_entered)
    print(
        f"  Правила ({len(plain_rules)} ячеек) → лист id={PLAIN_RULES_SHEET_ID} ({ws.title})",
        flush=True,
    )


def write_f17_audit_sheet(ss, rows: list[list]) -> None:
    ws = ss.get_worksheet_by_id(AUDIT_SHEET_ID)
    needed_rows = len(rows)
    needed_cols = max(len(r) for r in rows) if rows else 1
    if ws.row_count < needed_rows:
        ws.add_rows(needed_rows - ws.row_count)
    if ws.col_count < needed_cols:
        ws.add_cols(needed_cols - ws.col_count)
    ws.clear()
    ws.update(
        rows,
        value_input_option=ValueInputOption.user_entered,
    )
    ncol = max(len(r) for r in rows) if rows else 0
    print(
        f"  Аудит: {needed_rows - 1} объектов, {ncol} колонок "
        f"(все ячейки I12…Q34 + критерии) → id={AUDIT_SHEET_ID} ({ws.title})",
        flush=True,
    )


def _count_red_today_new(rooms: list, insp_map: dict, today_d: date) -> int:
    """K51/R51: сегодня последняя приёмка «Не принята», ранее такого статуса не было."""
    n = 0
    for room in rooms:
        insps = insp_map.get(room.get("id"), [])
        today_insps = [
            i for i in _dedupe_inspections(insps) if _parse_insp_calendar_day(i) == today_d
        ]
        if not today_insps:
            continue
        latest_today = _latest_inspection(today_insps)
        if not latest_today or _insp_status_id(latest_today) != INSP_NOT_ACCEPTED:
            continue
        if _had_not_accepted_before(insps, today_d):
            continue
        n += 1
    return n


# --- Техзаявки: только I42/I43/I53; где claim в логике — всегда typeId=1 ---

CLIENT_INSPECTION_TYPE_ID = 1  # inspectionTypes: «Клиентская приемка»
CLAIM_BASIS_OWNER_TYPE = "inspection"  # UI: «Документ основания» → Приёмка

CLAIM_STATUS_UPS_REVIEW = "на проверке"
CLAIM_STATUS_DONE = frozenset({"выполнено", "закрыто без выполнения"})
CLAIM_STATUS_RED_UPS = "выполнено"


def _norm_status_text(value) -> str:
    return str(value or "").strip().lower()


def _claim_status_name(claim: dict) -> str:
    st = claim.get("status") if isinstance(claim.get("status"), dict) else {}
    return _norm_status_text(st.get("name"))


def client_insp_ids_from_map(insp_map: dict) -> frozenset[int]:
    """ID всех загруженных клиентских приёмок (typeId=1) для проверки owner_id."""
    ids: set[int] = set()
    for insps in insp_map.values():
        for insp in _dedupe_inspections(insps):
            try:
                if int(insp.get("type_id") or 0) == CLIENT_INSPECTION_TYPE_ID:
                    ids.add(int(insp["id"]))
            except (TypeError, ValueError, KeyError):
                continue
    return frozenset(ids)


def _is_client_inspection_basis_claim(
    claim: dict,
    insp: dict,
    client_insp_ids: frozenset[int] | None = None,
) -> bool:
    """
    «Документ основания» = Приёмка (Клиентская приёмка):
    owner_type Inspection + owner.type_id=1 (или owner_id ∈ клиентских приёмок).
    """
    if str(claim.get("owner_type") or "").strip().lower() != CLAIM_BASIS_OWNER_TYPE:
        return False
    owner = claim.get("owner") if isinstance(claim.get("owner"), dict) else {}
    owner_type_id = owner.get("type_id")
    if owner_type_id is not None:
        try:
            return int(owner_type_id) == CLIENT_INSPECTION_TYPE_ID
        except (TypeError, ValueError):
            return False
    try:
        owner_id = int(claim.get("owner_id"))
    except (TypeError, ValueError):
        return False
    if client_insp_ids is not None:
        return owner_id in client_insp_ids
    try:
        return (
            owner_id == int(insp.get("id"))
            and int(insp.get("type_id") or 0) == CLIENT_INSPECTION_TYPE_ID
        )
    except (TypeError, ValueError):
        return False


def _claims_on_client_inspections(
    insps: list,
    client_insp_ids: frozenset[int] | None = None,
) -> list[tuple[dict, dict]]:
    """Пары (клиентская приёмка, техзаявка) с документом основания = клиентская приёмка."""
    pairs: list[tuple[dict, dict]] = []
    for insp in _dedupe_inspections(insps):
        if int(insp.get("type_id") or 0) != CLIENT_INSPECTION_TYPE_ID:
            continue
        for claim in insp.get("claims") or []:
            if isinstance(claim, dict) and _is_client_inspection_basis_claim(
                claim, insp, client_insp_ids
            ):
                pairs.append((insp, claim))
    return pairs


def _room_inspections(insp_map: dict, room_id) -> list:
    try:
        rid = int(room_id)
    except (TypeError, ValueError):
        return []
    return insp_map.get(rid, [])


def _is_red_pool(insps: list) -> bool:
    latest = _latest_inspection(insps)
    return bool(latest and _insp_status_id(latest) == INSP_NOT_ACCEPTED)


def _blue_has_claim_ups(insps: list, client_insp_ids: frozenset[int] | None = None) -> bool:
    """I42/P42: синий пул → клиентская приёмка → техзаявка «На проверке»."""
    return any(
        CLAIM_STATUS_UPS_REVIEW in _claim_status_name(claim)
        for _, claim in _claims_on_client_inspections(insps, client_insp_ids)
    )


def _blue_has_claim_done(insps: list, client_insp_ids: frozenset[int] | None = None) -> bool:
    """I43/P43: синий пул → клиентская приёмка → техзаявка «Выполнено» / «Закрыто без выполнения»."""
    return any(
        _claim_status_name(claim) in CLAIM_STATUS_DONE
        for _, claim in _claims_on_client_inspections(insps, client_insp_ids)
    )


def classify_blue_room(
    room: dict,
    insps: list,
    blue_status: int,
    client_insp_ids: frozenset[int] | None = None,
) -> str:
    """
    Класс синего объекта для I42/I43/I41 (взаимоисключающие приоритеты):
    ups → done → inwork.
    """
    if room.get("status_id") != blue_status:
        return ""
    if _blue_has_claim_ups(insps, client_insp_ids):
        return "ups"
    if _blue_has_claim_done(insps, client_insp_ids):
        return "done"
    return "inwork"


def _red_has_claim_ups_f17(
    room: dict,
    insps: list,
    client_insp_ids: frozenset[int] | None = None,
) -> bool:
    """
    I53 f17: красный пул (последняя приёмка «Не принята») + объект {2,4}
    + техзаявка «Выполнено» на последней клиентской приёмке.
    """
    if not _is_red_pool(insps):
        return False
    if room.get("status_id") not in F17_RED_UPS_ROOM_STATUSES:
        return False
    latest = _latest_inspection(insps)
    if not latest:
        return False
    for _, claim in _claims_on_client_inspections([latest], client_insp_ids):
        if _claim_status_name(claim) == CLAIM_STATUS_RED_UPS:
            return True
        if claim.get("status_id") in F17_CLAIMS_DONE:
            return True
    return False


def _red_has_claim_ups(
    room: dict,
    insps: list,
    red_ups_room_statuses: set,
    client_insp_ids: frozenset[int] | None = None,
) -> bool:
    """
    I53/P53: красный пул (последняя приёмка «Не принята») + объект {2,4}
    → клиентская приёмка → техзаявка «Выполнено».
    """
    if not _is_red_pool(insps):
        return False
    if room.get("status_id") not in red_ups_room_statuses:
        return False
    return any(
        _claim_status_name(claim) == CLAIM_STATUS_RED_UPS
        for _, claim in _claims_on_client_inspections(insps, client_insp_ids)
    )


def load_client_inspections_map(headers: dict, room_ids: list[int]) -> dict[int, list]:
    """Все клиентские приёмки по room_id с embed claims (с пагинацией)."""
    result: dict[int, list] = defaultdict(list)
    for i in range(0, len(room_ids), 50):
        chunk = room_ids[i : i + 50]
        ids_param = ",".join(str(rid) for rid in chunk)
        page = 1
        last_page = 1
        while page <= last_page:
            r = requests.get(
                "https://YOUR_CRM_API_HOST/api/v1/inspections",
                headers=headers,
                params={
                    "roomId": ids_param,
                    "typeId": CLIENT_INSPECTION_TYPE_ID,
                    "embed": "claims.status,claims.owner,status",
                    "perPage": 100,
                    "page": page,
                },
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            meta = data.get("meta") or {}
            last_page = int(meta.get("last_page") or 1)
            for insp in data.get("data") or []:
                try:
                    rid_key = int(insp.get("room_id"))
                except (TypeError, ValueError):
                    continue
                result[rid_key].append(insp)
            page += 1
    return dict(result)


def template_b_plain_rules_only() -> None:
    """Только лист id=PLAIN_RULES_SHEET_ID — Ячейка | правило."""
    creds = Path(__file__).resolve().parent / "service-account.json"
    if not creds.is_file():
        creds = Path(__file__).resolve().parent.parent / "service-account.json"
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    gs = gspread.authorize(
        ServiceAccountCredentials.from_service_account_file(
            str(creds),
            scopes=scope,
        )
    )
    ss = gs.open(os.environ.get("SPREADSHEET_TITLE", "YOUR_TEMPLATE_SPREADSHEET_TITLE"))
    write_f17_plain_rules_sheet(ss)
    print("✅ Готово.")


def _open_or_create_report_sheet(
    ss,
    template_id: int,
    sheet_prefix: str,
    report_date: date,
    *,
    force_new_sheet: bool = False,
):
    """Дублирует шаблон в лист «{prefix} YYYY-MM-DD»; при force — удаляет старый лист с тем же именем."""
    new_sheet_name = f"{sheet_prefix} {report_date}"
    if force_new_sheet:
        try:
            ss.del_worksheet(ss.worksheet(new_sheet_name))
            print(f"\nУдалён существующий лист: {new_sheet_name}")
        except gspread.exceptions.WorksheetNotFound:
            pass
    try:
        ss.duplicate_sheet(template_id, new_sheet_name=new_sheet_name)
        print(f"\nСоздан новый лист: {new_sheet_name}")
    except Exception:
        print(f"\nЛист {new_sheet_name} уже существует, используем его")
    return ss.worksheet(new_sheet_name)


def parser_template_b(
    audit_only: bool = False,
    report_date: date | None = None,
    force_new_sheet: bool = False,
):
    start_time = time.time()

    report_date = report_date or date.today()
    today = str(report_date)
    today_for_request = report_date.strftime("%d.%m.%Y")

    print(f"\n{'=' * 60}", flush=True)
    print(f"Запуск parser_template_b для дома 17_фаза_3_корпус (ID: 4824)", flush=True)
    print(f"Дата: {today_for_request}")
    if audit_only:
        print(f"Режим: только аудит → лист id={AUDIT_SHEET_ID}")
    else:
        print("Режим: template_b_server (дом 4824)")
    print(f"{'=' * 60}\n")

    # Авторизация в Google Sheets (ключ рядом со скриптом — не зависит от cwd / PyCharm)
    creds = Path(__file__).resolve().parent / "service-account.json"
    if not creds.is_file():
        creds = Path(__file__).resolve().parent.parent / "service-account.json"
    if not creds.is_file():
        raise FileNotFoundError(
            f"Нет файла ключа Google: {creds}\n"
            "Положите service-account.json в ту же папку, что и parser_template_b.py"
        )
    scope = ['https://www.googleapis.com/auth/spreadsheets',
             'https://www.googleapis.com/auth/drive']

    credentials = ServiceAccountCredentials.from_service_account_file(
        str(creds),
        scopes=scope,
    )
    gs = gspread.authorize(credentials)
    ss = gs.open(os.environ.get("SPREADSHEET_TITLE", "YOUR_TEMPLATE_SPREADSHEET_TITLE"))

    worksheet = None
    yesterday_values = {}
    comparison_cells = []
    found_sheet = found_date = days_diff = None

    if not audit_only:

        def normalize_sheet_title(t: str) -> str:
            if not t:
                return ""
            return (
                t.strip()
                .replace("\xa0", " ")
                .replace("\u2007", " ")
                .replace("\u202f", " ")
                .replace("\u2009", " ")
            )

        def extract_date_from_sheet_name(sheet_name):
            name = normalize_sheet_title(sheet_name)
            match = re.search(r"(\d{4}-\d{2}-\d{2})", name)
            return date.fromisoformat(match.group(1)) if match else None

        def find_latest_sheet_before(prefix, day_exclusive: date):
            best_ws, best_d = None, None
            pfx = normalize_sheet_title(prefix)
            for ws in ss.worksheets():
                name = normalize_sheet_title(ws.title)
                if not name.startswith(pfx):
                    continue
                d = extract_date_from_sheet_name(name)
                if d is None or d >= day_exclusive:
                    continue
                if best_d is None or d > best_d:
                    best_ws, best_d = ws, d
            if best_ws is None:
                return None, None, None
            return best_ws, best_d, (day_exclusive - best_d).days

        template_id = "620889509"
        template_worksheet = ss.get_worksheet_by_id(int(template_id))
        template_sms: dict[str, str] = {}
        print("\nШАГ 0: I18/P18 из шаблона")
        for cell in ("I18", "P18"):
            try:
                value = template_worksheet.acell(cell).value
                template_sms[cell] = value if value else "0"
                print(f"  {cell} = {template_sms[cell]}")
            except Exception as e:
                print(f"  Ошибка получения {cell} из шаблона: {e}")
                template_sms[cell] = "0"

        import_i15 = {}

        sheet_prefix = "ф17_к3 "
        comparison_cells = [
            "I15", "P15", "I16", "P16", "I18", "P18",
            "I38", "P38", "I40", "P40", "I45", "P45", "I49", "P49",
            "I51", "P51", "I58", "P58", "I59", "P59", "C32", "C34",
        ]

        print(f"\nШАГ 0.1: Поиск последнего листа с датой в имени < {report_date}")
        found_sheet, found_date, days_diff = find_latest_sheet_before(sheet_prefix, report_date)
        if found_sheet:
            print(f"  Опорный лист: {found_sheet.title} (дата {found_date}, на {days_diff} дн. раньше отчёта)")
        else:
            print("  Нет листов с датой раньше текущего отчёта — разницы будут относительно 0")

        if found_sheet:
            print(f"\nШАГ 0.2: Чтение опорных значений с листа {found_sheet.title}")
            try:
                prev_raw = found_sheet.batch_get(comparison_cells)
                for cell, block in zip(comparison_cells, prev_raw):
                    v = None
                    if block is not None and len(block) > 0:
                        row0 = block[0]
                        if isinstance(row0, (list, tuple)) and len(row0) > 0:
                            v = row0[0]
                        elif row0 is not None and not isinstance(row0, (list, tuple)):
                            v = row0
                    yesterday_values[cell] = _parse_float_cell(v)
                    print(f"  {cell} = {yesterday_values[cell]}")
            except Exception as e:
                print(f"  batch_get не удался ({e}), читаем по одной ячейке")
                for cell in comparison_cells:
                    try:
                        value = found_sheet.acell(cell).value
                        yesterday_values[cell] = _parse_float_cell(value)
                        print(f"  {cell} = {yesterday_values[cell]}")
                    except Exception as e2:
                        print(f"  Ошибка получения {cell}: {e2}")
                        yesterday_values[cell] = 0.0
        else:
            yesterday_values = {cell: 0.0 for cell in comparison_cells}
            print("\n  Используем нулевые значения для всех ячеек сравнения")

        worksheet = _open_or_create_report_sheet(
            ss,
            template_id,
            "ф17_к3",
            report_date,
            force_new_sheet=force_new_sheet,
        )
        try:
            layout_block = get_import_layout_block(ss)
            pre_updates = list(layout_block.items()) + list(template_sms.items())
            _batch_acells(worksheet, pre_updates)
        except Exception as e:
            print(f"  Ошибка вставки блока с «Импорт»/шаблона: {e}")
    else:
        template_sms = {}

    headers = iflat_request_headers()
    print("\nПодключение к API установлено\n")

    # ID дома
    house_id = 4824
    insp_status_names = fetch_insp_status_names(headers)
    claim_status_names = _merge_status_catalog(
        fetch_claim_status_names(headers), F17_CLAIM_STATUS_FALLBACK
    )
    status_names = _merge_status_catalog(
        fetch_room_status_names(headers), F17_ROOM_STATUS_FALLBACK
    )
    if WRITE_PLAIN_RULES_ON_RUN:
        plain_rules = build_f17_plain_rules(
            status_names, insp_status_names, claim_status_names
        )
        try:
            write_f17_plain_rules_sheet(ss, plain_rules)
        except Exception as e:
            print(f"  Правила → лист id={PLAIN_RULES_SHEET_ID}: {e}", flush=True)
            traceback.print_exc()

    # Типы помещений
    room_type = {
        "Кв": 1,  # Квартиры
        "Кл": 5  # Кладовые
    }

    # Статусы для разных показателей
    recomend_statuses = [2, 3, 4, 5, 7, 11, 12, 6]
    meeting_statuses = list(MEETING_STATUS_IDS)
    accepted_statuses = [11, 12, 6]
    transferred_today_statuses = [4, 8]
    green_statuses = [6, 12]
    blue_status = 11
    red_inspection_status = 5
    oapp_status = 12

    # Ячейки для обновления
    all_on = {"Кв": ["I12", "I13"], "Кл": ["P12", "P13"]}
    zapis = {"Кв": "I19", "Кл": "P19"}
    recomend = {"Кв": "I15", "Кл": "P15"}
    summary = {"Кв": ["C32", "L32", "V32"], "Кл": ["C34", "L34", "V34"]}
    peredano_today = {"Кв": "F32", "Кл": "F34"}
    green = {"Кв": "I38", "Кл": "P38"}
    blue = {"Кв": "I40", "Кл": "P40"}
    red_places = {"Кв": ["I51", "I54", "I52"], "Кл": ["P51", "P54", "P52"]}
    blue_places = {"Кв": ["I41", "I43"], "Кл": ["P41", "P43"]}
    blue_ups_places = {"Кв": "I42", "Кл": "P42"}
    oapp_places = {"Кв": "I56", "Кл": "P56"}
    app_ep_places = {"Кв": "Q32", "Кл": "Q34"}
    sleeping_places = {"Кв": "I20", "Кл": "P20"}
    red_ups_places = {"Кв": "I53", "Кл": "P53"}

    print("ШАГ 1: Загрузка всех данных...")

    # 1. Загружаем все комнаты ОДНИМ запросом на тип (с пагинацией)
    rooms_data = {"Кв": [], "Кл": []}
    rooms_by_status = {"Кв": defaultdict(list), "Кл": defaultdict(list)}

    for key, type_id in room_type.items():
        page = 1
        rooms_params_base = {
            "houseId": house_id,
            "roomType": type_id,
            "perPage": 100,
            "embed": "room_type,status,floor,house,deal",
            "orderBy": "-updated_at",
        }
        while True:
            r = requests.get(
                "https://YOUR_CRM_API_HOST/api/v1/rooms",
                headers=headers,
                params={**rooms_params_base, "page": page},
                timeout=30,
            ).json()
            rooms = r.get("data", [])
            if not rooms:
                break
            rooms_data[key].extend(rooms)
            meta = r.get("meta") or {}
            if page >= meta.get("last_page", 1):
                break
            page += 1
        print(f"  {key}: загружено {len(rooms_data[key])} комнат")

        # Группируем по статусам
        for room in rooms_data[key]:
            rooms_by_status[key][room["status_id"]].append(room)

    free_room_ids = fetch_free_room_ids(headers, house_id, room_type)
    print(f"  Всего FREE (не продано): {len(free_room_ids)}", flush=True)

    # 2. Быстрые метрики из уже загруженных данных
    print("\nШАГ 2: Расчет метрик из загруженных данных")

    # Словари для хранения текущих значений (сегодня)
    current_values = {
        "recomend": {"Кв": 0, "Кл": 0},
        "green": {"Кв": 0, "Кл": 0},
        "blue": {"Кв": 0, "Кл": 0}
    }

    for key in room_type:
        # Всего ОН
        total = len(rooms_data[key])

        # Не продано (отдельный запрос - он быстрый)
        r2 = requests.get(
            f"https://YOUR_CRM_API_HOST/api/v1/rooms?houseId={house_id}&saleStatuses=FREE&roomType={room_type[key]}",
            headers=headers
        ).json()
        not_sold = r2["meta"]["total"]

        # Рекомендовано
        recomend_total = sum(len(rooms_by_status[key].get(s, [])) for s in recomend_statuses)
        current_values["recomend"][key] = recomend_total

        # Зеленые
        green_total = sum(len(rooms_by_status[key].get(s, [])) for s in green_statuses)
        current_values["green"][key] = green_total

        # Синие
        blue_total = len(rooms_by_status[key].get(blue_status, []))
        current_values["blue"][key] = blue_total

        # Передано накопительным итогом
        accepted_rooms = []
        for s in accepted_statuses:
            accepted_rooms.extend(rooms_by_status[key].get(s, []))

        ct_hist = Counter(_deal_contract_type_id(r) for r in accepted_rooms)
        ddu = sum(1 for r in accepted_rooms if _deal_contract_type_id(r) == 1)
        app_dkp = sum(1 for r in accepted_rooms if _deal_contract_type_id(r) == 2)
        # ОАПП
        oapp_total = len(rooms_by_status[key].get(oapp_status, []))

        # C — принятые (6,11,12). L — ДДУ (contract_type_id 1). V — ДКП (тип 2), как sum3 в f7k2.
        # Q32/Q34 — с листа «Импорт» (B8/C8), см. get_import_layout_block.
        n_accepted = len(accepted_rooms)

        # Записываем в таблицу (одним batch — лимит Google Sheets 60 write/min)
        row_updates = [
            (all_on[key][0], total),
            (all_on[key][1], not_sold),
        ]

        # I15/P16 — API (f17), после загрузки приёмок для правила status 2/5
        pass

        # I19, красные/синие — после загрузки приёмок (ШАГ 4)
        row_updates.extend(
            [
                (green[key], green_total),
                (blue[key], blue_total),
                (summary[key][0], n_accepted),
                (summary[key][1], ddu),
                (summary[key][2], app_dkp),
                (oapp_places[key], oapp_total),
            ]
        )
        if not audit_only:
            _batch_acells(worksheet, row_updates)

        print(f"  {key}: Всего={total}, Не продано={not_sold}")
        print(f"    Зеленые={green_total}, Синие={blue_total}, ОАПП={oapp_total}")
        def _ct_label(k):
            return "нет" if k is None else str(k)

        ct_line = ", ".join(
            f"{_ct_label(k)}:{v}" for k, v in sorted(ct_hist.items(), key=lambda x: (x[0] is None, x[0] or 0))
        )
        print(
            f"    Принято (C)={n_accepted}, ДДУ (L)={ddu}, ДКП (V)={app_dkp} "
            f"[contract_type_id: {ct_line}]"
        )

    # 3. Загружаем инспекции (2 запроса) — для отчёта; аудиту достаточно ШАГ 4
    if audit_only:
        all_inspections = []
        today_inspections = []
    else:
        print("\nШАГ 3: Загрузка инспекций")

        def fetch_inspections(date_from=None, date_to=None):
            params = f"typeId=1&houseId={house_id}&embed=room&perPage=100"
            if date_from:
                params += f"&takeDateFrom={date_from}"
            if date_to:
                params += f"&takeDateTo={date_to}"

            out = []
            page = 1
            while True:
                r = requests.get(
                    f"https://YOUR_CRM_API_HOST/api/v1/inspections?{params}&page={page}",
                    headers=headers, timeout=30
                ).json()
                inspections = r.get("data", [])
                if not inspections:
                    break
                out.extend(inspections)
                if page >= r["meta"]["last_page"]:
                    break
                page += 1
            return out

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_all = executor.submit(fetch_inspections, None, today_for_request)
            future_today = executor.submit(fetch_inspections, today_for_request, today_for_request)
            all_inspections = future_all.result()
            today_inspections = future_today.result()

        print(f"  Загружено инспекций: всего={len(all_inspections)}, сегодня={len(today_inspections)}")

        kv_meets = sum(1 for i in all_inspections
                       if i["status_id"] in meeting_statuses and i["room"]["room_type_id"] == 1)
        kl_meets = sum(1 for i in all_inspections
                       if i["status_id"] in meeting_statuses and i["room"]["room_type_id"] == 5)
        kv_today = sum(1 for i in today_inspections
                       if i["status_id"] in meeting_statuses and i["room"]["room_type_id"] == 1)
        kl_today = sum(1 for i in today_inspections
                       if i["status_id"] in meeting_statuses and i["room"]["room_type_id"] == 5)

        meet_updates = [
            ("H24", kv_meets),
            ("H25", kl_meets),
            ("H28", kv_today),
            ("H29", kl_today),
        ]
        print(f"  Встреч: всего Кв={kv_meets}, Кл={kl_meets}, сегодня Кв={kv_today}, Кл={kl_today}")

        for key in room_type:
            today_count = sum(1 for i in today_inspections
                              if i["status_id"] in transferred_today_statuses
                              and i["room"]["room_type_id"] == room_type[key])
            meet_updates.append((peredano_today[key], today_count))
            print(f"  {key} передано сегодня: {today_count}")
        _batch_acells(worksheet, meet_updates)

    # 4. Анализ красных и синих (ОДНИМ запросом на все комнаты)
    print("\nШАГ 4: Анализ замечаний")

    # Собираем все ID комнат
    all_room_ids = []
    for key in room_type:
        all_room_ids.extend([room["id"] for room in rooms_data[key]])

    # Загружаем все клиентские приёмки с embed claims (с пагинацией)
    all_inspections_map = load_client_inspections_map(headers, all_room_ids)
    client_insp_ids = client_insp_ids_from_map(all_inspections_map)
    print(
        f"  Загружено инспекций с claims: {sum(len(v) for v in all_inspections_map.values())}, "
        f"клиентских приёмок (основание): {len(client_insp_ids)}"
    )

    red_total = {"Кв": 0, "Кл": 0}
    red_done = {"Кв": 0, "Кл": 0}
    red_ups = {"Кв": 0, "Кл": 0}
    red_inwork = {"Кв": 0, "Кл": 0}
    red_today_new = {"Кв": 0, "Кл": 0}
    zapis_counts = {"Кв": 0, "Кл": 0}
    recomend_counts = {"Кв": 0, "Кл": 0}

    for key in room_type:
        rooms = rooms_data[key]
        rt = room_type[key]
        recomend_counts[key] = _count_recommended_f17(
            rooms, free_room_ids, rt, all_inspections_map
        )
        red_total[key] = _count_red_total_f17(rooms, all_inspections_map)
        red_today_new[key] = _count_red_today_new(rooms, all_inspections_map, report_date)
        zapis_counts[key] = _count_zapisano_f17(rooms_by_status[key])
        red_done[key] = _count_red_done(rooms, all_inspections_map, accepted_statuses)
        red_ups[key] = sum(
            1
            for room in rooms
            if _red_has_claim_ups_f17(
                room, all_inspections_map.get(room.get("id"), []), client_insp_ids
            )
        )
        red_inwork[key] = max(0, red_total[key] - red_done[key])

    step4_updates = []
    for key in room_type:
        total = len(rooms_data[key])
        step4_updates.extend(
            [
                (recomend[key], recomend_counts[key]),
                ("I16" if key == "Кв" else "P16", total - recomend_counts[key]),
                (zapis[key], zapis_counts[key]),
                (red_places[key][0], red_total[key]),
                (red_places[key][1], red_done[key]),
                (red_ups_places[key], red_ups[key]),
                (red_places[key][2], red_inwork[key]),
            ]
        )
        print(
            f"  {key} рекомендовано={recomend_counts[key]}, не рекомендовано={total - recomend_counts[key]}, "
            f"записано={zapis_counts[key]}, красные={red_total[key]}, "
            f"устранено={red_done[key]}, УПС={red_ups[key]}, в работе={red_inwork[key]}, "
            f"+за сегодня (K/R51)={red_today_new[key]}"
        )

    # Синие (template_b_server: I41 = I40 − I43, I42 не используется)
    blue_inwork = {"Кв": 0, "Кл": 0}
    blue_done = {"Кв": 0, "Кл": 0}

    for key in room_type:
        blue_rooms = rooms_by_status[key].get(blue_status, [])

        for room in blue_rooms:
            insps = all_inspections_map.get(room.get("id"), [])
            if classify_blue_room_f17(room, insps, blue_status) == "done":
                blue_done[key] += 1

        blue_inwork[key] = max(0, len(blue_rooms) - blue_done[key])
        step4_updates.append((blue_places[key][0], blue_inwork[key]))
        step4_updates.append((blue_ups_places[key], 0))
        step4_updates.append((blue_places[key][1], blue_done[key]))
        print(f"  {key} синие: в работе={blue_inwork[key]}, передано клиенту={blue_done[key]}")

    print(f"\nШАГ 4.1: Аудит позиций → лист id={AUDIT_SHEET_ID}")
    try:
        audit_rows = build_f17_audit_rows(
            report_date=report_date,
            today_for_request=today_for_request,
            rooms_data=rooms_data,
            insp_map=all_inspections_map,
            free_room_ids=free_room_ids,
            insp_status_names=insp_status_names,
            status_names=status_names,
            recomend_statuses=recomend_statuses,
            accepted_statuses=accepted_statuses,
            green_statuses=green_statuses,
            blue_status=blue_status,
            oapp_status=oapp_status,
            client_insp_ids=client_insp_ids,
        )
        write_f17_audit_sheet(ss, audit_rows)
    except Exception as e:
        print(f"  Ошибка записи аудита: {e}", flush=True)
        traceback.print_exc()

    if audit_only:
        elapsed = time.time() - start_time
        print(f"\n{'=' * 60}")
        print(f"✅ Аудит записан за {elapsed:.1f} сек (лист id={AUDIT_SHEET_ID})")
        print(f"{'=' * 60}\n")
        return

    # Спящие: I18 - I19 - L32(ДДУ) - I52 - I53 (I19/P19 — из расчёта, не с листа: ячейки ещё не записаны)
    tv_sleep = _batch_get_cells_float(worksheet, ["I18", "P18", "L32", "L34"])
    for key in room_type:
        sms_cell = "I18" if key == "Кв" else "P18"
        ddu_cell = "L32" if key == "Кв" else "L34"
        sleeping = (
            tv_sleep.get(sms_cell, 0.0)
            - float(zapis_counts[key])
            - tv_sleep.get(ddu_cell, 0.0)
            - red_inwork[key]
            - red_ups[key]
        )
        step4_updates.append((sleeping_places[key], max(0, sleeping)))
        print(f"  {key} спящие ({sleeping_places[key]}): {sleeping}")

    # Обновляем дату
    step4_updates.append(("B4", today_for_request))
    _batch_acells(worksheet, step4_updates)
    print(f"  B4: дата обновлена на {today_for_request}")

    # ============= РАЗНИЦА: текущий лист (iflat) минус последний лист с более ранней датой =============
    print("\nШАГ 5: Разница с опорным листом (сегодня − прошлый лист по дате в названии)")
    tv = _batch_get_cells_float(worksheet, comparison_cells)
    yv = yesterday_values.get

    diff_k15 = tv["I15"] - yv("I15", 0.0)
    diff_r15 = tv["P15"] - yv("P15", 0.0)
    diff_i18 = tv["I18"] - yv("I18", 0.0)
    diff_p18 = tv["P18"] - yv("P18", 0.0)
    diff_green_kv = tv["I38"] - yv("I38", 0.0)
    diff_green_kl = tv["P38"] - yv("P38", 0.0)
    diff_blue_kv = tv["I40"] - yv("I40", 0.0)
    diff_blue_kl = tv["P40"] - yv("P40", 0.0)
    diff_i45 = tv["I45"] - yv("I45", 0.0)
    diff_p45 = tv["P45"] - yv("P45", 0.0)
    diff_i49 = tv["I49"] - yv("I49", 0.0)
    diff_p49 = tv["P49"] - yv("P49", 0.0)
    # K51/R51: diff I51/P51 (template_b_server)
    diff_i51 = tv["I51"] - yv("I51", 0.0)
    diff_p51 = tv["P51"] - yv("P51", 0.0)
    print(f"  K51/R51 (diff I51/P51): Кв={diff_i51}, Кл={diff_p51}")
    diff_i58 = tv["I58"] - yv("I58", 0.0)
    diff_p58 = tv["P58"] - yv("P58", 0.0)
    diff_i59 = tv["I59"] - yv("I59", 0.0)
    diff_p59 = tv["P59"] - yv("P59", 0.0)
    diff_c32 = tv["C32"] - yv("C32", 0.0)
    diff_c34 = tv["C34"] - yv("C34", 0.0)

    print(
        "  Разницы: K15/R15 (I15/P15), K18/R18, … — "
        f"пример I15: {tv['I15']} − {yv('I15', 0)} = {diff_k15}"
    )

    diff_pairs = [
        ("K15", diff_k15),
        ("R15", diff_r15),
        ("K18", diff_i18),
        ("R18", diff_p18),
        ("K38", diff_green_kv),
        ("R38", diff_green_kl),
        ("K40", diff_blue_kv),
        ("R40", diff_blue_kl),
        ("K45", diff_i45),
        ("R45", diff_p45),
        ("K49", diff_i49),
        ("R49", diff_p49),
        ("K51", diff_i51),
        ("R51", diff_p51),
        ("K58", diff_i58),
        ("R58", diff_p58),
        ("K59", diff_i59),
        ("R59", diff_p59),
        ("F32", diff_c32),
        ("F34", diff_c34),
    ]
    _write_diff_cells(worksheet, diff_pairs)
    print(f"  Записаны диффы в ячейки: {', '.join(c for c, _ in diff_pairs)}")

    if found_sheet and found_date:
        print(f"\n  Сравнение с листом «{found_sheet.title}» (дата {found_date}, на {days_diff} дн. раньше отчёта)")
    else:
        print("\n  Опорный лист не найден — разницы относительно 0")

    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"✅ parser_template_b завершил работу за {elapsed:.1f} сек")
    print("Режим: инструкция")
    print(f"{'=' * 60}\n")


def _parse_hhmm(s: str) -> tuple[int, int]:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError("Нужен формат HH:MM, например 09:30")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Часы 0–23, минуты 0–59")
    return h, m


def _seconds_until_local(hour: int, minute: int) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _last_run_state_file(script_dir: Path) -> Path:
    return script_dir / ".parser_template_b.last_run.txt"


def _read_last_run_date(script_dir: Path):
    state_file = _last_run_state_file(script_dir)
    try:
        raw = state_file.read_text(encoding="utf-8").strip()
        return date.fromisoformat(raw)
    except Exception:
        return None


def _write_last_run_date(script_dir: Path, d: date) -> None:
    state_file = _last_run_state_file(script_dir)
    state_file.write_text(d.isoformat(), encoding="utf-8")


def _cli_main():
    parser = argparse.ArgumentParser(
        description="МРК ф17 к3 (parser_template_b) — правила листа «Инструкция». Ключ: service-account.json."
    )
    g = parser.add_mutually_exclusive_group()
    g.add_argument(
        "--daily-at",
        metavar="HH:MM",
        help="Каждый день в это локальное время (по умолчанию 20:00; если после сна время уже прошло — запуск сразу)",
    )
    g.add_argument(
        "--interval-hours",
        type=float,
        metavar="N",
        help="Повторять прогон каждые N часов (первый прогон сразу)",
    )
    g.add_argument(
        "--interval-minutes",
        type=float,
        metavar="N",
        help="Повторять прогон каждые N минут (первый прогон сразу)",
    )
    parser.add_argument(
        "--on-error-sleep-sec",
        type=int,
        default=300,
        metavar="SEC",
        help="После ошибки в цикле ждать SEC секунд перед повтором (по умолчанию 300)",
    )
    parser.add_argument(
        "--audit-only",
        action="store_true",
        help=f"Только выгрузка аудита на лист id={AUDIT_SHEET_ID} (без дневного отчёта)",
    )
    parser.add_argument(
        "--plain-rules-only",
        action="store_true",
        help=f"Только справочник «Ячейка | правило» на лист id={PLAIN_RULES_SHEET_ID}",
    )
    parser.add_argument(
        "--report-date",
        metavar="YYYY-MM-DD",
        help="Дата отчёта и имени листа (по умолчанию — сегодня)",
    )
    parser.add_argument(
        "--force-new-sheet",
        action="store_true",
        help="Удалить лист с этой датой (если есть) и создать заново из шаблона",
    )
    args = parser.parse_args()
    if args.plain_rules_only:
        template_b_plain_rules_only()
        return
    if args.daily_at:
        try:
            _parse_hhmm(args.daily_at)
        except ValueError as e:
            parser.error(str(e))

    script_dir = Path(__file__).resolve().parent
    try:
        os.chdir(script_dir)
    except OSError as e:
        print(f"Не удалось chdir в {script_dir}: {e}", flush=True)

    interval_sec = None
    if args.interval_hours is not None:
        interval_sec = max(60.0, args.interval_hours * 3600.0)
    elif args.interval_minutes is not None:
        interval_sec = max(60.0, args.interval_minutes * 60.0)

    run_date = date.fromisoformat(args.report_date) if args.report_date else None

    def one_run():
        parser_template_b(
            audit_only=args.audit_only,
            report_date=run_date,
            force_new_sheet=args.force_new_sheet,
        )

    if not args.daily_at and interval_sec is None:
        try:
            one_run()
        except Exception:
            traceback.print_exc()
            raise
        return

    print(
        f"Режим цикла: daily-at={args.daily_at!r} interval_sec={interval_sec}\n"
        f"Рабочая папка: {script_dir}\nОстановка: Ctrl+C",
        flush=True,
    )
    while True:
        try:
            if args.daily_at:
                h, m = _parse_hhmm(args.daily_at)
                now = datetime.now()
                target_today = now.replace(hour=h, minute=m, second=0, microsecond=0)
                last_run_date = _read_last_run_date(script_dir)
                if now >= target_today and last_run_date != now.date():
                    print(
                        f"{now:%Y-%m-%d %H:%M:%S} — время {h:02d}:{m:02d} уже прошло, "
                        "запускаю сразу (догоняющий запуск после сна/простоя).",
                        flush=True,
                    )
                else:
                    wait = _seconds_until_local(h, m)
                    print(
                        f"{now:%Y-%m-%d %H:%M:%S} — следующий прогон в {h:02d}:{m:02d} "
                        f"через {wait / 3600:.2f} ч ({wait:.0f} с)",
                        flush=True,
                    )
                    time.sleep(wait)
            one_run()
            if args.daily_at:
                _write_last_run_date(script_dir, date.today())
        except KeyboardInterrupt:
            print("\nОстановлено (Ctrl+C).", flush=True)
            sys.exit(0)
        except Exception:
            traceback.print_exc()
            print(f"Пауза {args.on_error_sleep_sec} с после ошибки…", flush=True)
            time.sleep(args.on_error_sleep_sec)
            continue
        if args.daily_at:
            continue
        if interval_sec is not None:
            print(f"Пауза {interval_sec / 60:.1f} мин до следующего прогона…", flush=True)
            time.sleep(interval_sec)


if __name__ == "__main__":
    _cli_main()
