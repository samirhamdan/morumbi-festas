#!/usr/bin/env python3
"""Sprint 2.2 — migração para empresas (tenants).

Cria a empresa Morumbi Festas (ou usa a organização já cadastrada), liga todos
os dados atuais a ela, transforma usuários em membros, parâmetros em
configurações e cria a lista de serviços. Só acrescenta: nenhum registro é
apagado e nenhum valor, data, status, origem ou id muda — a ferramenta prova
isso comparando o conteúdo de cada tabela antes e depois.

1) Simulação (padrão): roda numa cópia temporária; o banco real só é lido.

    FESTAS_DADOS=/var/lib/morumbi-festas/festas.db \\
        venv/bin/python ferramentas/migrar_empresas.py

2) Aplicação, depois de validar o relatório (faz backup verificado antes):

    FESTAS_DADOS=/var/lib/morumbi-festas/festas.db \\
        venv/bin/python ferramentas/migrar_empresas.py --aplicar

A mesma migração roda sozinha quando o serviço reinicia; esta ferramenta
serve para ver o resultado antes e ter o backup garantido. É idempotente.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sistema import dados  # noqa: E402
from ferramentas.normalizar_historico import copiar_banco, secao, titulo  # noqa: E402

TABELAS = ("usuarios", "organizacoes", "parametros", "audit_log", "clientes",
           "tags_cliente", "categorias", "produtos", "fotos_produto", "tags_produto",
           "kits", "itens_kit", "fotos_kit", "origens_lead", "leads", "orcamentos",
           "itens_orcamento", "pedidos", "itens_pedido", "eventos_historico")


def _tabelas(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def retrato(caminho: str) -> dict:
    """Colunas e impressão digital do conteúdo de cada tabela."""
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    try:
        existentes = _tabelas(conn)
        r = {}
        for t in TABELAS:
            if t not in existentes:
                continue
            cols = [c[1] for c in conn.execute(f"PRAGMA table_info({t})")]
            r[t] = {"colunas": cols,
                    "linhas": conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0],
                    "ultimo": conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {t}"
                                           ).fetchone()[0]}
        return r
    finally:
        conn.close()


def digital(caminho: str, tabela: str, colunas: list, ultimo: int) -> str:
    """Impressão digital dos registros que já existiam (novos ficam de fora)."""
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        lista = ", ".join(colunas)
        for linha in conn.execute(f"SELECT {lista} FROM {tabela} WHERE rowid <= ?"
                                  " ORDER BY rowid", (ultimo,)):
            h.update(repr(tuple(linha)).encode())
        return h.hexdigest()
    finally:
        conn.close()


def relatorio(caminho: str, antes: dict, digitais: dict):
    depois = retrato(caminho)
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        titulo("RESULTADO")
        secao("Empresa")
        for e in conn.execute("SELECT id, nome, nome_fantasia, status FROM organizacoes"):
            print(f"  #{e['id']} {e['nome']}{' (' + e['nome_fantasia'] + ')' if e['nome_fantasia'] else ''}"
                  f" — {e['status']}")
        tid = conn.execute("SELECT MIN(id) FROM organizacoes").fetchone()[0]

        secao("Tabelas: registros antes → depois e empresa de cada registro")
        problemas = []
        for t, info in antes.items():
            n_depois = depois[t]["linhas"]
            extra = ""
            if "tenant_id" in depois[t]["colunas"]:
                fora = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE tenant_id != ?",
                                    (tid,)).fetchone()[0]
                extra = f" | na empresa principal: {n_depois - fora}"
                if fora:
                    problemas.append(f"{t}: {fora} registros fora da empresa principal")
            if n_depois < info["linhas"]:
                problemas.append(f"{t}: registros diminuíram")
            novos = [c for c in depois[t]["colunas"] if c not in info["colunas"]]
            print(f"  {t:<18} {info['linhas']:>6} → {n_depois:<6}{extra}"
                  + (f" | campos novos: {', '.join(novos)}" if novos else ""))

        secao("Conteúdo original (valores, datas, status, origens, ids)")
        for t, info in antes.items():
            igual = digital(caminho, t, info["colunas"], info["ultimo"]) == digitais[t]
            if not igual:
                problemas.append(f"{t}: conteúdo original mudou")
            print(f"  {t:<18} {'inalterado' if igual else 'ALTERADO'}")

        secao("Usuários → membros da empresa")
        for r in conn.execute("SELECT m.perfil, m.ativo, COUNT(*) AS n FROM membros m"
                              " GROUP BY 1, 2 ORDER BY 1, 2"):
            print(f"  {dados.ROTULOS_PERFIL.get(r['perfil'], r['perfil']):<14}"
                  f" {'ativos' if r['ativo'] else 'inativos'}: {r['n']}")
        sem = conn.execute("SELECT COUNT(*) FROM usuarios u WHERE NOT EXISTS"
                           " (SELECT 1 FROM membros m WHERE m.usuario_id = u.id)"
                           ).fetchone()[0]
        if sem:
            problemas.append(f"{sem} usuários sem vínculo com empresa")

        secao("Configurações e serviços da empresa principal")
        for r in conn.execute("SELECT chave, valor FROM configuracoes WHERE tenant_id = ?"
                              " ORDER BY chave", (tid,)):
            print(f"  configuração {r['chave']} = {r['valor']}")
        print("  (demais configurações usam o padrão: BRL, pt-BR, America/Campo_Grande,"
              " DD/MM/AAAA, roxo #6F1C85, laranja #FF8C00)")
        servicos = [r[0] for r in conn.execute(
            "SELECT nome FROM servicos WHERE tenant_id = ? ORDER BY ordem", (tid,))]
        print(f"  serviços: {', '.join(servicos) or '—'}")

        secao("Integridade")
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"  integrity_check: {ok} | foreign_key_check: {len(fk)} pendências")
        if ok != "ok":
            problemas.append(f"integrity_check: {ok}")
        for p in problemas:
            print(f"  PROBLEMA: {p}")
        if not problemas:
            print("  nada apagado, nada duplicado, nenhum valor alterado: ok")
        return problemas
    finally:
        conn.close()


def migrar(caminho: str):
    dados.CAMINHO_BD = caminho
    dados.inicializar()
    dados.inicializar()  # a segunda execução não pode mudar nada


def executar(caminho: str, aplicar: bool):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Banco: {caminho}")
    if aplicar:
        backup = f"{caminho}.backup-antes-sprint22-{ts}"
        n = 1
        while Path(backup).exists():  # nunca sobrescreve um backup anterior
            n += 1
            backup = f"{caminho}.backup-antes-sprint22-{ts}-{n}"
        copiar_banco(caminho, backup)
        print(f"Backup criado e conferido: {backup}")
        alvo, tmp = caminho, None
    else:
        tmp = tempfile.TemporaryDirectory()
        alvo = str(Path(tmp.name) / "simulacao.db")
        copiar_banco(caminho, alvo)
        print("Modo simulação: tudo roda numa cópia temporária."
              " O banco real não é modificado.")
    try:
        antes = retrato(alvo)
        digitais = {t: digital(alvo, t, i["colunas"], i["ultimo"])
                    for t, i in antes.items()}
        titulo("ANTES")
        for t, i in antes.items():
            print(f"  {t:<18} {i['linhas']:>6} registros")
        migrar(alvo)
        problemas = relatorio(alvo, antes, digitais)
    finally:
        if tmp:
            tmp.cleanup()
    if aplicar:
        print(f"\nConcluído. Reinicie o serviço. Para desfazer: pare o serviço e"
              f" restaure {backup}")
    else:
        print("\nSimulação concluída."
              + (" Corrija os problemas acima antes de aplicar." if problemas
                 else " Se o relatório estiver correto, rode com --aplicar."))
    return problemas


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aplicar", action="store_true",
                    help="aplica no banco real (faz backup antes)")
    args = ap.parse_args()
    caminho = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")
    if not Path(caminho).exists():
        raise SystemExit(f"Banco não encontrado: {caminho}")
    if args.aplicar:
        resposta = input(f"Aplicar a migração de empresas em {caminho}? Digite APLICAR: ")
        if resposta.strip() != "APLICAR":
            raise SystemExit("Cancelado.")
    if executar(caminho, args.aplicar):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
