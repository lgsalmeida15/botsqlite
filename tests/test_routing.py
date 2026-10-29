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

    def test_margem_do_formulario_herda_global_e_time(self):
        """Margem de fim de janela do formulario: time → global → 10 min."""
        self.assertEqual(routing.resolve_settings({}, None, "form")["form_window_margin_min"], 10)
        cfg = {"form_window_margin_min": "5"}
        self.assertEqual(routing.resolve_settings(cfg, None, "form")["form_window_margin_min"], 5)
        self.assertEqual(routing.resolve_settings(cfg, {}, "form")["form_window_margin_min"], 5)
        # 0 no time desliga o corte so neste time; valor negativo cai em 0.
        self.assertEqual(
            routing.resolve_settings(cfg, {"form_window_margin_min": 0}, "form")["form_window_margin_min"], 0
        )
        self.assertEqual(
            routing.resolve_settings(cfg, {"form_window_margin_min": "-3"}, "form")["form_window_margin_min"], 0
        )

    def test_affinity_require_online_nao_existe_mais(self):
        """Afinidade nao e excecao ao online: a chave antiga nao volta a valer."""
        s = routing.resolve_settings({"affinity_require_online": "true"}, None, "chat")
        self.assertNotIn("affinity_require_online", s)


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

    def test_só_formulario_nao_recebe_chat(self):
        pick, detail = routing.decide(
            [member(ANA), member(BIA)],
            settings=routing.resolve_settings({}, None, "chat"),
            kind="chat",
            online={"ana", "bia"},
            mrouting={ANA: {"kinds": "form"}},
        )
        self.assertEqual(pick["address"], BIA)
        self.assertIn("so_form=ana", detail)

    def test_kinds_do_membro_vale_para_os_dois_lados(self):
        """Quem so atende formulario recebe form (e nao chat); quem so atende chat, o inverso."""
        cfg = {}
        pick, _ = routing.decide(
            [member(ANA), member(BIA)],
            settings=routing.resolve_settings(cfg, None, "form"),
            kind="form",
            online={"ana", "bia"},
            mrouting={ANA: {"kinds": "form"}, BIA: {"kinds": "chat"}},
        )
        self.assertEqual(pick["address"], ANA)
        pick, _ = routing.decide(
            [member(ANA), member(BIA)],
            settings=routing.resolve_settings(cfg, None, "chat"),
            kind="chat",
            online={"ana", "bia"},
            mrouting={ANA: {"kinds": "form"}, BIA: {"kinds": "chat"}},
        )
        self.assertEqual(pick["address"], BIA)

    def test_tipo_padrao_do_time_vale_para_quem_nao_tem_linha(self):
        """`member_kinds` do time e herdado pelo operador sem ajuste individual."""
        s = routing.resolve_settings({}, {"member_kinds": "form"}, "form")
        self.assertEqual(s["member_kinds"], "form")
        r = routing.member_rules(None, s, "form")
        self.assertEqual(r["kinds"], "form")
        # Sem `member_kinds` no time, o padrao e atender os dois.
        self.assertEqual(routing.member_rules(None, routing.resolve_settings({}, None, "form"), "form")["kinds"], "both")
        # `kinds` invalido no banco cai no padrao (ambos), nunca bloqueia o operador.
        self.assertEqual(routing.member_rules({"kinds": "xpto"}, s, "form")["kinds"], "form")

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

    def test_form_para_de_oferecer_no_fim_da_janela(self):
        """Fim de escala: o formulario nao nasce com quem sai em minutos."""
        ms = [
            member(ANA, route_start="08:00:00", route_end="18:00:00"),
            member(BIA, route_start="08:00:00", route_end="22:00:00"),
        ]
        pick, detail = routing.decide(
            ms,
            settings=routing.resolve_settings({}, None, "form"),
            kind="form",
            online={"ana", "bia"},
            now=brt(17, 55),
            team_name="T1",
        )
        self.assertEqual(pick["address"], BIA)
        self.assertIn("fim_janela=ana:5min", detail)

    def test_margem_zero_desliga_o_corte_de_janela(self):
        ms = [member(ANA, route_start="08:00:00", route_end="18:00:00")]
        pick, _ = routing.decide(
            ms,
            settings=routing.resolve_settings({"form_window_margin_min": "0"}, None, "form"),
            kind="form",
            online={"ana"},
            now=brt(17, 55),
        )
        self.assertEqual(pick["address"], ANA)

    def test_chat_ignora_a_margem_de_fim_de_janela(self):
        """A margem e do FORMULARIO: o chat segue indo para quem esta de saida."""
        ms = [member(ANA, route_start="08:00:00", route_end="18:00:00")]
        pick, _ = routing.decide(
            ms, settings=self.settings(), kind="chat", online={"ana"}, now=brt(17, 55)
        )
        self.assertEqual(pick["address"], ANA)

    def test_escala_24h_nunca_cai_na_margem(self):
        pick, _ = routing.decide(
            [member(ANA)],
            settings=routing.resolve_settings({}, None, "form"),
            kind="form",
            online={"ana"},
            now=brt(23, 55),
        )
        self.assertEqual(pick["address"], ANA)

    def test_exclude_barra_o_alvo_proibido(self):
        """O proprio bot nunca entra como alvo (nem pelo rodizio)."""
        bot = "u:bot@claro.com"
        pick, detail = routing.decide(
            [member(ANA), member(bot)],
            settings=self.settings(),
            online={"ana", "bot"},
            exclude={bot},
            now=brt(10),
        )
        self.assertEqual(pick["address"], ANA)
        self.assertIn("proprio_bot=bot", detail)

    def test_exclude_sozinho_deixa_o_time_sem_elegivel(self):
        bot = "u:bot@claro.com"
        pick, detail = routing.decide(
            [member(bot)],
            settings=self.settings(),
            online={"bot"},
            exclude={bot},
            now=brt(10),
        )
        self.assertIsNone(pick)
        self.assertIn("proprio_bot=bot", detail)


