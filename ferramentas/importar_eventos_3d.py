#!/usr/bin/env python3
"""Importa eventos historicos do Morumbi 3D para o Morumbi Festas.

Somente pedidos com status 'entregue' no 3D sao considerados festas
realizadas. Todos os pedidos (exceto orcamento) sao importados como
eventos historicos para visualizacao na agenda.

Idempotente: usa (origem, origem_id) como chave unica. Reexecucoes
nao duplicam registros.

Uso:
    python3 ferramentas/importar_eventos_3d.py

Variaveis de ambiente:
    FESTAS_DADOS    caminho do banco Morumbi Festas (default: morumbi_festas.db)
    MORUMBI_DADOS   pasta do banco Morumbi 3D       (default: /var/lib/morumbi3d)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

FUSO = timezone(timedelta(hours=-4))

CAMINHO_FESTAS = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")
PASTA_3D = Path(os.environ.get("MORUMBI_DADOS", "/var/lib/morumbi3d"))
CAMINHO_3D = PASTA_3D / "sistema.sqlite3"
ORIGEM = "Morumbi 3D"


def agora() -> str:
    return datetime.now(FUSO).strftime("%Y-%m-%d %H:%M:%S")


def conectar_festas() -> sqlite3.Connection:
    conn = sqlite3.connect(CAMINHO_FESTAS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def conectar_3d() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CAMINHO_3D))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _mapa_clientes(conn_festas: sqlite3.Connection) -> dict[int, int]:
    """Retorna {cliente_3d_id: cliente_festas_id}."""
    rows = conn_festas.execute(
        "SELECT id, cliente_3d_id FROM clientes"
        " WHERE cliente_3d_id IS NOT NULL").fetchall()
    return {r["cliente_3d_id"]: r["id"] for r in rows}


def _ja_importados(conn_festas: sqlite3.Connection) -> set[int]:
    """Retorna set de origem_id ja importados."""
    rows = conn_festas.execute(
        "SELECT origem_id FROM eventos_historico WHERE origem = ?",
        (ORIGEM,)).fetchall()
    return {r["origem_id"] for r in rows}


def importar(conn_festas: sqlite3.Connection,
             conn_3d: sqlite3.Connection) -> dict:
    mapa = _mapa_clientes(conn_festas)
    ja = _ja_importados(conn_festas)

    pedidos_3d = conn_3d.execute(
        "SELECT id, cliente, cliente_id, canal, prazo, valor, desconto,"
        " status, observacao, criado_em, entregue_em"
        " FROM pedidos"
        " WHERE status != 'orcamento'"
        " ORDER BY id"
    ).fetchall()

    ts = agora()
    importados = 0
    ignorados_dup = 0
    sem_cliente = 0
    por_status: dict[str, int] = {}

    for p in pedidos_3d:
        pid = p["id"]
        if pid in ja:
            ignorados_dup += 1
            continue

        cliente_id_festas = mapa.get(p["cliente_id"])
        if not cliente_id_festas:
            sem_cliente += 1
            continue

        data_evento = p["entregue_em"] or p["prazo"] or p["criado_em"]
        if data_evento and len(data_evento) > 10:
            data_evento = data_evento[:10]

        descricao = p["cliente"] or ""
        if p["canal"]:
            descricao = f"{descricao} ({p['canal']})" if descricao else p["canal"]

        conn_festas.execute(
            "INSERT INTO eventos_historico"
            " (cliente_id, origem_id, origem, data_evento, descricao,"
            "  observacoes, canal, valor, status_origem, criado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cliente_id_festas, pid, ORIGEM, data_evento,
             descricao, p["observacao"] or "", p["canal"] or "",
             p["valor"] or 0, p["status"], ts))

        importados += 1
        por_status[p["status"]] = por_status.get(p["status"], 0) + 1

    conn_festas.commit()
    return {
        "importados": importados,
        "ignorados_duplicados": ignorados_dup,
        "sem_cliente_vinculado": sem_cliente,
        "por_status": por_status,
        "total_3d": len(pedidos_3d),
    }


def atualizar_classificacoes(conn_festas: sqlite3.Connection) -> dict:
    """Recalcula total_festas e classificacao de cada cliente com historico."""
    from sistema.dados import classificar_festas

    rows = conn_festas.execute("""
        SELECT cliente_id, COUNT(*) as total,
               MAX(data_evento) as ultima
        FROM eventos_historico
        WHERE cliente_id IS NOT NULL AND status_origem = 'entregue'
        GROUP BY cliente_id
    """).fetchall()

    atualizados = 0
    for r in rows:
        total = r["total"]
        conn_festas.execute(
            "UPDATE clientes SET total_festas = ?, classificacao = ?,"
            " ultima_festa = ? WHERE id = ?",
            (total, classificar_festas(total), r["ultima"], r["cliente_id"]))
        atualizados += 1

    conn_festas.commit()
    return {"clientes_atualizados": atualizados}


def executar() -> dict:
    if not Path(CAMINHO_FESTAS).exists():
        print(f"Banco Festas nao encontrado: {CAMINHO_FESTAS}")
        sys.exit(1)
    if not Path(CAMINHO_3D).exists():
        print(f"Banco 3D nao encontrado: {CAMINHO_3D}")
        sys.exit(1)

    conn_festas = conectar_festas()
    conn_3d = conectar_3d()

    try:
        from sistema.dados import inicializar
        inicializar()

        resultado = importar(conn_festas, conn_3d)
        class_result = atualizar_classificacoes(conn_festas)
        resultado.update(class_result)
        return resultado
    finally:
        conn_festas.close()
        conn_3d.close()


if __name__ == "__main__":
    ts = datetime.now(FUSO).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{ts}] Importando eventos historicos do Morumbi 3D...")
    print(f"  Festas: {CAMINHO_FESTAS}")
    print(f"  3D:     {CAMINHO_3D}")

    resultado = executar()

    print(f"\n  Total pedidos 3D (exceto orcamento): {resultado['total_3d']}")
    print(f"  Importados: {resultado['importados']}")
    print(f"  Ignorados (duplicados): {resultado['ignorados_duplicados']}")
    print(f"  Sem cliente vinculado: {resultado['sem_cliente_vinculado']}")
    if resultado.get("por_status"):
        print("  Por status:")
        for s, c in resultado["por_status"].items():
            print(f"    {s}: {c}")
    print(f"  Clientes com classificacao atualizada: {resultado['clientes_atualizados']}")
    print("  Concluido.")
