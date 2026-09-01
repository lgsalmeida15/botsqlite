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

from python.engine import TZ_BRT, Engine, _as_clock, _in_route_window  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
