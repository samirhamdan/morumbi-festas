#!/usr/bin/env python3
"""Sprint 1 — normalização dos pedidos históricos.

1) Diagnóstico + simulação (padrão). O banco real só é lido: tudo roda numa
   cópia temporária, que é apagada no fim.

    FESTAS_DADOS=/var/lib/morumbi-festas/festas.db \\
        python ferramentas/normalizar_historico.py

2) Aplicação no banco real, somente depois de validar o relatório acima.
   Faz backup verificado antes de alterar qualquer registro.

    FESTAS_DADOS=/var/lib/morumbi-festas/festas.db \\
        python ferramentas/normalizar_historico.py --aplicar

A rotina é idempotente: executar de novo não altera nada.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sistema import dados, formato  # noqa: E402

TABELAS_CONFERIDAS = ("pedidos", "itens_pedido", "eventos_historico", "clientes")


def copiar_banco(origem: str, destino: str):
    """Cópia consistente mesmo com o sistema rodando (API de backup do SQLite)."""
    src = sqlite3.connect(f"file:{origem}?mode=ro", uri=True)
    dst = sqlite3.connect(destino)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    conferir_copia(origem, destino)


def conferir_copia(origem: str, destino: str):
    a = sqlite3.connect(f"file:{origem}?mode=ro", uri=True)
    b = sqlite3.connect(destino)
    try:
        ok = b.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise SystemExit(f"Cópia corrompida ({destino}): {ok}")
        for t in TABELAS_CONFERIDAS:
            na = a.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            nb = b.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if na != nb:
                raise SystemExit(f"Cópia divergente em {t}: {na} != {nb}")
    finally:
        a.close()
        b.close()


def usar_banco(caminho: str) -> sqlite3.Connection:
    dados.CAMINHO_BD = caminho
    dados.inicializar()
    return dados.conectar()


def classificacoes(conn) -> dict:
    return {r["id"]: (r["nome"], r["classificacao"], r["total_festas"])
            for r in conn.execute(
                "SELECT id, nome, classificacao, total_festas FROM clientes")}


def faturamento_por_ano(conn) -> list:
    anos = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(d, 1, 4) FROM ("
        f" SELECT {dados._data_pedido_sql()} AS d FROM pedidos p"
        " UNION SELECT data_evento FROM eventos_historico"
        f" WHERE {dados._origem_visivel('')}"
        ") WHERE d IS NOT NULL AND d != '' ORDER BY 1")]
    linhas = []
    for ano in anos:
        f = dados.faturamento_periodo(f"{ano}-01-01", f"{ano}-12-31")
        linhas.append((ano, f))
    return linhas


# ---------------------------------------------------------------------------
# Impressão
# ---------------------------------------------------------------------------

def titulo(texto: str):
    print("\n" + "=" * 72)
    print(texto)
    print("=" * 72)


def secao(texto: str):
    print("\n" + texto)
    print("-" * len(texto))


def moeda(v) -> str:
    return formato.dinheiro(v) if v else "sem valor"


def imprimir_estrutura(conn):
    titulo("ETAPA 1 — DIAGNÓSTICO DA ESTRUTURA")
    for tabela in ("pedidos", "itens_pedido", "eventos_historico"):
        cols = conn.execute(f"PRAGMA table_info({tabela})").fetchall()
        n = conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
        print(f"\n{tabela} ({n} registros)")
        print("  campos: " + ", ".join(c[1] for c in cols))

    secao("pedidos — status comercial")
    for r in conn.execute("SELECT status_comercial, COUNT(*) FROM pedidos"
                          " GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r[0]:<14} {r[1]}")
    secao("pedidos — status operacional")
    for r in conn.execute("SELECT status_operacional, COUNT(*) FROM pedidos"
                          " GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r[0]:<14} {r[1]}")
    r = conn.execute("SELECT MIN(NULLIF(data_evento,'')), MAX(NULLIF(data_evento,''))"
                     " FROM pedidos").fetchone()
    print(f"\n  data_evento de {r[0]} a {r[1]}")

    secao("eventos_historico — origem × status na origem")
    for r in conn.execute(
            "SELECT COALESCE(origem,'(sem origem)'), status_origem, COUNT(*),"
            " MIN(NULLIF(data_evento,'')), MAX(NULLIF(data_evento,''))"
            " FROM eventos_historico GROUP BY 1, 2 ORDER BY 1, 3 DESC"):
        print(f"  {r[0]:<20} {str(r[1]):<14} {r[2]:>5}   {r[3]} a {r[4]}")

    print("""