class MargemDeJanelaTest(unittest.TestCase):
    """`_minutes_to_window_end`: quantos minutos faltam para a escala acabar."""

    def test_minutos_ate_o_fim_da_janela(self):
        dia = member(ANA, route_start="08:00:00", route_end="18:00:00")
        self.assertEqual(routing._minutes_to_window_end(dia, brt(17, 30)), 30)
        # Fora da janela (ainda nao entrou ou ja saiu) nao ha o que "faltar".
        self.assertEqual(routing._minutes_to_window_end(dia, brt(3)), 0)
        self.assertEqual(routing._minutes_to_window_end(dia, brt(20)), 0)
        # Escala 24h: nao existe fim de janela.
        self.assertIsNone(routing._minutes_to_window_end(member(ANA), brt(12)))

    def test_virada_de_meia_noite_conta_o_dia(self):
        noite = member(ANA, route_start="22:00:00", route_end="06:00:00")
        self.assertEqual(routing._minutes_to_window_end(noite, brt(23)), 420)
        self.assertEqual(routing._minutes_to_window_end(noite, brt(5, 50)), 10)


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
        self.assertIn("kinds", db._table_columns(self.conn, "team_member_routing"))
        self.assertIn("member_kinds", db._table_columns(self.conn, "team_routing"))
        cfg = db.settings(self.conn)
        self.assertEqual(cfg["max_open_forms_per_agent"], "3", "ajuste do usuário preservado")
        self.assertEqual(cfg["form_routing_enabled"], "true", "default reposto")

    def test_migracao_converte_forms_enabled_em_kinds(self):
        """Banco antigo: quem era 'so chat' passa a ter `kinds='chat'`; o resto fica 'both'."""
        self.conn.execute("INSERT INTO teams (name, active) VALUES ('T1', 1)")
        self.conn.execute(
            "INSERT INTO team_member_routing (team_id, address, weight, cap_cases, forms_enabled, kinds)"
            " VALUES (1, 'u:ana@claro.com', 0, 2, 0, 'both')"
        )
        self.conn.execute(
            "INSERT INTO team_member_routing (team_id, address, weight, cap_cases, forms_enabled, kinds)"
            " VALUES (1, 'u:bia@claro.com', 0, NULL, 1, 'both')"
        )
        db._migrate_kinds(self.conn)
        linhas = {
            str(r["address"]): str(r["kinds"])
            for r in self.conn.execute("SELECT * FROM team_member_routing").fetchall()
        }
        self.assertEqual(linhas["u:ana@claro.com"], "chat", "so chat migrado")
        self.assertEqual(linhas["u:bia@claro.com"], "both", "padrao = atende os dois")
        cap = self.conn.execute(
            "SELECT cap_cases FROM team_member_routing WHERE address = 'u:ana@claro.com'"
        ).fetchone()
        self.assertEqual(int(cap["cap_cases"]), 2, "a migracao nao mexe em cap nenhum")

    def test_migracao_traz_a_margem_e_apaga_a_chave_morta(self):
        """`form_window_margin_min` nasce no time + global; `affinity_require_online` sai."""
        self.conn.execute("INSERT INTO teams (name, active) VALUES ('T1', 1)")
        db.set_last_assigned(self.conn, 1, "chat", "u:ana@claro.com")
        # Banco antigo: a coluna nao existe e a chave morta esta gravada.
        self.conn.execute("ALTER TABLE team_routing DROP COLUMN form_window_margin_min")
        self.conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES ('affinity_require_online', 'true')"
            " ON CONFLICT (key) DO UPDATE SET value = 'true'"
        )
        db.ensure_schema(self.conn)
        self.assertIn("form_window_margin_min", db._table_columns(self.conn, "team_routing"))
        cfg = db.settings(self.conn)
        self.assertNotIn("affinity_require_online", cfg, "a chave morta nao volta")
        self.assertEqual(cfg["form_window_margin_min"], "10")
        # As linhas do time continuam inteiras (a coluna nova entra como NULL).
        linha = db.team_routing(self.conn, 1)
        self.assertEqual(linha["last_assigned_chat"], "u:ana@claro.com")
        self.assertIsNone(linha["form_window_margin_min"], "NULL = herda o global")

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
        self.assertEqual(
            db.due_routes(self.conn, form_min_wait_sec=0), {"chat": [], "form": []}
        )
        db.enqueue_route(self.conn, "form", 2, "h:q1", 1, delay_sec=0)
        due = db.due_routes(self.conn, form_min_wait_sec=0)
        self.assertEqual([int(r["chat_id"]) for r in due["form"]], [2])
        self.assertEqual(due["chat"], [], "linha do chat e apartada")
        db.retry_route(self.conn, int(due["form"][0]["id"]), 1, 0, "sem analista")
        self.assertEqual(int(db.due_routes(self.conn, form_min_wait_sec=0)["form"][0]["attempts"]), 1)

    def test_due_routes_separa_as_linhas_do_chat_e_do_formulario(self):
        """Um formulario novo nao entra na fila atras dos chats (linhas apartadas)."""
        for cid in range(1, 4):
            db.enqueue_route(self.conn, "chat", cid, "h:q1", 1, delay_sec=0)
        db.enqueue_route(self.conn, "form", 9, "h:q1", 1, delay_sec=0)
        due = db.due_routes(self.conn, form_min_wait_sec=0)
        self.assertEqual([int(r["chat_id"]) for r in due["chat"]], [1, 2, 3])
        self.assertEqual([int(r["chat_id"]) for r in due["form"]], [9], "form e a cabeca da linha dele")

    def test_due_routes_respeita_a_janela_minima_do_formulario(self):
        """Formulario recem-chegado nao aparece; apos a janela, aparece; `0` desliga."""
        rid = db.enqueue_route(self.conn, "form", 5, "h:q1", 1, delay_sec=0)
        self.assertEqual(db.due_routes(self.conn)["form"], [], "recem-chegado, ainda na janela")
        self.assertEqual(
            [int(r["id"]) for r in db.due_routes(self.conn, form_min_wait_sec=0)["form"]], [rid],
            "form_min_wait_sec=0 desliga a espera",
        )
        self.conn.execute(
            "UPDATE pending_routes SET created_at = datetime('now', '-6 minutes') WHERE id = ?", (rid,)
        )
        self.assertEqual(
            [int(r["id"]) for r in db.due_routes(self.conn)["form"]], [rid],
            "6 min depois (> 5 min padrao), aparece",
        )

    def test_waiting_routes_lista_o_que_espera_mesmo_adiado(self):
        """Bot desligado: o tick precisa de TODAS as rotas em espera, nas duas linhas.

        `due_routes` nao serve (so devolve o que venceu) e uma rota adiada por
        retry tambem tem de ser encerrada quando o bot sai.
        """
        db.enqueue_route(self.conn, "chat", 7, "h:q1", 1, delay_sec=0)
        db.enqueue_route(self.conn, "form", 8, "h:q1", 1, delay_sec=0)
        adiada = db.enqueue_route(self.conn, "chat", 9, "h:q1", 1, delay_sec=0)
        db.retry_route(self.conn, adiada, 2, 600, "sem analista")
        self.assertEqual([int(r["chat_id"]) for r in db.waiting_routes(self.conn)], [7, 8, 9])
        self.assertEqual([int(r["chat_id"]) for r in db.waiting_routes(self.conn, 2)], [7, 8])
        db.finish_route(self.conn, adiada, "failed", "encerrada")
        self.assertNotIn(9, [int(r["chat_id"]) for r in db.waiting_routes(self.conn)])

    def test_set_setting_grava_e_sobrescreve(self):
        db.set_setting(self.conn, "bot_own_address", "u:bot@claro.com")
        self.assertEqual(db.settings(self.conn)["bot_own_address"], "u:bot@claro.com")
        db.set_setting(self.conn, "bot_own_address", "u:bot2@claro.com")
        self.assertEqual(db.settings(self.conn)["bot_own_address"], "u:bot2@claro.com")
        db.set_setting(self.conn, "bot_own_address", None)
        self.assertEqual(db.settings(self.conn)["bot_own_address"], "")


if __name__ == "__main__":
    unittest.main()

