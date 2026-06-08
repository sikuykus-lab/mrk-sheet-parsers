"""
МРК ф11_к3 — дом 4662 (YOUR_TEMPLATE_A_TITLE).
Метрики I15/I19/I51–I54/синие — та же логика, что parser_template_b; I18/I58/Q32 — «Импорт»;
K51/R51 — «+за сегодня» по приёмкам.
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

# Ячейки «Импорт» по инструкции: I18/P18, I58/I59, Q32/Q34 (не I15 — там API)

# ID листа «Импорт» в этой книге
IMPORT_SHEET_ID = "869091486"
# Лист проверки правил parser_template_a (позиции по объектам)
AUDIT_SHEET_ID = 714384621
# Справочник: ячейка → назначение → правило
RULES_SHEET_TITLE = "Справочник ячеек template_a"

# (ячейка, назначение 1–2 слова, правило, источник: API|Импорт|Формула|Шаблон)
F11_CELL_RULES: list[tuple[str, str, str, str]] = [
    ("B4", "Дата отчёта", "Дата запуска скрипта, формат дд.мм.гггг", "API"),
    ("I12", "Всего кв", "COUNT rooms, houseId=4662, roomType=1 (все статусы)", "API"),
    ("P12", "Всего кл", "COUNT rooms, houseId=4662, roomType=5 (все статусы)", "API"),
    ("I13", "Не продано кв", "rooms saleStatuses=FREE, roomType=1 → meta.total", "API"),
    ("P13", "Не продано кл", "rooms saleStatuses=FREE, roomType=5 → meta.total", "API"),
    ("I15", "Рекомендовано кв", "Продано + status∈{2,3,4,5,6,7,11,12}; st7→нет; st2→без приёмок; st5→последняя приёмка «Не принята»; FREE→только st4", "API"),
    ("P15", "Рекомендовано кл", "status∈{2,3,4,5,6,7,11,12} или st1 «Строится»; те же уточнения st2/st5", "API"),
    ("I16", "Не рек. кв", "I12 − I15", "Формула"),
    ("P16", "Не рек. кл", "P12 − P15", "Формула"),
    ("I18", "SMS кв", "Лист «Импорт» B2 — отправлено SMS о НЗ", "Импорт"),
    ("P18", "SMS кл", "Лист «Импорт» C2 — отправлено SMS о НЗ", "Импорт"),
    ("I19", "Записано кв", "Статус объекта = 4 («Клиентская приёмка: в процессе»)", "API"),
    ("P19", "Записано кл", "Статус объекта = 4 («Клиентская приёмка: в процессе»)", "API"),
    ("I20", "Спящие кв", "I18 − I19 − L32(ДДУ) − I52 − I53", "Формула"),
    ("P20", "Спящие кл", "P18 − P19 − L34(ДДУ) − P52 − P53", "Формула"),
    ("H24", "Встречи кв", "Клиентские приёмки typeId=1, status∈{4,5,8}, за весь период до сегодня (счёт записей)", "API"),
    ("H25", "Встречи кл", "То же для roomType=5", "API"),
    ("H28", "Встречи сегодня кв", "Приёмки status∈{4,5,8}, дата = сегодня", "API"),
    ("H29", "Встречи сегодня кл", "То же для кладовых", "API"),
    ("C32", "Передано кв", "status∈{6,11,12} — принято / с замечаниями / ОАПП", "API"),
    ("C34", "Передано кл", "То же для кладовых", "API"),
    ("L32", "ДДУ кв", "C32 + deal.contract_type_id = 1", "API"),
    ("L34", "ДДУ кл", "C34 + contract_type_id = 1", "API"),
    ("V32", "ДКП кв", "C32 + contract_type_id = 2", "API"),
    ("V34", "ДКП кл", "C34 + contract_type_id = 2", "API"),
    ("Q32", "АПП кв", "Лист «Импорт» B8", "Импорт"),
    ("Q34", "АПП кл", "Лист «Импорт» C8", "Импорт"),
    ("I38", "Зелёные кв", "status∈{6,12} — принято / односторонняя передача", "API"),
    ("P38", "Зелёные кл", "То же для кладовых", "API"),
    ("I40", "Синие кв", "status = 11 (принято с замечаниями)", "API"),
    ("P40", "Синие кл", "status = 11", "API"),
    ("I42", "Синие УПС кв", "Не заполняется (0), как parser_template_b", "API"),
    ("P42", "Синие УПС кл", "Не заполняется (0)", "API"),
    ("I43", "Синие передано кв", "Синий пул → claim status_id∈{9,3}, нет inwork {1,2,8}", "API"),
    ("P43", "Синие передано кл", "То же", "API"),
    ("I41", "Синие в работе кв", "I40 − I43 − I42", "Формула"),
    ("P41", "Синие в работе кл", "P40 − P43 − P42", "Формула"),
    ("I51", "Красные кв", "Любая клиентская приёмка «Не принята» (5) в истории объекта", "API"),
    ("P51", "Красные кл", "То же", "API"),
    ("I53", "Красные УПС кв", "Последняя приёмка=5 + объект st∈{2,4} + claim «Выполнено» на последней приёмке", "API"),
    ("P53", "Красные УПС кл", "То же", "API"),
    ("I54", "Красные снято кв", "status∈{6,11,12} + в истории приёмок была «Не принята»", "API"),
    ("P54", "Красные снято кл", "То же", "API"),
    ("I52", "Красные в работе кв", "I51 − I54 (I53 в формулу не входит)", "Формула"),
    ("P52", "Красные в работе кл", "P51 − P54", "Формула"),
    ("I56", "ОАПП кв", "status = 12 (односторонняя передача)", "API"),
    ("P56", "ОАПП кл", "status = 12", "API"),
    ("I58", "Профприёмщик кв", "Лист «Импорт» B6", "Импорт"),
    ("I59", "Моб. бригада кв", "Лист «Импорт» B4", "Импорт"),
    ("P58", "Профприёмщик кл", "Лист «Импорт» C6", "Импорт"),
    ("P59", "Моб. бригада кл", "Лист «Импорт» C4", "Импорт"),
    ("F32", "Передано сегодня кв", "Приёмки status∈{4,8}, дата = сегодня (не из инструкции, legacy)", "API"),
    ("F34", "Передано сегодня кл", "То же", "API"),
    ("K51", "+красные сегодня кв", "Сегодня последняя приёмка=5, раньше 5 не было (не diff I51)", "API"),
    ("R51", "+красные сегодня кл", "То же", "API"),
    ("K15", "Δ рекоменд. кв", "I15 сегодня − I15 опорного листа", "Формула"),
    ("R15", "Δ рекоменд. кл", "P15 сегодня − P15 опорного листа", "Формула"),
    ("K18", "Δ SMS кв", "I18 сегодня − I18 опорного листа", "Формула"),
    ("R18", "Δ SMS кл", "P18 сегодня − P18 опорного листа", "Формула"),
    ("K38", "Δ зелёные кв", "I38 сегодня − I38 опорного листа", "Формула"),
    ("R38", "Δ зелёные кл", "P38 сегодня − P38 опорного листа", "Формула"),
    ("K40", "Δ синие кв", "I40 сегодня − I40 опорного листа", "Формула"),
    ("R40", "Δ синие кл", "P40 сегодня − P40 опорного листа", "Формула"),
    ("K58", "Δ проф кв", "I58 сегодня − I58 опорного листа", "Формула"),
    ("R58", "Δ проф кл", "P58 сегодня − P58 опорного листа", "Формула"),
    ("K59", "Δ моб кв", "I59 сегодня − I59 опорного листа", "Формула"),
    ("R59", "Δ моб кл", "P59 сегодня − P59 опорного листа", "Формула"),
    ("M13", "% не продано кв", "=IFERROR(I13/I12;0) — формула в шаблоне", "Шаблон"),
    ("T13", "% не продано кл", "=IFERROR(P13/P12;0)", "Шаблон"),
    ("M15", "% рекоменд. кв", "=IFERROR(I15/I12;0)", "Шаблон"),
    ("T15", "% рекоменд. кл", "=IFERROR(P15/P12;0)", "Шаблон"),
    ("H32", "% передано кв", "=IFERROR(C32/I12;0)", "Шаблон"),
    ("H34", "% передано кл", "=IFERROR(C34/P12;0)", "Шаблон"),
    ("R32", "% АПП/ДДУ кв", "=IFERROR(Q32/L32;0)", "Шаблон"),
    ("R34", "% АПП/ДДУ кл", "=IFERROR(Q34/L34;0)", "Шаблон"),
    ("M51", "% красных кв", "=IFERROR(I51/I12;0)", "Шаблон"),
    ("T51", "% красных кл", "=IFERROR(P51/P12;0)", "Шаблон"),
    ("M58", "% проф кв", "=IFERROR(I58/C32;0)", "Шаблон"),
    ("T58", "% проф кл", "=IFERROR(P58/C34;0)", "Шаблон"),
    ("M59", "% моб кв", "=IFERROR(I59/C32;0)", "Шаблон"),
    ("T59", "% моб кл", "=IFERROR(P59/C34;0)", "Шаблон"),
]


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
    """Лист «Импорт»: SMS, проф/моб, АПП (инструкция)."""
    import_worksheet = ss.get_worksheet_by_id(int(IMPORT_SHEET_ID))
    mapping = {
        "I18": "B2",  # Отправлено SMS о НЗ, Кв
        "P18": "C2",  # Отправлено SMS о НЗ, Кл
        "I58": "B6",
        "I59": "B4",
        "P58": "C6",
        "P59": "C4",
        "Q32": "B8",
        "Q34": "C8",
    }
    out: dict[str, str] = {}
    print("\nШАГ 0.6: I18/P18, I58/I59/P58/P59, Q32/Q34 с листа «Импорт»")
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
INSP_IN_PROGRESS_IDS = {2, 3}
MRK_RECOMEND_STATUSES = {2, 3, 4, 5, 7, 6, 11, 12}
MRK_NOT_RECOMEND_STATUSES = {1, 8, 9, 10}
MRK_RED_UPS_ROOM_STATUSES = {2, 4}
MRK_ZAPIS_ROOM_STATUS = 4
MRK_CLAIMS_INWORK = {1, 2, 8}
MRK_CLAIMS_DONE = {9, 3}
MEETING_STATUS_IDS = (INSP_ACCEPTED_OK, INSP_NOT_ACCEPTED, INSP_ACCEPTED_REMARKS)


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


def _is_red_mrk(insps: list) -> bool:
    """I51: в истории клиентских приёмок есть «Не принята»."""
    return any(
        _insp_status_id(i) == INSP_NOT_ACCEPTED for i in _dedupe_inspections(insps)
    )


def _count_red_total_mrk(rooms: list, insp_map: dict) -> int:
    return sum(
        1
        for room in rooms
        if _is_red_mrk(insp_map.get(room.get("id"), []))
    )


def _is_recommended_mrk(
    room: dict,
    is_free: bool,
    room_type_id: int,
    insp_map: dict | None = None,
) -> bool:
    """I15/P15: API, правила как parser_template_b."""
    st = room.get("status_id")
    if room_type_id == 5:
        return st in MRK_RECOMEND_STATUSES or st == 1
    if st in MRK_NOT_RECOMEND_STATUSES:
        return False
    if is_free:
        return st == MRK_ZAPIS_ROOM_STATUS
    if st not in MRK_RECOMEND_STATUSES:
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


def _count_recommended_mrk(
    rooms: list, free_ids: set[int], room_type_id: int, insp_map: dict | None = None
) -> int:
    return sum(
        1
        for room in rooms
        if _is_recommended_mrk(
            room, room.get("id") in free_ids, room_type_id, insp_map
        )
    )


def _count_zapisano_mrk(rooms_by_status: dict) -> int:
    """I19: статус помещения 4 («В процессе»)."""
    return len(rooms_by_status.get(MRK_ZAPIS_ROOM_STATUS, []))


def classify_blue_room_mrk(room: dict, insps: list, blue_status: int) -> str:
    """Синие: claims по status_id (как parser_template_b)."""
    if room.get("status_id") != blue_status:
        return ""
    has_inwork = has_done = False
    for insp in insps:
        for claim in insp.get("claims") or []:
            sid = claim.get("status_id")
            if sid in MRK_CLAIMS_INWORK:
                has_inwork = True
            elif sid in MRK_CLAIMS_DONE:
                has_done = True
    if has_done and not has_inwork:
        return "done"
    return "inwork"


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
    """I52: объекты из I51, не попавшие в I54 и I53 (по объектам, не diff счётчиков)."""
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


def fetch_insp_status_names(headers: dict) -> dict[int, str]:
    out: dict[int, str] = {}
    try:
        r = requests.get(
            "https://YOUR_CRM_API_HOST/api/v1/inspections/statuses",
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
        print(f"  Не удалось загрузить справочник статусов приёмки: {e}", flush=True)
    return out


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
        if claim.get("status_id") in MRK_CLAIMS_DONE:
            return True
    return False


def _audit_criteria_mrk(
    room: dict,
    on_type: str,
    flags: dict[str, bool],
    *,
    sid: int | None,
    ct: int | None,
    insps: list,
    latest,
    insp_status_names: dict[int, str],
    client_insp_ids: frozenset[int] | None,
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
            latest_l = _latest_inspection(insps)
            ls = _insp_status_id(latest_l) if latest_l else None
            parts.append(f"st5, посл.приёмка={ls}")
        elif sid == 7:
            parts.append("st7→нет")
        reasons[cell] = ", ".join(parts)

    zcell = "I19" if on_type == "Кв" else "P19"
    if flags.get(zcell):
        reasons[zcell] = f"status_id={sid} (нужен 4 «В процессе»)"

    for prefix, fkey, rule in (
        ("I51", "I51", "любая приёмка status=5 в истории"),
        ("P51", "P51", "любая приёмка status=5 в истории"),
    ):
        if flags.get(fkey):
            reasons[fkey] = rule

    r52 = "I52" if on_type == "Кв" else "P52"
    if flags.get(r52):
        reasons[r52] = "красный (I51) ∧ не I54 ∧ не I53"

    r53 = "I53" if on_type == "Кв" else "P53"
    if flags.get(r53):
        reasons[r53] = f"red_pool(посл.=5) ∧ st∈{{2,4}}={sid} ∧ claim выполнено"

    r54 = "I54" if on_type == "Кв" else "P54"
    if flags.get(r54):
        reasons[r54] = "st∈{6,11,12} ∧ в истории была приёмка 5"

    b43 = "I43" if on_type == "Кв" else "P43"
    if flags.get(b43):
        reasons[b43] = "st=11, claim done (9/3), нет inwork"

    b41 = "I41" if on_type == "Кв" else "P41"
    if flags.get(b41):
        reasons[b41] = "st=11, синие в работе (claims)"

    k51 = "K51" if on_type == "Кв" else "R51"
    if flags.get(k51):
        reasons[k51] = "сегодня посл.приёмка=5, раньше 5 не было"

    return reasons


def build_f11_audit_rows(
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
            f_recom = _is_recommended_mrk(room, f_free, rt, insp_map)
            f_zapis = sid == MRK_ZAPIS_ROOM_STATUS
            f_c32 = sid in accepted_statuses
            f_ddu = f_c32 and ct == 1
            f_dkp = f_c32 and ct == 2
            f_green = sid in green_statuses
            f_blue = sid == blue_status
            f_oapp = sid == oapp_status

            f_red = _is_red_mrk(insps)
            f_red_done = _is_red_done(room, insps, accepted_statuses)
            f_red_ups = _red_has_claim_ups_mrk(room, insps, client_insp_ids)
            f_red_pool = bool(latest and _insp_status_id(latest) == INSP_NOT_ACCEPTED)
            f_red_inwork = f_red and not f_red_done and not f_red_ups

            blue_bucket = classify_blue_room_mrk(room, insps, blue_status)
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

            criteria = _audit_criteria_mrk(
                room,
                key,
                flags,
                sid=sid,
                ct=ct,
                insps=insps,
                latest=latest,
                insp_status_names=insp_status_names,
                client_insp_ids=client_insp_ids,
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


def build_f11_rules_sheet_rows(
    values: dict[str, str | int | float] | None = None,
) -> list[list]:
    """Лист «Справочник ячеек template_a»: ячейка — назначение — правило (+ опционально значение)."""
    header = ["Ячейка", "Назначение", "Правило сбора данных", "Источник"]
    if values is not None:
        header.append("Значение (последний прогон)")
    rows: list[list] = [
        [f"Справочник ячеек parser_template_a — дом 4662", "", "", ""],
        header,
    ]
    for cell, purpose, rule, source in F11_CELL_RULES:
        row = [cell, purpose, rule, source]
        if values is not None:
            v = values.get(cell, "")
            row.append(v if v != "" else "—")
        rows.append(row)
    rows.append([])
    rows.append([
        "Связи",
        "",
        "I20 использует L32 (ДДУ), не C32. Q32/Q34/I18/I58/I59 — только «Импорт», не CRM. "
        "Аудит: колонка на каждую ячейку (I12…Q34) + «Критерии попадания»; Q32/I18 — только сводка.",
        "",
    ])
    return rows


def build_f11_report_summary_rows(
    *,
    report_date: str,
    values: dict[str, str | int | float],
) -> list[list]:
    """Верхний блок листа «выгрузка»: все ячейки отчёта со значениями и правилами."""
    rules_by_cell = {c: (p, r, s) for c, p, r, s in F11_CELL_RULES}
    rows: list[list] = [
        ["Сводка отчёта parser_template_a", report_date, "", "", ""],
        ["Ячейка", "Назначение", "Значение", "Источник", "Правило"],
    ]
    for cell, purpose, rule, source in F11_CELL_RULES:
        val = values.get(cell, "—")
        rows.append([cell, purpose, val, source, rule])
    rows.append([])
    rows.append(["Позиции по объектам (ниже)", "", "", "", ""])
    rows.append([])
    return rows


def ensure_rules_sheet(ss):
    try:
        return ss.worksheet(RULES_SHEET_TITLE)
    except gspread.WorksheetNotFound:
        return ss.add_worksheet(RULES_SHEET_TITLE, rows=120, cols=6)


def write_f11_rules_sheet(
    ss,
    values: dict[str, str | int | float] | None = None,
) -> None:
    ws = ensure_rules_sheet(ss)
    rows = build_f11_rules_sheet_rows(values)
    needed_rows = len(rows)
    needed_cols = max(len(r) for r in rows) if rows else 1
    if ws.row_count < needed_rows:
        ws.add_rows(needed_rows - ws.row_count)
    if ws.col_count < needed_cols:
        ws.add_cols(needed_cols - ws.col_count)
    ws.clear()
    ws.update(rows, value_input_option=ValueInputOption.user_entered)
    print(f"  Справочник: {len(F11_CELL_RULES)} правил → лист «{RULES_SHEET_TITLE}»", flush=True)


def write_f11_audit_sheet(
    ss,
    rows: list[list],
    summary_rows: list[list] | None = None,
) -> None:
    # Лист аудита могут удалить — отчёт должен продолжать жить без него.
    try:
        ws = ss.get_worksheet_by_id(AUDIT_SHEET_ID)
    except gspread.WorksheetNotFound:
        print(f"  Аудит: лист id={AUDIT_SHEET_ID} не найден — пропускаю запись аудита", flush=True)
        return
    body = list(summary_rows or []) + list(rows)
    needed_rows = len(body)
    needed_cols = max(len(r) for r in body) if body else 1
    if ws.row_count < needed_rows:
        ws.add_rows(needed_rows - ws.row_count)
    if ws.col_count < needed_cols:
        ws.add_cols(needed_cols - ws.col_count)
    ws.clear()
    ws.update(
        body,
        value_input_option=ValueInputOption.user_entered,
    )
    n_summary = len(summary_rows) if summary_rows else 0
    n_objects = len(rows) - 1 if rows else 0
    print(
        f"  Аудит: сводка {max(0, n_summary - 4)} ячеек + {n_objects} объектов "
        f"→ лист id={AUDIT_SHEET_ID} ({ws.title})",
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


def _red_has_claim_ups_mrk(
    room: dict,
    insps: list,
    client_insp_ids: frozenset[int] | None = None,
) -> bool:
    """I53: красный пул + объект {2,4} + claim «Выполнено» на последней приёмке."""
    if not _is_red_pool(insps):
        return False
    if room.get("status_id") not in MRK_RED_UPS_ROOM_STATUSES:
        return False
    latest = _latest_inspection(insps)
    if not latest:
        return False
    for _, claim in _claims_on_client_inspections([latest], client_insp_ids):
        if _claim_status_name(claim) == CLAIM_STATUS_RED_UPS:
            return True
        if claim.get("status_id") in MRK_CLAIMS_DONE:
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


def _open_or_create_report_sheet(
    ss,
    template_id: int,
    sheet_prefix: str,
    report_date: date,
    *,
    force_new_sheet: bool = False,
):
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


def parser_template_a(
    audit_only: bool = False,
    report_date: date | None = None,
    force_new_sheet: bool = False,
):
    start_time = time.time()

    report_date = report_date or date.today()
    today = str(report_date)
    today_for_request = report_date.strftime("%d.%m.%Y")

    print(f"\n{'=' * 60}", flush=True)
    print(f"Запуск parser_template_a для дома 11_фаза_3_корпус (ID: 4662)", flush=True)
    print(f"Дата: {today_for_request}")
    if audit_only:
        print(f"Режим: только аудит → лист id={AUDIT_SHEET_ID}")
    else:
        print("Режим: правила листа «Инструкция»")
    print(f"{'=' * 60}\n")

    from crm_auth import load_crm_oauth, google_service_account_path

    data = load_crm_oauth()
    scope = ['https://www.googleapis.com/auth/spreadsheets',
             'https://www.googleapis.com/auth/drive']

    credentials = ServiceAccountCredentials.from_service_account_file(
        str(google_service_account_path()),
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

        template_id = "717440077"
        sheet_prefix = "ф11_к3 "
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
            "ф11_к3",
            report_date,
            force_new_sheet=force_new_sheet,
        )
        try:
            layout_block = get_import_layout_block(ss)
            _batch_acells(worksheet, list(layout_block.items()))
        except Exception as e:
            print(f"  Ошибка вставки блока с «Импорт»: {e}")

    # Авторизация в API
    r = requests.post("https://YOUR_CRM_API_HOST/api/v1/oauth/token", data)
    ref_tok = r.json()["access_token"]
    headers = {
        "Authorization": f"Bearer {ref_tok}",
        "Content-Type": "application/json"
    }
    print("\nПодключение к API установлено\n")

    # ID дома
    house_id = 4662
    insp_status_names = fetch_insp_status_names(headers)

    # Типы помещений
    room_type = {
        "Кв": 1,  # Квартиры
        "Кл": 5  # Кладовые
    }

    # Названия статусов для справки
    status_names = {
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
        12: "Односторонняя передача"
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

    kv_meets = kl_meets = kv_today = kl_today = 0
    f32_count = {"Кв": 0, "Кл": 0}
    report_stored = {
        "totals": {},
        "not_sold": {},
        "n_accepted": {},
        "ddu": {},
        "dkp": {},
        "green": {},
        "blue_total": {},
        "oapp": {},
    }

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

        # I15/P16 — API, после загрузки приёмок (ШАГ 4)
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

        report_stored["totals"][key] = total
        report_stored["not_sold"][key] = not_sold
        report_stored["n_accepted"][key] = n_accepted
        report_stored["ddu"][key] = ddu
        report_stored["dkp"][key] = app_dkp
        report_stored["green"][key] = green_total
        report_stored["blue_total"][key] = blue_total
        report_stored["oapp"][key] = oapp_total

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
            f32_count[key] = today_count
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
        recomend_counts[key] = _count_recommended_mrk(
            rooms, free_room_ids, rt, all_inspections_map
        )
        red_total[key] = _count_red_total_mrk(rooms, all_inspections_map)
        red_today_new[key] = _count_red_today_new(rooms, all_inspections_map, report_date)
        zapis_counts[key] = _count_zapisano_mrk(rooms_by_status[key])
        red_done[key] = _count_red_done(rooms, all_inspections_map, accepted_statuses)
        red_ups[key] = sum(
            1
            for room in rooms
            if _red_has_claim_ups_mrk(
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

    blue_inwork = {"Кв": 0, "Кл": 0}
    blue_done = {"Кв": 0, "Кл": 0}

    for key in room_type:
        blue_rooms = rooms_by_status[key].get(blue_status, [])

        for room in blue_rooms:
            insps = all_inspections_map.get(room.get("id"), [])
            if classify_blue_room_mrk(room, insps, blue_status) == "done":
                blue_done[key] += 1

        blue_inwork[key] = max(0, len(blue_rooms) - blue_done[key])
        step4_updates.append((blue_places[key][0], blue_inwork[key]))
        step4_updates.append((blue_ups_places[key], 0))
        step4_updates.append((blue_places[key][1], blue_done[key]))
        print(f"  {key} синие: в работе={blue_inwork[key]}, передано клиенту={blue_done[key]}")

    try:
        import_block = get_import_layout_block(ss)
    except Exception as e:
        print(f"  «Импорт» для сводки: {e}", flush=True)
        import_block = {}

    sleeping_vals = {}
    for key in room_type:
        sms_cell = "I18" if key == "Кв" else "P18"
        ddu_cell = "L32" if key == "Кв" else "L34"
        sleep_cell = sleeping_places[key]
        sleeping_vals[sleep_cell] = max(
            0,
            _parse_float_cell(import_block.get(sms_cell, 0))
            - zapis_counts[key]
            - report_stored["ddu"][key]
            - red_inwork[key]
            - red_ups[key],
        )

    report_values: dict[str, str | int | float] = {
        "B4": today_for_request,
        "I12": report_stored["totals"]["Кв"],
        "P12": report_stored["totals"]["Кл"],
        "I13": report_stored["not_sold"]["Кв"],
        "P13": report_stored["not_sold"]["Кл"],
        "I15": recomend_counts["Кв"],
        "P15": recomend_counts["Кл"],
        "I16": report_stored["totals"]["Кв"] - recomend_counts["Кв"],
        "P16": report_stored["totals"]["Кл"] - recomend_counts["Кл"],
        "I19": zapis_counts["Кв"],
        "P19": zapis_counts["Кл"],
        "I20": sleeping_vals["I20"],
        "P20": sleeping_vals["P20"],
        "H24": kv_meets,
        "H25": kl_meets,
        "H28": kv_today,
        "H29": kl_today,
        "C32": report_stored["n_accepted"]["Кв"],
        "C34": report_stored["n_accepted"]["Кл"],
        "L32": report_stored["ddu"]["Кв"],
        "L34": report_stored["ddu"]["Кл"],
        "V32": report_stored["dkp"]["Кв"],
        "V34": report_stored["dkp"]["Кл"],
        "I38": report_stored["green"]["Кв"],
        "P38": report_stored["green"]["Кл"],
        "I40": report_stored["blue_total"]["Кв"],
        "P40": report_stored["blue_total"]["Кл"],
        "I41": blue_inwork["Кв"],
        "P41": blue_inwork["Кл"],
        "I42": 0,
        "P42": 0,
        "I43": blue_done["Кв"],
        "P43": blue_done["Кл"],
        "I51": red_total["Кв"],
        "P51": red_total["Кл"],
        "I52": red_inwork["Кв"],
        "P52": red_inwork["Кл"],
        "I53": red_ups["Кв"],
        "P53": red_ups["Кл"],
        "I54": red_done["Кв"],
        "P54": red_done["Кл"],
        "I56": report_stored["oapp"]["Кв"],
        "P56": report_stored["oapp"]["Кл"],
        "F32": f32_count["Кв"],
        "F34": f32_count["Кл"],
        "K51": red_today_new["Кв"],
        "R51": red_today_new["Кл"],
    }
    for cell in ("I18", "P18", "I58", "I59", "P58", "P59", "Q32", "Q34"):
        report_values[cell] = import_block.get(cell, "—")

    summary_rows = build_f11_report_summary_rows(
        report_date=today_for_request,
        values=report_values,
    )

    print(f"\nШАГ 4.1: Аудит и справочник → лист id={AUDIT_SHEET_ID}, «{RULES_SHEET_TITLE}»")
    try:
        write_f11_rules_sheet(ss, report_values)
        audit_rows = build_f11_audit_rows(
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
        write_f11_audit_sheet(ss, audit_rows, summary_rows=summary_rows)
    except Exception as e:
        print(f"  Ошибка записи аудита: {e}", flush=True)
        traceback.print_exc()

    if audit_only:
        elapsed = time.time() - start_time
        print(f"\n{'=' * 60}")
        print(f"✅ Аудит записан за {elapsed:.1f} сек (лист id={AUDIT_SHEET_ID})")
        print(f"{'=' * 60}\n")
        return

    # Спящие: I18 - I19 - L32(ДДУ) - I52 - I53 (I19/P19 из расчёта — на листе ещё старые значения)
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
    # K51/R51: не diff(I51), а «+за сегодня» по инструкции (дата = сегодня)
    diff_i51 = float(red_today_new["Кв"])
    diff_p51 = float(red_today_new["Кл"])
    print(f"  K51/R51 (+за сегодня): Кв={diff_i51}, Кл={diff_p51}")
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
    print(f"✅ parser_template_a завершил работу за {elapsed:.1f} сек")
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
    return script_dir / ".parser_template_a.last_run.txt"


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
        description="МРК ф11 к3 (parser_template_a) — правила листа «Инструкция». Ключ: service-account.json."
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
        parser_template_a(
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