Como cada informação está representada:
  • Pedidos do sistema ...... tabela pedidos (origem implícita: Morumbi Festas)
  • Pedidos importados ...... tabela eventos_historico, campo origem
                              ('Formulario Festas' = planilha, 'Morumbi 3D')
  • Data do evento .......... pedidos.data_evento (sem ela: devolução/retirada)
                              eventos_historico.data_evento
  • Valor ................... pedidos: soma de itens_pedido (quantidade × preço)
                              eventos_historico.valor (0 = não informado)
  • criado_em ............... data de criação/importação — NÃO é usada no
                              faturamento
  • Histórico ............... pedidos.historico = 1 após a normalização;
                              eventos_historico é histórico por definição
  • Situação dos importados . calculada, nunca gravada: cancelado se o status
                              de origem é cancelado; finalizado se entregue
                              ou se o evento é anterior ao corte (todas as
                              origens, inclusive Morumbi 3D)""")


def imprimir_pre(diag: dict, amostra: int):
    p, h = diag["pedidos"], diag["historico"]
    titulo("ETAPA 2 — RELATÓRIO DE PRÉ-VALIDAÇÃO")
    print(f"Regra de data: registros com evento anterior a {diag['data_corte']}")
    tres_d = h["por_origem"].get("Morumbi 3D", {}).get("total", 0)
    visiveis = [o for o in h["por_origem"].values() if not o["oculta"]]
    sem_valor = p["sem_valor_historicos"] + sum(o["sem_valor_finalizado"] for o in visiveis)
    sem_data = p["sem_data"] + sum(o["sem_data"] for o in visiveis)
    hist_finalizados = sum(o["finalizado"] for o in visiveis)
    print(f"""
Pedidos encontrados ................ {p['total'] + h['total']}
    no sistema (pedidos) ........... {p['total']}
    importados (eventos_historico) . {h['total']}
Pedidos históricos identificados ... {p['historicos'] + hist_finalizados}
    no sistema (regra de data) ..... {p['historicos']}
    importados finalizados ......... {hist_finalizados}
Pedidos já finalizados ............. {p['ja_finalizados']}
    (devolvido/finalizado; recebem só a padronização e a marca histórico)
    já normalizados por execução anterior: {p['ja_normalizados']}
Pedidos que serão alterados ........ {p['a_alterar']}
Pedidos atuais preservados ......... {p['atuais_preservados']}
Pedidos cancelados (inalterados) ... {p['cancelados']}
Pedidos Morumbi 3D preservados ..... {tres_d} (mantidos no banco, fora das telas e do faturamento)
Pedidos sem valor .................. {sem_valor}
Pedidos sem data válida ............ {sem_data}""")

    secao("Importados por origem (nada é regravado; a origem é preservada)")
    for origem, o in sorted(h["por_origem"].items()):
        if o["oculta"]:
            print(f"  {origem}: {o['total']} registros — OCULTOS (preservados no banco;"
                  " fora das telas, do faturamento e da classificação)")
            continue
        print(f"  {origem}: {o['total']} registros — {o['finalizado']} finalizados,"
              f" {o['cancelado']} cancelados, {o['pendente']} pendentes"
              f" | finalizados sem valor: {o['sem_valor_finalizado']}"
              f" | sem data: {o['sem_data']}")
        for s in o["status_origem"]:
            print(f"      status '{s['status']}' → {s['situacao']}: {s['quantidade']}"
                  f" ({s['primeira']} a {s['ultima']})")

    if p["amostra"]:
        secao(f"Amostra dos pedidos que serão alterados ({min(amostra, p['a_alterar'])}"
              f" de {p['a_alterar']})")
        print(f"  {'#':>6}  {'cliente':<28} {'evento':<10}  {'antes':<24} valor")
        for r in p["amostra"][:amostra]:
            antes = f"{r['status_comercial']}/{r['status_operacional']}"
            print(f"  {r['id']:>6}  {(r['cliente_nome'] or '—')[:28]:<28}"
                  f" {r['data_ref'] or '—':<10}  {antes:<24} {moeda(r['valor'])}")
        print("  depois: finalizado/finalizado, histórico = sim")

    if h["pendentes"]:
        secao(f"ATENÇÃO — {len(h['pendentes'])} importados pendentes: evento a partir de"
              f" {diag['data_corte']} ou sem data. Não entram no faturamento; revisar")
        for r in h["pendentes"][:amostra]:
            print(f"  {r['origem']} #{r['origem_id']}  {r['data_evento'] or 'sem data'}"
                  f"  {r['status_origem']}  {r['cliente_nome'] or '—'}")
    if p["inconsistentes"]:
        print(f"\nATENÇÃO — {p['inconsistentes']} pedidos marcados como histórico"
              " sem status finalizado")
    if diag["migracao_antiga_executada"]:
        print("\nATENÇÃO — a migração antiga (migracao_finalizados) já foi executada"
              " neste banco. Ela renomeava a origem dos importados não-3D para"
              " 'Morumbi Festas'; confira a origem desses registros.")


def imprimir_faturamento(linhas: list, rotulo: str):
    secao(f"Faturamento por ano — {rotulo} (pedidos finalizados, data do evento)")
    for ano, f in linhas:
        origens = "; ".join(f"{o}: {moeda(v['total'])} ({v['quantidade']})"
                            for o, v in sorted(f["por_origem"].items()))
        print(f"  {ano}: {moeda(f['total'])} — {f['quantidade']} pedidos"
              f" ({f['sem_valor']} sem valor) | {origens}")


def imprimir_pos(pre: dict, diag: dict, verif: dict, resultado: dict,
                 mudancas: list, reexecucao: dict | None):
    p, h = diag["pedidos"], diag["historico"]
    titulo("RELATÓRIO PÓS-EXECUÇÃO")
    tres_d = h["por_origem"].get("Morumbi 3D", {}).get("total", 0)
    visiveis = [o for o in h["por_origem"].values() if not o["oculta"]]
    sem_valor = p["sem_valor_historicos"] + sum(o["sem_valor_finalizado"] for o in visiveis)
    sem_data = p["sem_data"] + sum(o["sem_data"] for o in visiveis)
    problemas = p["inconsistentes"] + sum(verif.values()) + len(h["pendentes"])
    print(f"""
