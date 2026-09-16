#!/usr/bin/env python3
"""Sincroniza clientes entre Morumbi 3D e Morumbi Festas.

Ambos os sistemas rodam na mesma VPS com SQLite.
O cliente e unico: quem existe no 3D deve existir no Festas e vice-versa.
O vinculo e feito pelo campo `cliente_3d_id` na tabela de clientes do Festas.

Direcoes:
    3D -> Festas: clientes do 3D que nao existem no Festas sao criados.
    Festas -> 3D: clientes do Festas que nao existem no 3D sao criados.

Desduplicacao: CPF, WhatsApp ou email (nessa ordem de prioridade).
Nunca sobrescreve dados — apenas cria clientes novos e vincula.

Uso:
    python3 ferramentas/sincronizar_clientes.py

Variaveis de ambiente:
    FESTAS_DADOS    caminho do banco Morumbi Festas (default: morumbi_festas.db)
    MORUMBI_DADOS   pasta do banco Morumbi 3D       (default: /var/lib/morumbi3d)

Para rodar via cron (ex: a cada 30 minutos):
    */30 * * * * cd /opt/morumbi-festas && /opt/morumbi-festas/venv/bin/python3 ferramentas/sincronizar_clientes.py >> /var/log/sync_clientes.log 2>&1
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

FUSO = timezone(timedelta(hours=-4))

CAMINHO_FESTAS = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")
PASTA_3D = Path(os.environ.get("MORUMBI_DADOS", "/var/lib/morumbi3d"))
CAMINHO_3D = PASTA_3D / "sistema.sqlite3"


def agora() -> str:
    return datetime.now(FUSO).strftime("%Y-%m-%d %H:%M:%S")


def _so_digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


def _limpar_fone(texto: str | None) -> str:
    return "".join(c for c in (texto or "") if c.isdigit() or c == "+")


def conectar_festas() -> sqlite3.Connection:
    conn = sqlite3.connect(CAMINHO_FESTAS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def conectar_3d() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CAMINHO_3D) if not isinstance(CAMINHO_3D, str) else CAMINHO_3D)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _indice_festas(conn_festas: sqlite3.Connection) -> dict:
    """Monta indices de busca rapida para clientes do Festas."""
    clientes = [dict(r) for r in conn_festas.execute("SELECT * FROM clientes")]
    por_3d_id = {}
    por_cpf = {}
    por_whatsapp = {}
    por_email = {}

    for c in clientes:
        if c.get("cliente_3d_id"):
            por_3d_id[c["cliente_3d_id"]] = c
        cpf = _so_digitos(c.get("cpf_cnpj"))
        if len(cpf) >= 11:
            por_cpf[cpf] = c
        wpp = _so_digitos(c.get("whatsapp"))
        if len(wpp) >= 10:
            por_whatsapp[wpp] = c
        email = (c.get("email") or "").strip().lower()
        if email and "@" in email:
            por_email[email] = c

    return {
        "lista": clientes,
        "por_3d_id": por_3d_id,
        "por_cpf": por_cpf,
        "por_whatsapp": por_whatsapp,
        "por_email": por_email,
    }


def _indice_3d(conn_3d: sqlite3.Connection) -> dict:
    """Monta indices de busca rapida para clientes do 3D."""
    clientes = [dict(r) for r in conn_3d.execute("SELECT * FROM clientes")]
    por_id = {}
    por_cpf = {}
    por_whatsapp = {}
    por_email = {}

    for c in clientes:
        por_id[c["id"]] = c
        cpf = _so_digitos(c.get("cpf"))
        if len(cpf) >= 11:
            por_cpf[cpf] = c
        wpp = _so_digitos(c.get("whatsapp"))
        if len(wpp) >= 10:
            por_whatsapp[wpp] = c
        email = (c.get("email") or "").strip().lower()
        if email and "@" in email:
            por_email[email] = c

    return {
        "lista": clientes,
        "por_id": por_id,
        "por_cpf": por_cpf,
        "por_whatsapp": por_whatsapp,
        "por_email": por_email,
    }


def _encontrar_no_festas(cliente_3d: dict, idx_festas: dict) -> dict | None:
    """Procura um cliente do 3D na base do Festas por 3d_id, CPF, WhatsApp ou email."""
    id_3d = cliente_3d["id"]
    if id_3d in idx_festas["por_3d_id"]:
        return idx_festas["por_3d_id"][id_3d]

    cpf = _so_digitos(cliente_3d.get("cpf"))
    if len(cpf) >= 11 and cpf in idx_festas["por_cpf"]:
        return idx_festas["por_cpf"][cpf]

    wpp = _so_digitos(cliente_3d.get("whatsapp"))
    if len(wpp) >= 10 and wpp in idx_festas["por_whatsapp"]:
        return idx_festas["por_whatsapp"][wpp]

    email = (cliente_3d.get("email") or "").strip().lower()
    if email and "@" in email and email in idx_festas["por_email"]:
        return idx_festas["por_email"][email]

    return None


def _encontrar_no_3d(cliente_festas: dict, idx_3d: dict) -> dict | None:
    """Procura um cliente do Festas na base do 3D por cliente_3d_id, CPF, WhatsApp ou email."""
    id_3d = cliente_festas.get("cliente_3d_id")
    if id_3d and id_3d in idx_3d["por_id"]:
        return idx_3d["por_id"][id_3d]

    cpf = _so_digitos(cliente_festas.get("cpf_cnpj"))
    if len(cpf) >= 11 and cpf in idx_3d["por_cpf"]:
        return idx_3d["por_cpf"][cpf]

    wpp = _so_digitos(cliente_festas.get("whatsapp"))
    if len(wpp) >= 10 and wpp in idx_3d["por_whatsapp"]:
        return idx_3d["por_whatsapp"][wpp]

    email = (cliente_festas.get("email") or "").strip().lower()
    if email and "@" in email and email in idx_3d["por_email"]:
        return idx_3d["por_email"][email]

    return None


def sync_3d_para_festas(conn_festas, conn_3d) -> dict:
    """Cria no Festas os clientes que existem no 3D mas nao no Festas."""
    idx_festas = _indice_festas(conn_festas)
    idx_3d = _indice_3d(conn_3d)

    criados = 0
    vinculados = 0

    for c3d in idx_3d["lista"]:
        if not c3d.get("ativo", 1):
            continue

        match = _encontrar_no_festas(c3d, idx_festas)

        if match:
            if not match.get("cliente_3d_id"):
                conn_festas.execute(
                    "UPDATE clientes SET cliente_3d_id = ?, atualizado_em = ? WHERE id = ?",
                    (c3d["id"], agora(), match["id"]))
                vinculados += 1
                idx_festas["por_3d_id"][c3d["id"]] = match
            continue

        ts = agora()
        cur = conn_festas.execute(
            "INSERT INTO clientes (nome, cpf_cnpj, whatsapp, telefone, email,"
            " data_nascimento, endereco, bairro, cidade, cep, instagram,"
            " observacoes, origem, status, cliente_3d_id, criado_em, atualizado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c3d["nome"],
             c3d.get("cpf", ""),
             c3d.get("whatsapp", ""),
             "",
             c3d.get("email", ""),
             "",
             c3d.get("endereco", ""),
             c3d.get("bairro", ""),
             c3d.get("cidade", ""),
             c3d.get("cep", ""),
             "",
             c3d.get("observacao", ""),
             c3d.get("canal", ""),
             "ativo",
             c3d["id"],
             ts, ts))
        novo_id = cur.lastrowid
        novo = {"id": novo_id, "cliente_3d_id": c3d["id"],
                "cpf_cnpj": c3d.get("cpf", ""),
                "whatsapp": c3d.get("whatsapp", ""),
                "email": c3d.get("email", "")}
        idx_festas["por_3d_id"][c3d["id"]] = novo
        cpf = _so_digitos(c3d.get("cpf"))
        if len(cpf) >= 11:
            idx_festas["por_cpf"][cpf] = novo
        criados += 1

    conn_festas.commit()
    return {"criados": criados, "vinculados": vinculados}


def sync_festas_para_3d(conn_festas, conn_3d) -> dict:
    """Cria no 3D os clientes que existem no Festas mas nao no 3D."""
    idx_festas = _indice_festas(conn_festas)
    idx_3d = _indice_3d(conn_3d)

    criados = 0
    vinculados = 0

    for cf in idx_festas["lista"]:
        if cf.get("status") != "ativo":
            continue

        match = _encontrar_no_3d(cf, idx_3d)

        if match:
            if not cf.get("cliente_3d_id"):
                conn_festas.execute(
                    "UPDATE clientes SET cliente_3d_id = ?, atualizado_em = ? WHERE id = ?",
                    (match["id"], agora(), cf["id"]))
                vinculados += 1
            continue

        if cf.get("cliente_3d_id"):
            continue

        ts = agora()
        cur = conn_3d.execute(
            "INSERT INTO clientes (nome, whatsapp, email, cpf, endereco,"
            " complemento, bairro, cidade, cep, canal, observacao,"
            " ativo, criado_em, criado_por)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (cf["nome"],
             cf.get("whatsapp") or "",
             cf.get("email") or "",
             cf.get("cpf_cnpj") or "",
             cf.get("endereco") or "",
             "",
             cf.get("bairro") or "",
             cf.get("cidade") or "",
             cf.get("cep") or "",
             cf.get("origem") or "",
             cf.get("observacoes") or "",
             ts, "sync_festas"))
        novo_3d_id = cur.lastrowid

        conn_festas.execute(
            "UPDATE clientes SET cliente_3d_id = ?, atualizado_em = ? WHERE id = ?",
            (novo_3d_id, agora(), cf["id"]))
        criados += 1

    conn_3d.commit()
    conn_festas.commit()
    return {"criados": criados, "vinculados": vinculados}


def sincronizar() -> dict:
    if not Path(CAMINHO_FESTAS).exists():
        print(f"Banco Festas nao encontrado: {CAMINHO_FESTAS}")
        sys.exit(1)
    if not Path(CAMINHO_3D).exists():
        print(f"Banco 3D nao encontrado: {CAMINHO_3D}")
        sys.exit(1)

    conn_festas = conectar_festas()
    conn_3d = conectar_3d()

    try:
        r1 = sync_3d_para_festas(conn_festas, conn_3d)
        r2 = sync_festas_para_3d(conn_festas, conn_3d)

        return {
            "3d_para_festas": r1,
            "festas_para_3d": r2,
        }
    finally:
        conn_festas.close()
        conn_3d.close()


if __name__ == "__main__":
    ts = datetime.now(FUSO).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] Sincronizando clientes...")
    print(f"  Festas: {CAMINHO_FESTAS}")
    print(f"  3D:     {CAMINHO_3D}")

    resultado = sincronizar()

    r1 = resultado["3d_para_festas"]
    r2 = resultado["festas_para_3d"]

    print(f"\n  3D -> Festas: {r1['criados']} criados, {r1['vinculados']} vinculados")
    print(f"  Festas -> 3D: {r2['criados']} criados, {r2['vinculados']} vinculados")
    print("  Concluido.")
