# -*- coding: utf-8 -*-
"""Janela de plantão, inclusive 22:00–06:00 que atravessa a meia-noite."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import TZ_BRT, Engine, _as_clock, _in_route_window  # noqa: E402
from python.routing import _minutes_to_window_end, resolve_member_schedule  # noqa: E402
from tests.test_submenu import FakeSock, mem_conn, seed_queue_menu  # noqa: E402

NIGHT = {"route_start": "22:00:00", "route_end": "06:00:00"}
DAY = {"route_start": "08:00:00", "route_end": "18:00:00"}


def brt(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 9, 1, h, m, s, tzinfo=TZ_BRT)


class AsClockTest(unittest.TestCase):
    def test_string_sem_zero_e_milissegundo(self):
        self.assertEqual(_as_clock("6:00"), dt_time(6, 0, 0))
        self.assertEqual(_as_clock("22:00:00.000"), dt_time(22, 0, 0))

    def test_datetime_e_timedelta(self):
        self.assertEqual(_as_clock(datetime(2026, 9, 1, 22, 0, 0)), dt_time(22, 0, 0))
        self.assertEqual(_as_clock(timedelta(hours=6)), dt_time(6, 0, 0))
        self.assertEqual(_as_clock("2026-09-01 22:00:00"), dt_time(22, 0, 0))


class InRouteWindowTest(unittest.TestCase):
    def test_overnight_dentro(self):
        for hh, mm in ((22, 0), (23, 30), (0, 0), (5, 59)):
            self.assertTrue(
                _in_route_window(NIGHT, brt(hh, mm)),
                f"esperava dentro às {hh:02d}:{mm:02d}",
            )

    def test_overnight_fora(self):
        self.assertFalse(_in_route_window(NIGHT, brt(6, 0)))
        self.assertFalse(_in_route_window(NIGHT, brt(12, 0)))

    def test_mesmo_dia(self):
        self.assertTrue(_in_route_window(DAY, brt(8, 0)))
        self.assertFalse(_in_route_window(DAY, brt(7, 59)))
        self.assertFalse(_in_route_window(DAY, brt(18, 0)))

    def test_vazio_e_igual_sao_24h(self):
        self.assertTrue(_in_route_window({}, brt(3, 0)))
        self.assertTrue(
            _in_route_window({"route_start": "10:00:00", "route_end": "10:00:00"}, brt(3, 0))
        )


def shift(start, end, days_mask=127):
    return {"kind": "shift", "start_time": start, "end_time": end, "days_mask": days_mask}


def break_(start, end, days_mask=127):
    return {"kind": "break", "start_time": start, "end_time": end, "days_mask": days_mask}


class ScheduleRowsTest(unittest.TestCase):
    def test_dois_turnos_no_mesmo_dia(self):
        rows = [shift("08:00", "12:00"), shift("13:00", "18:00")]
        self.assertTrue(_in_route_window({}, brt(9, 0), rows=rows))
        self.assertFalse(_in_route_window({}, brt(12, 30), rows=rows))
        self.assertTrue(_in_route_window({}, brt(17, 0), rows=rows))
        self.assertFalse(_in_route_window({}, brt(19, 0), rows=rows))

    def test_descanso_dentro_do_turno(self):
        rows = [shift("08:00", "18:00"), break_("12:00", "13:00")]
        self.assertTrue(_in_route_window({}, brt(11, 59), rows=rows))
        self.assertFalse(_in_route_window({}, brt(12, 0), rows=rows))
        self.assertFalse(_in_route_window({}, brt(12, 59), rows=rows))
        self.assertTrue(_in_route_window({}, brt(13, 0), rows=rows))

    def test_descanso_fora_do_turno_e_no_op(self):
        rows = [shift("08:00", "12:00"), break_("14:00", "15:00")]
        self.assertFalse(_in_route_window({}, brt(14, 30), rows=rows))

    def test_sem_turno_cadastrado_e_sempre_disponivel_fora_do_descanso(self):
        rows = [break_("12:00", "13:00")]
        self.assertTrue(_in_route_window({}, brt(3, 0), rows=rows))
        self.assertFalse(_in_route_window({}, brt(12, 30), rows=rows))

    def test_days_mask_restringe_dias_uteis(self):
        dias_uteis = 0b0011111  # seg(0)..sex(4)
        rows = [shift("08:00", "18:00", days_mask=dias_uteis)]
        terca = brt(10, 0)  # 2026-09-01 e uma terca-feira (weekday()==1)
        self.assertEqual(terca.weekday(), 1)
        self.assertTrue(_in_route_window({}, terca, rows=rows))
        sabado = datetime(2026, 9, 5, 10, 0, tzinfo=TZ_BRT)
        self.assertEqual(sabado.weekday(), 5)
        self.assertFalse(_in_route_window({}, sabado, rows=rows))

    def test_minutes_to_window_end_considera_descanso_que_vai_comecar(self):
        rows = [shift("08:00", "18:00"), break_("12:00", "13:00")]
        self.assertEqual(_minutes_to_window_end({}, brt(11, 0), rows=rows), 60)
        self.assertEqual(_minutes_to_window_end({}, brt(15, 0), rows=rows), 180)

    def test_resolve_member_schedule_override_total_do_analista(self):
        schedules = {
            "": [shift("08:00", "18:00")],
            "u:ana@claro.com": [shift("22:00", "06:00")],
        }
        self.assertEqual(resolve_member_schedule(schedules, "u:ana@claro.com"), schedules["u:ana@claro.com"])
        self.assertEqual(resolve_member_schedule(schedules, "u:bia@claro.com"), schedules[""])
        self.assertEqual(resolve_member_schedule({}, "u:ana@claro.com"), [])


class PickAssigneeOvernightTest(unittest.IsolatedAsyncioTestCase):
    def _eng(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        conn.execute(
            "UPDATE team_members SET route_start = '22:00:00', route_end = '06:00:00' "
            "WHERE address = 'u:ana@claro.com'"
        )
        sock = FakeSock()
        return Engine(conn, sock), conn

    def _freeze(self, when: datetime):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                n = when
                if tz is not None:
                    return n.astimezone(tz) if n.tzinfo else n.replace(tzinfo=tz)
                return n

        return Frozen

    async def test_madrugada_escolhe_analista(self):
        eng, _conn = self._eng()
        with patch("python.engine.datetime", self._freeze(brt(3, 0))):
            picked, reason = await eng.pick_assignee("h:q1")
        self.assertIsNotNone(picked)
        self.assertEqual(picked["address"], "u:ana@claro.com")
        self.assertIn("22:00-06:00", reason)

    async def test_meio_dia_fora_horario(self):
        eng, _conn = self._eng()
        with patch("python.engine.datetime", self._freeze(brt(12, 0))):
            picked, reason = await eng.pick_assignee("h:q1")
        self.assertIsNone(picked)
        self.assertIn("fora_horario", reason)


class DbScheduleTest(unittest.TestCase):
    def test_migracao_converte_route_start_end_legado(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        conn.execute(
            "UPDATE team_members SET route_start = '08:00:00', route_end = '18:00:00'"
            " WHERE address = 'u:ana@claro.com'"
        )
        db.ensure_schema(conn)  # idempotente: roda a migracao de novo
        rows = db.team_schedules(conn, 1)
        self.assertEqual(len(rows.get("u:ana@claro.com", [])), 1)
        linha = rows["u:ana@claro.com"][0]
        self.assertEqual(linha["kind"], "shift")
        self.assertEqual(linha["start_time"], "08:00:00")
        self.assertEqual(linha["end_time"], "18:00:00")

    def test_replace_e_clear_member_schedules(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        db.replace_schedules(conn, 1, "", [{"kind": "shift", "start_time": "08:00", "end_time": "18:00"}])
        db.replace_schedules(
            conn,
            1,
            "u:ana@claro.com",
            [
                {"kind": "shift", "start_time": "22:00", "end_time": "06:00"},
                {"kind": "break", "start_time": "02:00", "end_time": "03:00"},
            ],
        )
        rows = db.team_schedules(conn, 1)
        self.assertEqual(len(rows[""]), 1)
        self.assertEqual(len(rows["u:ana@claro.com"]), 2)

        db.clear_member_schedules(conn, 1, "u:ana@claro.com")
        rows = db.team_schedules(conn, 1)
        self.assertNotIn("u:ana@claro.com", rows)
        self.assertEqual(len(rows[""]), 1)


class PickAssigneeBreakTest(unittest.IsolatedAsyncioTestCase):
    def _eng(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        conn.execute(
            "INSERT INTO team_member_schedules (team_id, address, kind, start_time, end_time)"
            " VALUES (1, 'u:ana@claro.com', 'shift', '08:00', '18:00')"
        )
        conn.execute(
            "INSERT INTO team_member_schedules (team_id, address, kind, start_time, end_time)"
            " VALUES (1, 'u:ana@claro.com', 'break', '12:00', '13:00')"
        )
        sock = FakeSock()
        return Engine(conn, sock), conn

    def _freeze(self, when: datetime):
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                n = when
                if tz is not None:
                    return n.astimezone(tz) if n.tzinfo else n.replace(tzinfo=tz)
                return n

        return Frozen

    async def test_em_descanso_nao_e_escolhida(self):
        eng, _conn = self._eng()
        with patch("python.engine.datetime", self._freeze(brt(12, 30))):
            picked, reason = await eng.pick_assignee("h:q1")
        self.assertIsNone(picked)
        self.assertIn("fora_horario", reason)

    async def test_depois_do_descanso_volta_a_ser_escolhida(self):
        eng, _conn = self._eng()
        with patch("python.engine.datetime", self._freeze(brt(13, 0))):
            picked, reason = await eng.pick_assignee("h:q1")
        self.assertIsNotNone(picked)
        self.assertEqual(picked["address"], "u:ana@claro.com")


if __name__ == "__main__":
    unittest.main()