Total de pedidos analisados ........ {p['total'] + h['total']}
Total normalizado como histórico ... {resultado['alterados']}
Total já finalizado ................ {pre['pedidos']['ja_finalizados']}
Total preservado ................... {p['atuais_preservados'] + p['cancelados'] + h['total']}
Total Morumbi 3D ................... {tres_d} (ocultos, preservados)
Total sem valor .................... {sem_valor}
Total sem data ..................... {sem_data}
Total com problemas ................ {problemas}""")
    secao("Conferência da operação (tudo deve ser 0)")
    for chave, n in verif.items():
        print(f"  históricos {chave.replace('_', ' '):<22} {n}")
    if reexecucao is not None:
        print(f"\nIdempotência: segunda execução alterou {reexecucao['alterados']} pedidos")
    secao(f"Classificação de clientes alterada: {len(mudancas)}")
    for nome, antes, depois in mudancas[:15]:
        print(f"  {nome[:30]:<30} {antes} → {depois}")
    if h["pendentes"]:
        print(f"\nInconsistência: {len(h['pendentes'])} importados continuam pendentes"
              " (não entram no faturamento nem na classificação).")


# ---------------------------------------------------------------------------

def executar(caminho: str, amostra: int, aplicar: bool):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Banco: {caminho}")

    if aplicar:
        backup = f"{caminho}.backup-antes-normalizacao-{ts}"
        copiar_banco(caminho, backup)
        print(f"Backup criado e conferido: {backup}")
        alvo = caminho
        tmp = None
    else:
        tmp = tempfile.TemporaryDirectory()
        alvo = str(Path(tmp.name) / "simulacao.db")
        copiar_banco(caminho, alvo)
        print("Modo simulação: alterações feitas numa cópia temporária."
              " O banco real não é modificado.")

    try:
        conn = usar_banco(alvo)
        imprimir_estrutura(conn)
        diag = dados.diagnostico_normalizacao(conn, amostra)
        imprimir_pre(diag, amostra)
        imprimir_faturamento(faturamento_por_ano(conn), "antes")
        antes = classificacoes(conn)

        titulo("ETAPA 3 — EXECUÇÃO" + ("" if aplicar else " (SIMULADA)"))
        resultado = dados.aplicar_normalizacao(conn)
        dados.atualizar_todas_classificacoes()
        print(f"Pedidos alterados: {resultado['alterados']}")
        reexecucao = dados.aplicar_normalizacao(conn)

        depois = classificacoes(conn)
        mudancas = [(antes[i][0], f"{antes[i][1]} ({antes[i][2]})",
                     f"{depois[i][1]} ({depois[i][2]})")
                    for i in antes if antes[i][1:] != depois[i][1:]]
        diag_pos = dados.diagnostico_normalizacao(conn, amostra)
        imprimir_pos(diag, diag_pos, dados.verificar_operacao_sem_historicos(conn),
                     resultado, mudancas, reexecucao)
        imprimir_faturamento(faturamento_por_ano(conn), "depois")
        conn.close()
    finally:
        if tmp:
            tmp.cleanup()

    if aplicar:
        print(f"\nConcluído. Para desfazer: pare o serviço e restaure {backup}")
    else:
        print("\nSimulação concluída. Se o relatório estiver correto, rode com --aplicar.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aplicar", action="store_true",
                    help="aplica no banco real (faz backup antes)")
    ap.add_argument("--amostra", type=int, default=20,
                    help="quantidade de registros exibidos nas amostras")
    args = ap.parse_args()

    caminho = os.environ.get("FESTAS_DADOS", "morumbi_festas.db")
    if not Path(caminho).exists():
        raise SystemExit(f"Banco não encontrado: {caminho}")
    if args.aplicar:
        resposta = input(f"Aplicar a normalização em {caminho}? Digite APLICAR: ")
        if resposta.strip() != "APLICAR":
            raise SystemExit("Cancelado.")
    executar(caminho, args.amostra, args.aplicar)


if __name__ == "__main__":
    main()
