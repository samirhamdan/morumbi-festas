#!/usr/bin/env python3
"""Diagnostico completo do banco Morumbi 3D.

Apresenta a estrutura de clientes e pedidos para validar antes da importacao.

Uso:
    MORUMBI_DADOS=/var/lib/morumbi3d FESTAS_DADOS=/var/lib/morumbi-festas/festas.db \
        python ferramentas/diagnostico_3d.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CAMINHO_FESTAS = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")
PASTA_3D = Path(os.environ.get("MORUMBI_DADOS", "/var/lib/morumbi3d"))
CAMINHO_3D = PASTA_3D / "sistema.sqlite3"


def _normalizar(nome: str) -> str:
    nome = nome.strip().lower()
    nome = unicodedata.normalize("NFKD", nome)
    nome = "".join(c for c in nome if not unicodedata.combining(c))
    return " ".join(nome.split())


def diagnostico():
    if not Path(CAMINHO_3D).exists():
        print(f"Banco 3D nao encontrado: {CAMINHO_3D}")
        sys.exit(1)

    conn_3d = sqlite3.connect(str(CAMINHO_3D))
    conn_3d.row_factory = sqlite3.Row

    conn_festas = sqlite3.connect(CAMINHO_FESTAS)
    conn_festas.row_factory = sqlite3.Row

    # ── Tabelas no banco 3D ──
    tabelas = [r[0] for r in conn_3d.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " ORDER BY name").fetchall()]
    print("=" * 70)
    print("DIAGNOSTICO MORUMBI 3D")
    print("=" * 70)
    print(f"\nBanco: {CAMINHO_3D}")
    print(f"Tabelas encontradas: {', '.join(tabelas)}")

    # ── Clientes 3D ──
    print("\n" + "-" * 70)
    print("CLIENTES NO MORUMBI 3D")
    print("-" * 70)
    clientes_3d = conn_3d.execute(
        "SELECT * FROM clientes ORDER BY id").fetchall()
    print(f"Total de clientes: {len(clientes_3d)}")
    for c in clientes_3d:
        d = dict(c)
        print(f"  id={d['id']} | nome={d.get('nome', '?')}"
              f" | whatsapp={d.get('whatsapp', '')}"
              f" | email={d.get('email', '')}"
              f" | ativo={d.get('ativo', '?')}")

    # ── Pedidos 3D ──
    print("\n" + "-" * 70)
    print("PEDIDOS NO MORUMBI 3D")
    print("-" * 70)
    pedidos_3d = conn_3d.execute(
        "SELECT * FROM pedidos ORDER BY id").fetchall()
    print(f"Total de pedidos: {len(pedidos_3d)}")

    por_status = {}
    por_cliente = {}
    com_data = 0
    sem_data = 0
    for p in pedidos_3d:
        d = dict(p)
        st = d.get("status", "?")
        por_status[st] = por_status.get(st, 0) + 1
        cli = d.get("cliente", "?")
        if cli not in por_cliente:
            por_cliente[cli] = []
        por_cliente[cli].append(d)
        data = d.get("entregue_em") or d.get("prazo") or d.get("criado_em")
        if data:
            com_data += 1
        else:
            sem_data += 1

    print(f"\nPor status:")
    for st, qtd in sorted(por_status.items()):
        print(f"  {st}: {qtd}")

    print(f"\nCom data de evento: {com_data}")
    print(f"Sem data de evento: {sem_data}")

    print(f"\nPor cliente:")
    for cli, peds in sorted(por_cliente.items()):
        print(f"\n  {cli} ({len(peds)} pedido(s)):")
        for p in peds:
            data = p.get("entregue_em") or p.get("prazo") or p.get("criado_em") or "?"
            if len(data) > 10:
                data = data[:10]
            print(f"    id={p['id']} | status={p.get('status', '?')}"
                  f" | data={data}"
                  f" | valor={p.get('valor', 0)}"
                  f" | canal={p.get('canal', '')}")

    # ── Detalhes de cada pedido ──
    print("\n" + "-" * 70)
    print("DETALHES COMPLETOS DE CADA PEDIDO")
    print("-" * 70)
    for p in pedidos_3d:
        d = dict(p)
        print(f"\n  Pedido #{d['id']}:")
        for k, v in d.items():
            if v is not None and v != "":
                print(f"    {k}: {v}")

    # ── Clientes Festas ──
    print("\n" + "-" * 70)
    print("CLIENTES NO MORUMBI FESTAS")
    print("-" * 70)
    clientes_festas = conn_festas.execute(
        "SELECT id, nome, whatsapp, email, cliente_3d_id, status"
        " FROM clientes ORDER BY nome").fetchall()
    print(f"Total de clientes: {len(clientes_festas)}")

    vinculados = 0
    for c in clientes_festas:
        d = dict(c)
        vinc = f" -> 3D id={d['cliente_3d_id']}" if d.get("cliente_3d_id") else ""
        print(f"  id={d['id']} | {d['nome']}"
              f" | wpp={d.get('whatsapp', '')}"
              f" | status={d.get('status', '')}{vinc}")
        if d.get("cliente_3d_id"):
            vinculados += 1

    print(f"\nClientes vinculados ao 3D: {vinculados}")
    print(f"Clientes sem vinculo: {len(clientes_festas) - vinculados}")

    # ── Correspondencia por nome ──
    print("\n" + "-" * 70)
    print("CORRESPONDENCIA POR NOME (3D -> FESTAS)")
    print("-" * 70)
    nomes_festas = {}
    for c in clientes_festas:
        chave = _normalizar(c["nome"])
        if chave and chave not in nomes_festas:
            nomes_festas[chave] = {"id": c["id"], "nome": c["nome"]}

    nomes_3d_vistos = set()
    for c in clientes_3d:
        chave = _normalizar(c["nome"])
        if chave in nomes_3d_vistos:
            continue
        nomes_3d_vistos.add(chave)
        match = nomes_festas.get(chave)
        if match:
            print(f"  3D '{c['nome']}' (id={c['id']})"
                  f" -> Festas '{match['nome']}' (id={match['id']})")
        else:
            print(f"  3D '{c['nome']}' (id={c['id']})"
                  f" -> SEM CORRESPONDENCIA")

    # ── Eventos ja importados ──
    print("\n" + "-" * 70)
    print("EVENTOS JA IMPORTADOS NO FESTAS")
    print("-" * 70)
    try:
        evts = conn_festas.execute(
            "SELECT h.*, c.nome AS cliente_nome"
            " FROM eventos_historico h"
            " LEFT JOIN clientes c ON c.id = h.cliente_id"
            " ORDER BY h.id").fetchall()
        print(f"Total importados: {len(evts)}")
        for e in evts:
            d = dict(e)
            print(f"  id={d['id']} | origem_id={d.get('origem_id')}"
                  f" | cliente={d.get('cliente_nome', '?')}"
                  f" | data={d.get('data_evento', '?')}"
                  f" | status={d.get('status_origem', '?')}")
    except sqlite3.OperationalError:
        print("  Tabela eventos_historico nao existe ainda.")

    print("\n" + "=" * 70)
    print("FIM DO DIAGNOSTICO")
    print("=" * 70)

    conn_3d.close()
    conn_festas.close()


if __name__ == "__main__":
    diagnostico()
