# -*- coding: utf-8 -*-
"""Checagem TEMPORÁRIA do formulário "TAREFA REALOCADA" contra o robô de realocação.

Enquanto a automação externa (robô) tenta resolver sozinha a atividade
reatribuída, o bot segura a entrega do formulário por alguns minutos e
confere a tabela `robo.toa__validacao_realocado` antes de oferecer o caso a
um analista. Tudo que é específico desta ponte mora aqui — para remover a
feature basta apagar este módulo e o ponto de chamada em `engine.py`
(`_realocado_segurar`).
"""
from __future__ import annotations

import re

CLASSIFICACAO = "TAREFA REALOCADA"

_ACTIVITY_RE = re.compile(r"activityId=(\d+)")


def is_realocado(textos: list[str]) -> bool:
    """O formulário tem a classificação TAREFA REALOCADA em alguma mensagem?"""
    return any(CLASSIFICACAO in (t or "") for t in textos)


def extract_activity_id(textos: list[str]) -> int | None:
    """`activityId` da URL do TOA presente em alguma mensagem (a mais recente que bater)."""
    for t in textos:
        m = _ACTIVITY_RE.search(t or "")
        if m:
            return int(m.group(1))
    return None
