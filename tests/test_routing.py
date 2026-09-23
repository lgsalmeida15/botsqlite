# -*- coding: utf-8 -*-
"""Motor de roteamento: herança de ajustes, filtros, estratégias e diagnóstico."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python import routing  # noqa: E402
from python.engine import TZ_BRT  # noqa: E402
from tests.test_submenu import mem_conn  # noqa: E402

ANA = "u:ana@claro.com"
BIA = "u:bia@claro.com"
CAIO = "u:caio@claro.com"
LOGIN = {"u:ana@claro.com": "ana", "u:bia@claro.com": "bia", "u:caio@claro.com": "caio"}


def member(addr, name="", **extra):
    m = {
        "team_id": 1,
        "address": addr,
        "name": name or addr,
        "sort_order": 0,
        "active": 1,
        "route_start": None,
        "route_end": None,
    }
    m.update(extra)
    return m


def brt(h, m=0):
    return datetime(2026, 9, 1, h, m, tzinfo=TZ_BRT)


class ResolveSettingsTest(unittest.TestCase):
    def test_padroes_sem_cfg_nem_linha(self):
        s = routing.resolve_settings({}, None, "chat")
        self.assertEqual(s["strategy"], "round_robin")
        self.assertEqual(s["cap"], 8)
        self.assertEqual(s["attempts"], 5)
        self.assertEqual(s["retry_interval_sec"], 60.0)
        self.assertEqual(s["fallback"], "close")
        self.assertTrue(s["require_online"])
        self.assertTrue(s["respect_window"])
        self.assertTrue(s["form_enabled"])

    def test_formulario_tem_cap_e_fallback_proprios(self):
        cfg = {
            "max_open_chats_per_agent": "8",
            "max_open_forms_per_agent": "2",
            "form_route_attempts": "3",
            "form_route_retry_interval_sec": "15",
            "form_route_fallback": "monitor",
        }
        s = routing.resolve_settings(cfg, None, "form")
        self.assertEqual(s["cap"], 2)
        self.assertEqual(s["attempts"], 3)
        self.assertEqual(s["retry_interval_sec"], 15.0)
        self.assertEqual(s["fallback"], "monitor")
        self.assertEqual(routing.resolve_settings(cfg, None, "chat")["cap"], 8)

    def test_linha_do_time_sobrepoe_o_global(self):
        cfg = {"routing_strategy": "round_robin", "max_open_forms_per_agent": "8"}
        row = {
            "strategy": "least_load",
            "cap_form": 1,
            "attempts": 2,
            "retry_interval_sec": 5,
            "fallback": "retry",
            "form_enabled": 0,
            "respect_window": 0,
            "require_online": 0,
        }
        s = routing.resolve_settings(cfg, row, "form")
        self.assertEqual(s["strategy"], "least_load")
        self.assertEqual(s["cap"], 1)
        self.assertEqual(s["attempts"], 2)
        self.assertEqual(s["retry_interval_sec"], 5.0)
        self.assertFalse(s["form_enabled"])
        self.assertFalse(s["respect_window"])
        self.assertFalse(s["require_online"])

    def test_fallback_invalido_cai_no_padrao_do_tipo(self):
        self.assertEqual(routing.resolve_settings({}, {"fallback": "xpto"}, "chat")["fallback"], "close")
        self.assertEqual(routing.resolve_settings({}, {"fallback": "xpto"}, "form")["fallback"], "retry")
        # 'close' não se aplica ao formulário: o bot não é participante do form.
        self.assertEqual(routing.resolve_settings({}, {"fallback": "close"}, "form")["fallback"], "retry")

    def test_estrategia_invalida_vira_round_robin(self):
        self.assertEqual(routing.resolve_settings({"routing_strategy": "xpto"}, None, "chat")["strategy"], "round_robin")

    def test_cap_zero_bloqueia_todos(self):
        self.assertEqual(routing.resolve_settings({}, {"cap_chat": 0}, "chat")["cap"], 0)

    def test_kind_desconhecido_vira_chat(self):
        self.assertEqual(routing.resolve_settings({}, None, "xpto")["kind"], "chat")


class MemberRulesTest(unittest.TestCase):
    def test_sem_linha_herda_o_cap_do_time(self):
        settings = routing.resolve_settings({}, {"cap_form": 3}, "form")
        r = routing.member_rules(None, settings, "form")
        self.assertEqual(r["cap"], 3)
        self.assertEqual(r["weight"], 0)
        self.assertTrue(r["forms_enabled"])

    def test_linha_do_membro_sobrepoe(self):
        settings = routing.resolve_settings({}, {"cap_form": 3}, "form")
        r = routing.member_rules({"cap_form": 1, "weight": 5, "forms_enabled": 0}, settings, "form")
        self.assertEqual(r["cap"], 1)
        self.assertEqual(r["weight"], 5)
        self.assertFalse(r["forms_enabled"])


class ChooseTest(unittest.TestCase):
    def test_round_robin_segue_o_ultimo(self):
        ms = [member(ANA), member(BIA), member(CAIO)]
        pick, where = routing.choose(ms, strategy="round_robin", last_assigned=BIA)
        self.assertEqual(pick["address"], CAIO)
        self.assertEqual(where, "pos=3/3")

    def test_round_robin_volta_ao_primeiro(self):
        ms = [member(ANA), member(BIA), member(CAIO)]
        pick, _ = routing.choose(ms, strategy="round_robin", last_assigned=CAIO)
        self.assertEqual(pick["address"], ANA)

    def test_round_robin_sem_ultimo_comeca_no_primeiro(self):
        ms = [member(ANA), member(BIA)]
        pick, _ = routing.choose(ms, strategy="round_robin", last_assigned="")
        self.assertEqual(pick["address"], ANA)
        pick, _ = routing.choose(ms, strategy="round_robin", last_assigned="u:zeca@claro.com")
        self.assertEqual(pick["address"], ANA)

    def test_prioridade_maior_weight_vence(self):
        ms = [member(ANA), member(BIA), member(CAIO)]
        w = {ANA: 0, BIA: 5, CAIO: 5}
        pick, _ = routing.choose(ms, strategy="priority", last_assigned=BIA, weights=w)
        self.assertEqual(pick["address"], CAIO)
        pick, _ = routing.choose(ms, strategy="priority", last_assigned=CAIO, weights=w)
        self.assertEqual(pick["address"], BIA)

    def test_menor_carga_escolhe_o_mais_livre(self):
        ms = [member(ANA), member(BIA), member(CAIO)]
        pick, _ = routing.choose(
            ms, strategy="least_load", last_assigned="", loads={ANA: 0, BIA: 5, CAIO: 2}
        )
        self.assertEqual(pick["address"], ANA)

    def test_menor_carga_desempata_por_weight_e_rodizio(self):
        ms = [member(ANA), member(BIA), member(CAIO)]
        pick, _ = routing.choose(
            ms,
            strategy="least_load",
            last_assigned=BIA,
            loads={ANA: 3, BIA: 0, CAIO: 0},
            weights={ANA: 9, BIA: 1, CAIO: 1},
        )
        self.assertEqual(pick["address"], CAIO)

    def test_sem_elegivel(self):
        pick, where = routing.choose([], strategy="round_robin")
        self.assertIsNone(pick)
        self.assertEqual(where, "")


class DecideTest(unittest.TestCase):
    def settings(self, **over):
        base = routing.resolve_settings({}, None, "chat")
        base.update(over)
        return base

    def test_detalhe_traz_o_diagnostico(self):
        pick, detail = routing.decide(
            [member(ANA)],
            settings=self.settings(),
            online={"ana"},
            now=brt(10),
            team_name="T1",
            attempts_used=2,
        )
        self.assertEqual(pick["address"], ANA)
        for campo in (
            "time=T1",
            "estrategia=round_robin",
            "cap=8",
            "membros=ana",
            "janela=ana:24h",
            "carga=ana=0",
            "tentativas=2",
            "rr=—→",
        ):
            self.assertIn(campo, detail)

    def test_fora_da_janela_nao_elege(self):
        ms = [member(ANA, route_start="08:00:00", route_end="18:00:00")]
        pick, detail = routing.decide(
            ms, settings=self.settings(), online={"ana"}, now=brt(3), team_name="T1"
        )
        self.assertIsNone(pick)
        self.assertIn("fora_horario=ana:08:00-18:00", detail)

    def test_respect_window_false_ignora_horario(self):
        ms = [member(ANA, route_start="08:00:00", route_end="18:00:00")]
        pick, _ = routing.decide(
            ms, settings=self.settings(respect_window=False), online={"ana"}, now=brt(3)
        )
        self.assertEqual(pick["address"], ANA)

    def test_sem_online_nao_elege(self):
        pick, detail = routing.decide([member(ANA)], settings=self.settings(), online=set())
        self.assertIsNone(pick)
        self.assertIn("sem_online=ana", detail)

    def test_require_online_false_ignora_online(self):
        pick, _ = routing.decide(
            [member(ANA)], settings=self.settings(require_online=False), online=set()
        )
        self.assertEqual(pick["address"], ANA)

    def test_cap_do_membro_bloqueia(self):
        pick, detail = routing.decide(
            [member(ANA)],
            settings=self.settings(cap=5),
            online={"ana"},
            mrouting={ANA: {"cap_chat": 1}},
            loads={ANA: 1},
        )
        self.assertIsNone(pick)
        self.assertIn("no_cap=ana", detail)

    def test_form_ignora_quem_nao_recebe_formulario(self):
        pick, detail = routing.decide(
            [member(ANA), member(BIA)],
            settings=routing.resolve_settings({}, None, "form"),
            kind="form",
            online={"ana", "bia"},
            mrouting={ANA: {"forms_enabled": 0}},
        )
        self.assertEqual(pick["address"], BIA)
        self.assertIn("so_chat=ana", detail)

    def test_cascata_prioridade_com_titular_no_cap(self):
        pick, _ = routing.decide(
            [member(ANA), member(BIA)],
            settings=self.settings(strategy="priority", cap=5),
            online={"ana", "bia"},
            mrouting={ANA: {"weight": 5, "cap_chat": 1}},
            loads={ANA: 1, BIA: 0},
        )
        self.assertEqual(pick["address"], BIA)

    def test_janela_suporta_virada_de_meia_noite(self):
        ms = [member(ANA, route_start="22:00:00", route_end="06:00:00")]
        pick, _ = routing.decide(
            ms, settings=self.settings(), online={"ana"}, now=brt(3)
        )
        self.assertEqual(pick["address"], ANA)


class MigracaoRoutingTest(unittest.TestCase):
    """Deploy em banco antigo: cria tabelas/settings sem apagar o que o usuário definiu."""

    def setUp(self):
        self.conn = mem_conn()

    def _drop_routing(self):
        for t in ("team_routing", "team_member_routing", "pending_routes"):
            self.conn.execute(f"DROP TABLE {t}")
        self.conn.execute("DELETE FROM bot_settings WHERE key = 'form_routing_enabled'")

    def test_ensure_schema_recria_tabelas_e_preserva_ajustes(self):
        self.conn.execute(
            "UPDATE bot_settings SET value = '3' WHERE key = 'max_open_forms_per_agent'"
        )
        self._drop_routing()
        db.ensure_schema(self.conn)
        for t in ("team_routing", "team_member_routing", "pending_routes"):
            self.assertIn(t, db._table_names(self.conn))
        self.assertIn("weight", db._table_columns(self.conn, "team_member_routing"))
        self.assertIn("last_assigned_form", db._table_columns(self.conn, "team_routing"))
        cfg = db.settings(self.conn)
        self.assertEqual(cfg["max_open_forms_per_agent"], "3", "ajuste do usuário preservado")
        self.assertEqual(cfg["form_routing_enabled"], "true", "default reposto")

    def test_ensure_schema_e_idempotente(self):
        self.conn.execute("INSERT INTO teams (name, active) VALUES ('T1', 1)")
        db.set_last_assigned(self.conn, 1, "form", "u:ana@claro.com")
        db.ensure_schema(self.conn)
        db.ensure_schema(self.conn)
        seed = db.team_routing(self.conn, 1)
        self.assertEqual(seed["last_assigned_form"], "u:ana@claro.com")

    def test_rodizio_de_chat_e_formulario_sao_independentes(self):
        self.conn.execute("INSERT INTO teams (name, active) VALUES ('T1', 1)")
        db.set_last_assigned(self.conn, 1, "chat", "u:ana@claro.com")
        db.set_last_assigned(self.conn, 1, "form", "u:bia@claro.com")
        row = db.team_routing(self.conn, 1)
        self.assertEqual(row["last_assigned_chat"], "u:ana@claro.com")
        self.assertEqual(row["last_assigned_form"], "u:bia@claro.com")
        teams = self.conn.execute("SELECT last_assigned FROM teams WHERE id = 1").fetchone()
        self.assertEqual(teams["last_assigned"], "u:ana@claro.com", "legado do chat mantido")

    def test_rota_vista_evita_duplicidade(self):
        self.assertFalse(db.route_seen(self.conn, "form", 99))
        rid = db.enqueue_route(self.conn, "form", 99, "h:q1", 1, detail="teste")
        self.assertTrue(db.route_seen(self.conn, "form", 99))
        db.finish_route(self.conn, rid, "done", "ok")
        self.assertTrue(db.route_seen(self.conn, "form", 99), "rota concluída continua bloqueando repetição")
        self.assertFalse(db.route_seen(self.conn, "form", 100))

    def test_due_routes_respeita_o_agendamento(self):
        db.enqueue_route(self.conn, "form", 1, "h:q1", 1, delay_sec=300)
        self.assertEqual(db.due_routes(self.conn), [])
        db.enqueue_route(self.conn, "form", 2, "h:q1", 1, delay_sec=0)
        due = db.due_routes(self.conn)
        self.assertEqual([int(r["chat_id"]) for r in due], [2])
        db.retry_route(self.conn, int(due[0]["id"]), 1, 0, "sem analista")
        self.assertEqual(int(db.due_routes(self.conn)[0]["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()

