# -*- coding: utf-8 -*-
"""Fila de espera com posicao (Fase 2): avisos, entrega, expiracao e restart."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from python import db  # noqa: E402
from python.engine import MEM, Engine  # noqa: E402
from tests.test_form_route import ANA, TEC, RouteSock, events, routes  # noqa: E402
from tests.test_submenu import awaiting, insert_option, mem_conn, seed_queue_menu  # noqa: E402

PERGUNTAS = [("Qual o protocolo?", "text", "")]


def seed_ask_leaf(conn) -> int:
    opt = insert_option(
        conn,
        menu_id=1,
        sort_order=1,
        label="Apoio",
        classification="apoio",
        action="ask_and_transfer",
        reply_text="Vou pedir uma informacao.",
    )
    for i, (prompt, kind, choices) in enumerate(PERGUNTAS, start=1):
        conn.execute(
            """INSERT INTO menu_option_questions (option_id, sort_order, prompt, kind, choices)
               VALUES (?, ?, ?, ?, ?)""",
            (opt, i, prompt, kind, choices),
        )
    return opt


def chat_row(conn, cid: int = 10) -> dict:
    return conn.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,)).fetchone()


def engine_cfg(conn, sock) -> Engine:
    eng = Engine(conn, sock)
    eng.cfg["transfer_retry_interval_sec"] = "0"
    return eng


class QueueTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        MEM.clear()

    def _eng(self, online=None):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_ask_leaf(conn)
        sock = RouteSock(online=online)
        return engine_cfg(conn, sock), conn, sock

    async def _entra_na_fila(self, eng, conn, cid: int = 10):
        await eng.handle_reply(cid, awaiting(conn, cid=cid), "1")
        await eng.handle_reply(cid, chat_row(conn, cid), "protocolo")
        await eng.handle_reply(cid, chat_row(conn, cid), "1")

    async def test_entra_na_fila_e_avisa_o_progresso(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        wait = routes(conn, "waiting")
        self.assertEqual(len(wait), 1)
        self.assertEqual(wait[0]["kind"], "chat")
        self.assertEqual(int(wait[0]["last_position"]), 1)
        # A espera NAO tem prazo: a rota nasce sem vencimento e so sai da fila por
        # entrega, saida do autor, remocao do chat, ausencia no TOA ou pelo botao
        # "Encerrar espera" do painel.
        self.assertIsNone(wait[0]["expires_at"])
        self.assertIn("queued", events(conn))
        # Padrao (`queue_show_pos=false`): o tecnico ouve que AVANCOU, nunca o numero.
        textos = sock.sent_texts()
        self.assertTrue(any("avan" in t.lower() for t in textos), textos)
        self.assertFalse(any("N\u00ba" in t for t in textos), textos)
        self.assertNotIn("leave_chat", sock.names())

    async def test_fila_ligada_nao_vence_sozinha(self):
        """Sem prazo: por mais que o tick rode, a rota segue `waiting` (sem timeout)."""
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        for k in range(5):
            conn.execute(
                "UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')"
            )
            await eng.tick_routes()
        wait = routes(conn, "waiting")
        self.assertEqual(len(wait), 1, "a rota continua esperando")
        self.assertIsNone(wait[0]["expires_at"], "sem vencimento")
        self.assertEqual(chat_row(conn)["status"], "classified", "o chat segue vivo")
        self.assertNotIn("queue_expired", events(conn))
        self.assertNotIn("leave_chat", sock.names())

    async def test_posicao_falada_quando_o_painel_liga(self):
        """`queue_show_pos=true` volta a falar o numero (comportamento antigo)."""
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_show_pos"] = "true"
        await self._entra_na_fila(eng, conn)
        self.assertTrue(any("N\u00ba 1" in t for t in sock.sent_texts()), sock.sent_texts())

    async def test_posicao_anunciada_conta_so_chats(self):
        """Formulario na fila nao infla o numero que o tecnico ouve."""
        eng, conn, _sock = self._eng(online=[])
        db.enqueue_route(conn, "form", 99, "h:q1", 1, detail="formulario", delay_sec=0)
        db.enqueue_route(conn, "form", 98, "h:q1", 1, detail="formulario", delay_sec=0)
        rid = db.enqueue_route(conn, "chat", 10, "h:q1", 1, detail="chat", delay_sec=0)
        # Fila interna: chat + formularios (ordem de entrega). Numero falado: so chats.
        self.assertEqual(db.queue_position(conn, rid), 3)
        self.assertEqual(db.queue_position(conn, rid, so_chat=True), 1)

    async def test_acima_do_topo_avisa_progresso_sem_numero(self):
        """Com a posicao ligada, acima de `queue_top_pos` o tecnico ouve que AVANCOU."""
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_show_pos"] = "true"
        eng.cfg["queue_top_pos"] = "1"   # so o primeiro da fila ouve numero
        db.enqueue_route(conn, "chat", 20, "h:q1", 1, detail="chat", delay_sec=0)
        db.enqueue_route(conn, "chat", 21, "h:q1", 1, detail="chat", delay_sec=0)
        await eng.tick_routes()
        textos = sock.sent_texts()
        self.assertTrue(any("N\u00ba 1" in t for t in textos), textos)
        self.assertTrue(any("avan" in t.lower() for t in textos), textos)
        self.assertFalse(any("N\u00ba 2" in t for t in textos), "acima do topo nao fala numero")

    async def test_formulario_nao_espera_a_linha_do_chat(self):
        """Linhas apartadas: com 10 chats na frente, o formulario e tentado no mesmo tick."""
        eng, conn, sock = self._eng(online=["ana"])
        eng.cfg["max_cases_per_agent"] = "1"   # Ana entrega 1 caso: o resto fica na fila
        for cid in range(11, 21):
            db.enqueue_route(conn, "chat", cid, "h:q1", 1, detail="chat", delay_sec=0)
        db.enqueue_route(conn, "form", 99, "h:q1", 1, detail="formulario", delay_sec=0)
        await eng.tick_routes()
        # O chat da cabeca foi entregue...
        self.assertEqual(len(routes(conn, "done")), 1)
        self.assertEqual(str(routes(conn, "done")[0]["kind"]), "chat")
        # ...e o formulario foi tentado NESTE tick (posicao 1 da linha dele).
        porTipo = {}
        for r in routes(conn, "waiting"):
            porTipo.setdefault(str(r["kind"]), []).append(int(r["attempts"]))
        self.assertEqual(porTipo.get("form"), [1], "formulario tentado no mesmo tick")
        # Algum chat do fim da linha nem foi tentado: o form nao esperou os 10.
        self.assertIn(0, porTipo.get("chat") or [], porTipo)

    async def test_espera_do_formulario_vence_e_encerra_a_rota(self):
        """Formulario sem elegivel nao gira para sempre: a espera vence e a rota para."""
        eng, conn, _sock = self._eng(online=[])
        db.enqueue_route(conn, "form", 99, "h:q1", 1, detail="formulario", delay_sec=0)
        conn.execute("UPDATE pending_routes SET expires_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        self.assertEqual(len(routes(conn, "failed")), 1)
        self.assertIn("queue_expired", events(conn))
        self.assertIn("form_no_agent", events(conn))
        self.assertEqual(len(routes(conn, "waiting")), 0)

    async def test_posicao_melhora_quando_o_da_frente_e_atendido(self):
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_show_pos"] = "true"   # numero falado (o avanco tambem e anunciado)
        await self._entra_na_fila(eng, conn, 10)
        await self._entra_na_fila(eng, conn, 11)
        porId = {int(r["chat_id"]): r for r in routes(conn, "waiting")}
        self.assertEqual(int(porId[11]["last_position"]), 2)
        self.assertEqual(int(porId[10]["last_position"]), 1)

        eng.cfg["max_cases_per_agent"] = "1"   # Ana atende 1 caso: o segundo chat continua na fila
        sock.online = ["ana"]
        await eng.tick_routes()
        self.assertEqual(chat_row(conn, 10)["status"], "closed")
        self.assertEqual(chat_row(conn, 11)["status"], "classified")
        segundo = [r for r in routes(conn, "waiting") if int(r["chat_id"]) == 11]
        self.assertEqual(len(segundo), 1)
        self.assertEqual(int(segundo[0]["last_position"]), 1)
        self.assertIn("queue_position", events(conn))
        self.assertTrue(any("N\u00ba 1" in t for t in sock.sent_texts()[-1:]))

    async def test_entrega_quando_aparece_analista(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        sock.online = ["ana"]
        await eng.tick_routes()
        self.assertEqual(sock.last("invite_to_chat")["address"], ANA)
        self.assertEqual(len(routes(conn, "done")), 1)
        self.assertIn("queued_assigned", events(conn))
        detalhe = [e for e in conn.execute(
            "SELECT detail FROM bot_events WHERE reason = 'queued_assigned'"
        ).fetchall()]
        self.assertTrue(any("espera=" in (d["detail"] or "") for d in detalhe))

    async def test_expira_a_espera_e_aplica_o_fallback(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        conn.execute("UPDATE pending_routes SET expires_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        self.assertEqual(len(routes(conn, "failed")), 1)
        self.assertIn("queue_expired", events(conn))
        self.assertEqual(chat_row(conn)["status"], "closed_no_agent")
        self.assertIn("leave_chat", sock.names())

    async def test_fila_desabilitada_sai_do_chat_e_encerra_a_espera(self):
        """Fila desligada depois de o bot entrar: ele sai do chat em silencio.

        Antes a rota era segurada `waiting` (sem gastar tentativa) para voltar
        quando a fila religasse: o chat ficava `classified` com uma rota pendurada e
        nem `due_routes` nem a recuperacao o viam de volta. Agora o atendimento
        volta para o quadro sem o bot dentro.
        """
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        self.assertEqual(len(routes(conn, "waiting")), 1)
        faladas = len(sock.sent_texts())
        conn.execute("UPDATE queues SET enabled = 0")
        eng.reload_config()
        await eng.tick_routes()
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("leave_chat", sock.names(), "o bot sai do chat")
        self.assertEqual(chat_row(conn)["status"], "left")
        self.assertEqual(routes(conn, "waiting"), [], "a espera e encerrada")
        falha = routes(conn, "failed")
        self.assertEqual(len(falha), 1)
        self.assertIn("fila desabilitada", falha[0]["detail"])
        self.assertIn("queue_off", events(conn))
        self.assertIn("left", events(conn))
        self.assertEqual(len(sock.sent_texts()), faladas, "saida silenciosa: nada e dito")
        # Fila religada: o bot nao volta sozinho para um chat que ele ja largou.
        conn.execute("UPDATE queues SET enabled = 1")
        eng.reload_config()
        sock.online = ["ana"]
        await eng.tick_routes()
        self.assertNotIn("invite_to_chat", sock.names())
        self.assertEqual(len(routes(conn, "done")), 0)

    async def test_chat_ja_entregue_sai_quando_a_fila_e_desabilitada(self):
        """A varredura pega quem JA foi entregue: sem rota em espera nao havia saida.

        E o caso do chat que o bot entregou (ou so acompanha) e cuja fila foi
        desabilitada depois: ele continua DENTRO do chat.
        """
        eng, conn, sock = self._eng(online=["ana"])
        await self._entra_na_fila(eng, conn)
        sock.online = ["ana"]
        await eng.tick_routes()
        self.assertEqual(chat_row(conn)["status"], "closed", "entrega normal")
        # O bot segue no quadro (acompanhando) quando a fila e desligada.
        db.upsert_chat(conn, 10, status="monitoring", queue_address="h:q1")
        conn.execute("UPDATE queues SET enabled = 0")
        eng.reload_config()
        await eng.tick_routes()
        self.assertEqual(chat_row(conn)["status"], "left")
        self.assertIn("leave_chat", sock.names())
        self.assertIn("queue_off", events(conn))
        # Sem fila bloqueada e sem espera: o proximo tick nao mexe em mais nada.
        antes = len(sock.names())
        await eng.tick_routes()
        self.assertEqual(len(sock.names()), antes)

    async def test_fila_desligada_nao_avisa_e_nao_expira_por_tempo(self):
        eng, conn, sock = self._eng(online=[])
        eng.cfg["queue_wait_enabled"] = "false"
        eng.cfg["transfer_attempts"] = "1"
        await self._entra_na_fila(eng, conn)
        self.assertNotIn("queued", events(conn))
        conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        self.assertEqual(chat_row(conn)["status"], "closed_no_agent")
        self.assertIn("no_agent", events(conn))

    async def test_escolha_normal_da_fila_nao_move_o_chat(self):
        """Sem analista o chat entra na fila: aviso de posicao sim, move_chat nao."""
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        self.assertFalse(db.chat_moved(conn, 10))
        self.assertNotIn("move_chat", sock.names())
        self.assertIn("queued", events(conn))

    async def test_formulario_na_fila_e_silencioso(self):
        eng, conn, sock = self._eng(online=[])
        db.enqueue_route(conn, "form", 99, "h:q1", 1, detail="formulario", delay_sec=0)
        await eng.tick_routes()
        # A fila e a MESMA do chat (a posicao conta os dois tipos), mas o
        # formulario nao recebe aviso nenhum: silencio absoluto.
        self.assertEqual(sock.sent_texts(), [], "formulario nao recebe aviso de posicao")
        self.assertEqual(len(routes(conn, "waiting")), 1)

    async def test_fila_sobrevive_ao_restart(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        # "restart": nova Engine/socket, fila continua no banco
        sock2 = RouteSock(online=["ana"])
        eng2 = engine_cfg(conn, sock2)
        await eng2.tick_routes()
        self.assertEqual(sock2.last("invite_to_chat")["address"], ANA)
        self.assertEqual(len(routes(conn, "done")), 1)

    async def test_chat_removido_encerra_a_rota_da_fila(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        await eng.on_note("chat_removed", {"chat_id": 10})
        self.assertEqual(len(routes(conn, "waiting")), 0)
        self.assertEqual(len(routes(conn, "failed")), 1)
        self.assertEqual(chat_row(conn)["status"], "left")

    async def test_tecnico_sai_encerra_a_rota_da_fila(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        MEM[10] = {"started_addr": TEC, "queue_address": "h:q1", "originator_spoke": True}
        await eng.on_left(10, TEC)
        self.assertEqual(chat_row(conn)["status"], "closed_originator_left")
        self.assertEqual(len(routes(conn, "waiting")), 0)

    async def test_encerrar_espera_no_painel_aplica_o_fallback(self):
        eng, conn, sock = self._eng(online=[])
        await self._entra_na_fila(eng, conn)
        # O painel grava expires_at vencido no botao "Encerrar espera".
        conn.execute("UPDATE pending_routes SET expires_at = datetime('now', '-1 seconds')")
        await eng.tick_routes()
        self.assertEqual(len(routes(conn, "failed")), 1)
        self.assertIn("queue_expired", events(conn))
        self.assertEqual(chat_row(conn)["status"], "closed_no_agent")

    async def test_chat_move_recusado_encerra_a_rota_no_teto(self):
        """Chat com `move_chat` recusado pelo TOA para de girar: encerra no teto.

        Antes, com a fila ligada e o chat ainda nao movido, a rota de chat retentava
        para sempre (o loop `e=10` das rotas orfas). Agora conta a recusa e encerra
        no teto, com o fallback do time.
        """
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        seed_ask_leaf(conn)
        sock = RouteSock(online=[], move_error="10: erro do TOA sem mensagem")
        eng = engine_cfg(conn, sock)
        await self._entra_na_fila(eng, conn)
        self.assertEqual(len(routes(conn, "waiting")), 1)
        sock.online = ["ana"]
        for _ in range(eng._move_max_fail()):
            conn.execute("UPDATE pending_routes SET next_try_at = datetime('now', '-1 seconds')")
            await eng.tick_routes()
        self.assertEqual(routes(conn, "waiting"), [], "a rota nao gira para sempre")
        falha = routes(conn, "failed")
        self.assertEqual(len(falha), 1)
        self.assertIn("move_falhou=10", falha[0]["detail"])
        self.assertIn("chat_move_refused", events(conn))
        self.assertEqual(chat_row(conn)["status"], "closed_no_agent", "fallback close aplicado")

    async def test_finish_orphan_routes_encerra_so_chat_fora_do_aberto(self):
        """A varredura fecha rota de chat orfa e respeita `classified`, form e lease."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        db.upsert_chat(conn, 30, queue_address="h:q1", status="left")
        db.upsert_chat(conn, 31, queue_address="h:q1", status="classified")
        db.upsert_chat(conn, 32, queue_address="h:q1", status="closed")
        db.upsert_chat(conn, 33, queue_address="h:q1", status="left")
        db.enqueue_route(conn, "chat", 30, "h:q1", 1, detail="x", delay_sec=0)
        db.enqueue_route(conn, "chat", 31, "h:q1", 1, detail="x", delay_sec=0)
        db.enqueue_route(conn, "chat", 32, "h:q1", 1, detail="x", delay_sec=0)
        r33 = db.enqueue_route(conn, "chat", 33, "h:q1", 1, detail="x", delay_sec=0)
        db.enqueue_route(conn, "form", 30, "h:q1", 1, detail="x", delay_sec=0)
        # Rota orfa COM posse (lease) viva: entrega em andamento, nao mexer.
        conn.execute(
            "UPDATE pending_routes SET claimed_by = 'tok',"
            " claim_expires_at = datetime('now', '+300 seconds') WHERE id = ?",
            (int(r33),),
        )
        n = db.finish_orphan_routes(conn)
        self.assertEqual(n, 2, "so o chat 30 (left) e 32 (closed)")
        mantidas = {(str(r["kind"]), int(r["chat_id"])) for r in routes(conn, "waiting")}
        self.assertEqual(mantidas, {("chat", 31), ("chat", 33), ("form", 30)})

    async def test_tick_timeouts_encerra_rota_orfa(self):
        """O tick fecha a rota orfa sozinho (contencao interna, sem watchdog externo)."""
        eng, conn, _sock = self._eng(online=[])
        db.enqueue_route(conn, "chat", 30, "h:q1", 1, detail="x", delay_sec=0)
        db.upsert_chat(conn, 30, queue_address="h:q1", status="left")
        await eng.tick_timeouts()
        self.assertEqual(routes(conn, "waiting"), [])
        self.assertIn("orphan_route_closed", events(conn))

    async def test_priorizar_item_fura_a_fila(self):
        """`set_route_priority` entrega o priorizado antes de quem chegou primeiro."""
        eng, conn, sock = self._eng(online=["ana"])
        eng.cfg["max_cases_per_agent"] = "1"
        db.enqueue_route(conn, "chat", 30, "h:q1", 1, detail="chat", delay_sec=0)
        segundo = db.enqueue_route(conn, "chat", 31, "h:q1", 1, detail="chat", delay_sec=0)
        self.assertTrue(db.set_route_priority(conn, segundo, 1))
        await eng.tick_routes()
        entregues = routes(conn, "done")
        self.assertEqual(len(entregues), 1)
        self.assertEqual(int(entregues[0]["chat_id"]), 31, "priorizado entregue primeiro")
        esperando = routes(conn, "waiting")
        self.assertEqual(len(esperando), 1)
        self.assertEqual(int(esperando[0]["chat_id"]), 30)

    def test_priorizar_so_vale_para_rota_em_espera(self):
        """Rota ja entregue (done) nao pode ser priorizada: a acao e so para quem espera."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        rid = db.enqueue_route(conn, "chat", 10, "h:q1", 1, delay_sec=0)
        db.finish_route(conn, rid, "done", "")
        self.assertFalse(db.set_route_priority(conn, rid, 1))

    def test_tentar_agora_ignora_o_backoff(self):
        """`retry_route_now` zera o proximo tick sem esperar o intervalo de retry."""
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        rid = db.enqueue_route(conn, "chat", 10, "h:q1", 1, delay_sec=55)
        self.assertEqual(db.due_routes(conn)["chat"], [], "ainda no backoff")
        self.assertTrue(db.retry_route_now(conn, rid))
        devidas = db.due_routes(conn)["chat"]
        self.assertEqual(len(devidas), 1)
        self.assertEqual(int(devidas[0]["id"]), rid)

    async def test_fechar_caso_acorda_a_fila_do_mesmo_time(self):
        """`_close_case` zera o backoff de quem espera: nao fica ate 60s parado.

        Antes, uma rota que tinha acabado de falhar (next_try_at no futuro, por
        `transfer_retry_interval_sec`) so era tentada de novo quando o backoff
        vencesse sozinho, mesmo que um analista liberasse vaga um segundo depois.
        """
        eng, conn, sock = self._eng(online=["ana"])
        eng.cfg["max_cases_per_agent"] = "1"
        db.enqueue_route(conn, "chat", 10, "h:q1", 1, detail="chat", delay_sec=0)
        db.enqueue_route(conn, "chat", 11, "h:q1", 1, detail="chat", delay_sec=0)
        await eng.tick_routes()
        entregues = routes(conn, "done")
        self.assertEqual(len(entregues), 1)
        self.assertEqual(int(entregues[0]["chat_id"]), 10, "ana recebeu o primeiro")
        esperando = routes(conn, "waiting")
        self.assertEqual(len(esperando), 1)
        rid11 = int(esperando[0]["id"])
        # Simula o backoff de uma tentativa que acabou de falhar (bem no futuro).
        conn.execute(
            "UPDATE pending_routes SET next_try_at = datetime('now', '+55 seconds') WHERE id = ?",
            (rid11,),
        )
        self.assertEqual(db.due_routes(conn)["chat"], [], "backoff ainda nao venceu")
        eng._close_case(10, "teste")
        devidas = db.due_routes(conn)["chat"]
        self.assertEqual(len(devidas), 1, "a fila foi acordada na hora, sem esperar o backoff")
        self.assertEqual(int(devidas[0]["id"]), rid11)


class QueueSchemaTest(unittest.TestCase):
    def test_colunas_ajustes_e_template(self):
        conn = mem_conn()
        cols = db._table_columns(conn, "pending_routes")
        self.assertIn("expires_at", cols)
        self.assertIn("last_position", cols)
        self.assertIn("priority", cols)
        self.assertIn("moved_at", db._table_columns(conn, "chats"))
        cfg = db.settings(conn)
        self.assertIn("queue_wait_enabled", cfg)
        self.assertNotIn("queue_max_wait_sec", cfg, "a espera nao tem prazo (ajuste removido)")
        self.assertIn("queue_top_pos", cfg)
        tpl = db.templates(conn)
        self.assertIn("{pos}", tpl.get("queue_position", ""))
        self.assertIn("avan", tpl.get("queue_progress", "").lower())
        self.assertTrue(tpl.get("queue_expired"), "texto da espera vencida semeado")

    def test_posicao_fifo_por_time(self):
        conn = mem_conn()
        seed_queue_menu(conn, with_team=True)
        a = db.enqueue_route(conn, "chat", 1, "h:q1", 1, delay_sec=0)
        b = db.enqueue_route(conn, "chat", 2, "h:q1", 1, delay_sec=0)
        c = db.enqueue_route(conn, "chat", 3, "h:q2", 2, delay_sec=0)
        self.assertEqual(db.queue_position(conn, a), 1)
        self.assertEqual(db.queue_position(conn, b), 2)
        self.assertEqual(db.queue_position(conn, c), 1)
        db.finish_route(conn, a, "done", "")
        self.assertEqual(db.queue_position(conn, b), 1)


if __name__ == "__main__":
    unittest.main()