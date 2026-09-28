# -*- coding: utf-8 -*-
"""Roteamento do formulário de validação: entrega, retry, travas e ajustes."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python import routing  # noqa: E402
from python.engine import MEM, Engine  # noqa: E402
from tests.test_form_open import form_chat  # noqa: E402
from tests.test_submenu import awaiting, insert_option, mem_conn, seed_queue_menu  # noqa: E402

ANA = "u:ana@claro.com"
BIA = "u:bia@claro.com"
TEC = "u:tec@claro.com"


class RouteSock:
    """Socket fake com online, carga e mensagens controláveis (logins sem domínio)."""

    own_address = "u:bot@claro.com"

    def __init__(self, online=None, chats=None, msgs=None, move_error=""):
        self.cmds: list[tuple[str, dict]] = []
        self.online = list(online if online is not None else ["ana"])
        self.chats = list(chats or [])
        self.msgs = dict(msgs or {})
        self.move_error = move_error

    async def command(self, name, payload=None, **kwargs):
        payload = payload or {}
        self.cmds.append((name, payload))
        if name == "get_online_users":
            return {"d": {"result": list(self.online)}}
        if name == "get_active_chats":
            return {"d": {"chats": list(self.chats)}}
        if name == "get_chat_messages":
            return {"d": {"messages": list(self.msgs.get(int(payload.get("chat_id")), []))}}
        if name == "move_chat" and self.move_error:
            raise RuntimeError(self.move_error)
        return {"d": {}}

    def names(self) -> list[str]:
        return [n for n, _ in self.cmds]

    def last(self, name: str) -> dict:
        for n, p in reversed(self.cmds):
            if n == name:
                return p
        raise AssertionError(f"comando {name} não foi enviado")

    def sent_texts(self) -> list[str]:
        return [p["message"]["text"] for n, p in self.cmds if n == "send_message"]


def load_chat(addr, cid=1):
    return {"chat_id": cid, "participants": {addr: {"address": addr, "present": True}}}


def load_form(addr, tec, cid=1):
    """Formulario em atendimento (kind form) na carga do operador."""
    return {
        "chat_id": cid,
        "mode": "message_scenario",
        "participants": {
            addr: {"address": addr, "present": True},
            tec: {"address": tec, "name": "Tec", "present": True},
        },
    }


def form_sem_identidade(cid=99, **over):
    """Formulario como o TOA manda de verdade: SEM o address do tecnico.

    E o caso que obrigava o bot a adivinhar o vinculo por fila/recencia.
    """
    c = {
        "chat_id": cid,
        "mode": "message_scenario",
        "lifecycle": "pending",
        "ended_time": -1,
        "person": "Message Scenario Agent",
        "helpdesk_address": "h:q1",
        "participants": {
            "CREATEDBYMESSAGESCENARIO": {
                "address": "CREATEDBYMESSAGESCENARIO",
                "role_desc": "Moderator",
            },
        },
        "first_message": {
            "subject": "Solicitação de Encerramento",
            "author": "CREATEDBYMESSAGESCENARIO",
        },
    }
    c.update(over)
    return c


def add_member(conn, addr, name="", **fields):
    cols = {"team_id": 1, "address": addr, "name": name or addr, "sort_order": 0, "active": 1}
    cols.update(fields)
    conn.execute(
        f"INSERT INTO team_members ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})",
        list(cols.values()),
    )


def events(conn) -> list[str]:
    return [r["reason"] for r in conn.execute("SELECT reason FROM bot_events ORDER BY id").fetchall()]


def routes(conn, status=None) -> list[dict]:
    sql = "SELECT * FROM pending_routes"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def engine_with(conn, sock) -> Engine:
    eng = Engine(conn, sock)
    eng.cfg["transfer_attempts"] = "1"
    eng.cfg["transfer_retry_interval_sec"] = "0"
    return eng


class FormRouteTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, online=None, chats=None, with_team=True, msgs=None, move_error=""):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=with_team)
        sock = RouteSock(online=online, chats=chats, msgs=msgs, move_error=move_error)
        return engine_with(conn, sock), conn, sock

    async def test_form_sem_identidade_usa_o_autor_das_mensagens(self):
        """Payload sem address: o AUTOR das mensagens vira o caso (origem=autor)."""
        eng, conn, _sock = self._eng(
            msgs={99: [{"author": TEC, "text": "validando", "time": 1}]}
        )
        await eng.consider(form_sem_identidade())
        caso = conn.execute("SELECT * FROM route_assignments WHERE chat_id = 99").fetchone()
        self.assertEqual(caso["case_key"], "a:" + TEC)
        self.assertEqual(caso["identity_source"], "autor")

    async def test_move_recusado_encerra_a_rota(self):
        """`move_chat` recusado pelo TOA não gira para sempre: para no teto."""
        eng, conn, _sock = self._eng(move_error="10: erro do TOA sem mensagem")
        eng.cfg["form_move_max_fail"] = "2"
        await eng.consider(form_chat())
        for _ in range(4):
            conn.execute("UPDATE pending_routes SET next_try_at = datetime('now','-1 seconds')")
            await eng.tick_routes()
        rota = conn.execute("SELECT * FROM pending_routes WHERE chat_id = 99").fetchone()
        self.assertEqual(rota["status"], "failed")
        self.assertIn("form_move_refused", events(conn))
        self.assertGreaterEqual(int(rota["move_fails"] or 0), 2)

    async def test_form_ja_com_analista_e_adotado_sem_mover(self):
        """Formulário já com um analista do time (`user_joined`): adota o dono."""
        eng, conn, sock = self._eng(
            msgs={99: [{"author": ANA, "system_message": "user_joined", "time": 2}]}
        )
        await eng.consider(form_chat())
        caso = conn.execute("SELECT * FROM route_assignments WHERE chat_id = 99").fetchone()
        self.assertEqual(caso["assignee"], ANA)
        self.assertEqual(caso["identity_source"], "adocao")
        self.assertEqual(caso["requester_addr"], TEC)
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("form_adopted", events(conn))

    async def test_adocao_usa_o_autor_quando_o_payload_nao_tem_address(self):
        """Sem address no payload, a adoção grava o AUTOR das mensagens como requisitante."""
        eng, conn, _sock = self._eng(
            msgs={
                99: [
                    {"author": TEC, "text": "validando", "time": 1},
                    {"author": ANA, "system_message": "user_joined", "time": 2},
                ]
            }
        )
        await eng.consider(form_sem_identidade())
        caso = conn.execute("SELECT * FROM route_assignments WHERE chat_id = 99").fetchone()
        self.assertEqual(caso["assignee"], ANA)
        self.assertEqual(caso["identity_source"], "adocao")
        self.assertEqual(caso["requester_addr"], TEC)

    async def test_form_anotado_como_passado_nao_e_entregue(self):
        """Formulário já encerrado (analista saiu) não vai para a fila."""
        eng, conn, sock = self._eng(
            msgs={
                99: [
                    {"author": ANA, "system_message": "user_joined", "time": 2},
                    {"author": ANA, "system_message": "user_left", "time": 3},
                ]
            }
        )
        await eng.consider(form_chat())
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("form_skip_past", events(conn))

    async def test_form_sem_elegivel_entra_na_fila_com_evento_queued(self):
        """Fila ligada: entrar na fila NAO e falha — o evento tem que dizer `queued`."""
        eng, conn, _sock = self._eng(online=[])
        await eng.consider(form_sem_identidade())
        self.assertIn("queued", events(conn))
        self.assertNotIn("form_no_agent", events(conn), "fila ligada: nao e falha")
        espera = [r for r in routes(conn, "waiting") if r["kind"] == "form"]
        self.assertEqual(len(espera), 1)
        self.assertIsNone(espera[0]["expires_at"], "espera sem prazo (nao vence sozinha)")

    async def test_form_sem_elegivel_com_fila_desligada_marca_falha(self):
        eng, conn, _sock = self._eng(online=[])
        eng.cfg["queue_wait_enabled"] = "false"
        await eng.consider(form_sem_identidade())
        self.assertIn("form_no_agent", events(conn))

    async def test_form_roteia_para_o_time_da_fila(self):
        eng, conn, sock = self._eng()
        await eng.consider(form_chat())
        move = sock.last("move_chat")
        self.assertEqual(move["chat_id"], 99)
        self.assertEqual(move["channel_address"], "h:q1")
        self.assertTrue(move["to_operator"])
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)
        self.assertIn("form_routed", events(conn))
        self.assertNotIn("leave_chat", sock.names())
        self.assertEqual(db.team_routing(conn, 1)["last_assigned_form"], ANA)
        self.assertEqual(len(routes(conn, "done")), 1)

    async def test_form_nao_recebe_mensagem_do_bot(self):
        eng, conn, sock = self._eng()
        await eng.consider(form_chat())
        self.assertEqual(sock.sent_texts(), [])

    async def test_nao_roteia_o_mesmo_form_duas_vezes(self):
        eng, conn, sock = self._eng()
        await eng.consider(form_chat())
        await eng.consider(form_chat())
        self.assertEqual(sock.names().count("move_chat"), 1)

    async def test_form_sem_fila_apenas_registra(self):
        eng, conn, sock = self._eng()
        await eng.consider(form_chat(helpdesk_address=None, channel_addr=None))
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("form_no_queue", events(conn))
        self.assertEqual(routes(conn), [])

    async def test_flag_desligada_mantem_comportamento_antigo(self):
        eng, conn, sock = self._eng()
        eng.cfg["form_routing_enabled"] = "false"
        await eng.consider(form_chat())
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("closure", events(conn))
        self.assertNotIn("form_routed", events(conn))

    async def test_fila_sem_time_nao_roteia(self):
        eng, conn, sock = self._eng(with_team=False)
        await eng.consider(form_chat())
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("form_no_team", events(conn))
        self.assertIn("closure", events(conn))

    async def test_fila_desabilitada_nao_roteia_formulario(self):
        """Mesmas regras do chat: fila desligada no painel = formulario so indexado.

        Sem isso um bot conectado apenas para observar movia formularios reais nos
        quadros (o caso de 28/09: 9 formularios movidos com todas as filas off).
        """
        eng, conn, sock = self._eng()
        conn.execute("UPDATE queues SET enabled = 0")
        eng.reload_config()
        await eng.consider(form_chat())
        self.assertNotIn("move_chat", sock.names())
        self.assertNotIn("invite_to_chat", sock.names())
        self.assertNotIn("form_routed", events(conn))
        self.assertNotIn("queued", events(conn))
        self.assertIn("form_queue_off", events(conn))
        self.assertIn("closure", events(conn))
        self.assertEqual(len(routes(conn, "waiting")), 0)

    async def test_time_sem_formulario_habilitado_nao_roteia(self):
        eng, conn, sock = self._eng()
        conn.execute("INSERT INTO team_routing (team_id, form_enabled) VALUES (1, 0)")
        await eng.consider(form_chat())
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("form_no_team", events(conn))

    async def test_sem_analista_enfileira_retry(self):
        eng, conn, sock = self._eng(online=[])
        await eng.consider(form_chat())
        self.assertNotIn("move_chat", sock.names())
        # Fila ligada: entrar na fila nao e falha — o evento e `queued`.
        self.assertIn("queued", events(conn))
        wait = routes(conn, "waiting")
        self.assertEqual(len(wait), 1)
        self.assertEqual(int(wait[0]["attempts"]), 0)
        self.assertEqual(wait[0]["queue_address"], "h:q1")

    async def test_fallback_monitor_nao_enfileira_com_fila_desligada(self):
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_wait_enabled"] = "false"
        conn.execute("INSERT INTO team_routing (team_id, fallback) VALUES (1, 'monitor')")
        await eng.consider(form_chat())
        self.assertIn("form_no_agent", events(conn))
        self.assertEqual(routes(conn), [])

    async def test_fallback_monitor_com_fila_ligada_entra_na_fila(self):
        """Com a fila ligada o formulario espera a vez, mesmo com fallback=monitor."""
        eng, conn, sock = self._eng(online=[])
        conn.execute("INSERT INTO team_routing (team_id, fallback) VALUES (1, 'monitor')")
        await eng.consider(form_chat())
        self.assertIn("queued", events(conn))
        self.assertNotIn("form_no_agent", events(conn), "com fila ligada ele espera a vez")
        self.assertEqual(len(routes(conn, "waiting")), 1)

    async def test_tick_routes_entrega_quando_analista_aparece(self):
        eng, conn, sock = self._eng(online=[])
        eng.cfg["form_route_retry_interval_sec"] = "0"
        await eng.consider(form_chat())
        sock.online = ["ana"]
        await eng.tick_routes()
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)
        self.assertIn("form_routed", events(conn))
        self.assertEqual(len(routes(conn, "done")), 1)
        self.assertEqual(len(routes(conn, "waiting")), 0)

    async def test_tick_routes_esgota_tentativas_com_fila_desligada(self):
        """Tentativas e fallback do formulario valem com a fila DESLIGADA."""
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_wait_enabled"] = "false"
        eng.cfg["form_route_attempts"] = "2"
        eng.cfg["form_route_retry_interval_sec"] = "0"
        await eng.consider(form_chat())
        for _ in range(3):
            await eng.tick_routes()
        self.assertNotIn("move_chat", sock.names())
        failed = routes(conn, "failed")
        self.assertEqual(len(failed), 1)
        self.assertIn("tentativas esgotadas", failed[0]["detail"])
        self.assertGreaterEqual(events(conn).count("form_route_retry"), 1)

    async def test_tick_routes_aproveita_analista_da_mesma_fila(self):
        eng, conn, sock = self._eng(online=[])
        eng.cfg["form_route_retry_interval_sec"] = "0"
        await eng.consider(form_chat())
        add_member(conn, BIA, "Bia", sort_order=1)
        sock.online = ["bia"]
        await eng.tick_routes()
        self.assertEqual(sock.last("invite_to_chat")["address"], BIA)

    async def test_prioridade_escolhe_o_maior_weight(self):
        eng, conn, sock = self._eng(online=["ana", "bia"])
        add_member(conn, BIA, "Bia", sort_order=1)
        conn.execute("INSERT INTO team_routing (team_id, strategy) VALUES (1, 'priority')")
        conn.execute(
            "INSERT INTO team_member_routing (team_id, address, weight) VALUES (1, ?, 5)", (BIA,)
        )
        await eng.consider(form_chat())
        self.assertEqual(sock.last("invite_to_chat")["address"], BIA)

    async def test_cap_de_formulario_por_membro(self):
        eng, conn, sock = self._eng(online=["ana", "bia"], chats=[load_form(ANA, TEC)])
        add_member(conn, BIA, "Bia", sort_order=1)
        conn.execute(
            "INSERT INTO team_member_routing (team_id, address, cap_form) VALUES (1, ?, 1)", (ANA,)
        )
        await eng.consider(form_chat())
        self.assertEqual(sock.last("invite_to_chat")["address"], BIA)

    async def test_chat_na_carga_nao_consome_cap_de_formulario(self):
        eng, conn, sock = self._eng(online=["ana", "bia"], chats=[load_chat(ANA)])
        add_member(conn, BIA, "Bia", sort_order=1)
        conn.execute(
            "INSERT INTO team_member_routing (team_id, address, cap_form) VALUES (1, ?, 1)", (ANA,)
        )
        await eng.consider(form_chat())
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)

    async def test_membro_fora_da_janela_nao_recebe_formulario(self):
        eng, conn, sock = self._eng(online=["ana"])
        conn.execute(
            "UPDATE team_members SET route_start = '08:00:00', route_end = '09:00:00' WHERE address = ?",
            (ANA,),
        )
        # A janela e comparada com a hora corrente: mockar a checagem torna o
        # teste deterministico (antes ele falhava quando a suite rodava entre
        # 08:00 e 09:00 BRT).
        with mock.patch.object(routing, "_in_route_window", return_value=False):
            await eng.consider(form_chat())
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("queued", events(conn))
        self.assertIn("fora_horario", routes(conn, "waiting")[0]["detail"])

    async def test_chat_continua_com_o_rodizio_legado_do_time(self):
        eng, conn, sock = self._eng(online=["ana", "bia"])
        add_member(conn, BIA, "Bia", sort_order=1)
        conn.execute("UPDATE teams SET last_assigned = ? WHERE id = 1", (ANA,))
        picked, detail = await eng.pick_assignee("h:q1")
        self.assertEqual(picked["address"], BIA)
        self.assertIn("estrategia=round_robin", detail)
        # pick_assignee não escreve estado: o rodízio é gravado só na entrega.
        self.assertIsNone(db.team_routing(conn, 1))
        self.assertEqual(
            conn.execute("SELECT last_assigned FROM teams WHERE id = 1").fetchone()["last_assigned"],
            ANA,
        )


class ChatFallbackTest(unittest.IsolatedAsyncioTestCase):
    """Sem analista: com a fila ligada o chat e segurado; sem fila vem o fallback."""

    def tearDown(self):
        MEM.clear()

    def _eng(self, online=None):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        insert_option(
            conn,
            menu_id=1,
            sort_order=1,
            label="Transferir",
            classification="tec",
            action="transfer_only",
        )
        sock = RouteSock(online=online)
        return engine_with(conn, sock), conn, sock

    async def _responde_1(self, eng, conn):
        row = awaiting(conn)
        await eng.handle_reply(10, row, "1")
        return conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()

    async def test_fila_ligada_segura_o_chat_sem_analista(self):
        eng, conn, sock = self._eng(online=[])
        chat = await self._responde_1(eng, conn)
        self.assertEqual(chat["status"], "classified")
        self.assertNotIn("leave_chat", sock.names())
        rs = events(conn)
        self.assertIn("no_agent", rs)
        self.assertIn("classified", rs)
        self.assertIn("queued", rs)
        wait = routes(conn, "waiting")
        self.assertEqual(len(wait), 1)
        self.assertEqual(wait[0]["kind"], "chat")
        self.assertIsNone(wait[0]["expires_at"])          # espera sem limite
        self.assertNotIn("closed_no_agent", rs)

    async def test_fila_ligada_entrega_quando_o_analista_aparece(self):
        eng, conn, sock = self._eng(online=[])
        await self._responde_1(eng, conn)
        sock.online = ["ana"]
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "closed")
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)
        self.assertEqual(len(routes(conn, "done")), 1)

    async def test_fila_desligada_fallback_close(self):
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_wait_enabled"] = "false"
        chat = await self._responde_1(eng, conn)
        self.assertEqual(chat["status"], "classified")     # ainda nao encerrou
        wait = routes(conn, "waiting")
        self.assertEqual(len(wait), 1)
        self.assertIsNone(wait[0]["expires_at"])
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "closed_no_agent")
        self.assertIn("leave_chat", sock.names())
        rs = events(conn)
        self.assertIn("no_agent", rs)
        self.assertIn("closed_no_agent", rs)

    async def test_fila_desligada_fallback_monitor_mantem_o_bot_no_chat(self):
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_wait_enabled"] = "false"
        conn.execute("INSERT INTO team_routing (team_id, fallback) VALUES (1, 'monitor')")
        await self._responde_1(eng, conn)
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "monitoring")
        self.assertNotIn("leave_chat", sock.names())
        self.assertIn("no_agent", events(conn))


class TickRoutesGuardTest(unittest.IsolatedAsyncioTestCase):
    """Guarda o tick sem rotas vencidas e o on_left, que divide o bloco com ele."""

    def tearDown(self):
        MEM.clear()

    def _eng(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        sock = RouteSock()
        return engine_with(conn, sock), conn, sock

    async def test_tick_sem_rota_vencida_nao_quebra(self):
        eng, _conn, sock = self._eng()
        await eng.tick_routes()
        self.assertEqual(sock.cmds, [])

    async def test_on_left_encerra_quando_o_tecnico_sai(self):
        eng, conn, sock = self._eng()
        db.upsert_chat(conn, 10, queue_address="h:q1", status="monitoring")
        MEM[10] = {"started_addr": TEC, "queue_address": "h:q1", "originator_spoke": True}
        await eng.on_left(10, TEC)
        chat = conn.execute("SELECT * FROM chats WHERE chat_id = 10").fetchone()
        self.assertEqual(chat["status"], "closed_originator_left")
        self.assertIn("leave_chat", sock.names())

    async def test_form_sai_do_indice_na_remocao_real(self):
        """`chat_removed` (remocao real) tira o formulario do indice.

        `helpdesk_chat_removed` NAO tira: e aviso de FILA (o `move_chat` para o
        analista gera um destes) e o formulario continua aberto — era por ali que
        a abertura form-aware se perdia em producao.
        """
        eng, _conn, _sock = self._eng()
        await eng.consider(form_chat())
        self.assertIn(99, eng._open_forms)
        await eng.on_note("helpdesk_chat_removed", {"chat_id": 99})
        self.assertIn(99, eng._open_forms, "aviso de fila nao encerra o formulario")
        await eng.on_note("chat_removed", {"chat_id": 99})
        self.assertNotIn(99, eng._open_forms)


if __name__ == "__main__":
    unittest.main()


